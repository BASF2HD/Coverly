from __future__ import annotations

import hashlib
import errno
import io
import json
import os
import re
import tempfile
import unicodedata
import warnings
from pathlib import Path, PurePosixPath, PureWindowsPath
import numpy as np
from PIL import Image, ImageOps, ImageCms
from sqlalchemy import select, func
from .db import Album, ImageAsset, AlbumImage, Audit, Page, uid

Image.MAX_IMAGE_PIXELS = 40_000_000
KINDS = {"front", "back", "vinyl_side_a", "vinyl_side_b", "vinyl", "inner", "additional"}


def safe_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:100].rstrip(" .")
    if not value or value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        value = "_" + (value or "Untitled")
    return value


def within(root: Path, relative: str) -> Path:
    w = PureWindowsPath(relative)
    p = PurePosixPath(relative)
    if w.drive or w.root or p.is_absolute() or "\\" in relative or any(x in {"..", "."} for x in p.parts):
        raise ValueError("Unsafe archive path")
    base = root.resolve()
    candidate = base / relative
    result = candidate.resolve()
    if not result.is_relative_to(base) or result == base:
        raise ValueError("Path escapes archive")
    # Validate the resolved destination, but retain the requested spelling.
    # Windows resolve() substitutes existing on-disk capitalization, which
    # otherwise turns an explicitly approved case-only rename into a no-op.
    # Keeping the lexical leaf also lets callers detect/reject symlinks.
    return candidate


def atomic_write(path: Path, data: bytes, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".coverly-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            # Hard-link is atomic and refuses to overwrite on NTFS, APFS and ext4.
            try:
                os.link(tmp, path)
            except OSError as exc:
                if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV, errno.EACCES}:
                    raise
                # exFAT/FAT cannot hard-link. Create exclusively; no database row
                # exposes this file until the full write and fsync succeed.
                out_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(out_fd, "wb") as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
        else:
            os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def folder_for(settings, album):
    suffix=f' ({album.year or "YYYY"})'
    title=safe_name(album.title)
    title=re.sub(r'\s*\((?:\d{4}|YYYY)\)\s*$','',title,flags=re.I)
    while len((title+suffix).encode('utf-8'))>240:title=title[:-1]
    return safe_name(album.artist)+'/'+title.rstrip(' .')+suffix


def available_folder(session,settings,album,reserved=None):
    """Allocate a readable suffix; never merge or overwrite another directory."""
    base=folder_for(settings,album)
    reserved=reserved if reserved is not None else set()
    current=getattr(album,'folder','')
    # Keep an allocated suffix stable when the saved title/year is unchanged.
    match=re.fullmatch(re.escape(base)+r'(?P<suffix>_\d+)?',current,re.I) if current else None
    if match:
        candidate=base+(match.group('suffix') or '')
        if candidate.casefold() not in reserved:
            reserved.add(candidate.casefold())
            return candidate
    index=0
    while True:
        candidate=base+(f'_{index}' if index else '')
        other=session.scalar(select(Album.id).where(func.lower(Album.folder)==candidate.lower(),Album.id!=album.id).limit(1))
        target=within(settings.data,candidate)
        own=bool(current and candidate.casefold()==current.casefold() and within(settings.data,current).exists()
                 and target.exists() and target.samefile(within(settings.data,current)))
        if not other and candidate.casefold() not in reserved and (not target.exists() or own):
            reserved.add(candidate.casefold())
            return candidate
        index+=1


def decode_image(data: bytes):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        im = Image.open(io.BytesIO(data))
        if im.format not in {"JPEG", "PNG", "WEBP", "TIFF", "BMP"}:
            raise ValueError("Unsupported image signature; use JPEG, PNG, WebP, TIFF or BMP")
        if getattr(im, "n_frames", 1) > 1:
            raise ValueError("Animated and multipage images require manual frame selection")
        im.load()
        fmt = im.format
        profile = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im)
        if profile:
            try:
                im = ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                               ImageCms.createProfile("sRGB"), outputMode="RGB")
            except (OSError, ValueError, ImageCms.PyCMSError):
                im = im.convert("RGB")
        if im.mode in {"RGBA", "LA"} or "transparency" in im.info:
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, "white")
            bg.alpha_composite(rgba)
            im = bg.convert("RGB")
        else:
            im = im.convert("RGB")
        return im, fmt


def perceptual_hash(im):
    # Difference hash, 64-bit perceptual fingerprint.
    a = np.asarray(im.convert("L").resize((9, 8)), dtype=np.int16)
    return f"{int(''.join('1' if b else '0' for b in (a[:, 1:] > a[:, :-1]).flat), 2):016x}"


