import json
import pytest
from sqlalchemy import select
from coverly.db import Album,AlbumImage,Page,Job,JobItem,ImageAsset
from coverly.jobs import Worker,enqueue
from coverly.metadata import source_metadata
from coverly.storage import within
from conftest import jpeg

@pytest.mark.parametrize('header,artist,title',[
 ('Rajathi Rojakili Tamil FIlm LP Vinyl Record by Chandrabose','Chandrabose','Rajathi Rojakili'),
 ('Malargale Malarungal Tamil Film EP Vinyl Record by Gangai Amaren','Gangai Amaren','Malargale Malarungal'),
 ('Test Tamil Vinyl LP by A. R. Rahman | Buy Online','A. R. Rahman','Test'),
 ('Pudhiya Theerppu Tamil Super 7 Vinyl-Record by Ilayaraja www.mossymart.com','Ilayaraja','Pudhiya Theerppu'),
])
def test_composer_header(header,artist,title):
    result=source_metadata({'title':header})
    assert result['artist']==artist and result['title']==title
    assert header in result['artist_evidence']


def test_discovery_lane_runs_while_download_lane_is_busy(client,settings,website):
    factory,_,_=website
    with client.app.state.sessions() as s:
        download=enqueue(s,'download',{},[('unfinished',{})]);download.status='running';download.worker_id='another-process';s.commit()
    job=client.post('/api/discover',json={'url':'https://records.example/tamil/','auto_download':False}).json()
    assert Worker(settings,client.app.state.sessions,factory,allowed_kinds=['discover'],quantum=1).tick()
    assert client.get('/api/discovery-status',params={'job_id':job['id']}).json()['job']['status']=='completed'


def test_download_quantum_yields_and_second_catalogue_starts(client,settings,website):
    factory,_,_=website
    discovery=client.post('/api/discover',json={'url':'https://records.example/tamil/','auto_download':False}).json()
    Worker(settings,client.app.state.sessions,factory).tick()
    pages=client.get('/api/discoveries').json()['items']
    first=client.post('/api/download',json={'ids':[p['id'] for p in pages]}).json()
    with client.app.state.sessions() as s:
        second=enqueue(s,'classify',{},[('missing-but-skipped',{})]);second_id=second.id;s.commit()
    worker=Worker(settings,client.app.state.sessions,factory,allowed_kinds=['download','classify'],quantum=1)
    worker.tick()
    state=next(j for j in client.get('/api/jobs').json() if j['id']==first['id'])
    assert state['status']=='queued' and state['progress']==1
    worker.tick()
    assert next(j for j in client.get('/api/jobs').json() if j['id']==second_id)['status']=='completed'
    worker.tick()
    assert next(j for j in client.get('/api/jobs').json() if j['id']==first['id'])['status']=='completed'


