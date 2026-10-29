import json
import pytest
from coverly.config import Settings
from coverly.search import search, media_type


def test_site_search(settings, monkeypatch):
    calls=[]
    class Fetcher:
        def __init__(self,*args):pass
        def get(self,url):
            calls.append(url)
            return b'<ul class="products"><li class="product"><a class="woocommerce-LoopProduct-link" href="/product/album/"><img src="/front.jpg"><h2>Nayakan Tamil vinyl LP record</h2></a></li></ul>',url
        def close(self):pass
    monkeypatch.setattr('coverly.search.SafeHTTP',Fetcher)
    results=search(settings,'Nayakan')
    assert len(results)==1 and results[0]['media_type']=='lp'
    assert 'post_type=product' in calls[0]
    assert results[0]['dimensions'] is None
    assert media_type('Nayakan Tamil audio cassette')=='other'


def test_brave_header_no_secrets_persisted(settings, monkeypatch):
    calls=[]
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','test-secret')
    class Fetcher:
        def __init__(self,*args):pass
        def _request(self,url,limit,headers):
            calls.append((url,headers))
            return json.dumps({'web':{'results':[{'url':'https://example.com/product/test/','title':'Tamil LP sleeve','description':'Vinyl record','thumbnail':{'src':'https://example.com/front.jpg'}}]}}).encode(),{},url
        def close(self):pass
    monkeypatch.setattr('coverly.search.SafeHTTP',Fetcher)
    settings.search_provider='brave'
    result=search(settings,'Nayakan')
    assert len(result)==1
    assert 'test-secret' not in calls[0][0]
    assert calls[0][1]['X-Subscription-Token']=='test-secret'
    assert 'test-secret' not in json.dumps(result)


def test_missing_api_credentials_explicit(settings,monkeypatch):
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY',raising=False)
    settings.search_provider='brave'
    with pytest.raises(ValueError,match='BRAVE_SEARCH_API_KEY'):
        search(settings,'Nayakan')
