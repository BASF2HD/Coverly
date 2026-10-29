import httpx
import pytest
from coverly.network import canonical_url, public_addresses, SafeHTTP


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://user:pass@example.com/", "http://example.com:8080", "http://x/\nheader", "http://x\\@localhost/"])
def test_invalid_urls(url):
    with pytest.raises(ValueError):
        canonical_url(url)


@pytest.mark.parametrize("ip", ["127.0.0.1", "192.168.1.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "0.0.0.0", "100.64.0.1"])
def test_ssrf_ips(monkeypatch, ip):
    monkeypatch.setattr('socket.getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, "", (ip, 443))])
    with pytest.raises(ValueError):
        public_addresses("example.com")


def test_canonicalisation():
    assert canonical_url("https://EXAMPLE.COM:443/abc?b=2&utm_source=test&a=1#x") == "https://example.com/abc?a=1&b=2"


def test_robots_and_redirect_permission(settings, website):
    factory, visited, pages = website
    pages['/redirect'] = (b'', 'text/html')
    def handler(request):
        visited.append(request.url.path)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nDisallow: /private')
        return httpx.Response(302, headers={'location':'/private'})
    fetcher = SafeHTTP(settings, {'records.example'}, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match='robots'):
        fetcher.get('https://records.example/redirect')
    assert '/private' not in visited
    fetcher.close()


def test_limits_mime_and_domain(settings, website):
    factory, visited, pages = website
    fetcher = factory(settings, {'records.example'})
    with pytest.raises(ValueError, match='allowlist'):
        fetcher.get('https://evil.example/x.jpg', image=True)
    with pytest.raises(ValueError, match='MIME'):
        fetcher.get('https://records.example/tamil/', image=True)
    with pytest.raises(httpx.HTTPStatusError):
        fetcher.get('https://records.example/missing')
    fetcher.close()


def test_retries(settings, website, monkeypatch):
    _, _, _ = website
    monkeypatch.setattr('coverly.network.time.sleep', lambda *_: None)
    count = [0]
    def handler(request):
        if request.url.path == '/robots.txt':
            return httpx.Response(404)
        count[0] += 1
        return httpx.Response(503 if count[0]<3 else 200, text='done', headers={'Content-Type':'text/html'})
    f = SafeHTTP(settings, {'records.example'}, httpx.Client(transport=httpx.MockTransport(handler)))
    assert f.get('https://records.example/')[0] == b'done'
    assert count[0] == 3
    f.close()


@pytest.mark.parametrize('failing_path', ['/robots.txt', '/front.jpg'])
def test_disconnected_response_retries_including_robots(settings, website, monkeypatch, failing_path):
    monkeypatch.setattr('coverly.network.time.sleep', lambda *_: None)
    attempts = []
    def handler(request):
        attempts.append(request.url.path)
        if request.url.path == failing_path and attempts.count(failing_path) < 3:
            raise httpx.RemoteProtocolError('Server disconnected without sending a response.', request=request)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nAllow: /')
        return httpx.Response(200, content=b'original artwork', headers={'Content-Type':'image/jpeg'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        f = SafeHTTP(settings, {'records.example'}, client)
        assert f.get('https://records.example/front.jpg', image=True)[0] == b'original artwork'
        assert attempts.count(failing_path) == 3


def test_persistent_disconnect_is_bounded_and_actionable(settings, website, monkeypatch):
    monkeypatch.setattr('coverly.network.time.sleep', lambda *_: None)
    attempts = []
    def handler(request):
        attempts.append(request.url.path)
        raise httpx.RemoteProtocolError('Server disconnected without sending a response.', request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match='after 3 attempts'):
            SafeHTTP(settings, {'records.example'}, client).get('https://records.example/front.jpg', image=True)
    assert attempts == ['/robots.txt'] * 3
