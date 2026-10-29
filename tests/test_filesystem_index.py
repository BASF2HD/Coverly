"""Finder/editor changes are indexed, never repaired. All artwork is temporary."""
import hashlib
import io
import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from coverly.app import create_app
from coverly.db import Album, AlbumImage, FilesystemFile, ImageAsset
from coverly.filesystem import FilesystemIndex
from coverly.intelligence import create_album
from coverly.storage import add_image, within


def picture(color, size=(600, 500), fmt='JPEG'):
    out=io.BytesIO()
    Image.new('RGB',size,color).save(out,fmt)
    return out.getvalue()


def tree(folder):
    return {p.relative_to(folder).as_posix():(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns)
            for p in folder.rglob('*') if p.is_file()}


@pytest.fixture
def indexed(settings,monkeypatch):
    settings=settings.model_copy(update={'filesystem_sync':True})
    # Most tests advance the same production scanner explicitly to make races
    # reproducible. The final test exercises its real background poller.
    monkeypatch.setattr(FilesystemIndex,'start',lambda self:None)
    app=create_app(settings)
    with TestClient(app,base_url='http://localhost:8000',headers={'X-Coverly-Request':'1'}) as client:
        yield client,app.state.file_index,app.state.sessions,settings


def existing(indexed):
    client,index,sessions,settings=indexed
    with sessions() as session:
        album=create_album(session,settings,{'title':'Aayusunooru','artist':'T. Rajendar','year':1987})
        asset,_=add_image(session,settings,album,picture('red'),caption='front')
        session.commit()
        return album.id,asset.id,within(settings.data,album.folder)


def detail(client,album_id):
    response=client.get('/api/albums/'+album_id)
    assert response.status_code==200,response.text
    return response.json()


def test_renamed_and_added_covers_replace_stale_gallery_without_touching_files(indexed):
    client,index,sessions,settings=indexed
    album_id,old_id,folder=existing(indexed)
    for file in (folder/'Covers').iterdir():file.unlink()
    for file in (folder/'Originals').iterdir():file.unlink()
    (folder/'Covers'/'Aayusunooru_Front.jpg').write_bytes(picture('blue'))
    (folder/'Covers'/'Aayusunooru_Back.png').write_bytes(picture('green',fmt='PNG'))
    before=tree(folder)
    value=detail(client,album_id)
    assert len(value['images'])==2
    assert old_id not in {im['id'] for im in value['images']}
    assert {im['classification'] for im in value['images']}=={'front','back'}
    for image in value['images']:
        response=client.get(image['display_url'])
        assert response.content==within(settings.data,image['cover_path']).read_bytes()
        assert response.headers['content-type'].startswith('image/')
        assert client.get('/api/images/'+image['id']+'/original').content==response.content
        assert client.get('/api/images/'+image['id']+'/thumbnail').status_code==200
    assert client.get('/api/albums?q=Aayusunooru').json()['items'][0]['image_count']==2
    assert tree(folder)==before
    assert not (settings.data/'.filesystem').exists()


def test_in_place_edit_invalidates_preview_and_full_image_and_rename_retains_content(indexed):
    client,index,sessions,settings=indexed
    album_id,_,folder=existing(indexed)
    first=detail(client,album_id)['images'][0]
    file=within(settings.data,first['cover_path'])
    original_source=list((folder/'Originals').iterdir())[0].read_bytes()
    old_thumb=client.get('/api/albums/'+album_id+'/images/'+first['id']+'/cover?thumbnail=true').content
    replacement=picture('purple',(800,800))
    file.write_bytes(replacement)
    updated=detail(client,album_id)['images'][0]
    assert updated['sha256']==hashlib.sha256(replacement).hexdigest()
    assert updated['id']!=first['id']
    assert updated['width']==800
    assert client.get(updated['display_url']).content==replacement
    assert client.get('/api/albums/'+album_id+'/images/'+updated['id']+'/cover?thumbnail=true').content!=old_thumb
    new_name=file.with_name('Improved Front.jpeg')
    file.rename(new_name)
    final=detail(client,album_id)['images'][0]
    assert final['id']==updated['id']
    assert final['filename']=='Improved Front.jpeg'
    assert client.get(final['display_url']).content==replacement
    assert list((folder/'Originals').iterdir())[0].read_bytes()==original_source


