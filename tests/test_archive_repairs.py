import hashlib,json
from pathlib import Path
import pytest
from sqlalchemy import select
from coverly.db import Album,AlbumImage,ImageAsset,Job
from coverly.jobs import Worker
from coverly.storage import within


def imported(client,raw,title,artist='Malaysia vasudevan',filename='front.jpg'):
    response=client.post('/api/import',data={'title':title,'artist':artist},files={'files':(filename,raw,'image/jpeg')})
    response.raise_for_status()
    return client.get('/api/albums/'+response.json()['album_id']).json()


def test_bulk_removal_and_undo_do_not_read_unrelated_missing_original(client,settings,image_bytes):
    from conftest import jpeg
    a=imported(client,image_bytes,'Album')
    client.post('/api/import',data={'album_id':a['id']},files={'files':('back.jpg',jpeg(label='DIFFERENT BACK'),'image/jpeg')}).raise_for_status()
    a=client.get('/api/albums/'+a['id']).json()
    front=next(i for i in a['images'] if i['classification']=='front');back=next(i for i in a['images'] if i['classification']=='back')
    # Reproduces the live crash: another active original is absent and its cover
    # would need regeneration. Removing the selected front is metadata only.
    within(settings.data,back['original']).unlink()
    within(settings.data,back['cover_path']).unlink()
    body={'items':[{'album_id':a['id'],'image_id':front['id']}]}
    response=client.post('/api/artwork/trash',json=body)
    assert response.status_code==200,response.text
    assert client.get('/api/albums/'+a['id']).json()['images'][0]['id']==back['id']
    assert within(settings.data,front['original']).read_bytes()==image_bytes
    assert client.post('/api/artwork/restore',json=body).status_code==200
    assert len(client.get('/api/albums/'+a['id']).json()['images'])==2


def test_artist_case_correction_moves_every_album_and_shared_reference(client,settings,image_bytes):
    from conftest import jpeg
    first=imported(client,image_bytes,'First')
    second=imported(client,jpeg(label='SECOND SCAN'),'Second')
    shared=imported(client,image_bytes,'Shared edition','Other Composer')
    plan=client.post('/api/artists/preview',json={'groups':[{'canonical':'Malaysia Vasudevan','names':['Malaysia vasudevan']}]}).json()
    job=client.post('/api/artists/plans/'+plan['plan_id']+'/apply').json()
    worker=Worker(settings,client.app.state.sessions,quantum=1)
    worker.tick()
    # Check original paths while the other album is still queued, not just at
    # the end: an artist directory is shared by both albums on APFS/NTFS.
    for old in [first,second,shared]:
        detail=client.get('/api/albums/'+old['id']).json()
        for image in detail['images']:assert within(settings.data,image['original']).is_file()
    worker.tick()
    for old in [first,second]:
        detail=client.get('/api/albums/'+old['id']).json()
        assert detail['artist']=='Malaysia Vasudevan'
        assert detail['folder'].startswith('Malaysia Vasudevan/')
    assert 'Malaysia Vasudevan' in {p.name for p in settings.data.iterdir() if p.is_dir()}
    assert 'Malaysia vasudevan' not in {p.name for p in settings.data.iterdir() if p.is_dir()}
    for old in [first,second,shared]:
        detail=client.get('/api/albums/'+old['id']).json()
        manifest=json.loads(within(settings.data,detail['folder']+'/album.json').read_text())
        for image in manifest['images']:assert within(settings.data,image['original']).is_file()
    state=next(j for j in client.get('/api/jobs').json() if j['id']==job['id'])
    assert state['status']=='completed'


