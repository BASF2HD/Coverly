import hashlib,json
from pathlib import Path
import pytest
from sqlalchemy import select
from coverly.archive_location import browse,copy_archive
from coverly.soundtracks import film_title,spelling_match,MetadataLookup
from coverly.db import Album,ImageAsset,Restoration,Job,JobItem
from coverly.jobs import Worker,enqueue,recover
from coverly.intelligence import create_album
from coverly.storage import add_image,within


def test_title_query_removes_retail_descriptor_and_distinguishes_year():
    assert film_title('Idhu Namma Aalu Tamil Film story and dialogues')=='Idhu Namma Aalu'
    assert spelling_match('Ethu Namma Aalu (Original Motion Picture Soundtrack)','Idhu Namma Aalu',1988,1988)
    assert not spelling_match('Ethu Namma Aalu','Idhu Namma Aalu',1988,2016)
    assert not spelling_match('Ennai Thedi','Unnai Thedi',1988,1988)


def test_folder_browse_and_copy_preserve_archive(settings,db,image_bytes,tmp_path):
    with db() as s:
        album=create_album(s,settings,{'title':'Portable','manual':True})
        image,_=add_image(s,settings,album,image_bytes);s.commit();key=album.id
    external=tmp_path/'outside-app'/'Art Archive'
    external.parent.mkdir()
    listing=browse(str(external.parent))
    assert listing['path']==str(external.parent) and listing['parent']==str(tmp_path)
    result=copy_archive(settings.data,external)
    assert result['path']==str(external)
    assert (external/'coverly.sqlite3').exists() and (settings.data/'coverly.sqlite3').exists()
    assert (external/image.original).read_bytes()==image_bytes
    assert json.loads((external/album.folder/'album.json').read_text())['id']==key
    with pytest.raises(ValueError,match='empty'):copy_archive(settings.data,external)
    with pytest.raises(ValueError,match='outside'):copy_archive(settings.data,settings.data/'nested')


