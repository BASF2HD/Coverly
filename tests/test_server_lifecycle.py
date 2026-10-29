import os
import signal
import socket
import subprocess
import sys
import time
import httpx
import pytest


def test_server_stops_with_live_sse_connection(tmp_path):
    if os.name=='nt':
        pytest.skip('POSIX process-signal lifecycle test; native Windows spawn is tested separately')
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1',8000))==0:
            pytest.skip('Port 8000 is already serving a user archive')
    env={**os.environ,'COVERLY_HOME':str(tmp_path)}
    process=subprocess.Popen([sys.executable,'-m','coverly','run'],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    client=httpx.Client(base_url='http://localhost:8000',timeout=3,trust_env=False)
    try:
        ready=False
        for _ in range(80):
            if process.poll() is not None:
                raise AssertionError(process.stdout.read().decode())
            try:
                ready=client.get('/api/health').status_code==200
            except httpx.HTTPError:
                pass
            if ready:
                break
            time.sleep(.1)
        assert ready,'Server never became ready'
        with client.stream('GET','/api/events') as stream:
            assert stream.status_code==200
            assert next(stream.iter_lines()).startswith('data: ')
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=20)
        assert process.returncode==0
    finally:
        client.close()
        if process.poll() is None:
            process.kill();process.wait(timeout=5)
