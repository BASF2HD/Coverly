import multiprocessing
import sqlite3
import sys
from coverly.runtime import worker_main
from coverly.db import open_db


def test_native_spawn_worker(tmp_path,monkeypatch):
    monkeypatch.setenv('COVERLY_HOME',str(tmp_path))
    ctx=multiprocessing.get_context('spawn')
    stop=ctx.Event()
    stop.set()
    process=ctx.Process(target=worker_main,args=(stop,))
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.terminate();process.join()
        raise AssertionError('Native spawn worker did not exit')
    assert process.exitcode==0
    assert (tmp_path/'archive'/'coverly.sqlite3').is_file()


def test_migrations_and_integrity(settings):
    for _ in range(2):
        engine,_=open_db(settings.data)
        engine.dispose()
    with sqlite3.connect(settings.data/'coverly.sqlite3') as db:
        assert db.execute('SELECT version FROM schema_migrations').fetchall()==[(1,),(2,),(3,),(4,),(5,),(6,),(7,)]
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_upgrade_existing_v1_archive_preserves_records_and_relative_paths(settings):
    from pathlib import Path
    from sqlalchemy import select
    from coverly.db import Album, AlbumImage, ImageAsset
    # Build the schema that shipped before Trash existed.
    settings.data.mkdir(parents=True)
    migration=Path(__file__).parents[1]/'coverly'/'migrations'/'001_initial.sql'
    with sqlite3.connect(settings.data/'coverly.sqlite3') as db:
        db.executescript(migration.read_text(encoding='utf-8'))
        db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied TEXT NOT NULL)')
        db.execute("INSERT INTO schema_migrations VALUES(1,'before-upgrade')")
        db.execute("INSERT INTO albums(id,title,tamil_title,artist,year,label,catalogue,confidence,review,compilation,folder,notes,created) VALUES('legacy','Legacy LP','','Composer',1987,'Echo','8000',.9,1,0,'Composer/Legacy--legacy','keep notes','2026-01-01')")
    engine,sessions=open_db(settings.data)
    with sessions() as session:
        album=session.get(Album,'legacy')
        assert album.title=='Legacy LP' and album.notes=='keep notes' and album.review
        assert album.folder=='Composer/Legacy--legacy' and album.deleted_at is None
        assert album.soundtrack=={}
    engine.dispose()
