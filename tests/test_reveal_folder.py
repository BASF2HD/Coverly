from pathlib import Path
import pytest

from coverly.file_manager import reveal


@pytest.mark.parametrize('system', ['Darwin','Windows','Linux'])
def test_native_reveal_uses_argument_list_for_exact_selected_file(tmp_path,monkeypatch,system):
    image=tmp_path/'Album & title (1987)'/'Front; keep.jpg'
    image.parent.mkdir()
    image.write_bytes(b'unchanged source')
    calls=[]
    monkeypatch.setattr('coverly.file_manager.platform.system',lambda:system)
    monkeypatch.setattr('coverly.file_manager.subprocess.Popen',lambda command,**kwargs:calls.append((command,kwargs)))
    reveal(image)
    expected={'Darwin':['open','-R',str(image)],'Windows':['explorer.exe','/select,'+str(image)],'Linux':['xdg-open',str(image.parent)]}
    assert calls[0][0]==expected[system]
    assert not calls[0][1].get('shell')
    assert image.read_bytes()==b'unchanged source'


def test_reveal_current_renamed_image_not_stale_original(client,settings,image_bytes,monkeypatch):
    from conftest import jpeg
    opened=[]
    monkeypatch.setattr('coverly.file_manager.reveal',lambda path:opened.append(path))
    album=client.post('/api/import',data={'title':'Album folder'},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    value=client.get('/api/albums/'+album).json()
    old=settings.data/value['images'][0]['cover_path']
    renamed=old.with_name('My improved Front.jpg')
    old.rename(renamed)
    renamed.write_bytes(jpeg(color='#ab5411'))
    before=renamed.read_bytes()
    settings.filesystem_sync=True
    current=client.get('/api/albums/'+album).json()['images'][0]
    response=client.post(f'/api/albums/{album}/reveal',json={'image_id':current['id']})
    assert response.status_code==200,response.text
    assert opened==[renamed]
    assert response.json()['folder']==str(renamed.parent)
    assert renamed.read_bytes()==before
    renamed.unlink()
    response=client.post(f'/api/albums/{album}/reveal',json={'image_id':current['id']})
    assert response.status_code==404
    assert len(opened)==1
    assert not renamed.exists() and not old.exists()


def test_reveal_scopes_to_album_and_handles_missing_manager(client,settings,image_bytes,monkeypatch):
    from conftest import jpeg
    albums=[]
    for title,color in [('First','red'),('Second','blue')]:
        albums.append(client.post('/api/import',data={'title':title},files={'files':('front.jpg',jpeg(color=color),'image/jpeg')}).json()['album_id'])
    first,second=[client.get('/api/albums/'+album).json() for album in albums]
    opened=[]
    monkeypatch.setattr('coverly.file_manager.reveal',lambda path:opened.append(path))
    assert client.post('/api/albums/'+first['id']+'/reveal',json={'image_id':second['images'][0]['id']}).status_code==404
    assert not opened
    assert client.post('/api/albums/'+first['id']+'/reveal',json={},headers={'Origin':'https://foreign.example'}).status_code==403
    assert not opened
    response=client.post('/api/albums/'+first['id']+'/reveal',json={})
    assert response.status_code==200
    assert opened==[settings.data/first['folder']]
    def fail(path):raise OSError('Controlled desktop unavailable')
    monkeypatch.setattr('coverly.file_manager.reveal',fail)
    assert client.post('/api/albums/'+first['id']+'/reveal',json={}).status_code==503
