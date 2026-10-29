import base64,hashlib,io,json,zipfile,time
from pathlib import Path
from PIL import Image
import pytest
from sqlalchemy import select
from coverly.db import Album,AlbumImage,ImageAsset,Restoration,ArtistAlias
from coverly.jobs import Worker
from coverly.storage import within,folder_for
from coverly.embedding import square_jpeg
from coverly.chatgpt_transfer import Transfers


def imported(client,image_bytes,title='Magudi',artist='Ilayaraja'):
    ident=client.post('/api/import',data={'title':title,'artist':artist},files={'files':('unlabelled.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    return client.get('/api/albums/'+ident).json()


def test_jpeg_passthrough_human_filenames_and_year_move(client,settings,image_bytes):
    a=imported(client,image_bytes);im=a['images'][0]
    assert im['original']==im['derivative']
    assert within(settings.data,im['cover_path']).read_bytes()==image_bytes
    assert client.patch(f"/api/albums/{a['id']}/images/{im['id']}",json={'classification':'front'}).status_code==200
    a=client.get('/api/albums/'+a['id']).json();path=a['images'][0]['cover_path']
    assert path.endswith('/Magudi - Front.jpg')
    assert list(within(settings.data,a['folder']+'/Covers').glob('*Additional*'))  # Existing Covers are preserved.
    old=a['folder']
    assert client.patch('/api/albums/'+a['id'],json={'title':'Magudi','artist':'Ilaiyaraaja','year':1984,'move_folder':True}).status_code==200
    a=client.get('/api/albums/'+a['id']).json()
    assert '(1984)' in a['folder'] and not within(settings.data,old).exists()
    assert within(settings.data,a['images'][0]['cover_path']).name=='Magudi - Front.jpg'
    assert client.get(a['images'][0]['display_url']).content==image_bytes


def test_long_title_retains_year(settings):
    from types import SimpleNamespace
    name=folder_for(settings,SimpleNamespace(artist='Artist',title='Long title '*50,year=1987,id='a'*32))
    assert name.endswith(' (1987)') and 'aaaaaaaaaaaa' not in name and len(name.split('/')[-1].encode('utf-8'))<=240


def test_replacement_and_approved_import_are_active_only_in_selected_album(client,settings,image_bytes):
    a=imported(client,image_bytes);b=imported(client,image_bytes,'Other edition');im=a['images'][0]
    new=Image.new('RGB',(400,500),'blue');buf=io.BytesIO();new.save(buf,'JPEG',quality=80);replacement=buf.getvalue()
    result=client.post(f"/api/albums/{a['id']}/images/{im['id']}/replacement",data={'generated':'false'},files={'file':('replacement.jpg',replacement,'image/jpeg')})
    assert result.status_code==200,result.text
    assert result.json()['status']=='approved'
    a=client.get('/api/albums/'+a['id']).json();b=client.get('/api/albums/'+b['id']).json()
    assert a['images'][0]['active_restoration_id']==result.json()['id']
    assert client.get(a['cover_url']).content==replacement
    assert client.get(b['cover_url']).content==image_bytes
    assert client.get(f"/api/images/{im['id']}/original").content==image_bytes
    assert within(settings.data,a['images'][0]['cover_path']).read_bytes()==replacement
    # External imports remain visible as pending variants, then approval changes the cover.
    im2=Image.new('RGB',(320,320),'green');out=io.BytesIO();im2.save(out,'JPEG');green=out.getvalue()
    r=client.post('/api/codex/import/'+im['id'],data={'album_id':a['id']},files={'file':('green.jpg',green,'image/jpeg')}).json()
    assert client.get(a['cover_url']).content==replacement
    assert client.post('/api/restorations/'+r['id']+'/approve',json={'album_id':a['id']}).status_code==200
    a=client.get('/api/albums/'+a['id']).json()
    assert client.get(a['cover_url']).content==green
    assert client.post('/api/restorations/'+r['id']+'/reject',json={'album_id':a['id']}).status_code==200
    assert r['id'] not in {v['id'] for v in client.get('/api/restorations?status=pending').json()}
    assert r['id'] in {v['id'] for v in client.get('/api/restorations?status=rejected').json()}


def test_optional_padding_preserves_all_edges_and_export_size_limit(client,settings,image_bytes):
    im=Image.new('RGB',(800,500),'white')
    # Opt-in padding preserves marks on all edges.
    for xy in [(0,250),(799,250),(400,0),(400,499)]:im.putpixel(xy,(255,0,0))
    buf=io.BytesIO();im.save(buf,'PNG');raw=buf.getvalue()
    result,info=square_jpeg(raw,mode="pad")
    with Image.open(io.BytesIO(result)) as square:assert square.size==(800,800)
    assert len(result)<500000 and info['crop']==(0,0,800,500)
    a=imported(client,image_bytes);im=a['images'][0]
    response=client.post('/api/embedding/export',json={'items':[{'album_id':a['id'],'image_id':im['id']}],'mode':'pad'})
    assert response.status_code==200
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        name=next(n for n in z.namelist() if n.endswith('.jpg'))
        assert 'Magudi - Additional' in name and len(z.read(name))<499000
        with Image.open(io.BytesIO(z.read(name))) as image:assert image.width==image.height
    assert client.get('/api/images/'+im['id']+'/original').content==image_bytes
    # An already suitable JPEG is copied byte-for-byte.
    existing,details=square_jpeg(image_bytes)
    assert existing==image_bytes and details['passthrough']


def test_artist_proposals_approval_moves_without_merging_editions(client,settings,image_bytes):
    a=imported(client,image_bytes,'First edition','Ilayaraja');b=imported(client,image_bytes,'Second edition','Illayaraja')
    groups=client.get('/api/artists/proposals').json()['groups']
    g=next(g for g in groups if g['canonical']=='Ilaiyaraaja')
    names=[n['name'] for n in g['names']]
    plan=client.post('/api/artists/preview',json={'groups':[{'canonical':'Ilaiyaraaja','names':names,'source_url':g['source_url']}]}).json()
    assert plan['album_count']==2
    assert client.get('/api/albums/'+a['id']).json()['artist']=='Ilayaraja' # Preview has no mutation.
    job=client.post('/api/artists/plans/'+plan['plan_id']+'/apply').json()
    Worker(settings,client.app.state.sessions).tick()
    for old in [a,b]:
        updated=client.get('/api/albums/'+old['id']).json()
        assert updated['artist']=='Ilaiyaraaja' and updated['folder'].startswith('Ilaiyaraaja/')
        assert client.get(updated['cover_url']).content==image_bytes
    future=imported(client,image_bytes,'Future edition','Ilayaraja')
    assert future['artist']=='Ilaiyaraaja'
    assert a['id']!=b['id']


def test_handoff_only_selected_files_and_expiry(client,image_bytes):
    a=imported(client,image_bytes);im=a['images'][0]
    response=client.post('/api/chatgpt-transfers',json={'items':[{'album_id':a['id'],'image_id':im['id']}],'prompt':'Restore this image; preserve all text.'})
    assert response.status_code==200
    token=response.json()['token'];bundle=client.get('/api/chatgpt-transfers/'+token).json()
    assert len(bundle['files'])==1 and base64.b64decode(bundle['files'][0]['data'])==image_bytes
    assert bundle['files'][0]['name'].startswith('Magudi - Additional')
    assert client.get('/api/chatgpt-transfers/not-a-token').status_code==400
    assert client.post('/api/chatgpt-transfers',json={'items':[{}],'prompt':'Restore'}).status_code==400
    transfers=Transfers();key=transfers.create([{'album_id':'a','image_id':'b'}],'Restore')
    transfers.items[key]['expires']=time.time()-1
    with pytest.raises(ValueError,match='expired'):transfers.get(key)


def test_tamil_filenames_stay_within_portable_component_limits(client,settings,image_bytes):
    a=imported(client,image_bytes,title='தமிழ்'*35)
    filename=Path(a['images'][0]['cover_path']).name
    assert len(filename.encode('utf-8'))<255
    assert within(settings.data,a['images'][0]['cover_path']).read_bytes()==image_bytes


def test_curated_artist_group_matches_the_filesystem_xerox_variant(client,image_bytes):
    a=imported(client,image_bytes,artist='Ilaiyaraaja *Xerox Label*')
    group=next(g for g in client.get('/api/artists/proposals').json()['groups'] if g['canonical']=='Ilaiyaraaja')
    assert a['artist'] in {n['name'] for n in group['names']}


def test_single_embedding_download_is_a_named_jpeg(client,image_bytes):
    a=imported(client,image_bytes);im=a['images'][0]
    r=client.get(f"/api/albums/{a['id']}/images/{im['id']}/embedding?download=true")
    assert r.status_code==200 and r.headers['content-type']=='image/jpeg'
    assert 'attachment' in r.headers['content-disposition'] and 'Magudi' in r.headers['content-disposition']
    assert len(r.content)<499000


def test_purge_of_a_shared_owner_retains_active_artwork_for_other_album(client,image_bytes):
    a=imported(client,image_bytes);b=imported(client,image_bytes,'Other edition');im=a['images'][0]
    replacement=client.post(f"/api/albums/{a['id']}/images/{im['id']}/replacement",files={'file':('photo.jpg',image_bytes,'image/jpeg')}).json()
    assert client.post('/api/restorations/'+replacement['id']+'/approve',json={'album_id':b['id']}).status_code==200
    client.post('/api/albums/trash',json={'ids':[a['id']]})
    plan=client.post('/api/trash/preview',json={'ids':[a['id']]}).json()
    result=client.post('/api/trash/purge',json={'plan_id':plan['plan_id'],'confirmation':'DELETE'})
    assert result.status_code==200,result.text
    survivor=client.get('/api/albums/'+b['id']).json()
    assert client.get(survivor['cover_url']).content==image_bytes


def test_square_crop_trims_scan_background_without_padding():
    from PIL import ImageDraw,ImageChops
    source=Image.new('RGB',(1000,900),'white');draw=ImageDraw.Draw(source)
    draw.rectangle((170,120,769,719),fill='navy');draw.rectangle((175,125,764,714),outline='red',width=5)
    out=io.BytesIO();source.save(out,'PNG')
    raw,info=square_jpeg(out.getvalue())
    assert info['crop']==(170,120,770,720)
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size==(600,600) and image.getpixel((300,300))[2]>80
        assert image.getpixel((0,0))[0]<60 # No white scanner margin / new padding.
        pixels=image.convert('RGB').copy()


def test_crop_position_can_retain_edge_text_and_export_is_uncapped():
    import numpy as np
    im=Image.fromarray(np.random.default_rng(4).integers(0,256,(600,1000,3),dtype=np.uint8))
    data=io.BytesIO();im.save(data,'PNG')
    left,a=square_jpeg(data.getvalue(),position_x=0)
    right,b=square_jpeg(data.getvalue(),position_x=1)
    assert a['crop']==(0,0,600,600) and b['crop']==(400,0,1000,600)
    assert len(left)>500000 and len(right)>500000


def test_square_restoration_job_keeps_source(client,settings):
    from PIL import ImageDraw
    image=Image.new('RGB',(700,500),'white');ImageDraw.Draw(image).rectangle((150,50,549,449),fill='navy')
    out=io.BytesIO();image.save(out,'PNG');source=out.getvalue()
    a=imported(client,source);im=a['images'][0]
    job=client.post('/api/restorations',json={'ids':[im['id']],'preset':'gentle','size':500,'square':True,'image_albums':{im['id']:a['id']}})
    assert job.status_code==200,job.text
    Worker(settings,client.app.state.sessions).tick()
    result=client.get('/api/restorations?status=pending').json()[0]
    raw=within(settings.data,result['path']).read_bytes()
    with Image.open(io.BytesIO(raw)) as restored:assert restored.size==(500,500)
    assert client.get('/api/images/'+im['id']+'/original').content==source
