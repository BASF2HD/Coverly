"""Synthetic catalogue structures, with controlled network/gallery responses."""
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import select

from coverly.browser import BrowserHTTP
from coverly.db import Page
from coverly.extraction import adapter_for
from coverly.jobs import Worker

BASE = 'https://newgramophonehouse.com/product-category/vinyl-records/'
URLS = [BASE + (f'page/{n}/' if n > 1 else '') + '?filter_language=tamil' for n in (1, 2, 3)]
FIXTURES = Path(__file__).parent / 'fixtures' / 'ngh'


def test_synthetic_three_page_catalogue_extracts_53_distinct_records():
    records = []
    for n, url in enumerate(URLS, 1):
        result = adapter_for((FIXTURES / f'page-{n}.html').read_text(encoding='utf-8')).extract((FIXTURES / f'page-{n}.html').read_text(encoding='utf-8'), url)
        assert not result['is_product']
        assert len(result['products']) == (24 if n < 3 else 5)
        assert all(p['pending_gallery'] and not p['images'] for p in result['products'])
        assert all('filter_language=tamil' in link['url'] for link in result['links'] if link['pagination'])
        records.extend(result['products'])
    assert len({p['url'] for p in records}) == 53
    assert any('Ethir Kaatru' in p['metadata']['title'] for p in records)


def test_three_category_pages_and_selected_full_gallery(client, settings, website, monkeypatch):
    factory, visited, pages = website
    for n in (1, 2, 3):
        path = '/product-category/vinyl-records/' + (f'page/{n}/' if n > 1 else '')
        pages[path] = ((FIXTURES / f'page-{n}.html').read_bytes(), 'text/html')
    monkeypatch.setattr('coverly.browser.BrowserHTTP', lambda cfg, domains, **kw: factory(cfg, domains))
    worker = Worker(settings, client.app.state.sessions, factory)
    job = client.post('/api/discover', json={'url': URLS[0], 'max_pages': 3, 'browser_assisted': True,'auto_download':False}).json()
    worker.tick()
    status = client.get('/api/discovery-status', params={'job_id': job['id']}).json()['job']
    assert status['status'] == 'completed'
    assert status['successful_items'] == 3 and status['albums_found'] == 53
    assert not status['payload']['limit_reached']
    records = client.get('/api/discoveries').json()['items']
    assert len(records) == 53
    assert not any('/product/' in path for path in visited)
    selected = next(p for p in records if p['url'] == 'https://newgramophonehouse.com/product/ethir-kaatru-ilaiyaraaja-8000-829-lp-record/')
    pages['/product/ethir-kaatru-ilaiyaraaja-8000-829-lp-record/'] = ((Path(__file__).parent / 'fixtures' / 'product.html').read_bytes(), 'text/html')
    response = client.post('/api/download', json={'ids': [selected['id']]})
    assert response.status_code == 200 and response.json()['total'] == 1
    worker.tick()
    result = next(j for j in client.get('/api/jobs').json() if j['id'] == response.json()['id'])
    assert result['status'] == 'completed', result['first_error']
    albums = client.get('/api/albums').json()['items']
    assert len(albums) == 1 and albums[0]['image_count'] == 3
    with client.app.state.sessions() as session:
        page = session.get(Page, selected['id'])
        assert page.album_id and len(page.metadata_json['images']) == 3
        assert not page.metadata_json.get('pending_gallery')


def test_verification_service_is_subresource_exception_only(settings):
    transport = BrowserHTTP(settings, {'newgramophonehouse.com'})
    calls = []
    def route(url, method='GET'):
        request = SimpleNamespace(url=url, method=method, is_navigation_request=lambda: False)
        transport._route(SimpleNamespace(request=request, continue_=lambda: calls.append('allowed'), abort=lambda: calls.append('blocked')))
    route('https://challenges.cloudflare.com/turnstile/v0/api.js')
    route('https://challenges.cloudflare.com/cdn-cgi/challenge-platform/', 'POST')
    route('https://unlisted.example/script.js')
    route('http://localhost:8000/api/albums')
    route('https://newgramophonehouse.com/cart/', 'POST')
    assert calls == ['allowed', 'allowed', 'blocked', 'blocked', 'blocked']
    import pytest
    with pytest.raises(ValueError, match='allowlist'):
        transport.get('https://challenges.cloudflare.com/')
    transport.close()


