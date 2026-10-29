import copy
from sqlalchemy import select
from coverly.db import Album,Artist,JobItem,Job
from coverly.jobs import Worker
from coverly.bulk_metadata import merge_documents
from coverly.soundtracks import MetadataLookup,document,track


def sources(title='Example Film'):
    apple=document(title,'https://music.apple.com/album/123','Apple Music')
    apple.update(composer='Composer One',release_year=2024,tracks=[track(1,'First Song',performers='Published Singer',length='4:20')])
    wiki=document(title,'https://en.wikipedia.org/wiki/Example_Film','Wikipedia')
    wiki.update(composer='Composer One',film_year=1982,tracks=[track(1,'First Song',lyrics='Lyricist One',singers='Singer One',length='4:20')])
    return [apple,wiki]


def seed(client,count=55):
    with client.app.state.sessions() as s:
        for index in range(count):
            s.add(Album(title='Example Film '+str(index),artist='Unknown Artist',folder=f'Unknown Artist/Film {index} (YYYY)',review=True))
        s.commit()
        return list(s.scalars(select(Album.id)))


def test_all_pages_selection_matches_filters_and_bulk_import_leaves_files_untouched(client,settings,monkeypatch):
    ids=seed(client)
    with client.app.state.sessions() as s:
        first=s.get(Album,ids[0]);first.title='Example Film'
        folder=settings.data/first.folder/'Covers';folder.mkdir(parents=True)
        artwork=folder/'My restored front.jpg';artwork.write_bytes(b'MY MANUALLY RESTORED ARTWORK')
        s.commit()
    assert client.get('/api/albums?review=true').json()['total']==55
    selection=client.get('/api/albums/ids?review=true').json()
    assert len(selection['ids'])==55
    assert client.get('/api/albums/ids?q=Film%2054&review=true').json()['total']==1
    def lookup(self,title,*args,**kwargs):return sources(title),[]
    monkeypatch.setattr(MetadataLookup,'run',lookup)
    response=client.post('/api/metadata/bulk',json={'ids':selection['ids']})
    assert response.status_code==200,response.text
    job=response.json()
    assert client.post('/api/metadata/bulk',json={'ids':ids}).json()['id']==job['id']
    worker=Worker(settings,client.app.state.sessions,quantum=1)
    worker.tick()
    status=client.get('/api/jobs/'+job['id']).json()
    assert status['progress']==1 and status['status']=='queued'  # Yield after one album.
    while worker.tick():pass
    result=client.get('/api/jobs/'+job['id']).json()
    assert result['metadata_applied']==55 and result['metadata_unresolved']==0
    value=client.get('/api/albums/'+ids[0]).json()
    assert value['artist']=='Composer One' and value['year']==1982 and value['review']
    assert value['folder'].startswith('Unknown Artist/') and '(YYYY)' in value['folder']
    assert value['soundtrack']['tracks'][0]['singers']=='Singer One'
    assert value['soundtrack']['field_sources']['film_year'].startswith('https://en.wikipedia.org/')
    assert artwork.read_bytes()==b'MY MANUALLY RESTORED ARTWORK'
    assert not (folder.parent/'album.json').exists()


def test_merge_prefers_apple_tracks_and_never_digital_year_or_album_artist_as_composer():
    docs=sources()
    merged=merge_documents(list(reversed(docs)),'Example Film')
    assert merged['provider']=='Apple Music'
    assert merged['film_year']==1982 and merged['release_year']==2024
    assert merged['tracks'][0]['lyrics']=='Lyricist One'
    only=copy.deepcopy(docs[0]);only['composer']='';only['album_artist']='Singer One'
    incomplete=merge_documents([only],'Example Film')
    assert not incomplete['composer'] and incomplete['film_year'] is None
    docs[1]['composer']='Different Composer'
    assert merge_documents(docs,'Example Film')['conflict']


def test_manual_edits_and_conflicting_sources_are_not_overwritten(client,settings,monkeypatch):
    ids=seed(client,2)
    job=client.post('/api/metadata/bulk',json={'ids':ids}).json()
    with client.app.state.sessions() as s:
        album=s.get(Album,ids[0]);album.notes='I reviewed this while the lookup was queued';album.review=False;s.commit()
    visits=[]
    def lookup(self,title,*args,**kwargs):
        visits.append(title)
        docs=sources(title);docs[1]['composer']='Conflicting Composer';return docs,[]
    monkeypatch.setattr(MetadataLookup,'run',lookup)
    Worker(settings,client.app.state.sessions).tick()
    assert len(visits)==1
    assert client.get('/api/albums/'+ids[0]).json()['review'] is False
    assert all(client.get('/api/albums/'+ident).json()['artist']=='Unknown Artist' for ident in ids)
    result=client.get('/api/jobs/'+job['id']).json()
    assert result['metadata_applied']==0 and result['metadata_unresolved']==2


def test_paused_bulk_job_does_not_run_or_save_until_resumed(client,settings,monkeypatch):
    ids=seed(client,1)
    job=client.post('/api/metadata/bulk',json={'ids':ids}).json()
    client.post('/api/jobs/'+job['id']+'/pause').raise_for_status()
    calls=[]
    monkeypatch.setattr(MetadataLookup,'run',lambda self,title,*a,**k:(calls.append(title) or sources(title),[]))
    worker=Worker(settings,client.app.state.sessions)
    assert not worker.tick() and not calls
    client.post('/api/jobs/'+job['id']+'/resume').raise_for_status()
    assert worker.tick() and calls


def test_requested_provider_order_apple_spotify_wikipedia_then_google(settings,monkeypatch):
    visits=[]
    class Lookup(MetadataLookup):
        def apple(self,title,year):visits.append('Apple Music');self.add(sources(title)[0])
        def spotify(self,title,year):visits.append('Spotify')
        def get(self,url):
            assert 'wikipedia.org' in url
            visits.append('Wikipedia')
            return b'<h1>Example Film</h1><table><tr><th>Music by</th><td>Composer One</td></tr><tr><th>Release date</th><td>1982</td></tr></table>'
        def web_sources(self,title,year):visits.append('Google')
    monkeypatch.setenv('SPOTIFY_ACCESS_TOKEN','controlled-test-token')
    settings=settings.model_copy(update={'search_provider':'google'})
    Lookup(settings).run('Example Film',refresh=True)
    assert visits==['Apple Music','Spotify','Wikipedia','Google']


def test_selection_and_queue_support_more_than_one_thousand_albums(client):
    ids=seed(client,1005)
    assert client.get('/api/albums/ids?review=true').json()['total']==1005
    result=client.post('/api/metadata/bulk',json={'ids':ids})
    assert result.status_code==200 and result.json()['total']==1005
