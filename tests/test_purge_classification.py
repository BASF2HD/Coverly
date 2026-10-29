import io,json
import pytest
from PIL import Image,ImageDraw
from sqlalchemy import select
from coverly.db import Album,AlbumImage,ImageAsset,Page,JobItem
from coverly.jobs import Worker,enqueue
from coverly.storage import within
from coverly.classification import decide
from test_api import import_image


def test_circle_and_gallery_classification_with_manual_override(client,settings,image_bytes):
    circle=Image.new('RGB',(256,256),'white');d=ImageDraw.Draw(circle);d.ellipse((5,5,251,251),fill='black');d.ellipse((90,90,166,166),fill='red')
    assert decide(circle)[0]=='vinyl_side_a'
    assert decide(circle,disc_index=1)[0]=='vinyl_side_b'
    assert decide(circle,text='SIDE B')[0]=='vinyl_side_b'
    sleeve=Image.new('RGB',(256,256),'#739279')
    assert decide(sleeve,index=0)[0]=='front' and decide(sleeve,index=1)[0]=='back'
    album=import_image(client,image_bytes);image=client.get('/api/albums/'+album).json()['images'][0]
    client.patch(f"/api/albums/{album}/images/{image['id']}",json={'classification':'inner'}).raise_for_status()
    client.post('/api/classify-albums',json={'ids':[album]}).raise_for_status();Worker(settings,client.app.state.sessions).tick()
    assert client.get('/api/albums/'+album).json()['images'][0]['classification']=='inner'


def test_purge_exclusive_removes_original_and_associated_folder(client,settings,image_bytes):
    album=import_image(client,image_bytes);detail=client.get('/api/albums/'+album).json();im=detail['images'][0]
    client.post('/api/albums/trash',json={'ids':[album]}).raise_for_status()
    plan=client.post('/api/trash/preview',json={}).json()
    assert plan['album_count']==1 and plan['originals_removed']==1
    assert client.post('/api/trash/purge',json={'plan_id':plan['plan_id'],'confirmation':'wrong'}).status_code==422
    r=client.post('/api/trash/purge',json={'plan_id':plan['plan_id'],'confirmation':'DELETE'})
    assert r.status_code==200,r.text
    assert client.get('/api/albums/'+album).status_code==404
    assert client.get(f"/api/images/{im['id']}/original").status_code==404
    assert not within(settings.data,detail['folder']).exists()
    assert client.post('/api/trash/purge',json={'plan_id':plan['plan_id'],'confirmation':'DELETE'}).status_code==400


def test_purge_promotes_shared_images_and_updates_other_manifest(client,settings,image_bytes):
    first=import_image(client,image_bytes,'Owner');second=import_image(client,image_bytes,'Other edition')
    before=client.get('/api/albums/'+first).json();image=before['images'][0]
    client.post('/api/albums/trash',json={'ids':[first]}).raise_for_status()
    plan=client.post('/api/trash/preview',json={'ids':[first]}).json()
    assert plan['shared_originals_retained']==1 and plan['originals_removed']==0
    response=client.post('/api/trash/purge',json={'plan_id':plan['plan_id'],'confirmation':'DELETE'})
    assert response.status_code==200,response.text
    after=client.get('/api/albums/'+second).json()
    assert after['images'][0]['original'].startswith('.shared/')
    assert not within(settings.data,before['folder']).exists()
    assert client.get(f"/api/images/{image['id']}/original").content==image_bytes
    manifest=json.loads(within(settings.data,after['folder']+'/album.json').read_text(encoding='utf-8'))
    assert manifest['images'][0]['original'].startswith('.shared/')


def test_purge_rolls_back_shared_moves_on_database_or_manifest_error(client,settings,image_bytes,monkeypatch):
    first=import_image(client,image_bytes,'Owner');second=import_image(client,image_bytes,'Other')
    original=client.get('/api/albums/'+first).json();image=original['images'][0]
    client.post('/api/albums/trash',json={'ids':[first]}).raise_for_status()
    plan=client.post('/api/trash/preview',json={}).json()
    def fail(*args):raise ValueError('Controlled manifest failure')
    monkeypatch.setattr('coverly.purge.write_manifest',fail)
    response=client.post('/api/trash/purge',json={'plan_id':plan['plan_id'],'confirmation':'DELETE'})
    assert response.status_code==400
    assert client.get('/api/albums/'+first).status_code==200
    assert within(settings.data,image['original']).read_bytes()==image_bytes
    assert client.get('/api/albums/'+second).json()['images'][0]['original']==image['original']


def test_purge_refuses_active_gallery_and_stale_preview(client,settings,image_bytes):
    album=import_image(client,image_bytes)
    client.post('/api/albums/trash',json={'ids':[album]})
    plan=client.post('/api/trash/preview',json={}).json()
    client.post('/api/albums/restore',json={'ids':[album]})
    assert client.post('/api/trash/purge',json={'plan_id':plan['plan_id'],'confirmation':'DELETE'}).status_code==400
    assert client.get('/api/albums/'+album).status_code==200
