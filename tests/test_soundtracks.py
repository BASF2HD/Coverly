import json
from urllib.parse import urlsplit,parse_qs
import pytest
from coverly.soundtracks import extract_html,apple_credits,MetadataLookup,same_title
from coverly.jobs import Worker
from coverly.storage import within


def credits_html(identifier='42',title='First Song'):
    # Minimal public credit structure, excluding artwork, analytics and unrelated albums.
    return '<script id="serialized-server-data" type="application/json">'+json.dumps({'data':[{
        'intent':{'contentDescriptor':{'identifiers':{'storeAdamID':identifier}}},
        'data':{'sections':[
            {'id':'songDetail','items':[{'title':title}]},
            {'id':'performer','items':[{'name':'Singer One','roleNames':['Vocals']},{'name':'Actor One','roleNames':['Actor']}]},
            {'id':'composer-and-lyrics','items':[{'name':'Composer One','roleNames':['Composer']},{'name':'Lyricist One','roleNames':['Lyrics']}]},
            {'id':'more-by-artist','items':[{'name':'Unrelated Composer','roleNames':['Composer']}]}
        ]}}]})+'</script>'


@pytest.mark.parametrize('label',['Music','Music Director','Composer','Music by','Music composed by'])
def test_music_roles_and_shared_track_credits(label):
    html=f'''<h1>Example Film (1985)</h1><table><tr><th>Director</th><td>Film Director</td></tr>
    <tr><th>{label}</th><td>Composer One<sup>[1]</sup></td></tr></table>
    <table><tr><th>No.</th><th>Title</th><th>Lyrics</th><th>Singer(s)</th><th>Length</th></tr>
    <tr><td>1.</td><td>First song</td><td rowspan="2">Lyricist One</td><td>Singer One</td><td>4:20</td></tr>
    <tr><td>2.</td><td>Second song</td><td>Singer Two</td><td>3:40</td></tr></table>'''
    d=extract_html(html,'https://films.example/example','Example Film')
    assert d['composer']=='Composer One' and d['film_year']==1985
    assert [(s['lyrics'],s['singers'],s['length']) for s in d['tracks']]==[('Lyricist One','Singer One','4:20'),('Lyricist One','Singer Two','3:40')]
    assert extract_html(html,'https://films.example/example','Different Film') is None


def test_roles_are_not_inferred_from_cast_or_album_artist():
    html='<h1>Example Film</h1><table><tr><th>Director</th><td>Director Name</td></tr><tr><th>Lyricist</th><td>Writer Name</td></tr></table>'
    assert extract_html(html,'https://films.example/example','Example Film') is None
    d=extract_html('<h1>Example Film</h1><script type="application/ld+json">'+json.dumps({'@type':'MusicAlbum','byArtist':{'name':'Singer One'},'datePublished':'1985-01-01'})+'</script>','https://films.example/example')
    assert d['album_artist']=='Singer One' and not d['composer']
    c=apple_credits(credits_html())
    assert c['composers']==['Composer One'] and c['lyricists']==['Lyricist One']
    assert c['singers']==['Singer One'] and 'Actor One' not in c['performers']
    assert same_title('Unnai Thedi Varuven','Unnai Thedi Varuven (Original Motion Picture Soundtrack) - EP')
    assert not same_title('Unnai Thedi Varuven','Unnai Thedi')


def lookup_factory(visits,wrong_identity=False):
    class Fetcher:
        def __init__(self,settings,domains):pass
        def close(self):pass
        def get(self,url):
            visits.append(url);p=urlsplit(url)
            if p.hostname in {'www.moviebuff.com','en.wikipedia.org'}:raise ValueError('Source access refused')
            if p.path=='/search':
                data={'results':[{'collectionName':'Different Film','collectionId':8},
                    {'collectionName':'Example Film (Original Motion Picture Soundtrack)','collectionId':7,'collectionViewUrl':'https://music.apple.com/album/7','artistName':'Composer One','releaseDate':'1985-01-01'}]}
            elif p.path=='/lookup':
                assert parse_qs(p.query)['id']==['7']
                data={'results':[{'wrapperType':'collection','collectionId':7},
                    {'wrapperType':'track','collectionId':7,'trackNumber':1,'trackId':42,'trackName':'First Song','artistName':'Singer One','trackTimeMillis':240000},
                    {'wrapperType':'track','collectionId':8,'trackId':99,'trackName':'Wrong album'}]}
            elif p.hostname=='music.apple.com':return credits_html('99' if wrong_identity else '42').encode(),url
            else:raise AssertionError(url)
            return json.dumps(data).encode(),url
    return Fetcher


