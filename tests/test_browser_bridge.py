"""Controlled-page tests only: these never contact or sign into ChatGPT."""
import base64,json
from pathlib import Path
import pytest


@pytest.mark.parametrize('after_login',[False,True])
def test_bridge_offers_selected_files_and_prompt_without_sending(image_bytes,after_login):
    api=pytest.importorskip('playwright.sync_api')
    source=Path(__file__).parents[1]/'browser-bridge'/'handoff.js'
    with api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page()
        page.route('https://chatgpt.com/**',lambda r:r.fulfill(body='<html><body><input type="file" multiple accept="image/*"><textarea id="prompt-textarea"></textarea><button id="send">Send</button><script>window.sends=0;document.querySelector("#send").onclick=()=>window.sends++;</script></body></html>',content_type='text/html'))
        payload={'files':[{'name':'Magudi - Front.jpg','mimeType':'image/jpeg','data':base64.b64encode(image_bytes).decode()}],'prompt':'Restore Tamil movie artwork without cutting text.'}
        page.add_init_script('window.chrome={runtime:{sendMessage:async message=>message.type==="coverly-bundle"?'+json.dumps(payload)+':message.type==="coverly-has-transfer"?{pending:true}:{ok:true}}};')
        page.goto('https://chatgpt.com/' if after_login else 'https://chatgpt.com/#coverly-transfer='+'a'*43)
        page.add_script_tag(content=source.read_text())
        page.wait_for_function('document.querySelector("input[type=file]").files.length===1',timeout=5000)
        values=page.evaluate('async()=>({name:document.querySelector("input[type=file]").files[0].name,bytes:Array.from(new Uint8Array(await document.querySelector("input[type=file]").files[0].arrayBuffer())),prompt:document.querySelector("textarea").value,sends:window.sends})')
        assert values['name']=='Magudi - Front.jpg' and bytes(values['bytes'])==image_bytes
        assert values['prompt']==payload['prompt'] and values['sends']==0
        browser.close()


def test_bridge_preserves_an_existing_draft():
    api=pytest.importorskip('playwright.sync_api')
    source=Path(__file__).parents[1]/'browser-bridge'/'handoff.js'
    with api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        page.route('https://chatgpt.com/**',lambda r:r.fulfill(body='<input type="file" multiple><textarea id="prompt-textarea">My existing draft</textarea>',content_type='text/html'))
        page.add_init_script('window.chrome={runtime:{sendMessage:async()=>{throw Error("Must not fetch files over an existing draft")}}};')
        page.goto('https://chatgpt.com/#coverly-transfer='+'b'*43);page.add_script_tag(content=source.read_text())
        page.get_by_text('Attach selected Coverly images',exact=True).click()
        assert page.locator('textarea').input_value()=='My existing draft'
        assert page.locator('input').evaluate('(input)=>input.files.length')==0
        browser.close()


def test_background_restricts_file_access_to_coverly_created_chatgpt_tabs():
    api=pytest.importorskip('playwright.sync_api')
    source=Path(__file__).parents[1]/'browser-bridge'/'background.js'
    with api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        page.add_init_script('''window.saved={};window.requests=[];window.created=[];
        window.fetch=async url=>{requests.push(url);return {ok:true,json:async()=>({files:[]})}};
        window.chrome={runtime:{onMessage:{addListener:fn=>window.bridgeListener=fn}},tabs:{create:async data=>{created.push(data.url);return {id:10}},sendMessage:async()=>{}},storage:{session:{set:async data=>Object.assign(saved,data),get:async key=>({[key]:saved[key]}),remove:async key=>delete saved[key]}}};''')
        page.goto('about:blank');page.add_script_tag(content=source.read_text())
        result=page.evaluate('''async()=>{
         const call=(m,s)=>new Promise(resolve=>bridgeListener(m,s,resolve));
         const bad=await call({type:'coverly-start',token:'a'.repeat(43)},{url:'https://evil.example',tab:{id:2}});
         const start=await call({type:'coverly-start',token:'a'.repeat(43)},{url:'http://localhost:8000/#studio',tab:{id:2}});
         const unrelated=await call({type:'coverly-bundle'},{url:'https://chatgpt.com/',tab:{id:99}});
         const foreign=await call({type:'coverly-bundle'},{url:'https://evil.example/',tab:{id:10}});
         const allowed=await call({type:'coverly-bundle',url:'https://evil.example/'},{url:'https://chatgpt.com/',tab:{id:10}});
         return {bad,start,unrelated,foreign,allowed,requests,created};
        }''')
        assert result['bad'].get('error') and result['unrelated'].get('error') and result['foreign'].get('error')
        assert result['start']['ok'] and len(result['requests'])==1
        assert result['requests'][0]=='http://localhost:8000/api/chatgpt-transfers/'+'a'*43
        assert result['created']==['https://chatgpt.com/#coverly-transfer='+'a'*43]
        browser.close()


