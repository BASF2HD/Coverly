import io
import zipfile
import pytest
from sqlalchemy import select, func
from PIL import Image
from coverly.storage import safe_name, within, add_image, folder_for, decode_image, atomic_write
from coverly.intelligence import create_album, transliterate, identify
from coverly.db import ImageAsset, AlbumImage


@pytest.mark.parametrize("value", ["CON", "PRN.jpg", "NUL", "COM1", "LPT9"])
def test_windows_devices(value):
    assert safe_name(value).startswith("_")


@pytest.mark.parametrize("value", ["../secret", "/etc/passwd", "C:/Windows/system.ini", "\\\\server\\share\\x", "safe/../../x", "safe\\x"])
def test_traversal(settings, value):
    with pytest.raises(ValueError):
        within(settings.data, value)


def test_cross_platform_name_and_unknown(settings, db):
    assert safe_name('நாயகன்: front? <>.') == 'நாயகன்_ front_ __'
    with db() as s:
        a = create_album(s, settings, {"title": ""})
        assert a.title=="Unknown album"
        b = create_album(s, settings, {"title": "X" * 300, "artist": "A" * 200})
        duplicate=create_album(s, settings, {"title": "X" * 300, "artist": "A" * 200})
        assert duplicate.folder==b.folder+'_1'
        assert b.id[:12] not in b.folder


def test_original_and_duplicate_preservation(settings, db, image_bytes):
    with db() as s:
        a = create_album(s, settings, {"title": "Test", "manual": True})
        first, dup = add_image(s, settings, a, image_bytes, caption="front.jpg")
        assert not dup
        second, dup = add_image(s, settings, a, image_bytes)
        assert dup and second.id == first.id
        assert within(settings.data, first.original).read_bytes() == image_bytes
        assert s.scalar(select(func.count()).select_from(ImageAsset)) == 1
        assert s.scalar(select(func.count()).select_from(AlbumImage)) == 1
        assert within(settings.data, first.derivative).is_file()
        b = create_album(s, settings, {"title": "Different edition"})
        add_image(s, settings, b, image_bytes)
        assert s.scalar(select(func.count()).select_from(AlbumImage)) == 2
        s.commit()


def test_corrupt_animated_and_exclusive(settings):
    with pytest.raises(Exception):
        decode_image(b"not an image")
    out = io.BytesIO()
    Image.new("RGB", (10, 10)).save(out, "GIF")
    with pytest.raises(ValueError):
        decode_image(out.getvalue())
    path = settings.data / "atomic.txt"
    atomic_write(path, b"original", exclusive=True)
    with pytest.raises(FileExistsError):
        atomic_write(path, b"overwrite", exclusive=True)
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize("tamil, english", [("நாயகன்", "Nayakan"), ("தளபதி", "Thalapathi"), ("மௌன ராகம்", "Mouna Ragam")])
def test_conventional_transliteration(tamil, english):
    assert transliterate(tamil) == english


def test_mechanical_transliteration_and_compilation():
    assert transliterate("தமிழ்") != "தமிழ்"
    assert identify({"title": "Nayakan / Thalapathi / Mouna Ragam"})["compilation"]
    assert identify({"title": ""})["confidence"] == 0


def test_exfat_exclusive_write_fallback(tmp_path, monkeypatch):
    import errno
    def unavailable(*args):
        raise OSError(errno.EOPNOTSUPP, "Filesystem cannot hard-link")
    monkeypatch.setattr('coverly.storage.os.link', unavailable)
    path=tmp_path/'exfat'/'original.jpg'
    atomic_write(path,b'original',exclusive=True)
    assert path.read_bytes()==b'original'
    with pytest.raises(FileExistsError):
        atomic_write(path,b'replacement',exclusive=True)
    assert path.read_bytes()==b'original'


def test_uncertain_name_is_not_folder_identity(settings,db):
    with db() as s:
        album=create_album(s,settings,{'title':'Unverified album guess'})
        assert album.review
        assert album.title in album.folder and album.id[:12] not in album.folder