def test_public_provider_fallback_progress_and_cache(settings):
    visits=[];updates=[]
    lookup=MetadataLookup(settings,lookup_factory(visits),lambda msg,docs:updates.append((msg,len(docs))))
    docs,errors=lookup.run('Example Film')
    assert len(docs)==1 and len(errors)==2
    doc=docs[0]
    assert doc['composer']=='Composer One' and doc['release_year']==1985
    assert len(doc['tracks'])==1 and doc['tracks'][0]['lyrics']=='Lyricist One'
    assert doc['tracks'][0]['length']=='4:00' and doc['tracks'][0]['singers']=='Singer One'
    assert any('song credits 1 of 1' in m for m,n in updates)
    count=len(visits)
    assert MetadataLookup(settings,lookup_factory(visits)).run('Example Film')[0]==docs
    assert len(visits)==count
    bad,_=MetadataLookup(settings.model_copy(update={'storage':str(settings.data/'other')}),lookup_factory([],True)).run('Example Film')
    assert not bad[0]['composer'] and not bad[0]['tracks'][0]['lyrics']


def test_lookup_save_tracks_move_and_reopen(client,settings,image_bytes):
    visits=[]
    album=client.post('/api/import',data={'title':'Example Film'},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    other=client.post('/api/import',data={'title':'Another Film'},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    job=client.post('/api/albums/'+album+'/lookup').json()
    assert client.post('/api/albums/'+album+'/lookup').json()['id']==job['id']
    before=client.get('/api/albums/'+album).json()
    worker=Worker(settings,client.app.state.sessions,lookup_factory(visits));worker.tick()
    detail=client.get('/api/albums/'+album).json()
    assert detail['metadata_lookup']['status']=='completed'
    candidate=detail['soundtrack_candidates'][0]
    assert detail['metadata_suggestions'][0]['value']=='Composer One'
    choice={'job_id':candidate['job_id'],'index':candidate['index']}
    r=client.patch('/api/albums/'+other,json={'title':'Another Film','soundtrack_choice':choice})
    assert r.status_code==422
    r=client.patch('/api/albums/'+album,json={'title':'Example Film','artist':'Composer One','year':1985,'soundtrack_choice':choice,'move_folder':True,'metadata_version':before['metadata_version']})
    assert r.status_code==200,r.text
    saved=client.get('/api/albums/'+album).json()
    assert saved['soundtrack']['tracks'][0]['lyrics']=='Lyricist One'
    assert saved['folder'].startswith('Composer One/') and not saved['review']
    manifest=json.loads(within(settings.data,saved['folder']+'/album.json').read_text())
    assert manifest['soundtrack']==saved['soundtrack']
    assert client.get('/api/images/'+saved['images'][0]['id']+'/original').content==image_bytes
    # Ordinary metadata edits preserve the saved track table.
    r=client.patch('/api/albums/'+album,json={'title':'Example Film','artist':'Composer One','year':1985})
    assert r.status_code==200
    assert client.get('/api/albums/'+album).json()['soundtrack']==saved['soundtrack']
    # A previously open form cannot overwrite a newly selected edition.
    r=client.patch('/api/albums/'+album,json={'title':'Example Film','metadata_version':before['metadata_version']})
    assert r.status_code==409


def test_tamil_transliteration_variants_still_require_matching_title():
    from coverly.soundtracks import title_variants
    assert same_title('Puyal Padum Paattu','Puyal Paadum Paattu (Original Motion Picture Soundtrack)')
    assert 'Puyal Paadum Paattu' in title_variants('Puyal Padum Paattu')
    assert not same_title('Puyal Padum Paattu','Puyal Oru Paattu')


def test_public_structured_album_imports_tracks_without_inventing_composer(settings):
    data={'@type':'MusicAlbum','name':'Puyal Paadum Paattu','byArtist':[{'name':'Singer One'}],
          'track':{'@type':'ItemList','itemListElement':[{'@type':'ListItem','position':1,'item':{'@type':'MusicRecording','name':'First Song','duration':'PT4M21S','byArtist':{'name':'Singer One'}}}]}}
    html='<script type="application/ld+json">'+json.dumps(data)+'</script>'
    class Fetcher:
        def __init__(self,*args):pass
        def close(self):pass
        def get(self,url):return html.encode(),url
    docs,errors=MetadataLookup(settings,Fetcher).run('Puyal Padum Paattu',source_url='https://open.spotify.com/album/example',source_only=True)
    assert len(docs)==1 and not errors
    assert docs[0]['tracks'][0]['length']=='4:21' and docs[0]['tracks'][0]['performers']=='Singer One'
    assert not docs[0]['composer'] and not docs[0]['tracks'][0]['singers']


def test_copied_table_is_a_reviewable_suggestion_and_does_not_change_album(client,image_bytes):
    album=client.post('/api/import',data={'title':'Puyal Padum Paattu'},files={'files':('cover.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    response=client.post('/api/albums/'+album+'/metadata-import',json={'title':'Puyal Padum Paattu','source_url':'https://en.wikipedia.org/wiki/Puyal_Paadum_Paattu','composer':'Ilaiyaraaja','year':1987,
        'text':'No.\tTitle\tLyrics\tSinger(s)\tLength\n1\tSong One\tLyricist One\tSinger One\t4:20\n2\tSong Two\tLyricist Two\tSinger Two\t3:50'})
    assert response.status_code==200,response.text
    detail=client.get('/api/albums/'+album).json()
    assert detail['artist']=='Unknown Artist' and not detail['soundtrack']
    candidate=detail['soundtrack_candidates'][0]
    assert len(candidate['document']['tracks'])==2 and candidate['document']['tracks'][1]['singers']=='Singer Two'
    assert candidate['document']['warnings']
    response=client.patch('/api/albums/'+album,json={'title':detail['title'],'artist':'Ilaiyaraaja','year':1987,'soundtrack_choice':{'job_id':candidate['job_id'],'index':candidate['index']}})
    assert response.status_code==200,response.text
    assert len(client.get('/api/albums/'+album).json()['soundtrack']['tracks'])==2


def test_album_page_dates_are_never_used_as_film_years():
    html='<h1>Example Film (1985)</h1><table><tr><th>Release date</th><td>2024-04-12</td></tr></table><script type="application/ld+json">'+json.dumps({'@type':'MusicAlbum','name':'Example Film','byArtist':{'name':'Artist'}})+'</script>'
    doc=extract_html(html,'https://open.spotify.com/album/example','Example Film')
    assert doc['film_year'] is None and doc['release_year']==2024


def test_browser_page_import_returns_complete_reviewable_table(client,image_bytes):
    album=client.post('/api/import',data={'title':'Example Film'},files={'files':('cover.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    html='<h1>Example Film</h1><table><tr><th>Music by</th><td>Composer One</td></tr><tr><th>Release date</th><td>1985</td></tr></table><table><tr><th>No.</th><th>Title</th><th>Lyrics</th><th>Singer(s)</th><th>Length</th></tr><tr><td>1</td><td>First Song</td><td>Writer</td><td>Singer</td><td>4:21</td></tr></table>'
    response=client.post('/api/albums/'+album+'/metadata-page',json={'source_url':'https://en.wikipedia.org/wiki/Example_Film','html':html})
    assert response.status_code==200,response.text
    doc=response.json()['document'];assert doc['film_year']==1985 and doc['tracks'][0]['singers']=='Singer'
    detail=client.get('/api/albums/'+album).json()
    assert detail['artist']=='Unknown Artist' and not detail['soundtrack']
    assert detail['soundtrack_candidates'][0]['document']['composer']=='Composer One'
    assert client.post('/api/albums/'+album+'/metadata-page',json={'source_url':'https://films.example/other','html':'<h1>Another Film</h1><p>Music by: Someone</p>'}).status_code==400


def test_partial_apple_results_do_not_skip_wider_configured_search(settings,monkeypatch):
    searched=[]
    lookup=MetadataLookup(settings.model_copy(update={'search_provider':'brave'}),lookup_factory([]))
    def web(title,year):searched.append(title)
    monkeypatch.setattr(lookup,'web_sources',web)
    lookup.run('Example Film',refresh=True)
    assert searched==['Example Film'] # Apple has tracks/composer, but no film-year evidence.


def test_wikipedia_table_import_includes_paragraph_lyricist_and_film_year(client,image_bytes,settings):
    from pathlib import Path
    html=(Path(__file__).parent/'fixtures/vaai-kozhuppu.html').read_text()
    url='https://en.wikipedia.org/wiki/Vaai_Kozhuppu'
    doc=extract_html(html,url,'Vaai Kozhuppu')
    assert doc['composer']=='Chandrabose' and doc['film_year']==1989
    assert len(doc['tracks'])==5
    assert all(song['lyrics']=='Vairamuthu' for song in doc['tracks'])
    assert doc['tracks'][0]['singers']=='Vani Jairam'
    class Fetcher:
        def __init__(self,*args):pass
        def get(self,requested):assert requested==url;return html.encode(),url
        def close(self):pass
    documents,errors=MetadataLookup(settings,Fetcher).run('Vaikkozhuppu and En Kanavar',1980,source_url=url,source_only=True,refresh=True)
    assert len(documents)==1 and not errors
    assert documents[0]['film_year']==1989 and len(documents[0]['warnings'])>=2
    album=client.post('/api/import',data={'title':'Vaai Kozhuppu'},files={'files':('front.jpg',image_bytes,'image/jpeg')}).json()['album_id']
    result=client.post('/api/albums/'+album+'/metadata-page',json={'source_url':url,'html':html}).json()
    candidate=client.get('/api/albums/'+album).json()['soundtrack_candidates'][0]
    client.patch('/api/albums/'+album,json={'title':'Vaai Kozhuppu','artist':'Chandrabose','year':1989,'soundtrack_choice':{'job_id':candidate['job_id'],'index':candidate['index']}}).raise_for_status()
    saved=client.get('/api/albums/'+album).json()
    assert saved['soundtrack']['tracks']==doc['tracks']
    assert not saved['soundtrack_candidates'] and not saved['metadata_suggestions']
    # The user can explicitly ask for another reference after accepting one.
    client.post('/api/albums/'+album+'/metadata-page',json={'source_url':url,'html':html}).raise_for_status()
    assert client.get('/api/albums/'+album).json()['soundtrack_candidates']