def test_storage_picker_api_and_relocation_guard(client,settings,image_bytes,tmp_path,monkeypatch):
    # Selecting a folder only returns its path; settings and originals are untouched.
    r=client.post('/api/storage/browse',json={'path':str(tmp_path)})
    assert r.status_code==200
    r=client.post('/api/storage/create-folder',json={'path':str(tmp_path),'name':'External Archive'})
    assert r.status_code==200
    assert client.post('/api/storage/create-folder',json={'path':str(tmp_path),'name':'../escape'}).status_code==422
    album=client.post('/api/import',data={'title':'Move test'},files={'files':('art.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    target=tmp_path/'External Archive'
    body=settings.model_dump();body['storage']=str(target)
    assert client.put('/api/settings',json=body).status_code==409
    # Copy is refused while a worker may still write into the archive.
    with client.app.state.sessions() as s:
        job=enqueue(s,'restore',{'preset':'gentle','size':256},[]);s.commit();jid=job.id
    assert client.post('/api/storage/copy',json={'path':str(target)}).status_code==409
    client.post('/api/jobs/'+jid+'/pause')
    assert client.post('/api/storage/copy',json={'path':str(target)}).status_code==200
    saved=[];monkeypatch.setattr('coverly.app.save_settings',lambda s:saved.append(s))
    assert client.put('/api/settings',json=body).status_code==200 and saved[0].data==target
    assert client.get('/api/albums/'+album).status_code==200


def test_restart_keeps_completed_images_and_manually_paused_jobs(settings,db):
    with db() as s:
        job=enqueue(s,'download',{},[('already-saved',{}),('interrupted',{})]);s.flush()
        items=list(s.scalars(select(JobItem).where(JobItem.job_id==job.id).order_by(JobItem.position)))
        items[0].status='done';items[1].status='running';job.status='running';job.progress=1
        paused=enqueue(s,'download',{},[('later',{})]);paused.status='paused';s.commit()
        first,second=job.id,paused.id
    with db() as s:
        recover(s)
        assert s.get(Job,first).status=='queued' and s.get(Job,first).progress==1
        assert [i.status for i in s.scalars(select(JobItem).where(JobItem.job_id==first).order_by(JobItem.position))]==['done','pending']
        assert s.get(Job,second).status=='paused'


def test_ai_result_is_separate_generated_and_does_not_lock_archive_during_inference(client,settings,image_bytes,monkeypatch):
    from coverly.ai_restoration import AIProvider
    from coverly.locking import file_lock
    album=client.post('/api/import',data={'title':'AI test'},files={'files':('art.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    image=client.get('/api/albums/'+album).json()['images'][0]
    monkeypatch.setattr('coverly.ai_restoration.capabilities',lambda:{'available':True})
    def inference(self,original,size,progress):
        # Another archive operation can acquire the lock while inference runs.
        with file_lock(settings.data/'.archive.lock'):
            assert original==image_bytes
        progress('AI enhancement on CPU · tile 1 of 1',1,1)
        return image_bytes,'cpu'
    monkeypatch.setattr(AIProvider,'restore',inference)
    job=client.post('/api/restorations',json={'ids':[image['id']],'preset':'ai','size':1000}).json()
    Worker(settings,client.app.state.sessions).tick()
    result=client.get('/api/restorations').json()[0]
    assert result['generated'] and result['status']=='pending' and result['title']=='AI test'
    assert 'CPU' in result['provider']
    assert within(settings.data,image['original']).read_bytes()==image_bytes
    assert client.get('/api/images/'+image['id']+'/original').content==image_bytes


def test_apple_ignores_digital_year_and_does_not_union_director_with_composer(settings):
    from test_soundtracks import credits_html
    from urllib.parse import urlsplit
    class Fetcher:
        def __init__(self,*args):pass
        def close(self):pass
        def get(self,url):
            path=urlsplit(url).path
            if path=='/search':
                data={'results':[{'collectionName':'Different Film (Original Motion Picture Soundtrack)','collectionId':8,'releaseDate':'1988-01-01'},
                    {'collectionName':'Ethu Namma Aalu (Original Motion Picture Soundtrack)','collectionId':7,'collectionViewUrl':'https://music.apple.com/album/7','artistName':'Composer One','releaseDate':'2016-01-01'}]}
            elif path=='/lookup':
                assert 'id=7' in url
                data={'results':[{'wrapperType':'track','collectionId':7,'trackId':42,'trackName':'First Song','artistName':'Singer One','trackNumber':1,'trackTimeMillis':240000}]}
            else:
                html=credits_html().replace('{"name": "Composer One", "roleNames": ["Composer"]}', '{"name": "Director Name", "roleNames": ["Composer"]}, {"name": "Composer One", "roleNames": ["Composer"]}')
                return html.encode(),url
            return json.dumps(data).encode(),url
    lookup=MetadataLookup(settings,Fetcher)
    lookup.apple('Idhu Namma Aalu',1988)
    assert len(lookup.documents)==1
    doc=lookup.documents[0]
    assert doc['release_year']==2016 and doc['film_year'] is None and doc['composer']=='Composer One'
    assert any('digital release date is ignored' in w for w in doc['warnings'])
    assert 'Director Name' in doc['track_composers']
    assert any('not confirmed' in w for w in doc['warnings'])


def test_chatgpt_export_retains_originals_and_image_ids(client,image_bytes):
    import io,zipfile
    album=client.post('/api/import',data={'title':'ChatGPT handoff'},files={'files':('art.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    image=client.get('/api/albums/'+album).json()['images'][0]
    response=client.post('/api/codex/export?target=chatgpt',json={'ids':[image['id']]})
    assert response.status_code==200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest=json.loads(archive.read('restoration-job.json'))
        assert manifest['provider']=='chatgpt-interactive'
        assert manifest['images'][0]['image_id']==image['id']
        assert archive.read(manifest['images'][0]['original'])==image_bytes
        assert 'Attach' in manifest['instructions']