def test_in_app_verification_relays_only_explicit_fresh_input(client, settings):
    import json
    import time
    from coverly.db import Job
    from coverly.verification import directory, publish
    with client.app.state.sessions() as session:
        job = Job(kind='discover', status='running', payload={'url': URLS[0], 'browser_assisted': True})
        session.add(job); session.flush(); identifier = job.id; session.commit()
    publish(settings, identifier, {'waiting': True, 'width': 1280, 'height': 850})
    calls = []
    page = SimpleNamespace(evaluate=lambda *a: 'performing security verification', mouse=SimpleNamespace(click=lambda x,y: calls.append(('click',x,y))),
                           keyboard=SimpleNamespace(press=lambda key: calls.append(('key',key))),
                           bring_to_front=lambda: calls.append(('focus',)), screenshot=lambda **kw: b'controlled-screen')
    browser = BrowserHTTP(settings, {'newgramophonehouse.com'}, job_id=identifier)
    browser.page = page
    browser._verification_handoff()
    assert calls == []  # Publishing the view never clicks or solves verification.
    assert client.get(f'/api/jobs/{identifier}/browser-screen').content == b'controlled-screen'
    assert client.post(f'/api/jobs/{identifier}/browser-input', json={'action':'click','x':320,'y':400}).status_code == 200
    assert client.post(f'/api/jobs/{identifier}/browser-input', json={'action':'click','x':1280,'y':400}).status_code == 422
    assert client.post(f'/api/jobs/{identifier}/browser-input', json={'action':'key','key':'F12'}).status_code == 422
    assert client.post(f'/api/jobs/{identifier}/browser-input', json={'action':'click'}).status_code == 400
    browser._verification_handoff()
    assert calls == [('click',320,400)]
    commands = directory(settings, identifier) / 'commands'
    (commands / 'stale.json').write_text(json.dumps({'action':'click','x':1,'y':1,'created':time.time()-20}))
    browser._verification_handoff()
    assert len(calls) == 1
    assert not list(commands.glob('*.json'))
    publish(settings, identifier, {'waiting': False})
    assert client.post(f'/api/jobs/{identifier}/browser-input', json={'action':'click','x':1,'y':1}).status_code == 409
    browser.close()


def test_repeated_discovery_reuses_active_browser_job(client):
    first = client.post('/api/discover', json={'url':URLS[0], 'browser_assisted':True, 'max_pages':3,'auto_download':False}).json()
    second = client.post('/api/discover', json={'url':URLS[0], 'browser_assisted':True, 'max_pages':30,'auto_download':False}).json()
    assert second['id'] == first['id'] and second['reused']
    assert len(client.get('/api/jobs').json()) == 1


def test_human_approval_navigation_is_polled_instead_of_failed():
    def navigating(*args):
        raise RuntimeError('Page.evaluate: Execution context was destroyed, most likely because of a navigation')
    assert BrowserHTTP._challenge(SimpleNamespace(evaluate=navigating))
    assert not BrowserHTTP._challenge(SimpleNamespace(evaluate=lambda *a: {'title':'Vinyl Records', 'text':'Records catalogue'}))


def test_repeated_verification_stops_instead_of_looping(settings):
    import pytest
    browser = BrowserHTTP(settings, {'records.example'}, job_id='a'*32)
    browser._human_clicked = browser._saw_verifying = True
    browser.page = SimpleNamespace(evaluate=lambda *a:'Performing security verification'.lower())
    with pytest.raises(ValueError, match='checkbox returned'):
        browser._verification_handoff()
    browser.close()


def test_closed_page_reopens_once_but_verification_closure_pauses(settings, monkeypatch):
    import pytest
    from coverly.browser import BrowserSessionClosed
    browser = BrowserHTTP(settings, {'records.example'})
    attempts, disposed = [], []
    def navigate(*args, **kwargs):
        attempts.append(args[0])
        if len(attempts) == 1:
            raise RuntimeError('Page.goto: Target page, context or browser has been closed')
        return b'catalogue', args[0]
    monkeypatch.setattr(browser, '_navigate_once', navigate)
    monkeypatch.setattr(browser, '_dispose_browser', lambda: disposed.append(True))
    assert browser._navigate('https://records.example/')[0] == b'catalogue'
    assert len(attempts) == 2 and len(disposed) == 1
    def verification(*args, **kwargs):
        browser._waiting = True
        raise RuntimeError('Page.wait_for_timeout: Target page, context or browser has been closed')
    monkeypatch.setattr(browser, '_navigate_once', verification)
    with pytest.raises(BrowserSessionClosed, match='paused at its saved page'):
        browser._navigate('https://records.example/')
    browser.close()


