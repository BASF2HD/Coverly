from sqlalchemy import select
import httpx
from coverly.db import Job, JobItem
from coverly.jobs import Worker
from coverly.network import SafeHTTP


FILTERED_URL = 'https://records.example/product-category/vinyl-records/?filter_language=tamil'


def test_blocked_discovery_has_visible_reason(client, settings, website):
    factory, visited, pages = website
    def handler(request):
        visited.append(request.url.path)
        return httpx.Response(403, text='Forbidden',headers={'content-type':'text/html'})
    def blocked_fetcher(cfg,domains):
        return SafeHTTP(cfg,domains,httpx.Client(transport=httpx.MockTransport(handler)))
    response = client.post('/api/discover', json={'url': FILTERED_URL,'auto_download':False})
    identifier = response.json()['id']
    Worker(settings, client.app.state.sessions, blocked_fetcher).tick()
    result = client.get('/api/discovery-status', params={'job_id':identifier}).json()['job']
    assert result['status'] == 'failed'
    assert result['access_blocked']
    assert result['source_url'] == FILTERED_URL
    assert result['successful_items'] == result['albums_found'] == 0
    assert result['failed_items'] == 1
    assert '403' in result['message']
    assert 'robots.txt' in result['first_error']
    assert client.get('/api/discoveries').json()['total'] == 0
    assert visited == ['/robots.txt']


def test_historical_job_error_is_shown_without_rewriting(client):
    with client.app.state.sessions() as s:
        job = Job(kind='discover', status='completed_with_errors', message='Finished with 1 failed item(s)', payload={'url':FILTERED_URL,'found':0})
        s.add(job);s.flush()
        s.add(JobItem(job_id=job.id,key=FILTERED_URL,status='failed',error='robots.txt unavailable (403); crawl stopped'))
        identifier=job.id;s.commit()
    result=client.get('/api/discovery-status').json()['job']
    assert result['id']==identifier and result['access_blocked']
    assert '403' in result['message']
    assert '403' in client.get('/api/jobs').json()[0]['first_error']
    with client.app.state.sessions() as s:
        assert s.get(Job,identifier).message=='Finished with 1 failed item(s)'


def test_discovery_status_empty_and_specific_jobs(client):
    assert client.get('/api/discovery-status').json()=={'job':None}
    first=client.post('/api/discover',json={'url':FILTERED_URL,'auto_download':False}).json()
    second=client.post('/api/discover',json={'url':'https://another.example/tamil/','auto_download':False}).json()
    assert client.get('/api/discovery-status').json()['job']['id']==second['id']
    assert client.get('/api/discovery-status',params={'job_id':first['id']}).json()['job']['id']==first['id']
    assert client.get('/api/discovery-status?job_id=missing').status_code==404


def test_successful_discovery_reports_found_albums(client,settings,website):
    factory,_,_=website
    job=client.post('/api/discover',json={'url':'https://records.example/tamil/','max_pages':10,'auto_download':False}).json()
    Worker(settings,client.app.state.sessions,factory).tick()
    result=client.get('/api/discovery-status',params={'job_id':job['id']}).json()['job']
    assert result['status']=='completed'
    assert result['albums_found']==2
    assert result['successful_items']==2
    assert not result['access_blocked']
    assert result['first_error']==''
