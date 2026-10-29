"""Browser fixtures for bulk review, folder imports and accepted soundtrack state."""
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from coverly.jobs import Worker


@pytest.fixture
def ui(client):
    import socket,threading,time,uvicorn
    api=pytest.importorskip('playwright.sync_api')
    # A real HTTP fixture preserves multipart binary uploads. Playwright's
    # route.post_data_buffer omits/corrupts file blobs on some Chromium builds.
    # Only this test wrapper maps its ephemeral port to the production origin.
    async def fixture_app(scope,receive,send):
        if scope['type']=='http':
            scope={**scope,'headers':[(k,b'localhost:8000' if k==b'host' else b'http://localhost:8000' if k==b'origin' else v) for k,v in scope['headers']]}
        await client.app(scope,receive,send)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(fixture_app,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and time.monotonic()<deadline:time.sleep(.02)
    assert server.started
    with api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000});page.set_default_timeout(10000)
        page.add_init_script('window.coverlyTestStreams={};window.EventSource=class{constructor(){} addEventListener(name,callback){window.coverlyTestStreams[name]=callback;}};')
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.route('**/*',lambda r:r.continue_() if urlsplit(r.request.url).hostname=='127.0.0.1' and urlsplit(r.request.url).port==port else r.abort())
        class FixturePage:
            def goto(self,url):return page.goto(url.replace('http://localhost:8000',f'http://127.0.0.1:{port}'))
            def __getattr__(self,name):return getattr(page,name)
        try:
            yield FixturePage()
            assert not errors, errors
        finally:
            browser.close();server.should_exit=True;thread.join(timeout=10);sock.close()


def test_folder_import_transfers_front_back_and_both_disc_sides(ui,client,tmp_path):
    from conftest import jpeg
    folder=tmp_path/'Selected Album';folder.mkdir()
    for index,name in enumerate(['Front.jpg','Back.jpg','Side A.jpg','Side B.jpg']):
        (folder/name).write_bytes(jpeg(label='UNIQUE CONTROLLED IMAGE '+str(index)))
    (folder/'.DS_Store').write_text('hidden file')
    ui.goto('http://localhost:8000/#library')
    ui.locator('.page-heading').get_by_role('button',name='Import artwork',exact=True).click()
    ui.locator('#import-folder-picker').set_input_files(str(folder))
    ui.get_by_text('4 images selected',exact=False).wait_for()
    assert ui.get_by_label('Album title',exact=True).input_value()=='Selected Album'
    ui.locator('#import-form').get_by_role('button',name='Import artwork',exact=True).click()
    ui.get_by_role('button',name='Save & mark reviewed',exact=True).wait_for()
    album=client.get('/api/albums').json()['items'][0]
    details=client.get('/api/albums/'+album['id']).json()
    assert details['image_count']==4
    assert {i['classification'] for i in details['images']} >= {'front','back','vinyl_side_a','vinyl_side_b'}