def test_folder_rename_with_manifest_and_without_manifest_and_new_folder_discovery(indexed):
    client,index,sessions,settings=indexed
    album_id,_,folder=existing(indexed)
    index.scan()
    renamed=folder.with_name('Aayusu Nooruu (1987)')
    folder.rename(renamed)
    before=tree(renamed)
    index.scan()
    value=detail(client,album_id)
    assert value['folder'].endswith('Aayusu Nooruu (1987)')
    assert value['title']=='Aayusunooru'  # Verified metadata is not replaced by directory guesses.
    assert client.get('/api/albums?q=Aayusu%20Nooruu').json()['total']==1
    assert tree(renamed)==before
    (renamed/'album.json').unlink()
    new_parent=settings.data/'T Rajendar'
    new_parent.mkdir()
    moved=new_parent/'My edited album (1987)'
    renamed.rename(moved)
    index.scan()
    assert detail(client,album_id)['folder']=='T Rajendar/My edited album (1987)'
    assert not folder.exists()
    assert client.get('/api/albums').json()['total']==1
    added=settings.data/'Chandrabose'/'பூக்கள் (1983)'
    added.mkdir(parents=True)
    (added/'Front.jpg').write_bytes(picture('yellow'))
    index.scan()
    albums=client.get('/api/albums?q=பூக்கள்').json()
    assert albums['total']==1
    assert albums['items'][0]['year']==1983
    assert not (added/'album.json').exists()
    assert not (added/'Originals').exists()
    assert list(added.iterdir())==[added/'Front.jpg']


def test_deletions_hide_files_and_albums_without_original_fallback_or_recreation(indexed):
    client,index,sessions,settings=indexed
    album_id,_,folder=existing(indexed)
    first=detail(client,album_id)
    old=first['images'][0]
    within(settings.data,old['cover_path']).unlink()
    assert detail(client,album_id)['images']==[]
    assert client.get(old['display_url']).status_code==404
    assert list((folder/'Originals').iterdir())  # Hidden Original must not reappear in gallery.
    shutil.rmtree(folder/'Covers')
    index.scan()
    assert detail(client,album_id)['images']==[]
    shutil.rmtree(folder)
    index.scan()
    assert client.get('/api/albums').json()['total']==0
    assert not folder.exists()
    assert detail(client,album_id)['images']==[]


def test_missing_covers_before_first_scan_are_not_recovered(indexed):
    client,index,_,_=indexed
    album_id,_,folder=existing(indexed)
    shutil.rmtree(folder/'Covers')
    assert detail(client,album_id)['images']==[]
    assert not (folder/'Covers').exists()


def test_stable_scan_is_incremental_and_soft_removal_is_not_undone(indexed):
    client,index,_,settings=indexed
    album_id,_,folder=existing(indexed)
    image=detail(client,album_id)['images'][0]
    before=tree(folder)
    revision=index.revision
    index.scan()
    index.scan()
    assert index.revision==revision
    assert tree(folder)==before
    assert client.post(f"/api/albums/{album_id}/images/{image['id']}/trash").status_code==200
    after_explicit_removal=tree(folder)
    index.scan()
    assert detail(client,album_id)['images']==[]
    assert tree(folder)==after_explicit_removal


def test_identical_cover_copies_do_not_trigger_endless_refresh_and_internal_results_are_not_albums(indexed):
    client,index,_,settings=indexed
    album_id,_,folder=existing(indexed)
    image=detail(client,album_id)['images'][0]
    raw=within(settings.data,image['cover_path']).read_bytes()
    (folder/'Covers'/'additional_duplicate.jpg').write_bytes(raw)
    internal=settings.data/'Restored'/'imported'
    internal.mkdir(parents=True)
    (internal/'external-result.jpg').write_bytes(picture('pink'))
    index.scan()
    revision=index.revision
    index.scan()
    assert index.revision==revision
    assert client.get('/api/albums').json()['total']==1
    value=detail(client,album_id)
    assert len(value['images'])==1
    assert 'additional_duplicate' not in value['images'][0]['filename']


