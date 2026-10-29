import sqlite3
import zipfile
from pathlib import Path
import pytest
from coverly.backup import make_backup, restore_backup
from coverly.intelligence import create_album
from coverly.storage import add_image, write_manifest
from coverly.db import open_db, Album, ImageAsset
from sqlalchemy import select


def test_backup_roundtrip_and_no_secrets(settings, db, image_bytes, tmp_path):
    with db() as s:
        album=create_album(s,settings,{'title':'Roundtrip','manual':True})
        album.soundtrack={'title':'Roundtrip','composer':'Test Composer','source_url':'https://music.example/roundtrip','tracks':[{'number':1,'title':'Test song','lyrics':'Test lyricist'}]}
        add_image(s,settings,album,image_bytes)
        s.commit()
    (settings.data/'.env').write_text('SECRET=do-not-export')
    (settings.data/'personal.txt').write_text('unrelated file')
    backup=tmp_path/'backup.zip'
    make_backup(settings.data,backup)
    with zipfile.ZipFile(backup) as z:
        assert '.env' not in z.namelist() and 'personal.txt' not in z.namelist()
        assert any(x.startswith('.thumbnails/') for x in z.namelist())
    destination=tmp_path/'windows-copy'
    restore_backup(backup,destination)
    engine,sessions=open_db(destination)
    with sessions() as s:
        image=s.scalar(select(ImageAsset))
        assert (destination/image.original).read_bytes()==image_bytes
        restored=s.scalar(select(Album))
        assert restored.title=='Roundtrip' and restored.soundtrack['tracks'][0]['lyrics']=='Test lyricist'
    engine.dispose()


@pytest.mark.parametrize('path',['../escape','C:/windows/file','safe\\..\\file','/absolute','NUL.txt'])
def test_malicious_archive(tmp_path,path):
    archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('coverly.sqlite3',b'x')
        z.writestr('coverly-backup.json','{}')
        z.writestr(path,b'evil')
    with pytest.raises(ValueError):
        restore_backup(archive,tmp_path/'destination')
    assert not list((tmp_path/'destination').rglob('*'))
