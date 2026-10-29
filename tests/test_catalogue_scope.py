from pathlib import Path
from urllib.parse import urlsplit
import httpx
import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select
from coverly.db import Job,JobItem,Page
from coverly.extraction import adapter_for,scoped_page_url,page_number
from coverly.jobs import Worker
from coverly.network import SafeHTTP

FIX=Path(__file__).parent/'fixtures'/'catalogue-sites'
SITES=[('vintage','https://www.vintageav.shop/vinyl-records/tamil-vinyl/',16,96),
       ('mossy','https://mossymart.com/product-category/vinyl-lp-records/tamil-records/',24,132)]

@pytest.mark.parametrize('name,url,count,total',SITES)
def test_catalogue_cards_and_pagination(name,url,count,total):
    result=adapter_for((FIX/f'{name}-page1.html').read_text(encoding='utf-8')).extract((FIX/f'{name}-page1.html').read_text(encoding='utf-8'),url)
    assert not result['is_product']
    assert len(result['products'])==count
    assert result['total_pages']==total
    assert result['pagination'][0]['number']==2
    assert all(scoped_page_url(link['url'],url) for link in result['pagination'])
    assert all(p['pending_gallery'] and not p['images'] for p in result['products'])
    assert all('/product/' not in p['url'] for p in result['products'])

@pytest.mark.parametrize('name,url,count,total',SITES)
def test_walks_every_numbered_page_and_never_follows_sidebar(client,settings,monkeypatch,name,url,count,total):
    monkeypatch.setattr('coverly.network.public_addresses',lambda *a:['93.184.216.34'])
    monkeypatch.setattr(SafeHTTP,'throttle',lambda *a:None)
    visited=[]
    original=BeautifulSoup((FIX/f'{name}-page1.html').read_text(encoding='utf-8'),'html.parser')
    products=original.select_one('.products')
    def respond(request):
        path=request.url.path
        if path=='/robots.txt':return httpx.Response(200,text='User-agent: *\nAllow: /')
        original_url='https://'+request.headers['host']+request.url.raw_path.decode()
        n=page_number(original_url);visited.append(original_url)
        assert scoped_page_url(original_url,url), 'Crawler left its exact catalogue'
        soup=BeautifulSoup(str(products),'html.parser')
        for anchor in soup.select('a[href]'):
            if 'add-to-cart' not in anchor['href']:anchor['href']+='?fixture_page='+str(n)
        pagination='<nav class="woocommerce-pagination">'+''.join(f'<a class="page-numbers" href="{url}page/{i}/">{i}</a>' for i in sorted({min(n+1,total),total}) if i!=n)+'</nav>'
        return httpx.Response(200,text='<main>'+str(soup)+pagination+'</main><aside><a href="/cds/">Tamil CD</a><a href="/cassettes/">Tamil cassettes</a></aside>',headers={'Content-Type':'text/html'})
    factory=lambda cfg,domains:SafeHTTP(cfg,domains,httpx.Client(transport=httpx.MockTransport(respond)))
    job=client.post('/api/discover',json={'url':url,'auto_download':False}).json()
    Worker(settings,client.app.state.sessions,factory).tick()
    status=client.get('/api/discovery-status',params={'job_id':job['id']}).json()['job']
    assert status['status']=='completed',status['first_error']
    assert status['progress']==status['successful_items']==status['total']==total
    assert status['albums_found']==count*total
    assert [page_number(u) for u in visited]==list(range(1,total+1))
    assert client.get('/api/discoveries',params={'job_id':job['id'],'offset':100}).json()['total']==count*total
    assert len(client.get('/api/discoveries',params={'job_id':job['id'],'offset':100}).json()['items'])==100


def test_exact_scope_carries_language_and_rejects_filters_and_media():
    root='https://records.example/category/tamil/?filter_language=tamil'
    assert scoped_page_url('https://records.example/category/tamil/page/2/',root).endswith('/page/2/?filter_language=tamil')
    for path in ['/category/cds/','/category/tamil/children/','/category/tamil/?filter_language=hindi','/category/tamil/?filter_format=cd']:
        assert scoped_page_url('https://records.example'+path,root) is None


def test_limit_continuation_and_all_results_download(client,settings,website):
    factory,visited,_=website
    job=client.post('/api/discover',json={'url':'https://records.example/tamil/','max_pages':1,'auto_download':False}).json()
    worker=Worker(settings,client.app.state.sessions,factory);worker.tick()
    state=client.get('/api/discovery-status',params={'job_id':job['id']}).json()['job']
    assert state['payload']['limit_reached'] and state['albums_found']==1
    assert client.post('/api/discover/'+job['id']+'/continue').status_code==200
    worker.tick()
    assert client.get('/api/discovery-status',params={'job_id':job['id']}).json()['job']['albums_found']==2
    assert visited.count('/tamil/')==1
    download=client.post('/api/download',json={'discovery_job_id':job['id']}).json()
    assert download['total']==2
    assert client.post('/api/download',json={'discovery_job_id':job['id']}).json()['id']==download['id']
    with client.app.state.sessions() as session:
        items=list(session.scalars(select(JobItem).where(JobItem.job_id==download['id'])))
        assert all(i.data['inspect_gallery'] for i in items)
        assert {i.data['catalogue_page'] for i in items}=={1,2}


def test_auto_download_child_progress_and_reuse(client,settings,website):
    factory,visited,_=website;worker=Worker(settings,client.app.state.sessions,factory)
    job=client.post('/api/discover',json={'url':'https://records.example/tamil/','max_pages':1,'auto_download':True}).json()
    worker.tick()
    status=client.get('/api/discovery-status',params={'job_id':job['id']}).json()['job']
    child=status['download_job'];assert child['total']==1
    worker.tick()
    child=next(j for j in client.get('/api/jobs').json() if j['id']==child['id'])
    assert child['images_done']==child['images_total']==3
    assert child['images_failed']==0 and child['files_new']==3
    page=client.get('/api/discoveries',params={'job_id':job['id']}).json()['items'][0]
    second=client.post('/api/download',json={'ids':[page['id']]}).json();worker.tick()
    repeat=next(j for j in client.get('/api/jobs').json() if j['id']==second['id'])
    assert repeat['images_done']==repeat['files_reused']==3
    assert repeat['files_new']==0

@pytest.mark.parametrize('name,url,_,__',SITES)
def test_product_gallery_excludes_related_recommendations(name,url,_,__):
    html=(FIX/f'{name}-product.html').read_text(encoding='utf-8')
    result=adapter_for(html).extract(html,url)
    assert result['is_product'] and len(result['images'])==2
    assert not result['pagination']
    assert all(im['alternatives'] for im in result['images'])
