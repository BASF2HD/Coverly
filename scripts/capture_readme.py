"""Capture real Coverly screens using a disposable, entirely synthetic archive."""
from __future__ import annotations
import argparse
import io
import socket
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont
from fastapi.testclient import TestClient
from coverly.app import create_app
from coverly.config import Settings
from coverly.db import Album, now
from coverly.jobs import Worker


def artwork(title, index, back=False):
    palettes = [('#e9dbb8','#943e31','#d18a48'),('#28433c','#e9d3a2','#899c6e'),
                ('#dca773','#343d4c','#c96342'),('#303745','#d5a875','#758c8b'),
                ('#e6c9a0','#3e5946','#b67c42'),('#924537','#edcea1','#d99a57')]
    bg, ink, accent = palettes[index % len(palettes)]
    image = Image.new('RGB',(720,720),bg)
    d=ImageDraw.Draw(image)
    font=lambda size: ImageFont.load_default(size=size)
    d.rectangle((24,24,696,696),outline=ink,width=2)
    d.text((54,50),'COVERLY  /  DEMO PRESSINGS',font=font(18),fill=ink)
    if back:
        d.text((54,125),title.upper(),font=font(28),fill=ink)
        for n, song in enumerate(['First Light','The River Song','Evening Breeze','A Letter Home'],1):
            y=210+n*65;d.text((54,y),f'0{n}   {song}',font=font(23),fill=ink)
            d.line((54,y+43,655,y+43),fill=accent,width=2)
    else:
        if index%3==0:
            d.ellipse((245,135,595,485),fill=accent)
            for n in range(5):
                y=360+n*32;d.arc((-100,y,900,y+330),180,360,fill=ink,width=18)
        elif index%3==1:
            for n in range(5):
                d.arc((120+n*25,150+n*25,620-n*25,650-n*25),10,320,fill=accent,width=13)
            d.ellipse((310,340,410,440),fill=ink)
        else:
            for n in range(7):
                x=75+n*80;d.rounded_rectangle((x,170+(n%3)*40,x+47,460),radius=23,fill=accent if n%2 else ink)
        title_lines=textwrap.wrap(title.upper(),19)
        for n,line in enumerate(title_lines):d.text((54,520+n*47),line,font=font(39),fill=ink)
    d.text((54,652),f'ILLUSTRATIVE ARTWORK   /   STEREO   /   33 RPM',font=font(15),fill=ink)
    output=io.BytesIO();image.save(output,'JPEG',quality=92)
    return output.getvalue()


def capture(destination):
    import uvicorn
    from playwright.sync_api import sync_playwright
    titles=['Moonlit Melodies','Echoes of Madras','Songs of the Monsoon','Along the River',
            'Summer in Chennai','The Golden Hour','A Quiet Raga','Letters from Home',
            'The Blue Courtyard','Morning on the Coast','Across the Fields','After the Rain']
    with tempfile.TemporaryDirectory(prefix='coverly-public-screens-') as temporary:
        cfg=Settings(storage=str(Path(temporary)/'archive'),auto_ocr=False,filesystem_sync=False)
        app=create_app(cfg)
        with TestClient(app,base_url='http://localhost:8000',headers={'X-Coverly-Request':'1'}) as client:
            albums=[]
            for index,title in enumerate(titles):
                response=client.post('/api/import',data={'title':title,'artist':['Arun Dev','Maya Raman','Ravi Sundar'][index%3],
                    'year':1980+index,'label':'Demo Pressings','catalogue':f'DP {1000+index}'},
                    files=[('files',('Front.jpg',artwork(title,index),'image/jpeg')),
                           ('files',('Back.jpg',artwork(title,index,True),'image/jpeg'))])
                response.raise_for_status();identifier=response.json()['album_id'];albums.append(identifier)
                with app.state.sessions() as session:
                    album=session.get(Album,identifier);album.review=index%4==0
                    album.soundtrack={'title':title,'composer':album.artist,'film_year':album.year,'accepted_at':now(),
                        'provider':'Demo archive','edition':'Illustrative soundtrack credits','source_url':'https://example.org/demo',
                        'tracks':[{'number':n,'title':song,'lyrics':'M. Kannan','singers':'Anita Rao, S. Kumar','length':'4:2'+str(n)}
                                  for n,song in enumerate(['First Light','The River Song','Evening Breeze','A Letter Home'],1)]}
                    session.commit()
            first=client.get('/api/albums/'+albums[0]).json()
            client.post('/api/restorations',json={'ids':[first['images'][0]['id']],'preset':'gentle','size':1000}).raise_for_status()
            Worker(cfg,app.state.sessions).tick()
            # Only this disposable documentation server maps its ephemeral port
            # to the production origin guard. The production app is unchanged.
            async def documentation_app(scope,receive,send):
                if scope['type']=='http':
                    scope={**scope,'headers':[(k,b'localhost:8000' if k==b'host' else b'http://localhost:8000' if k==b'origin' else v) for k,v in scope['headers']]}
                await app(scope,receive,send)
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            server=uvicorn.Server(uvicorn.Config(documentation_app,log_level='error',access_log=False,lifespan='off'))
            thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
            stop=threading.Event()
            def background():
                worker=Worker(cfg,app.state.sessions)
                while not stop.is_set():
                    worker.tick()
                    (cfg.data/'.worker-heartbeat').write_text(str(time.time()))
                    stop.wait(.5)
            worker_thread=threading.Thread(target=background,daemon=True);worker_thread.start()
            deadline=time.monotonic()+10
            while not server.started and time.monotonic()<deadline:time.sleep(.02)
            if not server.started:raise RuntimeError('Documentation fixture server did not start')
            destination.mkdir(parents=True,exist_ok=True)
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True)
                    context=browser.new_context(viewport={'width':1440,'height':1090},device_scale_factor=1)
                    context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(f'http://127.0.0.1:{port}/') else r.abort())
                    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    def shot(name,route):
                        page.goto(f'http://127.0.0.1:{port}/#{route}')
                        page.locator('.page-heading').wait_for()
                        page.evaluate('document.fonts.ready')
                        page.locator('img').evaluate_all('(images)=>Promise.all(images.map(im=>im.decode().catch(()=>{})))')
                        assert not page.locator('.error-page').count()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.screenshot(path=str(destination/name),animations='disabled',full_page=route=='studio')
                    shot('library-dark.png','library')
                    page.locator('[data-theme-toggle]').click()
                    shot('library-light.png','library')
                    page.locator('[data-theme-toggle]').click()
                    page.locator('[data-album-select="'+albums[0]+'"]').check()
                    page.locator('[data-album-select="'+albums[1]+'"]').check()
                    shot('restoration.png','studio')
                    shot('overview.png','dashboard')
                    if errors:raise RuntimeError('Screenshot page errors: '+repr(errors))
                    browser.close()
            finally:
                stop.set();worker_thread.join(timeout=10)
                server.should_exit=True;thread.join(timeout=10);sock.close()
        print('Captured four real Coverly screens from a disposable demo archive:',destination)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=Path('docs/screenshots'))
    capture(parser.parse_args().output)
