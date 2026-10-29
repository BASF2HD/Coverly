"""Native desktop shell; the archive and resumable workers stay outside the app."""
from __future__ import annotations

import argparse
import contextlib
import json
import multiprocessing
import os
import platform
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = 'http://localhost:8000'
from . import __version__


def desktop_home(system=None, environ=None, home=None):
    env = os.environ if environ is None else environ
    if env.get('COVERLY_HOME'):
        return Path(env['COVERLY_HOME']).expanduser().resolve()
    home = Path.home() if home is None else Path(home)
    system = system or platform.system()
    if system == 'Darwin':
        return home / 'Library' / 'Application Support' / 'Coverly'
    if system == 'Windows':
        return Path(env.get('LOCALAPPDATA', home / 'AppData' / 'Local')) / 'Coverly'
    return Path(env.get('XDG_DATA_HOME', home / '.local' / 'share')) / 'Coverly'


def existing_server():
    # No proxy/environment forwarding for a local archive. A different service
    # on port 8000 must never be displayed as Coverly or terminated.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(URL + '/api/desktop-info', timeout=2) as response:
            info = json.load(response)
        if info.get('application') == 'coverly' and Path(info['storage']).is_absolute():
            return info
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        pass
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1', 8000)) == 0:
            raise RuntimeError('Port 8000 is already in use. Close the other service (or restart an older Coverly server) and open Coverly again.')
    return None


def select_storage(home, storage):
    """Persist only a chosen path; never copy or delete an existing archive."""
    home, storage = Path(home), Path(storage).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    path = home / 'config.json'
    config = json.loads(path.read_text('utf-8')) if path.exists() else {}
    config['storage'] = str(storage)
    temporary = home / 'config.desktop.tmp'
    temporary.write_text(json.dumps(config, indent=2), encoding='utf-8')
    temporary.replace(path)


def _backend(stop, port=8000):
    log = desktop_home() / 'desktop-server.log'
    # Frozen windowed applications have no console streams. Give the backend
    # and its spawn children real inherited descriptors for diagnostics.
    with log.open('a', buffering=1, encoding='utf-8') as stream:
        for descriptor in (1, 2):
            with contextlib.suppress(OSError):
                os.dup2(stream.fileno(), descriptor)
        sys.stdout = sys.stderr = stream
        from .runtime import run
        run(stop_event=stop, bind_port=port)


def self_test(report):
    """Exercise bundled resources, SQLite, imports and JPEG processing off-port."""
    from fastapi.testclient import TestClient
    from .app import create_app
    from .config import Settings
    from .restoration import LocalProvider
    from .embedding import square_jpeg
    from PIL import Image
    import io
    with tempfile.TemporaryDirectory(prefix='coverly-desktop-test-') as temp:
        cfg = Settings(storage=str(Path(temp) / 'archive'), auto_ocr=False)
        app = create_app(cfg)
        with TestClient(app, base_url=URL, headers={'X-Coverly-Request':'1'}) as client:
            assert client.get('/api/health').json()['application'] == 'coverly'
            assert client.get('/').status_code == 200
            assert 'Close all completed sections' in client.get('/static/app.js').text
            out = io.BytesIO()
            Image.new('RGB', (720, 760), '#d7ef7b').save(out, 'JPEG')
            raw = out.getvalue()
            result = client.post('/api/import', data={'title':'Desktop package test'}, files={'files':('test.jpg', raw, 'image/jpeg')})
            assert result.status_code == 200, result.text
            restored = LocalProvider().restore(raw, 'gentle', 800)
            exported, info = square_jpeg(restored, max_bytes=500_000)
            with Image.open(io.BytesIO(exported)) as image:
                assert image.width == image.height and image.format == 'JPEG'
            assert len(exported) < 500_000
            assert client.get('/api/browser-bridge').status_code == 200
    import webview
    value = {'ok':True, 'platform':platform.system(), 'architecture':platform.machine(), 'frozen':bool(getattr(sys, 'frozen', False)),
             'checks':['SQLite migrations', 'static interface', 'artwork import', 'CPU restoration', 'square JPEG under 500 KB', 'browser bridge', 'native webview import']}
    Path(report).write_text(json.dumps(value, indent=2), encoding='utf-8')