def test_metadata_save_never_regenerates_indexed_cover_with_historical_restoration(indexed):
    from coverly.db import Restoration
    from coverly.storage import write_manifest
    client,index,sessions,settings=indexed
    album_id,_,folder=existing(indexed)
    image=detail(client,album_id)['images'][0]
    before=tree(folder/'Covers')
    with sessions() as session:
        result=Restoration(image_id=image['id'],album_id=album_id,path='Restored/old-result.jpg',provider='test',preset='gentle',status='approved')
        session.add(result);session.flush()
        link=session.get(AlbumImage,(album_id,image['id']))
        link.active_restoration_id=result.id
        album=session.get(Album,album_id)
        album.notes='Explicit metadata edit only'
        write_manifest(session,settings,album)
        session.commit()
    assert tree(folder/'Covers')==before
    assert not (settings.data/'Restored'/'old-result.jpg').exists()


def test_corrupt_save_retries_and_symlinks_are_not_followed(indexed,tmp_path):
    client,index,_,settings=indexed
    album_id,_,folder=existing(indexed)
    target=folder/'Covers'/'Edited Back.jpg'
    target.write_bytes(b'editor still saving')
    assert len(detail(client,album_id)['images'])==1
    target.write_bytes(picture('orange'))
    assert len(detail(client,album_id)['images'])==2
    outside=tmp_path/'private.jpg'
    outside.write_bytes(picture('black'))
    try:(folder/'Covers'/'Outside Front.jpg').symlink_to(outside)
    except OSError:pytest.skip('Symlink creation needs OS permission')
    assert len(detail(client,album_id)['images'])==2


def test_background_poller_notices_editor_changes_without_a_manual_request(settings):
    settings=settings.model_copy(update={'filesystem_sync':True,'filesystem_poll_seconds':1})
    app=create_app(settings)
    folder=settings.data/'Composer'/'New film (1989)'
    folder.mkdir(parents=True)
    first=folder/'Front.jpg'
    first.write_bytes(picture('cyan'))
    before=tree(folder)
    with TestClient(app,base_url='http://localhost:8000') as client:
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            response=client.get('/api/albums?q=New%20film').json()
            if response['total']==1 and response['items'][0]['image_count']==1:break
            time.sleep(.1)
        else:pytest.fail('Background index did not discover the folder')
        first.unlink()
        (folder/'Back.jpg').write_bytes(picture('magenta'))
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            with app.state.sessions() as session:
                rows=list(session.scalars(select(FilesystemFile).where(FilesystemFile.present==True)))
                if len(rows)==1 and rows[0].path.endswith('/Back.jpg'):break
            time.sleep(.1)
        else:pytest.fail('Background index did not detect editor changes')
        assert client.get('/api/filesystem/status').json()['enabled']
    assert not first.exists()
    assert not (folder/'album.json').exists()
    assert not (folder/'Originals').exists()


def test_indexed_files_backup_roundtrip_and_explicit_purge(indexed,tmp_path):
    from coverly.backup import make_backup,restore_backup
    from coverly.db import open_db
    client,index,sessions,settings=indexed
    album_id,_,folder=existing(indexed)
    for file in (folder/'Covers').iterdir():file.unlink()
    for file in (folder/'Originals').iterdir():file.unlink()
    raw=picture('steelblue')
    (folder/'Covers'/'My improved Front.jpg').write_bytes(raw)
    value=detail(client,album_id)
    backup=tmp_path/'portable.zip'
    make_backup(settings.data,backup)
    destination=tmp_path/'another-computer'
    restore_backup(backup,destination)
    assert (destination/value['images'][0]['cover_path']).read_bytes()==raw
    assert not (destination/value['images'][0]['original']).exists()
    engine,copied=open_db(destination)
    try:
        index_copy=FilesystemIndex(settings.model_copy(update={'storage':str(destination)}),copied)
        index_copy.scan()
        with copied() as session:
            from coverly.filesystem import current_files,asset_path
            files=current_files(session,album_id)
            assert len(files)==1
            asset=session.get(ImageAsset,next(iter(files)))
            assert asset_path(session,index_copy.settings,asset,album_id).read_bytes()==raw
    finally:engine.dispose()
    assert client.post('/api/albums/trash',json={'ids':[album_id]}).status_code==200
    plan=client.post('/api/trash/preview',json={'ids':[album_id]}).json()
    response=client.post('/api/trash/purge',json={'plan_id':plan['plan_id'],'confirmation':'DELETE'})
    assert response.status_code==200,response.text
    assert not folder.exists()
    index.scan()
    assert client.get('/api/albums').json()['total']==0