def test_case_rename_rolls_back_files_paths_and_shared_manifests(client,settings,image_bytes):
    from coverly.archive_ops import relocate_album
    a=imported(client,image_bytes,'First');other=imported(client,image_bytes,'Shared','Other')
    before={old['id']:within(settings.data,old['folder']+'/album.json').read_bytes() for old in [a,other]}
    with client.app.state.sessions() as s:
        album=s.get(Album,a['id']);album.artist='Malaysia Vasudevan'
        with pytest.raises(ValueError,match='controlled rollback'):
            with relocate_album(s,settings,album):raise ValueError('controlled rollback')
    for old in [a,other]:
        assert within(settings.data,old['folder']+'/album.json').read_bytes()==before[old['id']]
        assert within(settings.data,old['images'][0]['original']).read_bytes()==image_bytes
    assert 'Malaysia vasudevan' in {p.name for p in settings.data.iterdir() if p.is_dir()}



def test_manually_deleted_album_folder_is_not_recreated_by_remove_or_composer_fix(client,settings,image_bytes):
    import shutil
    a=imported(client,image_bytes,'Intentionally deleted')
    folder=within(settings.data,a['folder']);shutil.rmtree(folder)
    image=a['images'][0]
    client.post('/api/artwork/trash',json={'items':[{'album_id':a['id'],'image_id':image['id']}]}).raise_for_status()
    assert not folder.exists()
    client.post('/api/artwork/restore',json={'items':[{'album_id':a['id'],'image_id':image['id']}]}).raise_for_status()
    assert not folder.exists()
    plan=client.post('/api/artists/preview',json={'groups':[{'canonical':'Malaysia Vasudevan','names':['Malaysia vasudevan']}]}).json()
    client.post('/api/artists/plans/'+plan['plan_id']+'/apply').raise_for_status()
    Worker(settings,client.app.state.sessions).tick()
    after=client.get('/api/albums/'+a['id']).json()
    assert after['artist']=='Malaysia Vasudevan'
    assert not within(settings.data,after['folder']).exists()
    assert not within(settings.data,after['images'][0]['original']).exists()
    assert after['images'][0]['original_available'] is False


def test_background_download_does_not_recreate_manually_deleted_album(client,settings,website):
    import shutil
    factory,visited,_=website;worker=Worker(settings,client.app.state.sessions,factory)
    client.post('/api/discover',json={'url':'https://records.example/product/nayakan/','auto_download':False}).raise_for_status();worker.tick()
    page=client.get('/api/discoveries').json()['items'][0]
    client.post('/api/download',json={'ids':[page['id']]}).raise_for_status();worker.tick()
    album=client.get('/api/albums').json()['items'][0]
    folder=within(settings.data,album['folder']);shutil.rmtree(folder)
    before=len(visited)
    retry=client.post('/api/download',json={'ids':[page['id']]}).json();worker.tick()
    state=next(j for j in client.get('/api/jobs').json() if j['id']==retry['id'])
    assert state['status']=='completed_with_errors' and 'removed' in state['first_error']
    assert not folder.exists() and len(visited)==before


def test_shared_manifest_refresh_does_not_recreate_deleted_folder(client,settings,image_bytes):
    import shutil
    from coverly.storage import write_existing_manifest
    first=imported(client,image_bytes,'Owner');other=imported(client,image_bytes,'Deleted edition','Another composer')
    removed=within(settings.data,other['folder']);shutil.rmtree(removed)
    with client.app.state.sessions() as s:
        image=s.get(ImageAsset,first['images'][0]['id'])
        legacy=within(settings.data,first['folder']+'/legacy.jpg');legacy.write_bytes(image_bytes)
        image.derivative=legacy.relative_to(settings.data).as_posix()
        write_existing_manifest(s,settings,s.get(Album,first['id']),sync=True);s.commit()
    assert not removed.exists()
    assert within(settings.data,first['images'][0]['original']).read_bytes()==image_bytes


def test_album_folder_name_is_title_and_film_year_only(settings):
    from types import SimpleNamespace
    from coverly.config import Settings
    from coverly.storage import folder_for
    album=SimpleNamespace(id='a'*32,title='Pookkal Vidum Thoothu',artist='Composer',year=1983)
    assert folder_for(settings,album)=='Composer/Pookkal Vidum Thoothu (1983)'
    legacy=Settings(storage=str(settings.data),naming='{artist}/{title}{year}--{id}')
    assert folder_for(legacy,album)=='Composer/Pookkal Vidum Thoothu (1983)'
    album.title='Pookkal Vidum Thoothu (1983)'
    assert folder_for(legacy,album)=='Composer/Pookkal Vidum Thoothu (1983)'