def test_user_selected_page_capture_excludes_forms_and_private_script_state():
    api=pytest.importorskip('playwright.sync_api')
    source=Path(__file__).parents[1]/'browser-bridge'/'capture.js'
    public={'@type':'MusicAlbum','name':'Example Film','byArtist':{'name':'Singer One'},'authToken':'PRIVATE_TOKEN','track':[{'@type':'MusicRecording','name':'First Song','duration':'PT4M21S'}]}
    html='<main><h1>Example Film</h1><form><input value="PRIVATE_PASSWORD"></form><div data-account="PRIVATE_ACCOUNT">Music: Composer One</div><script>window.token="PRIVATE_STATE"</script><script type="application/ld+json">'+json.dumps(public)+'</script><table><tr><th>Title</th><th>Singer(s)</th><th>Length</th></tr><tr><td>First Song</td><td>Singer One</td><td>4:21</td></tr></table></main>'
    with api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        page.route('https://films.example/**',lambda r:r.fulfill(body=html,content_type='text/html'))
        page.goto('https://films.example/example?private_tracking_token=SECRET')
        page.add_script_tag(content=source.read_text())
        captured=page.evaluate('captureCoverlyMetadata()')
        assert captured['source_url']=='https://films.example/example'
        assert 'First Song' in captured['html'] and 'Singer One' in captured['html']
        assert not any(v in captured['html'] for v in ['PRIVATE_PASSWORD','PRIVATE_ACCOUNT','PRIVATE_STATE','PRIVATE_TOKEN'])
        from coverly.soundtracks import extract_html
        doc=extract_html(captured['html'],captured['source_url'],'Example Film')
        assert doc['tracks'][0]['singers']=='Singer One' and not doc['composer']
        browser.close()


def test_local_bridge_returns_selected_page_to_review_api():
    api=pytest.importorskip('playwright.sync_api')
    source=Path(__file__).parents[1]/'browser-bridge'/'handoff.js'
    with api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        page.route('http://localhost:8000/**',lambda r:r.fulfill(body='<html><body>Coverly fixture</body></html>',content_type='text/html'))
        page.add_init_script('''window.listeners=[];window.requests=[];
         window.chrome={runtime:{onMessage:{addListener:fn=>listeners.push(fn)},sendMessage:async()=>({ok:true})}};
         window.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({job_id:'fixture'})}};''')
        page.goto('http://localhost:8000/#library');page.add_script_tag(content=source.read_text())
        result=page.evaluate('''async()=>{
         const message={type:'coverly-page-metadata',album_id:'a'.repeat(32),title:'Example Film',page:{source_url:'https://films.example/film',html:'<h1>Example Film</h1>'}};
         const reply=await new Promise(resolve=>listeners.forEach(listener=>listener(message,{id:'this-extension'},resolve)));
         return {reply,requests,ready:document.documentElement.dataset.coverlyMetadataBridge};
        }''')
        assert result['ready']=='1' and result['reply']['ok']
        request=result['requests'][0]
        assert request['url']=='/api/albums/'+'a'*32+'/metadata-page'
        assert request['options']['headers']['X-Coverly-Request']=='1'
        assert json.loads(request['options']['body'])['source_url']=='https://films.example/film'
        browser.close()


def test_popup_recovers_album_connection_instead_of_leaving_import_disabled():
    api=pytest.importorskip('playwright.sync_api')
    root=Path(__file__).parents[1]/'browser-bridge'
    with api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        page.set_content('<p id="target"></p><button id="capture" disabled>Use this page in Coverly</button><button id="open">Open Coverly</button><p id="status"></p>')
        page.evaluate('''()=>{window.messages=[];window.saved={};window.chrome={
         storage:{session:{get:async()=>({}),set:async data=>Object.assign(saved,data)}},
         tabs:{query:async query=>query.active?[{id:2,url:'https://en.wikipedia.org/wiki/Vaai_Kozhuppu'}]:[{id:1,url:'http://localhost:8000/#album/fixture'}],
           sendMessage:async(id,message)=>{messages.push({id,message});return message.type==='coverly-resolve-album'?{albumId:'a'.repeat(32),title:'Vaai Kozhuppu'}:{ok:true};},update:async()=>{}},
         scripting:{executeScript:async()=>[{result:{source_url:'https://en.wikipedia.org/wiki/Vaai_Kozhuppu',html:'<h1>Vaai Kozhuppu</h1>'}}]}}}''')
        page.add_script_tag(content=(root/'capture.js').read_text())
        page.add_script_tag(content=(root/'popup.js').read_text())
        page.get_by_role('button',name='Use this page in Coverly',exact=True).click()
        page.get_by_text('Details returned to Coverly. Review them before saving.',exact=True).wait_for()
        messages=page.evaluate('messages')
        assert messages[0]['message']['type']=='coverly-resolve-album'
        assert messages[-1]['id']==1 and messages[-1]['message']['album_id']=='a'*32
        browser.close()


def test_wikipedia_browser_capture_keeps_full_table_and_shared_lyrics_credit():
    api=pytest.importorskip('playwright.sync_api')
    root=Path(__file__).parent
    with api.sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        page.route('https://en.wikipedia.org/**',lambda r:r.fulfill(body=(root/'fixtures/vaai-kozhuppu.html').read_text(),content_type='text/html'))
        page.goto('https://en.wikipedia.org/wiki/Vaai_Kozhuppu')
        page.add_script_tag(content=(root.parent/'browser-bridge/capture.js').read_text())
        captured=page.evaluate('captureCoverlyMetadata()')
        from coverly.soundtracks import extract_html
        doc=extract_html(captured['html'],captured['source_url'],'Vaai Kozhuppu')
        assert len(doc['tracks'])==5 and doc['film_year']==1989 and doc['composer']=='Chandrabose'
        assert all(t['lyrics']=='Vairamuthu' for t in doc['tracks'])
        browser.close()