def test_automatic_download_is_default_and_header_repairs_shared_paths(client,settings,website,image_bytes):
    factory,_,_=website
    job=client.post('/api/discover',json={'url':'https://records.example/tamil/','max_pages':1}).json()
    assert job['payload']['auto_download'] is True
    worker=Worker(settings,client.app.state.sessions,factory);worker.tick()
    assert client.get('/api/discovery-status',params={'job_id':job['id']}).json()['job']['download_job']
    worker.tick()
    first=client.post('/api/import',data={'title':'Legacy Unknown','artist':'Unknown Artist','source_page':'https://records.example/legacy/'},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    second=client.post('/api/import',data={'title':'Shared edition','artist':'Test Artist'},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    old=client.get('/api/albums/'+first).json();image=old['images'][0]
    with client.app.state.sessions() as s:
        page=s.scalar(select(Page).where(Page.album_id==first));page.metadata_json={**page.metadata_json,'metadata':{'title':'Rajathi Rojakili Tamil Film LP Vinyl Record by Chandrabose'}};s.commit()
    repair=client.post('/api/metadata/repair-unknown').json();worker.tick()
    updated=client.get('/api/albums/'+first).json()
    assert updated['artist']=='Chandrabose' and updated['folder'].startswith('Chandrabose/')
    assert not within(settings.data,old['folder']).exists()
    assert client.get(f"/api/images/{image['id']}/original").content==image_bytes
    shared=client.get('/api/albums/'+second).json()
    manifest=json.loads(within(settings.data,shared['folder']+'/album.json').read_text(encoding='utf-8'))
    assert manifest['images'][0]['original'].startswith('Chandrabose/')


def test_manual_save_can_move_unknown_artist_folder(client,settings,image_bytes):
    album=client.post('/api/import',data={'title':'Unknown source'},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    old=client.get('/api/albums/'+album).json()
    r=client.patch('/api/albums/'+album,json={'title':'Reviewed','artist':'Gangai Amaren','move_folder':True})
    assert r.status_code==200,r.text
    value=client.get('/api/albums/'+album).json()
    assert value['folder'].startswith('Gangai Amaren/') and not value['review']
    assert not within(settings.data,old['folder']).exists()


def test_full_resolution_avoids_thumbnail_requests(client,settings,website):
    factory,visited,_=website;worker=Worker(settings,client.app.state.sessions,factory)
    job=client.post('/api/discover',json={'url':'https://records.example/product/nayakan/'}).json();worker.tick();worker.tick()
    assert '/front.jpg' in visited and '/front-150x150.jpg' not in visited


def test_folder_move_rollback_preserves_shared_original(settings,db,image_bytes,monkeypatch):
    from coverly.intelligence import create_album
    from coverly.storage import add_image,folder_for
    from coverly.archive_ops import relocate_album
    with db() as s:
        album=create_album(s,settings,{'title':'Rollback','artist':'Unknown Artist','manual':True})
        image,_=add_image(s,settings,album,image_bytes);s.commit()
        original=image.original;old_folder=album.folder
        album.artist='Chandrabose';new_folder=folder_for(settings,album)
        def fail():raise ValueError('Controlled commit failure')
        monkeypatch.setattr(s,'commit',fail)
        with pytest.raises(ValueError,match='Controlled commit'):
            with relocate_album(s,settings,album):s.commit()
        assert within(settings.data,original).read_bytes()==image_bytes
        assert within(settings.data,old_folder).exists() and not within(settings.data,new_folder).exists()


def test_similarity_does_not_form_transitive_hash_chains(client):
    from PIL import Image,ImageDraw
    import io
    ids=[]
    for index,tint in enumerate([0,20,40]):
        im=Image.new('RGB',(128,128),(80+tint,90+tint,100+tint));draw=ImageDraw.Draw(im)
        for x in range(0,128,16):draw.line((x,0,127-x,127),fill=(150+tint,160+tint,170+tint),width=3)
        buf=io.BytesIO();im.save(buf,'PNG')
        identifier=client.post('/api/import',data={'title':f'Variant {index}'},files={'files':('art.png',buf.getvalue(),'image/png')}).json()['album_id'];ids.append(identifier)
    groups=client.get('/api/duplicates').json()['items']
    assert all(len({m['album_id'] for m in group['members']})<3 for group in groups)


def test_scheduler_order_is_explicit_and_repeated_download_reuses_job(client,settings,website):
    factory,_,_=website
    job=client.post('/api/discover',json={'url':'https://records.example/tamil/','auto_download':False}).json()
    Worker(settings,client.app.state.sessions,factory).tick()
    first=client.post('/api/download',json={'discovery_job_id':job['id']}).json()
    second=client.post('/api/download',json={'discovery_job_id':job['id']}).json()
    assert first['id']==second['id'] and second['reused']
    items=client.get('/api/jobs/'+first['id']+'/items').json()
    assert sorted(i['position'] for i in items)==[0,1]


def test_delimited_artist_uses_known_archive_identity_without_guessing():
    header='Ladies Tailor – Ilaiyaraaja – Original Vintage Tamil EP Vinyl Record'
    result=source_metadata({'title':header},['Unknown Artist','Ilaiyaraaja'])
    assert result['artist']=='Ilaiyaraaja'
    assert not source_metadata({'title':'A mystery title – Unrecognised Person – LP'},['Ilaiyaraaja']).get('artist')


def test_stale_metadata_form_cannot_overwrite_background_artist_repair(client,image_bytes):
    album=client.post('/api/import',data={'title':'Example'},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    stale=client.get('/api/albums/'+album).json()
    with client.app.state.sessions() as s:s.get(Album,album).artist='Chandrabose';s.commit()
    response=client.patch('/api/albums/'+album,json={'title':stale['title'],'artist':'Unknown Artist','metadata_version':stale['metadata_version']})
    assert response.status_code==409
    assert client.get('/api/albums/'+album).json()['artist']=='Chandrabose'


def test_source_artist_field_excludes_retail_website_suffix():
    assert source_metadata({'title':'Source LP','artist':'Ilayaraja www.mossymart.com'})['artist']=='Ilayaraja'