def backend_self_test(report):
    """Spawn the actual frozen server/workers on an isolated test port."""
    previous = os.environ.get('COVERLY_HOME')
    with tempfile.TemporaryDirectory(prefix='coverly-backend-test-') as temp:
        os.environ['COVERLY_HOME'] = temp
        from fastapi.testclient import TestClient
        from .app import create_app
        from .config import Settings
        from PIL import Image
        import io
        raw = io.BytesIO();Image.new('RGB',(512,512),'#d7ef7b').save(raw,'JPEG')
        with TestClient(create_app(Settings(storage=str(Path(temp)/'archive'),auto_ocr=False)), base_url=URL, headers={'X-Coverly-Request':'1'}) as client:
            album = client.post('/api/import',data={'title':'Frozen worker test'},files={'files':('front.jpg',raw.getvalue(),'image/jpeg')}).json()
            image = client.get('/api/albums/'+album['album_id']).json()['images'][0]
            job = client.post('/api/restorations',json={'ids':[image['id']],'preset':'gentle','size':512}).json()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        ctx = multiprocessing.get_context('spawn')
        stop = ctx.Event()
        process = ctx.Process(target=_backend, args=(stop, port))
        process.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        ready = False
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and process.is_alive():
                try:
                    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/health', headers={'Host':'localhost:8000'})
                    with opener.open(request, timeout=2) as response:
                        health = json.load(response)
                    if health.get('application') == 'coverly' and health.get('worker'):
                        request = urllib.request.Request(f'http://127.0.0.1:{port}/api/jobs', headers={'Host':'localhost:8000'})
                        with opener.open(request, timeout=2) as response:
                            jobs = json.load(response)
                        completed = next((j for j in jobs if j['id'] == job['id']), {})
                        if completed.get('status') == 'completed':
                            ready = True
                            break
                except OSError:
                    pass
                time.sleep(.2)
        finally:
            stop.set()
            process.join(timeout=90)
            if process.is_alive():
                process.terminate();process.join(timeout=10)
            if previous is None:
                os.environ.pop('COVERLY_HOME', None)
            else:
                os.environ['COVERLY_HOME'] = previous
        if not ready or process.exitcode != 0:
            log = Path(temp) / 'desktop-server.log'
            raise RuntimeError('Frozen backend lifecycle failed: ' + (log.read_text('utf-8')[-8000:] if log.exists() else 'no log'))
        Path(report).write_text(json.dumps({'ok':True, 'frozen':bool(getattr(sys,'frozen',False)),
            'checks':['native spawn server', 'native spawn workers', 'live HTTP health', 'background restoration job', 'cooperative shutdown']} , indent=2), encoding='utf-8')


def gui_self_test(report):
    """Render an isolated native test document, then close its own window."""
    import webview
    errors = []
    window = webview.create_window('Coverly package check', html='<html><title>Coverly native test</title><body><p id="check">Ready</p></body></html>', width=500, height=300)
    def check():
        try:
            assert window.evaluate_js('document.querySelector("#check").textContent') == 'Ready'
            Path(report).write_text(json.dumps({'ok':True,'platform':platform.system(),'architecture':platform.machine(),'checks':['native window creation','native web engine rendering','JavaScript bridge']}),encoding='utf-8')
        except Exception as exc:
            errors.append(exc)
        finally:
            window.destroy()
    window.events.loaded += check
    webview.start(private_mode=True)
    if errors:
        raise RuntimeError('Native window smoke test failed') from errors[0]


WELCOME = '''<!doctype html><html><meta charset="utf-8"><style>
body{background:#151812;color:#edf0e5;font:16px -apple-system,Segoe UI,sans-serif;margin:60px;max-width:650px}h1{font-size:32px}p{color:#b5bea3;line-height:1.6}button{background:#d7ef7b;color:#192012;border:1px solid #d7ef7b;border-radius:8px;padding:10px 16px;margin:10px 8px 0 0;cursor:pointer}button:focus-visible{outline:none;border-color:white}#status{white-space:pre-wrap}</style>
<h1>Welcome to Coverly.</h1><p>Your artwork stays in a folder you choose, separate from the application. Choose your existing archive folder (the one containing coverly.sqlite3), or start a new archive. Downloads resume from saved checkpoints when Coverly reopens.</p>
<button onclick="choose()">Choose existing archive…</button><button onclick="start()">Create new archive</button><p id="status"></p><script>
async function choose(){const r=await pywebview.api.choose_archive();if(r)document.getElementById('status').textContent=r;}
async function start(){document.getElementById('status').textContent='Starting your archive…';await pywebview.api.start_new();}
</script></html>'''