def test_closed_browser_discovery_keeps_pending_page_for_resume(client, settings, website, monkeypatch):
    from coverly.browser import BrowserSessionClosed
    from coverly.db import Job, JobItem
    factory, visited, pages = website
    class Closed:
        def get(self, url):
            raise BrowserSessionClosed('The website browser closed. Resume to continue.')
        def close(self):
            pass
    worker = Worker(settings, client.app.state.sessions, lambda *args: Closed())
    job = client.post('/api/discover', json={'url':'https://records.example/tamil/', 'auto_download':False}).json()
    worker.tick()
    with client.app.state.sessions() as s:
        saved = s.get(Job, job['id'])
        item = s.scalar(select(JobItem).where(JobItem.job_id == saved.id))
        assert saved.status == 'paused' and item.status == 'pending'
        assert not saved.payload['seen'] and saved.payload['queue'] == [['https://records.example/tamil/', 0]]
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 200
    Worker(settings, client.app.state.sessions, factory).tick()
    saved = next(j for j in client.get('/api/jobs').json() if j['id'] == job['id'])
    assert saved['status'] == 'completed' and saved['albums_found'] > 0


def test_captured_metadata_remains_unconfirmed(client, image_bytes):
    response = client.post('/api/import', data={'title':'Source title', 'source_page':URLS[0], 'metadata_confirmed':'false'}, files={'files':('artwork.jpg',image_bytes,'image/jpeg')})
    assert response.status_code == 200
    album = client.get('/api/albums/'+response.json()['album_id']).json()
    assert album['review'] and album['confidence'] == .9


def test_shared_binary_keeps_each_edition_source(client, settings, image_bytes):
    import json
    from coverly.storage import within
    for n in (1,2):
        page=f'https://records.example/product/edition-{n}/'
        original=f'https://records.example/edition-{n}.jpg'
        result=client.post('/api/import',data={'title':f'Edition {n}','source_page':page,'source_images':json.dumps({'art.jpg':original}),'metadata_confirmed':'false'},files={'files':('art.jpg',image_bytes,'image/jpeg')}).json()
        album=client.get('/api/albums/'+result['album_id']).json()
        assert album['pages'][0]['metadata_json']['images'][0]['url']==original
        manifest=json.loads(within(settings.data,album['folder']+'/album.json').read_text(encoding='utf-8'))
        assert manifest['sources'][0]['page_url']==page
        assert manifest['sources'][0]['images'][0]['url']==original
    assert client.get('/api/albums').json()['total']==2


def test_shared_older_disc_does_not_replace_source_primary_cover(client, image_bytes):
    import json
    from conftest import jpeg
    first = client.post('/api/import', data={'title':'Earlier edition','source_page':'https://records.example/product/earlier/'}, files={'files':('shared.jpg',image_bytes,'image/jpeg')})
    assert first.status_code == 200
    primary = jpeg(700,700,label='CONTROLLED PRIMARY SLEEVE')
    source_map={'sleeve.jpg':'https://records.example/sleeve.jpg','shared.jpg':'https://records.example/shared.jpg'}
    response=client.post('/api/import',data={'title':'New edition','source_page':'https://records.example/product/new/','source_images':json.dumps(source_map)},files=[('files',('sleeve.jpg',primary,'image/jpeg')),('files',('shared.jpg',image_bytes,'image/jpeg'))])
    album=client.get('/api/albums/'+response.json()['album_id']).json()
    expected=next(i for i in album['images'] if i['source_url']==source_map['sleeve.jpg'])
    assert album['cover_id']==expected['id']


def test_real_browser_closure_during_verification_has_recoverable_error(settings, monkeypatch):
    import pytest
    api = pytest.importorskip('playwright.sync_api')
    from coverly.browser import BrowserSessionClosed
    with api.sync_playwright() as p:
        engine = p.chromium.launch(headless=True)
        context = engine.new_context()
        page = context.new_page()
        page.route('https://records.example/**', lambda route:route.fulfill(content_type='text/html',body='''
            <title>Just a moment</title><p>Performing security verification</p>
            <script>setTimeout(()=>console.log('controlled-close'),150)</script>'''))
        page.on('console', lambda message:page.close() if message.text == 'controlled-close' else None)
        transport = BrowserHTTP(settings, {'records.example'})
        transport.page, transport.context = page, context
        monkeypatch.setattr(transport, '_start', lambda:None)
        monkeypatch.setattr(transport.limiter, 'throttle', lambda *_:None)
        with pytest.raises(BrowserSessionClosed, match='paused at its saved page'):
            transport._navigate('https://records.example/robots.txt', robots=True)
        assert transport.page is None and transport.context is None
        transport.close()
        engine.close()


def test_javascript_renderer_retries_closed_browser_then_preserves_checkpoint(monkeypatch):
    import pytest
    from coverly import extraction
    from coverly.browser import BrowserSessionClosed
    attempts=[]
    def render(*args):
        attempts.append(True)
        raise RuntimeError('Page.wait_for_timeout: Target page, context or browser has been closed')
    monkeypatch.setattr(extraction,'_render_page_once',render)
    with pytest.raises(BrowserSessionClosed,match='paused at its saved page'):
        extraction.render_page('https://records.example/',None)
    assert len(attempts)==2
