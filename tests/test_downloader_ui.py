"""Controlled UI regression: no network requests leave the ASGI test archive."""
from urllib.parse import urlsplit
import pytest
from coverly.jobs import enqueue


def test_completed_cards_can_be_closed_without_hiding_active_downloads(client):
    api=pytest.importorskip('playwright.sync_api')
    with client.app.state.sessions() as s:
        discovery=enqueue(s,'discover',{'url':'https://records.example/tamil/','found':0},[])
        discovery.status='completed';discovery.message='Catalogue discovery complete'
        download=enqueue(s,'download',{'parent_job_id':discovery.id},[])
        download.status='completed';download.message='Completed successfully'
        discovery.payload={**discovery.payload,'download_job_id':download.id}
        active=enqueue(s,'download',{'source_url':'https://other.example/tamil/'},[('next',{})])
        active.status='running';active.message='Receiving artwork'
        restoration=enqueue(s,'restore',{},[]);restoration.status='completed'
        paused=enqueue(s,'ocr',{},[]);paused.status='paused'
        failed=enqueue(s,'discover',{},[]);failed.status='failed';failed.message='Page.wait_for_timeout: Target page, context or browser has been closed';failed.created='2000-01-01T00:00:00+00:00'
        s.commit();active_id=active.id;restoration_id=restoration.id;paused_id=paused.id;failed_id=failed.id;download_id=download.id
    with api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        page.add_init_script('window.EventSource=class{constructor(){}};')
        def route(request_route):
            request=request_route.request;parts=urlsplit(request.url)
            response=client.request(request.method,parts.path+('?' +parts.query if parts.query else ''),content=request.post_data_buffer,headers={'Content-Type':request.headers.get('content-type','application/json')})
            request_route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','text/plain'))
        page.route('http://localhost:8000/**',route)
        page.goto('http://localhost:8000/#downloader')
        page.get_by_role('button',name='Close completed catalogue status').click()
        page.get_by_role('button',name='Close completed download status').click()
        page.reload()
        page.get_by_role('button',name='Show status').wait_for()
        assert page.get_by_role('button',name='Close completed download status').count()==0
        assert page.locator(f'#active-downloads [data-job="{active_id}"]').count()==1
        field=page.get_by_label('Website URL',exact=True);field.focus()
        style=field.evaluate('(el)=>({outline:getComputedStyle(el).outlineStyle,shadow:getComputedStyle(el).boxShadow})')
        assert style['outline']=='none' and style['shadow']=='none'
        page.get_by_role('button',name='Show status').click()
        page.get_by_role('button',name='Close completed catalogue status').wait_for()
        page.locator('nav a[href="#jobs"]').click()
        page.get_by_role('button',name='Show closed jobs',exact=True).click()
        page.locator('#job-list .job-message').filter(has_text='Completed successfully').wait_for()
        page.get_by_role('button',name='Close all completed sections',exact=True).click()
        page.locator(f'#job-list [data-job="{restoration_id}"]').wait_for(state='detached')
        assert page.locator(f'#job-list [data-job="{download_id}"]').count()==0
        assert page.locator(f'#job-list [data-job="{failed_id}"]').count()==0
        for retained in (active_id,paused_id):
            assert page.locator(f'#job-list [data-job="{retained}"]').count()==1
        page.reload()
        page.get_by_role('button',name='Close all completed sections',exact=True).wait_for()
        assert page.locator(f'#job-list [data-job="{restoration_id}"]').count()==0
        statuses={j['id']:j['status'] for j in client.get('/api/jobs').json()}
        assert statuses[restoration_id]=='completed' and statuses[active_id]=='running'
        page.get_by_role('button',name='Show closed jobs',exact=True).click()
        page.locator(f'#job-list [data-job="{restoration_id}"]').wait_for(state='visible')

        page.get_by_role('button',name='Switch to light theme',exact=True).click()
        assert page.locator('html').get_attribute('data-theme')=='light'
        page.reload()
        assert page.get_by_role('button',name='Switch to dark theme',exact=True).is_visible()
        assert page.locator('html').get_attribute('data-theme')=='light'
        page.get_by_role('button',name='Switch to dark theme',exact=True).click()
        assert page.locator('html').get_attribute('data-theme')=='dark'
        # A long-lived window offers a safe refresh and keeps unsaved input.
        page.goto('http://localhost:8000/#downloader')
        field=page.get_by_label('Website URL',exact=True);field.fill('https://records.example/new-unsaved/')
        page.route('http://localhost:8000/api/interface-version',lambda r:r.fulfill(json={'app.js':'new-build','style.css':'new-build','theme.js':'new-build'}))
        page.evaluate('window.dispatchEvent(new Event("focus"))')
        page.get_by_role('button',name='Refresh Coverly',exact=True).wait_for()
        assert field.input_value()=='https://records.example/new-unsaved/'
        page.goto('http://localhost:8000/#library')
        page.get_by_role('button',name='Close all completed sections',exact=True).wait_for(state='visible')
        page.get_by_role('button',name='Close all completed sections',exact=True).click()
        assert page.locator('#global-job-actions').is_visible()
        browser.close()


def test_master_close_hides_old_failed_catalogue_outside_recent_jobs(client):
    api=pytest.importorskip('playwright.sync_api')
    with client.app.state.sessions() as s:
        failed=enqueue(s,'discover',{'url':'https://records.example/tamil/'},[])
        failed.status='failed';failed.message='Page.wait_for_timeout: Target page, context or browser has been closed'
        failed.created='2000-01-01T00:00:00+00:00';identifier=failed.id
        for _ in range(80):
            other=enqueue(s,'identify',{},[]);other.status='completed'
        active=enqueue(s,'download',{},[]);active.status='running';active_id=active.id
        s.commit()
    assert identifier not in {j['id'] for j in client.get('/api/jobs').json()}
    assert identifier in client.get('/api/jobs/finished-ids').json()
    with api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page();page.set_default_timeout(5000)
        page.add_init_script('window.EventSource=class{constructor(){}};localStorage.setItem("coverly-discovery",'+repr(identifier)+');')
        def route(r):
            request=r.request;parts=urlsplit(request.url)
            response=client.request(request.method,parts.path+('?' +parts.query if parts.query else ''),content=request.post_data_buffer,headers={'Content-Type':request.headers.get('content-type','application/json')})
            r.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','text/plain'))
        page.route('http://localhost:8000/**',route)
        page.goto('http://localhost:8000/#downloader')
        page.locator('#discovery-status').get_by_text('Page.wait_for_timeout:',exact=False).wait_for()
        page.get_by_role('button',name='Close all completed sections',exact=True).click()
        page.locator('#discovery-status').get_by_role('button',name='Show status',exact=True).wait_for()
        assert page.locator('#discovery-status').get_by_text('Page.wait_for_timeout:',exact=False).count()==0
        assert page.locator(f'[data-job="{active_id}"]').is_visible()
        browser.close()
