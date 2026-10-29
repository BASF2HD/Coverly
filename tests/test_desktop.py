"""Native shell path selection, archive reuse and owned backend lifecycle."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from coverly.desktop import desktop_home, select_storage, DesktopSession, WelcomeBridge


@pytest.mark.parametrize('system,expected', [
    ('Darwin', 'Library/Application Support/Coverly'),
    ('Windows', 'AppData/Local/Coverly'),
    ('Linux', '.local/share/Coverly'),
])
def test_desktop_data_lives_outside_bundle(tmp_path, system, expected):
    assert desktop_home(system, {}, tmp_path) == tmp_path / expected
    assert desktop_home(system, {'COVERLY_HOME':str(tmp_path / 'portable')}, tmp_path) == tmp_path / 'portable'


def test_windows_and_linux_user_data_overrides(tmp_path):
    assert desktop_home('Windows', {'LOCALAPPDATA':str(tmp_path)}, Path('/unused')) == tmp_path / 'Coverly'
    assert desktop_home('Linux', {'XDG_DATA_HOME':str(tmp_path)}, Path('/unused')) == tmp_path / 'Coverly'


def test_existing_archive_choice_preserves_config_and_originals(tmp_path):
    home = tmp_path / 'settings';home.mkdir()
    (home / 'config.json').write_text(json.dumps({'jpg_quality':91,'auto_ocr':False}))
    archive = tmp_path / 'external drive' / 'archive';archive.mkdir(parents=True)
    original = archive / 'original.jpg';original.write_bytes(b'preserve-original')
    select_storage(home, archive)
    assert json.loads((home / 'config.json').read_text()) == {'storage':str(archive),'jpg_quality':91,'auto_ocr':False}
    assert original.read_bytes() == b'preserve-original'


def test_attach_existing_server_never_starts_or_stops_it(tmp_path, monkeypatch):
    from coverly import desktop
    archive = tmp_path / 'existing archive'
    monkeypatch.setattr(desktop, 'existing_server', lambda:{'application':'coverly','storage':str(archive)})
    urls = []
    session = DesktopSession(tmp_path / 'settings', None)
    session.window = SimpleNamespace(load_url=urls.append)
    session.start();session.start();session.close()
    assert session.process is None and session.stop is None
    from coverly import __version__
    assert urls == ['http://localhost:8000/?desktop=1&build='+__version__+'#library']
    assert json.loads((tmp_path / 'settings/config.json').read_text())['storage'] == str(archive)


def test_welcome_api_cannot_change_archive_after_start(tmp_path):
    session = DesktopSession(tmp_path, None);session.started = True
    bridge = WelcomeBridge(session)
    assert bridge.choose_archive() is None
    assert bridge.start_new() is None
    assert not (tmp_path / 'config.json').exists()


def test_closing_owned_backend_signals_checkpoint_shutdown(tmp_path):
    calls = []
    session = DesktopSession(tmp_path, None)
    session.stop = SimpleNamespace(set=lambda:calls.append('stop'))
    session.process = SimpleNamespace(join=lambda timeout:calls.append(('join',timeout)), is_alive=lambda:False)
    session.close()
    assert calls == ['stop',('join',90)]


def test_desktop_info_points_to_actual_archive(client, settings):
    assert client.get('/api/desktop-info').json()['storage'] == str(settings.data)
    assert client.get('/api/desktop-info',headers={'Host':'evil.example'}).status_code == 403


def test_native_folder_handoff_transfers_only_chosen_images(tmp_path):
    import base64
    folder=tmp_path/'Album';folder.mkdir()
    (folder/'Front.jpg').write_bytes(b'front-original')
    (folder/'Back.png').write_bytes(b'back-original')
    (folder/'.hidden.jpg').write_bytes(b'excluded')
    (folder/'notes.txt').write_text('not artwork')
    window=SimpleNamespace(get_current_url=lambda:'http://localhost:8000/#library',create_file_dialog=lambda *args,**kw:[str(folder)])
    session=DesktopSession(tmp_path,SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=20)))
    session.started=True;session.window=window
    result=WelcomeBridge(session).choose_artwork_folder()
    assert result['folder']=='Album' and len(result['files'])==2
    assert {f['name']:base64.b64decode(f['data']) for f in result['files']}=={'Front.jpg':b'front-original','Back.png':b'back-original'}
    window.get_current_url=lambda:'https://unrelated.example/'
    assert WelcomeBridge(session).choose_artwork_folder().get('error')
