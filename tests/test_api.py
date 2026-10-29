import io
import json
import zipfile
from sqlalchemy import select
from coverly.db import Job, JobItem, ImageAsset
from coverly.jobs import Worker


def import_image(client, image_bytes, title='Test Archive'):
    response=client.post('/api/import', data={'title':title,'artist':'Test Artist','year':'1987'}, files=[('files',('front.jpg',image_bytes,'image/jpeg'))])
    assert response.status_code == 200, response.text
    return response.json()['album_id']


def test_local_api_guards(client):
    assert client.get('/api/stats', headers={'Host':'attacker.example'}).status_code == 403
    assert client.post('/api/discover', json={'url':'https://example.com','auto_download':False}, headers={'X-Coverly-Request':'','Origin':'https://evil.example'}).status_code == 403
    assert client.get('/').status_code == 200
    assert 'frame-ancestors' in client.get('/').headers['content-security-policy']


def test_import_edit_classify_rename_undo(client, image_bytes):
    identifier=import_image(client,image_bytes)
    album=client.get('/api/albums/'+identifier).json()
    image=album['images'][0]
    assert client.get(f"/api/images/{image['id']}/original").content == image_bytes
    assert client.patch(f"/api/albums/{identifier}/images/{image['id']}",json={'classification':'back'}).status_code == 200
    assert client.patch(f"/api/albums/{identifier}/images/{image['id']}",json={'classification':'../bad'}).status_code == 400
    update={key:album[key] for key in ['title','tamil_title','artist','year','label','catalogue','notes','compilation','review']}
    update['title']='Verified Album'
    update['review']=False
    assert client.patch('/api/albums/'+identifier,json=update).status_code == 200
    plan=client.post('/api/rename/preview',json={'ids':[identifier]}).json()
    assert len(plan['items']) == 1
    applied=client.post(f"/api/rename/{plan['plan_id']}/apply")
    assert applied.status_code == 200, applied.text
    assert client.get(f"/api/images/{image['id']}/original").content == image_bytes
    assert client.post(f"/api/rename/{applied.json()['operation_id']}/undo").status_code == 200
    assert client.get(f"/api/images/{image['id']}/original").content == image_bytes


def test_restore_approval_export(client, image_bytes, settings):
    identifier=import_image(client,image_bytes)
    image=client.get('/api/albums/'+identifier).json()['images'][0]
    response=client.post('/api/restorations',json={'ids':[image['id']],'preset':'upscale','size':1000})
    assert response.status_code == 200
    Worker(settings,client.app.state.sessions).tick()
    result=client.get('/api/restorations').json()[0]
    assert result['status']=='pending'
    assert client.get('/api/export/approved').status_code == 409
    assert client.post(f"/api/restorations/{result['id']}/approve").status_code == 200
    exported=client.get('/api/export/approved')
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as z:
        assert any(n.endswith('.jpg') for n in z.namelist())


def test_pause_waits_for_inflight_item(client, image_bytes):
    job=client.post('/api/discover',json={'url':'https://example.com','auto_download':False}).json()
    with client.app.state.sessions() as s:
        s.get(Job,job['id']).status='running'
        s.add(JobItem(job_id=job['id'],key='in-flight',status='running'))
        s.commit()
    assert client.post(f"/api/jobs/{job['id']}/pause").status_code==200
    assert client.get('/api/backup').status_code==409
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code==409


def test_external_restoration(client, image_bytes):
    identifier=import_image(client,image_bytes)
    image=client.get('/api/albums/'+identifier).json()['images'][0]
    exported=client.post('/api/codex/export',json={'ids':[image['id']]})
    assert exported.status_code==200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as z:
        manifest=json.loads(z.read('restoration-job.json'))
        assert manifest['requires_approval']
        assert z.read(manifest['images'][0]['original'])==image_bytes
    result=client.post('/api/codex/import/'+image['id'],files={'file':('restored.jpg',image_bytes,'image/jpeg')}).json()
    assert result['generated'] and result['status']=='pending'
    assert client.get(f"/api/images/{image['id']}/original").content==image_bytes


def test_similar_artwork_review_filter(client,image_bytes):
    album_id=import_image(client,image_bytes)
    image=client.get('/api/albums/'+album_id).json()['images'][0]
    assert client.get('/api/albums?duplicate=true').json()['total']==0
    with client.app.state.sessions() as s:
        s.get(ImageAsset,image['id']).near_duplicate=True
        s.commit()
    results=client.get('/api/albums?duplicate=true').json()
    assert results['total']==0  # An old hint alone is not a matching pair.
    second=import_image(client,image_bytes,title='Second edition')
    results=client.get('/api/albums?duplicate=true').json()
    assert results['total']==2
    groups=client.get('/api/duplicates').json()['items']
    assert any({album_id,second} <= {m['album_id'] for m in group['members']} for group in groups)


def test_browser_saved_import_retains_source_metadata(client,image_bytes):
    source='https://records.example/product/ethir-kaatru/'
    original='https://records.example/images/original-front.jpg'
    data={'title':'Ethir Kaatru','artist':'Ilaiyaraaja','year':'1990','label':'Echo','catalogue':'8000 829','source_page':source,'source_images':json.dumps({'front.jpg':original})}
    response=client.post('/api/import',data=data,files=[('files',('front.jpg',image_bytes,'image/jpeg'))])
    assert response.status_code==200,response.text
    album=client.get('/api/albums/'+response.json()['album_id']).json()
    assert album['source']=='records.example'
    assert album['year']==1990 and album['label']=='Echo' and album['catalogue']=='8000 829'
    assert album['images'][0]['source_url']==original
    assert album['images'][0]['page_url']==source
    assert album['images'][0]['classification']=='front'
    repeated=client.post('/api/import',data=data,files=[('files',('front.jpg',image_bytes,'image/jpeg'))])
    assert repeated.json()['album_id']==album['id']
    assert repeated.json()['duplicates']==1
    assert client.get('/api/albums').json()['total']==1
    assert client.get('/api/discoveries').json()['items'][0]['album_id']==album['id']


def test_browser_import_rejects_invalid_provenance(client,image_bytes):
    response=client.post('/api/import',data={'source_page':'javascript:alert(1)'},files=[('files',('front.jpg',image_bytes,'image/jpeg'))])
    assert response.status_code==400
    response=client.post('/api/import',data={'source_images':'["not a source map"]'},files=[('files',('front.jpg',image_bytes,'image/jpeg'))])
    assert response.status_code==400
    assert client.get('/api/albums').json()['total']==0


def test_versioned_interface_revalidates_assets(client):
    import re
    response=client.get('/')
    assert 'no-cache' in response.headers['cache-control']
    versions=client.get('/api/interface-version')
    assert versions.status_code==200 and versions.headers['cache-control']=='no-store'
    assert set(versions.json())=={'app.js','style.css','theme.js'}
    for filename in ['app.js','theme.js','style.css']:
        assert re.search(r'/static/'+re.escape(filename)+r'\?v=[a-f0-9]{12}',response.text)
        assert 'no-cache' in client.get('/static/'+filename).headers['cache-control']
