from pathlib import Path
from sqlalchemy import select

from coverly.db import Album, FilesystemFile
from coverly.storage import folder_for,within


def imported(client,raw,title='Yaaro Ezhuthiya Kavithai',year=''):
    response=client.post('/api/import',data={'title':title,'artist':'Composer','year':year},files={'files':('Front.jpg',raw,'image/jpeg')})
    response.raise_for_status()
    return client.get('/api/albums/'+response.json()['album_id']).json()


def test_year_placeholder_changes_on_explicit_save_preserving_manually_edited_images(client,settings,image_bytes):
    value=imported(client,image_bytes)
    assert value['folder']=='Composer/Yaaro Ezhuthiya Kavithai (YYYY)'
    file=within(settings.data,value['images'][0]['cover_path'])
    from conftest import jpeg
    file.write_bytes(jpeg(label='MY MANUALLY RESTORED FRONT'))
    raw=file.read_bytes()
    settings.filesystem_sync=True
    current=client.get('/api/albums/'+value['id']).json()
    response=client.patch('/api/albums/'+value['id'],json={'title':value['title'],'artist':'Composer','year':1982,'move_folder':True})
    response.raise_for_status()
    after=client.get('/api/albums/'+value['id']).json()
    assert after['folder']=='Composer/Yaaro Ezhuthiya Kavithai (1982)'
    assert client.get(after['images'][0]['display_url']).content==raw
    assert not (settings.data/value['folder']).exists()
    assert within(settings.data,after['images'][0]['cover_path']).name==file.name
    with client.app.state.sessions() as s:
        rows=list(s.scalars(select(FilesystemFile).where(FilesystemFile.album_id==value['id'],FilesystemFile.present==True)))
        assert rows and all(row.path.startswith(after['folder']+'/') for row in rows)


def test_conflicts_use_stable_readable_suffixes_and_never_merge(client,settings,image_bytes):
    albums=[imported(client,image_bytes,year='1982') for _ in range(3)]
    assert [a['folder'] for a in albums]==['Composer/Yaaro Ezhuthiya Kavithai (1982)'+suffix for suffix in ['', '_1','_2']]
    assert len({a['id'] for a in albums})==3
    target=albums[1]
    for _ in range(2):
        client.patch('/api/albums/'+target['id'],json={'title':target['title'],'artist':'Composer','year':1982,'move_folder':True}).raise_for_status()
        assert client.get('/api/albums/'+target['id']).json()['folder']==target['folder']
    assert all(within(settings.data,a['folder']).is_dir() for a in albums)


def test_year_save_can_leave_folder_unchanged_when_user_disables_rename(client,settings,image_bytes):
    value=imported(client,image_bytes)
    client.patch('/api/albums/'+value['id'],json={'title':value['title'],'artist':'Composer','year':1982,'move_folder':False}).raise_for_status()
    after=client.get('/api/albums/'+value['id']).json()
    assert after['year']==1982 and after['folder']==value['folder']
    assert within(settings.data,value['folder']).is_dir()


def test_preview_allocates_unique_names_without_moving_files_then_apply_and_undo(client,settings,image_bytes):
    from coverly.archive_ops import relocate_album
    values=[imported(client,image_bytes,year='1982') for _ in range(2)]
    with client.app.state.sessions() as s:
        for value in values:
            album=s.get(Album,value['id'])
            with relocate_album(s,settings,album,target_folder=album.folder+'--'+album.id[:12]):s.commit()
    before=[client.get('/api/albums/'+a['id']).json() for a in values]
    plan=client.post('/api/rename/preview',json={'ids':[a['id'] for a in values]}).json()
    targets=[row['new'] for row in plan['items']]
    assert targets==['Composer/Yaaro Ezhuthiya Kavithai (1982)','Composer/Yaaro Ezhuthiya Kavithai (1982)_1']
    assert all(within(settings.data,a['folder']).is_dir() for a in before)
    applied=client.post('/api/rename/'+plan['plan_id']+'/apply').json()
    for target in targets:assert within(settings.data,target).is_dir()
    client.post('/api/rename/'+applied['operation_id']+'/undo').raise_for_status()
    assert all(within(settings.data,a['folder']).is_dir() for a in before)