def test_manual_covers_survive_metadata_changes_and_are_used_for_export(client,settings,image_bytes):
    from conftest import jpeg
    a=imported(client,image_bytes,'Old title','Composer');image=a['images'][0]
    manual=jpeg(label='MANUALLY RESTORED GOOD COVER')
    cover=within(settings.data,image['cover_path']);cover.write_bytes(manual)
    extra=cover.parent/'My carefully restored image.jpg';extra.write_bytes(manual)
    client.patch('/api/albums/'+a['id'],json={'title':'Pookkal Vidum Thoothu','artist':'Composer','year':1983,'move_folder':True}).raise_for_status()
    after=client.get('/api/albums/'+a['id']).json();im=after['images'][0]
    assert after['folder']=='Composer/Pookkal Vidum Thoothu (1983)'
    assert client.get(im['display_url']).content==manual
    assert client.get(f"/api/albums/{a['id']}/images/{im['id']}/embedding").content==manual
    assert within(settings.data,after['folder']+'/Covers/'+extra.name).read_bytes()==manual
    assert within(settings.data,after['folder']+'/Covers/'+cover.name).read_bytes()==manual
    assert within(settings.data,im['original']).read_bytes()==image_bytes


def test_metadata_does_not_regenerate_manually_deleted_cover(client,settings,image_bytes):
    a=imported(client,image_bytes,'Album','Composer');image=a['images'][0]
    cover=within(settings.data,image['cover_path']);cover.unlink()
    client.patch('/api/albums/'+a['id'],json={'title':'Album','artist':'Composer','notes':'Metadata only'}).raise_for_status()
    assert not cover.exists()
    assert not list(cover.parent.glob('*.jpg'))


def test_exact_name_collision_never_overwrites_existing_manual_art(client,settings,image_bytes):
    folder=settings.data/'Composer'/'Pookkal Vidum Thoothu (1983)'/'Covers';folder.mkdir(parents=True)
    file=folder/'Front.jpg';file.write_bytes(b'manually saved file; never touch')
    response=client.post('/api/import',data={'title':'Pookkal Vidum Thoothu','artist':'Composer','year':'1983'},files={'files':('front.jpg',image_bytes,'image/jpeg')})
    assert response.status_code==200,response.text
    imported=client.get('/api/albums/'+response.json()['album_id']).json()
    assert imported['folder']=='Composer/Pookkal Vidum Thoothu (1983)_1'
    assert file.read_bytes()==b'manually saved file; never touch'
    assert not (folder.parent/'Originals').exists()


def test_composer_case_change_and_legacy_suffix_removal_are_one_safe_move(client,settings,image_bytes):
    from coverly.archive_ops import relocate_album
    a=imported(client,image_bytes,'First')
    with client.app.state.sessions() as s:
        album=s.get(Album,a['id'])
        with relocate_album(s,settings,album,target_folder=album.folder+'--'+album.id[:12]):s.commit()
    plan=client.post('/api/artists/preview',json={'groups':[{'canonical':'Malaysia Vasudevan','names':['Malaysia vasudevan']}]}).json()
    client.post('/api/artists/plans/'+plan['plan_id']+'/apply').raise_for_status()
    Worker(settings,client.app.state.sessions).tick()
    after=client.get('/api/albums/'+a['id']).json()
    assert after['folder']=='Malaysia Vasudevan/First (YYYY)'
    assert 'Malaysia Vasudevan' in {p.name for p in settings.data.iterdir()}
    assert 'Malaysia vasudevan' not in {p.name for p in settings.data.iterdir()}
    assert within(settings.data,after['images'][0]['original']).read_bytes()==image_bytes
