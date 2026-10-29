from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from .storage import within, safe_name


def make_backup(root: Path, destination: Path):
    """Use SQLite's backup API so WAL state is included; copy only archive files, never config/secrets."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="coverly-backup-") as tmp:
        snapshot = Path(tmp) / "coverly.sqlite3"
        with closing(sqlite3.connect(root / "coverly.sqlite3")) as src, closing(sqlite3.connect(snapshot)) as dst:
            src.backup(dst)
        with closing(sqlite3.connect(snapshot)) as db:
            relative_files = {p for row in db.execute("SELECT original, derivative, thumbnail FROM images") for p in row}
            relative_files.update(p for row in db.execute("SELECT path, input_path FROM restorations") for p in row if p)
            relative_files.update(p for (p,) in db.execute("SELECT cover_path FROM album_images") if p)
            relative_files.update(folder + "/album.json" for (folder,) in db.execute("SELECT folder FROM albums"))
            indexed = db.execute('SELECT 1 FROM filesystem_albums LIMIT 1').fetchone() is not None
            relative_files.update(p for (p,) in db.execute('SELECT path FROM filesystem_files WHERE present=1'))
            relative_files = {p for p in relative_files if not p.startswith('.filesystem/')}
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as z:
            z.write(snapshot, "coverly.sqlite3")
            for relative in sorted(relative_files):
                path = within(root, relative)
                if not path.is_file():
                    if indexed:
                        continue  # Historical index references are not missing artwork to recover.
                    raise ValueError("Referenced archive file is missing: " + relative)
                if path.is_symlink():
                    raise ValueError("Archive backup refuses symlinked files")
                z.write(path, relative)
            z.writestr("coverly-backup.json", json.dumps({"schema": 1, "relative_paths": True}))


def restore_backup(archive: Path, destination: Path):
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Restore destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len(infos) > 200_000 or sum(i.file_size for i in infos) > 500 * 1024**3:
            raise ValueError("Archive exceeds restore limits")
        seen = set()
        for info in infos:
            path = within(destination, info.filename.rstrip("/"))
            normalized = info.filename.rstrip("/").casefold()
            if normalized in seen or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Duplicate path or symlink in archive")
            seen.add(normalized)
            if any((safe_name(part) != part and part != ".thumbnails") or len(part) > 100 for part in Path(info.filename).parts):
                raise ValueError("Archive contains non-portable filenames")
            if info.compress_size and info.file_size / info.compress_size > 1000:
                raise ValueError("Suspicious compression ratio")
        if "coverly.sqlite3" not in z.namelist() or "coverly-backup.json" not in z.namelist():
            raise ValueError("Not a Coverly backup")
        # All entries validated before any file is written.
        for info in infos:
            path = within(destination, info.filename.rstrip("/"))
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, path.open("xb") as dst:
                    import shutil
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
    with closing(sqlite3.connect(destination / "coverly.sqlite3")) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Backup database failed integrity validation")
