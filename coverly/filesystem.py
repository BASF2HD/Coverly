"""Read the user's archive as it is. Never write to album folders.

Only SQLite index records and disposable .library-cache previews are written.
No manifests, artwork, directories or job checkpoints are repaired by this module.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import threading
from pathlib import Path

from sqlalchemy import select, event

from .db import Album, AlbumImage, Artist, FilesystemAlbum, FilesystemFile, ImageAsset, uid
from .storage import atomic_write, classify, decode_image, perceptual_hash, within
from .locking import file_lock

log = logging.getLogger(__name__)
EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp'}
INFRASTRUCTURE = {'restored', 'originals', 'covers'}


def signature(path):
    stat = path.stat()
    return f'{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'


def identity(path):
    stat = path.stat()
    return f'{stat.st_dev}:{stat.st_ino}' if stat.st_ino else ''


def current_files(session, album_id):
    """None means not indexed yet; an empty dict means intentionally no files."""
    indexed = session.get(FilesystemAlbum, album_id)
    if indexed is None:
        return None
    if not indexed.present:
        return {}
    rows = session.scalars(select(FilesystemFile).where(
        FilesystemFile.album_id == album_id, FilesystemFile.present == True))
    result = {}
    # Prefer descriptive filenames over legacy Additional copies of identical bytes.
    for row in sorted(rows, key=lambda f: (classify(Path(f.path).stem)[0] == 'additional', f.path.casefold())):
        result.setdefault(row.image_id, row)
    return result


def asset_path(session, settings, asset, album_id=None):
    """Resolve index-only assets without inventing a replacement source file."""
    if album_id:
        files = current_files(session, album_id)
        if files is not None:
            row = files.get(asset.id)
            if not row:
                raise FileNotFoundError('Artwork was removed from this album folder')
            path = within(settings.data, row.path)
            if not path.is_file():
                raise FileNotFoundError('Artwork was removed from this album folder')
            return path
    if asset.original.startswith('.filesystem/'):
        rows = session.scalars(select(FilesystemFile).join(FilesystemAlbum,
            FilesystemAlbum.album_id == FilesystemFile.album_id).where(
            FilesystemFile.image_id == asset.id, FilesystemFile.present == True,
            FilesystemAlbum.present == True))
        for row in rows:
            path = within(settings.data, row.path)
            if path.is_file():
                return path
        raise FileNotFoundError('Artwork file is no longer in the archive')
    path = within(settings.data, asset.original)
    if not path.is_file():
        raise FileNotFoundError('Original artwork file is no longer in the archive')
    return path


def preview_path(session, settings, asset):
    source = asset_path(session, settings, asset)
    # Include stat info: never serve an old thumbnail for an in-place edit while
    # the next index pass is still reading the rest of a large archive.
    version = hashlib.sha256((asset.sha256 + signature(source)).encode()).hexdigest()
    target = settings.data / '.library-cache' / (version + '.jpg')
    if not target.is_file():
        image, _ = decode_image(source.read_bytes())
        image.thumbnail((480, 480))
        out = io.BytesIO()
        image.save(out, 'JPEG', quality=86)
        try:
            atomic_write(target, out.getvalue(), exclusive=True)
        except FileExistsError:
            pass
    return target


class FilesystemIndex:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.revision = 0
        self.scanning = False
        self.scanned = 0
        self.total = 0
        self.error = ''

    def status(self):
        return dict(enabled=self.settings.filesystem_sync, revision=self.revision,
                    scanning=self.scanning, scanned=self.scanned, total=self.total, error=self.error)

    def start(self):
        if self.settings.filesystem_sync:
            self.thread = threading.Thread(target=self._run, name='coverly-file-index', daemon=True)
            self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=10)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.scan()
                self.error = ''
            except Exception:
                log.exception('Archive file indexing failed; source files were not changed')
                self.error = 'Could not read the archive. Check that the storage drive is available.'
            self.wake.wait(self.settings.filesystem_poll_seconds)
            self.wake.clear()

    def _directories(self):
        root = self.settings.data
        # Two levels are the portable archive convention: Composer / Album.
        # Known legacy/custom paths are also scanned, without following symlinks.
        result = set()
        for artist in root.iterdir():
            if artist.name.startswith('.') or artist.name.casefold() in INFRASTRUCTURE or artist.is_symlink() or not artist.is_dir():
                continue
            for folder in artist.iterdir():
                if not folder.name.startswith('.') and not folder.is_symlink() and folder.is_dir():
                    result.add(folder.relative_to(root).as_posix())
        with self.sessions() as session:
            for folder in session.scalars(select(Album.folder)):
                try:
                    path = within(root, folder)
                    if path.is_dir() and not (root / folder).is_symlink():
                        result.add(folder)
                except (OSError, ValueError):
                    continue
        return sorted(result)

    def scan(self):
        if not self.settings.filesystem_sync:
            return
        self.scanning, self.scanned = True, 0
        try:
            directories = self._directories()
            self.total = len(directories)
            for folder in directories:
                if self.stop_event.is_set():
                    return
                self.scan_folder(folder)
                self.scanned += 1
            # A disconnected/unreadable drive raises above, rather than being
            # mistaken for thousands of intentional deletions.
            with self.lock, self.sessions() as session:
                changed = False
                for album in session.scalars(select(Album)):
                    if not within(self.settings.data, album.folder).exists():
                        indexed = session.get(FilesystemAlbum, album.id)
                        if indexed is None:
                            session.add(FilesystemAlbum(album_id=album.id,folder=album.folder,present=False))
                            changed = True
                        elif indexed.present:
                            indexed.present = False
                            changed = True
                session.commit()
                if changed:
                    self.revision += 1
        finally:
            self.scanning = False

    def scan_album(self, album_id):
        if not self.settings.filesystem_sync:
            return
        with self.sessions() as session:
            album = session.get(Album, album_id)
            folder = album.folder if album else None
        if folder and within(self.settings.data, folder).is_dir():
            self.scan_folder(folder)
        elif folder:
            # Directory renames are resolved by the next full pass (ID or inode).
            self.wake.set()

    def scan_folder(self, relative):
        root = self.settings.data
        directory = within(root, relative)
        if (root / relative).is_symlink():
            return
        try:
            folder_identity = identity(directory)
            document = {}
            manifest = directory / 'album.json'
            if manifest.is_file() and not manifest.is_symlink() and manifest.stat().st_size < 4_000_000:
                try:
                    document = json.loads(manifest.read_text('utf-8'))
                    if not isinstance(document, dict):
                        document = {}
                except (OSError, ValueError):
                    pass
            children = list(directory.iterdir())
            covers = next((p for p in children if p.name.casefold() == 'covers' and p.is_dir() and not p.is_symlink()), None)
            root_images = [p for p in children if p.suffix.lower() in EXTENSIONS and p.is_file() and not p.is_symlink() and not p.name.startswith('.')]
        except (FileNotFoundError, NotADirectoryError):
            return  # An editor/Finder move is still in progress.
        with self.lock, file_lock(root/'.archive.lock'), self.sessions() as session:
            @event.listens_for(session, 'before_flush')
            def track_changes(s, *_):
                if s.new or any(s.is_modified(obj) for obj in s.dirty):
                    s.info['filesystem_changed'] = True
            album = session.scalar(select(Album).where(Album.folder == relative))
            if album is None and document.get('id'):
                candidate = session.get(Album, str(document['id']))
                if candidate and not within(root, candidate.folder).exists():
                    album = candidate
            if album is None and folder_identity:
                indexed = session.scalar(select(FilesystemAlbum).where(FilesystemAlbum.identity == folder_identity))
                if indexed:
                    candidate = session.get(Album, indexed.album_id)
                    if candidate and not within(root, candidate.folder).exists():
                        # Inodes can be recycled after deletion. A surviving
                        # indexed file identity must also match before retaining
                        # metadata for a directory without a stable manifest ID.
                        old_files = {f.signature.rsplit(':',3)[0] for f in session.scalars(
                            select(FilesystemFile).where(FilesystemFile.album_id==candidate.id))}
                        moved_files = root_images + (self._images(covers) if covers else [])
                        if any(identity(p) in old_files for p in moved_files):
                            album = candidate
            if album is None:
                # Ignore Copies/Originals infrastructure and empty directories.
                if not covers and not root_images:
                    return
                title = re.sub(r'--[0-9a-f]{12,32}$', '', directory.name)
                title = re.sub(r'(?<=\))_\d+$','',title)
                match = re.search(r'\s*\((18\d{2}|19\d{2}|20\d{2}|YYYY)\)$', title,flags=re.I)
                year = int(match[1]) if match and match[1].upper()!='YYYY' else None
                if match:
                    title = title[:match.start()]
                artist_name = directory.parent.name
                artist = session.scalar(select(Artist).where(Artist.name == artist_name))
                if artist is None:
                    artist = Artist(name=artist_name)
                    session.add(artist)
                    session.flush()
                album = Album(title=title, artist=artist_name, artist_id=artist.id, year=year, folder=relative)
                session.add(album)
                session.flush()
            indexed = session.get(FilesystemAlbum, album.id)
            if indexed is None:
                had_covers = session.scalar(select(AlbumImage.image_id).where(AlbumImage.album_id==album.id,
                    AlbumImage.cover_path.contains('/Covers/')).limit(1)) is not None
                indexed = FilesystemAlbum(album_id=album.id, folder=relative, identity=folder_identity, covers_seen=had_covers)
                session.add(indexed)
            # This is an index pointer update only, never a physical folder move
            # or a manifest rewrite. Saved music credits remain unchanged.
            album.folder = relative
            indexed.folder, indexed.identity, indexed.present = relative, folder_identity, True
            indexed.covers_seen = indexed.covers_seen or covers is not None
            paths = root_images
            if covers:
                # Nested cover folders are allowed; symlink targets are not.
                paths += self._images(covers)
            elif not indexed.covers_seen:
                originals = directory / 'Originals'
                if originals.is_dir() and not originals.is_symlink():
                    paths += self._images(originals, recurse=False)
            existing = {f.path: f for f in session.scalars(select(FilesystemFile).where(FilesystemFile.album_id == album.id))}
            present = set()
            for path in sorted(paths):
                if self.stop_event.is_set():
                    return
                name = path.relative_to(root).as_posix()
                present.add(name)
                prior = existing.get(name)
                try:
                    stamp = signature(path)
                    if prior and prior.signature == stamp:
                        prior.present = True
                        continue
                    if path.stat().st_size > self.settings.max_image_mb * 1024 * 1024:
                        continue
                    data = path.read_bytes()
                    if signature(path) != stamp:
                        continue  # Never index a partially saved editor buffer.
                    sha = hashlib.sha256(data).hexdigest()
                    asset = session.scalar(select(ImageAsset).where(ImageAsset.sha256 == sha))
                    if asset is None:
                        image, fmt = decode_image(data)
                        ident = uid()
                        ext = {'JPEG':'jpg','PNG':'png','WEBP':'webp','TIFF':'tif','BMP':'bmp'}[fmt]
                        # Virtual source reference; no copy is ever created here.
                        asset = ImageAsset(id=ident, sha256=sha, phash=perceptual_hash(image),
                            original=f'.filesystem/{ident}/source.{ext}', derivative=f'.filesystem/{ident}/source.{ext}',
                            thumbnail=f'.filesystem/{ident}/thumbnail', format=fmt, width=image.width,
                            height=image.height, size=len(data))
                        session.add(asset)
                        session.flush()
                    if prior is None:
                        prior = FilesystemFile(album_id=album.id, image_id=asset.id, path=name, signature=stamp)
                        session.add(prior)
                    else:
                        prior.image_id, prior.signature, prior.present = asset.id, stamp, True
                except (OSError, ValueError, SyntaxError):
                    # A corrupt/new half-written file stays unindexed. An older
                    # valid record is retained until a complete save is readable.
                    continue
            for name, prior in existing.items():
                if name not in present:
                    prior.present = False
            session.flush()
            for row in current_files(session,album.id).values():
                self._link(session,album,row,within(root,row.path))
            session.commit()
            if session.info.get('filesystem_changed'):
                self.revision += 1

    @staticmethod
    def _images(directory, recurse=True):
        images = []
        for path in directory.iterdir():
            if path.name.startswith('.') or path.is_symlink():
                continue
            if path.is_file() and path.suffix.lower() in EXTENSIONS:
                images.append(path)
            elif recurse and path.is_dir():
                images.extend(FilesystemIndex._images(path))
        return images

    @staticmethod
    def _link(session, album, row, path):
        link = session.get(AlbumImage, (album.id, row.image_id))
        kind, confidence = classify(path.stem)
        if link is None:
            link = AlbumImage(album_id=album.id, image_id=row.image_id, classification=kind,
                confidence=confidence, cover_path=row.path, cover_source='filesystem')
            session.add(link)
        elif not link.deleted_at:
            # Keep explicit classification/restoration decisions. Descriptive
            # filenames beat automatic Additional guesses, but never manual ones.
            if kind != 'additional' and not link.manual:
                link.classification, link.confidence = kind, confidence
            link.cover_path = row.path
            link.cover_source = 'filesystem'
