from sqlalchemy import select
from coverly.db import Job,JobItem,ImageAsset,Page
from coverly.jobs import Worker


def test_gallery_retry_keeps_saved_images_and_reports_errors(client,settings,website):
    factory,visited,pages=website
    source=client.post('/api/discover',json={'url':'https://records.example/product/nayakan/','auto_download':False}).json()
    worker=Worker(settings,client.app.state.sessions,factory);worker.tick()
    page=client.get('/api/discoveries',params={'job_id':source['id']}).json()['items'][0]
    back=pages.pop('/back.jpg')
    download=client.post('/api/download',json={'ids':[page['id']]}).json();worker.tick()
    state=next(j for j in client.get('/api/jobs').json() if j['id']==download['id'])
    assert state['status']=='completed_with_errors'
    assert state['images_done']==2 and state['images_failed']==1 and state['images_total']==3
    first_requests=visited.count('/front.jpg')
    pages['/back.jpg']=back
    assert client.post('/api/jobs/'+download['id']+'/retry').status_code==200
    worker.tick()
    state=next(j for j in client.get('/api/jobs').json() if j['id']==download['id'])
    assert state['status']=='completed'
    assert state['images_done']==3 and state['images_failed']==0
    assert visited.count('/front.jpg')==first_requests
    with client.app.state.sessions() as session:
        metadata=session.get(Page,page['id']).metadata_json
        assert all(im.get('sha256') for im in metadata['images'])


def test_pause_mid_gallery_resumes_without_refetching_completed_images(client,settings,website,monkeypatch):
    factory,visited,_=website;worker=Worker(settings,client.app.state.sessions,factory)
    client.post('/api/discover',json={'url':'https://records.example/product/nayakan/','auto_download':False});worker.tick()
    page=client.get('/api/discoveries').json()['items'][0]
    job=client.post('/api/download',json={'ids':[page['id']]}).json()
    actual=worker.download
    calls=[]
    def pause_after_first(*args,**kwargs):
        result=actual(*args,**kwargs);calls.append(result)
        if len(calls)==1:client.post('/api/jobs/'+job['id']+'/pause')
        return result
    monkeypatch.setattr(worker,'download',pause_after_first);worker.tick()
    state=next(j for j in client.get('/api/jobs').json() if j['id']==job['id'])
    assert state['status']=='paused' and state['images_done']==1
    with client.app.state.sessions() as session:
        assert session.scalar(select(JobItem).where(JobItem.job_id==job['id'])).status=='pending'
    assert client.post('/api/jobs/'+job['id']+'/resume').status_code==200
    worker.tick()
    state=next(j for j in client.get('/api/jobs').json() if j['id']==job['id'])
    assert state['status']=='completed' and state['images_done']==3
    assert len(calls)==3
