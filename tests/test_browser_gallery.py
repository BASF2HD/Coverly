import pytest
from coverly.extraction import render_page, GenericAdapter


def test_javascript_rendered_gallery(settings):
    pytest.importorskip('playwright.sync_api')
    class ControlledFetcher:
        def get(self,url):
            return b'''<html><body><div class="product-detail"><h1>Controlled JS album</h1><div id="gallery"></div></div><script>document.getElementById('gallery').innerHTML='<img alt="front" data-large_image="https://records.example/front.jpg" src="https://records.example/thumb.jpg"><img alt="back" src="https://records.example/back.jpg">';</script></body></html>''',url
    try:
        html=render_page('https://records.example/product/test/',ControlledFetcher())
    except Exception as exc:
        if 'Executable doesn\'t exist' in str(exc):
            pytest.skip('Chromium not installed on this host')
        raise
    result=GenericAdapter().extract(html,'https://records.example/product/test/')
    assert len(result['images'])==2
    assert result['images'][0]['url'].endswith('/front.jpg')