def classify(caption):
    text = caption.lower().replace("_", " ").replace("-", " ")
    for pattern, kind in [(r"side\s*[a1]\b", "vinyl_side_a"), (r"side\s*[b2]\b", "vinyl_side_b"),
                          (r"\bback\b|rear|reverse", "back"), (r"\bfront\b", "front"),
                          (r"\binner\b|insert", "inner"), (r"\bdisc\b|\blabel\b|\bvinyl record photo", "vinyl")]:
        if re.search(pattern, text):
            return kind, .88
    return "additional", 0.0


def add_image(session, settings, album, data, source_url="", page_url="", caption=""):
    if len(data) > settings.max_image_mb * 1024 * 1024:
        raise ValueError("Image exceeds configured size limit")
    sha = hashlib.sha256(data).hexdigest()
    asset = session.scalar(select(ImageAsset).where(ImageAsset.sha256 == sha))
    duplicate = asset is not None
    kind, confidence = classify(caption)
    if asset is None:
        im, fmt = decode_image(data)
        identifier = uid()
        ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "TIFF": "tif", "BMP": "bmp"}[fmt]
        base = album.folder
        original = f"{base}/Originals/{identifier}.{ext}"
        derivative = original if fmt=="JPEG" else f"{base}/Originals/Converted/{identifier}.jpg"
        thumb = f".thumbnails/{identifier}.jpg"
        phash = perceptual_hash(im)
        near = any((int(h, 16) ^ int(phash, 16)).bit_count() <= 4 for h in session.scalars(select(ImageAsset.phash)))
        atomic_write(within(settings.data, original), data, exclusive=True)
        out = io.BytesIO()
        if fmt!="JPEG":
            im.save(out, "JPEG", quality=settings.jpg_quality, subsampling=0)
            atomic_write(within(settings.data, derivative), out.getvalue(), exclusive=True)
        im.thumbnail((480, 480))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=86)
        atomic_write(within(settings.data, thumb), out.getvalue(), exclusive=True)
        asset = ImageAsset(id=identifier, sha256=sha, phash=phash, original=original, derivative=derivative,
                           thumbnail=thumb, format=fmt, width=decode_dimensions(data)[0], height=decode_dimensions(data)[1],
                           size=len(data), source_url=source_url, page_url=page_url, near_duplicate=near)
        session.add(asset)
        session.flush()
    if session.get(AlbumImage, (album.id, asset.id)) is None:
        session.add(AlbumImage(album_id=album.id, image_id=asset.id, classification=kind, confidence=confidence))
    if duplicate:
        session.add(Audit(action="duplicate_skipped", target=asset.id, detail={"album_id": album.id, "source": source_url}))
    session.flush()
    write_manifest(session, settings, album)
    return asset, duplicate


def decode_dimensions(data):
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    return im.size


def write_existing_manifest(session, settings, album, sync=False):
    """Metadata-only actions respect folders the user removed manually."""
    if within(settings.data,album.folder).is_dir():
        write_manifest(session,settings,album,sync=sync)


def write_manifest(session, settings, album, sync=True):
    from .archive_files import sync_covers
    affected=sync_covers(session,settings,album) if sync else set()
    images = session.execute(select(ImageAsset, AlbumImage).join(AlbumImage, ImageAsset.id == AlbumImage.image_id)
                             .where(AlbumImage.album_id == album.id)).all()
    document = {"schema": 1, "id": album.id, "title": album.title, "tamil_title": album.tamil_title,
                "artist": album.artist, "year": album.year, "soundtrack": album.soundtrack, "label": album.label, "catalogue": album.catalogue,
                "confidence": album.confidence, "compilation": album.compilation, "review": album.review, "deleted_at": album.deleted_at,
                'sources': [{'page_url': p.url, 'images': p.metadata_json.get('images', [])}
                            for p in session.scalars(select(Page).where(Page.album_id == album.id))],
                "images": [{"id": a.id, "original": a.original, "jpg": a.derivative, "sha256": a.sha256,
                            "cover":link.cover_path,"active_restoration_id":link.active_restoration_id,"classification": link.classification, "classification_confidence":link.confidence, "classification_evidence":link.evidence, "manual_classification":link.manual, "deleted_at": link.deleted_at, "source_url": a.source_url,
                            "page_url": a.page_url, "width": a.width, "height": a.height} for a, link in images]}
    atomic_write(within(settings.data, f"{album.folder}/album.json"), json.dumps(document, ensure_ascii=False, indent=2).encode())
    if affected:
        from .db import Album
        for ident in affected:
            related=session.get(Album,ident)
            if related:write_existing_manifest(session,settings,related)