def test_keep_last_selection_review_removal_and_undo(ui,client,image_bytes):
    identifiers=[]
    for index in range(4):
        identifiers.append(client.post('/api/import',data={'title':'Edition '+str(index)},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id'])
    ui.goto('http://localhost:8000/#library')
    ui.get_by_role('button',name='Similar artwork',exact=True).click()
    ui.get_by_role('button',name='Select all except last · all pages',exact=True).click()
    ui.get_by_text('3 artwork selected',exact=True).wait_for()
    assert ui.locator('[data-match-select]:disabled').count()==1
    assert ui.locator('[data-match-select]:checked').count()==3
    assert ui.locator('.duplicate-grid img').first.evaluate('(im)=>im.getBoundingClientRect().height')<=130
    import os
    if os.environ.get('COVERLY_UI_SCREENSHOTS'):
        directory=Path(os.environ['COVERLY_UI_SCREENSHOTS']);directory.mkdir(parents=True,exist_ok=True);ui.screenshot(path=str(directory/'compact-duplicate-selection.png'),full_page=True)
    ui.reload();ui.get_by_role('button',name='Similar artwork',exact=True).click()
    ui.get_by_text('3 artwork selected',exact=True).wait_for()
    assert ui.locator('[data-match-select]:checked').count()==3
    ui.get_by_role('button',name='Remove selected artwork',exact=True).click()
    ui.get_by_role('button',name='Remove selected artwork now',exact=True).click()
    ui.get_by_role('button',name='Undo last artwork removal',exact=True).wait_for()
    remaining=[client.get('/api/albums/'+key).json() for key in identifiers]
    assert sum(len(a['images']) for a in remaining)==1
    ui.get_by_role('button',name='Undo last artwork removal',exact=True).click()
    ui.get_by_role('button',name='Select all except last · all pages',exact=True).wait_for()
    ui.locator('[data-action=undo-artwork-removal]').wait_for(state='detached')
    assert sum(len(client.get('/api/albums/'+key).json()['images']) for key in identifiers)==4


def test_wikipedia_url_button_uses_open_album_and_saved_choice_stays_compact(ui,client,settings,image_bytes):
    html=(Path(__file__).parent/'fixtures/vaai-kozhuppu.html').read_text()
    url='https://en.wikipedia.org/wiki/Vaai_Kozhuppu'
    album=client.post('/api/import',data={'title':'Vaai Kozhuppu'},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    ui.goto('http://localhost:8000/#album/'+album)
    ui.get_by_role('button',name='Search album on the web',exact=True).click()
    ui.get_by_label('Reference page URL',exact=True).fill(url)
    ui.get_by_role('button',name='Import this URL',exact=True).click()
    class Fetcher:
        def __init__(self,*args):pass
        def get(self,requested):assert requested==url;return html.encode(),url
        def close(self):pass
    ui.locator('.lookup-progress').wait_for()
    Worker(settings,client.app.state.sessions,Fetcher).tick()
    ui.get_by_role('button',name='Use these details',exact=True).wait_for()
    assert ui.locator('.soundtrack-table tbody tr').count()==5
    ui.get_by_role('button',name='Use these details',exact=True).click()
    ui.get_by_role('heading',name='Selected soundtrack details',exact=True).wait_for()
    assert ui.get_by_role('button',name='Use these details',exact=True).count()==0
    ui.get_by_role('button',name='Save & mark reviewed',exact=True).click()
    ui.locator('#modal').wait_for(state='hidden')
    ui.goto('http://localhost:8000/#album/'+album)
    ui.get_by_role('heading',name='Saved soundtrack details',exact=True).wait_for()
    assert ui.get_by_role('button',name='Use these details',exact=True).count()==0
    assert ui.locator('.soundtrack-table tbody tr').count()==5
    assert ui.get_by_role('button',name='Look up artist online',exact=True).is_visible()


def test_native_folder_bridge_files_use_the_same_multi_image_upload(ui,client,image_bytes):
    import base64,json
    payload={'folder':'Native Album','files':[{'name':name,'relative':'Native Album/'+name,'modified':1,'type':'image/jpeg','data':base64.b64encode(image_bytes).decode()} for name in ['Front.jpg','Back.jpg']]}
    # Identical bytes intentionally demonstrate deduplication while confirming
    # both chosen files were submitted through the UI's multipart transport.
    ui.add_init_script('window.pywebview={api:{choose_artwork_folder:async()=>('+json.dumps(payload)+')}};')
    ui.goto('http://localhost:8000/#library')
    ui.locator('.page-heading').get_by_role('button',name='Import artwork',exact=True).click()
    ui.get_by_role('button',name='Add album folder…',exact=True).click()
    ui.get_by_text('2 images selected',exact=False).wait_for()
    assert ui.get_by_label('Album title',exact=True).input_value()=='Native Album'
    ui.locator('#import-form').get_by_role('button',name='Import artwork',exact=True).click()
    ui.get_by_role('button',name='Save & mark reviewed',exact=True).wait_for()
    assert client.get('/api/albums').json()['total']==1
    from sqlalchemy import select
    from coverly.db import Audit
    with client.app.state.sessions() as session:
        assert session.scalar(select(Audit).where(Audit.action=='manual_import')).detail['images']==2


def test_file_refresh_updates_gallery_and_viewer_without_losing_form_edits(ui,client,settings,image_bytes):
    from conftest import jpeg
    from coverly.db import Album
    result=client.post('/api/import',data={'title':'Edited artwork','artist':'Composer'},files={'files':('Front.jpg',image_bytes,'image/jpeg')}).json()
    album_id=result['album_id']
    settings.filesystem_sync=True
    index=client.app.state.file_index
    index.scan()
    value=client.get('/api/albums/'+album_id).json()
    path=settings.data/value['images'][0]['cover_path']
    ui.goto('http://localhost:8000/#library')
    ui.locator('#library-search input').fill('unsent search draft')
    path.write_bytes(jpeg(900,800,color='#996633'))
    index.scan()
    ui.evaluate('async()=>{await window.coverlyTestStreams.filesystem({data:JSON.stringify(await (await fetch("/api/filesystem/status")).json())});}')
    assert ui.locator('#library-search input').input_value()=='unsent search draft'
    ui.locator('a.album-art').click()
    ui.get_by_role('button',name='Save & mark reviewed',exact=True).wait_for()
    ui.get_by_label('Album title',exact=True).fill('Unsaved metadata draft')
    ui.locator('[data-action=view-image]').first.click()
    ui.locator('#viewer-image').wait_for()
    path.write_bytes(jpeg(777,777,color='#338899'))
    index.scan()
    ui.evaluate('async()=>{await window.coverlyTestStreams.filesystem({data:JSON.stringify(await (await fetch("/api/filesystem/status")).json())});}')
    pytest.importorskip('playwright.sync_api').expect(ui.locator('#viewer-image')).to_have_js_property('naturalWidth',777)
    ui.locator('[data-action=viewer-close]').click()
    assert ui.get_by_label('Album title',exact=True).input_value()=='Unsaved metadata draft'
    assert ui.locator('.detail-gallery').get_by_text('777 × 777',exact=False).count()==1
    path.unlink()
    index.scan()
    ui.evaluate('async()=>{await window.coverlyTestStreams.filesystem({data:JSON.stringify(await (await fetch("/api/filesystem/status")).json())});}')
    assert ui.locator('.detail-gallery img').count()==0
    assert ui.get_by_label('Album title',exact=True).input_value()=='Unsaved metadata draft'


def test_right_click_show_folder_on_card_gallery_and_viewer(ui,client,settings,image_bytes,monkeypatch):
    opened=[]
    monkeypatch.setattr('coverly.file_manager.reveal',lambda path:opened.append(path))
    album=client.post('/api/import',data={'title':'Right click album'},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    value=client.get('/api/albums/'+album).json()
    expected=settings.data/value['images'][0]['cover_path']
    before=expected.read_bytes()
    ui.goto('http://localhost:8000/#library')
    ui.locator('.album-art img').click(button='right')
    with ui.expect_response(lambda response:response.url.endswith('/reveal')):
        ui.get_by_role('menuitem',name='Show in folder').click()
    ui.get_by_role('menu',name='Artwork actions').wait_for(state='detached')
    ui.locator('.album-art').click()
    ui.locator('.detail-gallery img').first.click(button='right')
    with ui.expect_response(lambda response:response.url.endswith('/reveal')):
        ui.get_by_role('menuitem',name='Show in folder').click()
    ui.locator('[data-action=view-image]').first.click()
    ui.locator('#viewer-image').click(button='right')
    with ui.expect_response(lambda response:response.url.endswith('/reveal')):
        ui.get_by_role('menuitem',name='Show in folder').click()
    ui.locator('#viewer-image').click(button='right')
    ui.keyboard.press('Escape')
    assert ui.locator('#image-viewer').is_visible()
    assert ui.get_by_role('menu',name='Artwork actions').count()==0
    ui.locator('[data-action=viewer-close]').click()
    ui.locator('.detail-gallery .artwork-open').first.focus()
    ui.keyboard.press('Shift+F10')
    with ui.expect_response(lambda response:response.url.endswith('/reveal')):
        ui.get_by_role('menuitem',name='Show in folder').press('Enter')
    ui.get_by_role('menu',name='Artwork actions').wait_for(state='detached')
    assert opened==[expected]*4
    assert expected.read_bytes()==before


def test_master_metadata_selection_spans_pages_and_progress_survives_refresh(ui,client):
    from test_bulk_metadata import seed
    seed(client,55)
    ui.goto('http://localhost:8000/#review')
    ui.get_by_label('All 55 albums · all pages',exact=True).check()
    ui.get_by_text('55 albums selected',exact=True).wait_for()
    assert ui.locator('[data-album-select]:checked').count()==48
    ui.get_by_role('button',name='Next →',exact=True).click()
    pytest.importorskip('playwright.sync_api').expect(ui.locator('[data-album-select]:checked')).to_have_count(7)
    assert ui.get_by_label('All 55 albums · all pages',exact=True).is_checked()
    ui.get_by_role('button',name='Look up selected albums',exact=True).click()
    assert not ui.get_by_label('Rename composer / album folders using imported metadata',exact=True).is_checked()
    ui.get_by_role('button',name='Import details in background',exact=True).click()
    ui.locator('#bulk-metadata-progress .job-card').wait_for()
    job=client.get('/api/jobs').json()[0]
    assert job['total']==55 and job['payload']['bulk_apply'] and not job['payload']['move_folders']
    ui.reload()
    ui.locator('#bulk-metadata-progress .job-card').wait_for()
    assert ui.get_by_label('All 55 albums · all pages',exact=True).is_checked()