class DesktopSession:
    def __init__(self, home, webview):
        self.home, self.webview = home, webview
        self.process = self.stop = self.window = None
        self.started = False
        self.start_lock = threading.Lock()

    def choose_archive(self):
        if self.started:
            return None
        chosen = self.window.create_file_dialog(self.webview.FileDialog.FOLDER, directory=str(Path.home()))
        if not chosen:
            return None
        folder = Path(chosen[0])
        if not (folder / 'coverly.sqlite3').is_file():
            return 'Choose the archive folder containing coverly.sqlite3. No files have been changed.'
        select_storage(self.home, folder)
        self.start()
        return None

    def start_new(self):
        if not self.started:
            select_storage(self.home, self.home / 'archive')
            self.start()

    def start(self):
        with self.start_lock:
            if self.started:
                return
            self.started = True
        try:
            info = existing_server()
            if info:
                # Remember the already-open user's archive, not the build tree.
                select_storage(self.home, info['storage'])
            else:
                ctx = multiprocessing.get_context('spawn')
                self.stop = ctx.Event()
                self.process = ctx.Process(target=_backend, args=(self.stop,))
                self.process.start()
                deadline = time.monotonic() + 60
                while True:
                    if not self.process.is_alive():
                        raise RuntimeError('Coverly could not start. See desktop-server.log in ' + str(self.home))
                    try:
                        if existing_server():
                            break
                    except RuntimeError:
                        pass  # The owned server may be binding during startup.
                    if time.monotonic() > deadline:
                        raise RuntimeError('Coverly startup timed out. See desktop-server.log in ' + str(self.home))
                    time.sleep(.2)
            self.window.load_url(URL + '/?desktop=1&build='+__version__+'#library')
        except Exception as exc:
            import html
            self.window.load_html('<h2>Coverly could not open</h2><pre>' + html.escape(str(exc)) + '</pre>')

    def close(self):
        if self.stop:
            self.stop.set()
        if self.process:
            # The coordinator waits for workers, then resets interrupted items.
            # Never terminate a server that was running before this window.
            self.process.join(timeout=90)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=15)


class WelcomeBridge:
    """Welcome actions and a user-selected folder handoff for the native document."""
    def __init__(self, session):
        self._session = session

    def choose_archive(self):
        return self._session.choose_archive()

    def start_new(self):
        return self._session.start_new()

    def choose_artwork_folder(self):
        # WKWebView's pywebview file-input delegate handles files, not directory
        # inputs. Use the public native folder dialog and hand only the selected
        # images back to Coverly's normal upload/validation path.
        def local():
            return (self._session.window.get_current_url() or '').startswith(URL + '/')
        if not self._session.started or not local():
            return {'error':'Open Coverly before choosing artwork.'}
        chosen=self._session.window.create_file_dialog(self._session.webview.FileDialog.FOLDER,directory=str(Path.home()))
        if not chosen:return None
        if not local():return {'error':'The Coverly window changed. Choose the folder again.'}
        import base64,mimetypes
        root=Path(chosen[0]).resolve()
        allowed={'.jpg','.jpeg','.png','.webp','.tif','.tiff','.bmp'}
        paths=sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in allowed
                     and not any(part.startswith('.') for part in p.relative_to(root).parts)
                     and root in p.resolve().parents)
        if len(paths)>100:return {'error':'Choose at most 100 images for one album.'}
        if sum(p.stat().st_size for p in paths)>256*1024*1024:
            return {'error':'Choose a smaller batch (up to 256 MB) for the native folder handoff.'}
        files=[]
        for path in paths:
            files.append({'name':path.name,'relative':root.name+'/'+path.relative_to(root).as_posix(),
                          'modified':round(path.stat().st_mtime*1000),'type':mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                          'data':base64.b64encode(path.read_bytes()).decode('ascii')})
        return {'folder':root.name,'files':files}


def main():
    parser = argparse.ArgumentParser(description='Coverly desktop')
    parser.add_argument('--self-test-report', type=Path)
    parser.add_argument('--backend-self-test-report', type=Path)
    parser.add_argument('--gui-self-test-report', type=Path)
    args = parser.parse_args()
    home = desktop_home()
    if args.self_test_report:
        self_test(args.self_test_report)
        return
    if args.backend_self_test_report:
        backend_self_test(args.backend_self_test_report)
        return
    if args.gui_self_test_report:
        gui_self_test(args.gui_self_test_report)
        return
    home.mkdir(parents=True, exist_ok=True)
    os.environ['COVERLY_HOME'] = str(home)
    # Finder and Windows shortcuts often start with a minimal PATH.
    if platform.system() == 'Darwin':
        os.environ['PATH'] = os.environ.get('PATH', '') + os.pathsep + '/opt/homebrew/bin' + os.pathsep + '/usr/local/bin'
    import webview
    webview.settings['ALLOW_DOWNLOADS'] = True
    session = DesktopSession(home, webview)
    session.window = webview.create_window('Coverly', html=WELCOME, width=1400, height=920, min_size=(900, 640), js_api=WelcomeBridge(session))
    def ready():
        try:
            if (home / 'config.json').exists() or existing_server():
                session.start()
        except Exception as exc:
            import html
            session.window.load_html('<h2>Coverly could not open</h2><pre>' + html.escape(str(exc)) + '</pre>')
    try:
        webview.start(ready, private_mode=False, storage_path=str(home / 'webview'))
    finally:
        session.close()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
