"""Run the complete archive workflow in an isolated demo directory; no live requests."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch
import httpx
from PIL import Image, ImageDraw, ImageFont
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coverly.config import Settings
from coverly.app import create_app
from coverly.jobs import Worker
from coverly.network import SafeHTTP
from coverly.db import Job


def sample_image(size, color, label):
    im=Image.new('RGB', size, color)
    draw=ImageDraw.Draw(im)
    font=ImageFont.load_default(size=max(16,size[0]//24))
    small=ImageFont.load_default(size=max(10,size[0]//48))
    draw.rectangle((size[0]*.06,size[1]*.06,size[0]*.94,size[1]*.94), outline='#ead9ab',width=max(2,size[0]//200))
    draw.ellipse((size[0]*.3,size[1]*.25,size[0]*.7,size[1]*.65),fill='#bd8a4c')
    draw.text((size[0]*.1,size[1]*.13),'COVERLY / TEST PRESSING',fill='#ead9ab',font=small)
    draw.text((size[0]*.1,size[1]*.72),label,fill='#f4ead0',font=font)
    draw.text((size[0]*.1,size[1]*.85),'SYNTHETIC FIXTURE - NOT HISTORICAL ARTWORK',fill='#ead9ab',font=small)
    out=io.BytesIO();im.save(out,'JPEG',quality=95)
    return out.getvalue()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='output/demo')
    args=parser.parse_args()
    output=Path(args.output).resolve()
    if (output/'archive').exists():
        parser.error('Use a new output directory; existing archives are never overwritten')
    output.mkdir(parents=True,exist_ok=True)
    cfg=Settings(storage=str(output/'archive'),request_delay=.25,auto_ocr=False)
    fixture=Path(__file__).resolve().parent.parent/'tests'/'fixtures'
    routes={
        '/robots.txt':(b'User-agent: *\nAllow: /','text/plain'),
        '/tamil/':((fixture/'category.html').read_bytes(),'text/html'),
        '/tamil/page/2/':((fixture/'page2.html').read_bytes(),'text/html'),
        '/product/nayakan/':((fixture/'product.html').read_text(encoding='utf-8').replace('Nayakan','[DEMO] Nayakan').encode(),'text/html'),
        '/product/compilation/':((fixture/'compilation.html').read_text(encoding='utf-8').replace('Nayakan','[DEMO] Nayakan').encode(),'text/html'),
        '/front.jpg':(sample_image((1200,1200),'#344d35','FRONT SLEEVE'),'image/jpeg'),
        '/front-150x150.jpg':(sample_image((150,150),'#344d35','FRONT'),'image/jpeg'),
        '/back.jpg':(sample_image((900,1000),'#753f34','BACK SLEEVE'),'image/jpeg'),
        '/disc.jpg':(sample_image((800,800),'#3d3e60','SIDE A'),'image/jpeg')}
    def handler(req):
        body,mime=routes.get(req.url.path,(b'not found','text/plain'))
        return httpx.Response(200 if req.url.path in routes else 404,content=body,headers={'content-type':mime})
    def factory(settings,domains):
        return SafeHTTP(settings,domains,httpx.Client(transport=httpx.MockTransport(handler)))
    app=create_app(cfg)
    worker=Worker(cfg,app.state.sessions,factory)
    with patch('coverly.network.public_addresses',return_value=['93.184.216.34']),patch.object(SafeHTTP,'throttle',lambda *_:None),TestClient(app,base_url='http://localhost:8000',headers={'X-Coverly-Request':'1'}) as c:
        response=c.post('/api/discover',json={'url':'https://records.example/tamil/','max_pages':10,'auto_download':False})
        response.raise_for_status();worker.tick()
        pages=c.get('/api/discoveries').json()['items']
        assert len(pages)==2
        response=c.post('/api/download',json={'ids':[p['id'] for p in pages]});response.raise_for_status();worker.tick()
        library=c.get('/api/albums').json()['items']
        assert len(library)==2
        details=[c.get('/api/albums/'+a['id']).json() for a in library]
        image_ids=list(dict.fromkeys(im['id'] for a in details for im in a['images']))
        assert len(image_ids)==3
        originals={i:hashlib.sha256(c.get(f'/api/images/{i}/original').content).hexdigest() for i in image_ids}
        response=c.post('/api/restorations',json={'ids':image_ids,'preset':'gentle','size':1000});response.raise_for_status();worker.tick()
        results=c.get('/api/restorations').json()
        assert len(results)==3
        for result in results:
            c.post(f"/api/restorations/{result['id']}/approve").raise_for_status()
        export=c.get('/api/export/approved');export.raise_for_status()
        (output/'approved-artwork.zip').write_bytes(export.content)
        for i in image_ids:
            assert hashlib.sha256(c.get(f'/api/images/{i}/original').content).hexdigest()==originals[i]
        backup=c.get('/api/backup');backup.raise_for_status()
        (output/'archive-backup.zip').write_bytes(backup.content)
        report={'controlled_fixture_only':True,'real_artwork_downloaded':False,'stats':c.get('/api/stats').json(),
                'jobs':[{k:j[k] for k in ['kind','status','progress','total']} for j in c.get('/api/jobs').json()],
                'originals_unchanged':True,'original_sha256':originals,'approved_export_verified':True,'backup_verified':True}
        (output/'verification.json').write_text(json.dumps(report,indent=2))
    # Relative path settings make this demo independently portable.
    (output/'config.json').write_text(json.dumps({**cfg.model_dump(),'storage':'archive'},indent=2))
    print(json.dumps(report,indent=2))
    print('Demo archive:',output)


if __name__=='__main__':
    main()
