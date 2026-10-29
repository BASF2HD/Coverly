import io
from pathlib import Path
import httpx
import pytest
from PIL import Image, ImageDraw
from fastapi.testclient import TestClient
from coverly.app import create_app
from coverly.config import Settings
from coverly.db import open_db
from coverly.network import SafeHTTP


@pytest.fixture
def settings(tmp_path):
    return Settings(storage=str(tmp_path / "archive"), request_delay=.25, auto_ocr=False, filesystem_sync=False)


@pytest.fixture
def db(settings):
    engine, sessions = open_db(settings.data)
    yield sessions
    engine.dispose()


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost:8000", headers={"X-Coverly-Request": "1"}) as client:
        yield client


def jpeg(width=640, height=640, color="#4e7951", label="CONTROLLED TEST ARTWORK"):
    im = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(im)
    draw.rectangle((width*.1, height*.2, width*.9, height*.8), outline="#ebd9ae", width=4)
    draw.text((width*.15, height*.45), label, fill="white")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=95)
    return out.getvalue()


@pytest.fixture
def image_bytes():
    return jpeg()


@pytest.fixture
def website(settings, monkeypatch):
    import coverly.network as network
    monkeypatch.setattr(network, "public_addresses", lambda host, port=443: ["93.184.216.34"])
    monkeypatch.setattr(SafeHTTP, "throttle", lambda *args: None)
    fixtures = Path(__file__).parent / "fixtures"
    pages = {
        "/robots.txt": (b"User-agent: *\nAllow: /\nDisallow: /private", "text/plain"),
        "/tamil/": ((fixtures / "category.html").read_bytes(), "text/html"),
        "/tamil/page/2/": ((fixtures / "page2.html").read_bytes(), "text/html"),
        "/product/nayakan/": ((fixtures / "product.html").read_bytes(), "text/html"),
        "/product/compilation/": ((fixtures / "compilation.html").read_bytes(), "text/html"),
        "/front-150x150.jpg": (jpeg(150, 150), "image/jpeg"),
        "/front.jpg": (jpeg(1200, 1200), "image/jpeg"),
        "/back.jpg": (jpeg(900, 1000, "#a34f3f", "TEST BACK ARTWORK"), "image/jpeg"),
        "/disc.jpg": (jpeg(800, 800, "#353a64", "TEST DISC A"), "image/jpeg"),
        "/bad.jpg": (b"not an image", "image/jpeg"),
    }
    visited = []
    def handler(request):
        path = request.url.path
        visited.append(path)
        body, mime = pages.get(path, (b"Missing", "text/plain"))
        return httpx.Response(200 if path in pages else 404, content=body, headers={"Content-Type": mime})
    def factory(cfg, domains):
        return SafeHTTP(cfg, domains, httpx.Client(transport=httpx.MockTransport(handler)))
    return factory, visited, pages
