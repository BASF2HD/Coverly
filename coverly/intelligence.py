from __future__ import annotations

import re
import shutil
from .db import Album, Artist, Edition, Candidate, uid
from .storage import available_folder
from sqlalchemy import select

CONVENTIONAL = {"நாயகன்": "Nayakan", "தளபதி": "Thalapathi", "மௌன ராகம்": "Mouna Ragam"}


def transliterate(text):
    text = text.strip()
    if text in CONVENTIONAL:
        return CONVENTIONAL[text]
    if not re.search(r"[\u0b80-\u0bff]", text):
        return text
    from indic_transliteration import sanscript
    result = sanscript.transliterate(text, sanscript.TAMIL, sanscript.ITRANS)
    return result  # Mechanical transliteration is preserved as a candidate, not a verified film title.


def identify(metadata):
    title = str(metadata.get("title", "")).strip()
    tamil_title = metadata.get("tamil_title", "")
    if re.search(r"[\u0b80-\u0bff]", title):
        tamil_title = title
        title = transliterate(title)
    reliable = bool(title and title.lower() not in {"untitled", "product", "image", "home", "shop"})
    confidence = .9 if reliable and metadata.get("structured") else .75 if reliable else 0
    if reliable and metadata.get("manual"):
        confidence = 1
    if tamil_title and tamil_title not in CONVENTIONAL:
        confidence = min(confidence, .6)
    parts = [x.strip() for x in re.split(r"\s+(?:/|&|and|\+|with)\s+", title, flags=re.I) if x.strip()]
    compilation = len(parts) > 1 or bool(re.search(r"compilation|film hits|various films|2.in.1|3.in.1", title, re.I))
    return {"title": title if reliable else "", "tamil_title": tamil_title, "confidence": confidence,
            "compilation": compilation, "components": parts if compilation and len(parts) > 1 else []}


def create_album(session, settings, metadata, source_url=None):
    if source_url:
        existing = session.scalar(select(Edition).where(Edition.source_url == source_url))
        if existing:
            return session.get(Album, existing.album_id)
    from .metadata import source_metadata
    metadata = source_metadata(metadata,list(session.scalars(select(Artist.name))))
    from .artists import approved_name
    metadata["artist"]=approved_name(session,str(metadata.get("artist") or "Unknown Artist"))
    info = identify(metadata)
    identifier = uid()
    artist_name = str(metadata.get("artist") or "Unknown Artist")[:200]
    artist = session.scalar(select(Artist).where(Artist.name == artist_name))
    if not artist:
        artist = Artist(name=artist_name)
        session.add(artist)
        session.flush()
    year = metadata.get("year")
    try:
        year = int(year) if year else None
        if year is not None and not 1850 <= year <= 2100:
            year = None
    except (ValueError, TypeError):
        year = None
    album = Album(id=identifier, title=info["title"][:250] or "Unknown album",
                  tamil_title=info["tamil_title"], artist=artist_name, artist_id=artist.id, year=year,
                  label=str(metadata.get("label", ""))[:200], catalogue=str(metadata.get("catalogue", ""))[:200],
                  confidence=info["confidence"], review=artist_name=="Unknown Artist" or info["confidence"] < settings.high_confidence or info["compilation"],
                  compilation=info["compilation"], folder="temporary-" + identifier)
    album.folder = available_folder(session,settings,album)
    session.add(album)
    session.flush()
    session.add(Edition(album_id=album.id, label=album.label, catalogue=album.catalogue, source_url=source_url))
    session.add(Candidate(album_id=album.id, title=album.title, evidence="Source metadata; " + str(source_url or "manual import"), confidence=album.confidence))
    for component in info["components"]:
        session.add(Candidate(album_id=album.id, title=component, evidence="Possible compilation component; confirm before creating relationship", confidence=.5))
    return album


def ocr_capabilities():
    import pytesseract
    executable = shutil.which("tesseract")
    if not executable:
        return {"available": False, "languages": [], "message": "Install Tesseract with tam and eng language data"}
    try:
        languages = pytesseract.get_languages(config="")
        return {"available": True, "languages": languages, "message": "Tamil ready" if "tam" in languages else "Tamil language data is missing"}
    except Exception as exc:
        return {"available": False, "languages": [], "message": str(exc)}


def run_ocr(path, languages):
    import pytesseract
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter
    caps = ocr_capabilities()
    missing = set(languages.split("+")) - set(caps["languages"])
    if not caps["available"] or missing:
        raise RuntimeError("OCR unavailable: install Tesseract and language data for " + ", ".join(sorted(missing)))
    with Image.open(path) as src:
        im = ImageOps.autocontrast(ImageOps.grayscale(src))
        im.thumbnail((2400, 2400))
        im = ImageEnhance.Contrast(im.filter(ImageFilter.MedianFilter(3))).enhance(1.15)
        best = ("", 0.0)
        for angle in (0, 90, 270):
            data = pytesseract.image_to_data(im.rotate(angle, expand=True), lang=languages, config="--psm 11", timeout=60, output_type=pytesseract.Output.DICT)
            words = [(t, float(c)) for t, c in zip(data["text"], data["conf"]) if t.strip() and float(c) >= 20]
            if words:
                result = (" ".join(t for t, _ in words), sum(c for _, c in words) / len(words) / 100)
                if result[1] > best[1]:
                    best = result
        return best
