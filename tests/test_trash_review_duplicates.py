import json
from sqlalchemy import select
from coverly.db import Album, AlbumImage, ImageAsset
from coverly.storage import within
from test_api import import_image


def test_album_trash_restore_preserves_shared_original(client,image_bytes,settings):
    first=import_image(client,image_bytes,'First edition');second=import_image(client,image_bytes,'Second edition')
    detail=client.get('/api/albums/'+first).json();im=detail['images'][0]
    assert client.get('/api/duplicates').json()['total']==1
    assert client.post('/api/albums/trash',json={'ids':[first]}).status_code==200
    assert client.get('/api/albums').json()['total']==1
    assert client.get('/api/albums?trashed=true').json()['items'][0]['id']==first
    assert client.get('/api/duplicates').json()['total']==0
    assert client.get('/api/albums/'+second).json()['images'][0]['id']==im['id']
    assert client.get(f"/api/images/{im['id']}/original").content==image_bytes
    manifest=json.loads(within(settings.data,detail['folder']+'/album.json').read_text(encoding='utf-8'))
    assert manifest['deleted_at']
    assert client.post('/api/albums/restore',json={'ids':[first]}).status_code==200
    assert client.get('/api/albums').json()['total']==2
    assert client.get('/api/duplicates').json()['total']==1


def test_remove_image_from_one_album_restore_and_review(client,image_bytes):
    first=import_image(client,image_bytes,'First');second=import_image(client,image_bytes,'Second')
    im=client.get('/api/albums/'+first).json()['images'][0]
    assert client.post(f"/api/albums/{first}/images/{im['id']}/trash").status_code==200
    detail=client.get('/api/albums/'+first).json()
    assert detail['images']==[] and detail['cover_id'] is None
    assert detail['trashed_images'][0]['id']==im['id']
    assert len(client.get('/api/albums/'+second).json()['images'])==1
    assert client.post(f"/api/albums/{first}/images/{im['id']}/restore").status_code==200
    with client.app.state.sessions() as session:
        session.get(Album,first).review=True;session.get(Album,second).review=True;session.commit()
    assert client.get('/api/albums?review=true').json()['total']==2
    assert client.patch('/api/albums/'+first,json={'title':'Reviewed title'}).status_code==200
    assert client.get('/api/albums/'+first).json()['review'] is False
    assert client.get('/api/albums?review=true').json()['total']==1


def test_trash_batch_is_atomic_and_invalid_image_rejected(client,image_bytes):
    album=import_image(client,image_bytes)
    assert client.post('/api/albums/trash',json={'ids':[album,'missing']}).status_code==404
    assert not client.get('/api/albums/'+album).json()['deleted_at']
    assert client.post(f'/api/albums/{album}/images/missing/trash').status_code==404


def test_trash_and_image_removal_survive_portable_backup(client,image_bytes,settings,tmp_path):
    from coverly.backup import make_backup,restore_backup
    from coverly.db import open_db
    album=import_image(client,image_bytes,'Portable edition')
    detail=client.get('/api/albums/'+album).json();image=detail['images'][0]
    client.post(f"/api/albums/{album}/images/{image['id']}/trash").raise_for_status()
    client.post('/api/albums/trash',json={'ids':[album]}).raise_for_status()
    archive=tmp_path/'portable.zip';restored=tmp_path/'restored'
    make_backup(settings.data,archive);restore_backup(archive,restored)
    engine,sessions=open_db(restored)
    with sessions() as session:
        assert session.get(Album,album).deleted_at
        assert session.get(AlbumImage,(album,image['id'])).deleted_at
        asset=session.get(ImageAsset,image['id'])
        assert within(restored,asset.original).read_bytes()==image_bytes
    engine.dispose()
