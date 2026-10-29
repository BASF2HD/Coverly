"""Source-backed film credits and soundtrack editions; never fetches audio/lyrics."""
from __future__ import annotations
import hashlib,json,re,time,os,csv,io,html as html_module
from urllib.parse import quote,urlencode,urlsplit
from difflib import SequenceMatcher
from bs4 import BeautifulSoup
from .metadata import source_metadata,UNKNOWN
from .network import SafeHTTP,canonical_url
from .storage import atomic_write
from .db import now

COMPOSER={'music','musicdirector','musicdirection','composer','composers','musicby','musiccomposedby','composedby','musicalscore','scoreby'}
YEAR={'year','releaseyear','albumreleasingyear','released','releasedate','release','datepublished','albumyear'}

def clean(value):
    soup=BeautifulSoup(str(value or ''),'html.parser')
    for tag in soup.select('sup,.reference,.sortkey'):tag.decompose()
    return re.sub(r'\s+',' ',soup.get_text(' ',strip=True)).strip(' \"“”')

def key(value):return re.sub(r'[^\w]','',clean(value).casefold())


def provider_rank(doc):
    host=(urlsplit(doc.get('source_url','')).hostname or '').lower()
    if host in {'music.apple.com','itunes.apple.com'}:return 0
    if host in {'open.spotify.com','api.spotify.com'}:return 1
    if host.endswith('.wikipedia.org'):return 2
    return 3

def film_title(value):
    title=source_metadata({'title':str(value)})['title']
    title=re.split(r'\s+(?:Tamil|Telugu|Hindi|Malayalam|Kannada)\s+(?:Film|Movie)\b',title,1,flags=re.I)[0]
    title=re.split(r'\s+(?:story\s*(?:and|&)\s*dialogues|original\s+(?:motion\s+picture\s+)?soundtrack)\b',title,1,flags=re.I)[0]
    title=re.split(r'\s*\((?:original|motion picture|soundtrack|film|19\d\d|20\d\d)',title,1,flags=re.I)[0]
    title=re.split(r'\s+[–—]\s+',title,1)[0]
    return title.strip(' –—-')

def same_title(a,b):
    a,b=key(film_title(a)),key(film_title(b))
    # Long/short vowels and doubled t vary in Tamil retail transliterations.
    roman=lambda text:re.sub(r'([aeiout])\1+',r'\1',text)
    return bool(a and b and (a==b or (min(len(a),len(b))>=10 and roman(a)==roman(b)) or (abs(len(a)-len(b))<=2 and SequenceMatcher(None,a,b).ratio()>=.94)))

def title_variants(title):
    """Bounded transliteration alternatives; do not broaden to unrelated shop words."""
    variants=[title]
    for old,new in [('idhu','ithu'),('adhu','athu')]:
        candidate=re.sub(r'\b'+old+r'\b',new,title,flags=re.I)
        if candidate!=title:variants.append(candidate)
    expanded=re.sub(r'\bpadum\b','Paadum',title,flags=re.I)
    expanded=re.sub(r'\bpa(?:t|tt)u\b','Paattu',expanded,flags=re.I)
    if expanded not in variants:variants.append(expanded)
    return variants


def spelling_match(a,b,year,release_year):
    if same_title(a,b):return True
    # Catalogues sometimes vary the initial Tamil transliteration (Idhu / Ithu / Ethu).
    # Require every remaining title word, and the requested year when supplied.
    if year and year!=release_year:return False
    a,b=film_title(a).casefold().split(),film_title(b).casefold().split()
    return len(a)>=3 and len(a)==len(b) and a[1:]==b[1:] and bool(re.fullmatch(r'[ie][dt]hu',a[0]) and re.fullmatch(r'[ie][dt]hu',b[0]))



def year_of(value):
    match=re.search(r'\b(18\d{2}|19\d{2}|20\d{2})\b',str(value or ''))
    return int(match[1]) if match else None

def document(title,url,provider,edition='Film / soundtrack reference'):
    return dict(title=film_title(title),source_url=canonical_url(url),provider=provider,edition=edition,
        composer='',album_artist='',film_year=None,release_year=None,tracks=[],retrieved_at=now(),warnings=[])

def track(number,title,**values):
    return dict(number=number,title=clean(title)[:500],lyrics=clean(values.get('lyrics',''))[:500],
        singers=clean(values.get('singers',''))[:1000],performers=clean(values.get('performers',''))[:1000],
        length=clean(values.get('length',''))[:40],source_url=values.get('source_url',''))

def copied_table(title,url,text='',html='',composer='',year=None):
    """Explicit user import for sites whose page/API cannot be read automatically."""
    if len(text)+len(html)>500000:raise ValueError('Paste at most 500,000 characters')
    if html:
        # Use only copied tables, never execute supplied markup or scripts.
        tables=BeautifulSoup(html,'html.parser').select('table')
        content=''.join(str(t) for t in tables)
    else:
        rows=list(csv.reader(io.StringIO(text),delimiter='\t' if '\t' in text else '|'))
        content='<table>'+''.join('<tr>'+''.join('<td>'+html_module.escape(cell)+'</td>' for cell in row)+'</tr>' for row in rows)+'</table>'
    doc=extract_html('<h1>'+html_module.escape(title)+'</h1>'+content,url,title)
    if not doc:doc=document(title,url,'Copied source table')
    if composer:doc['composer']=clean(composer)[:200]
    if year:doc['film_year']=year
    if not doc['tracks'] and not doc['composer']:raise ValueError('No credits found. Copy the track table including its Title, Lyrics, Singer(s) and Length headers, or enter the credited composer.')
    doc['provider']='Copied source table (user supplied)'
    doc['warnings'].append('Imported from text you supplied. Check it against the linked source before saving.')
    return doc

def table_grid(table):
    """Expand row/column spans so shared lyricist cells stay on correct songs."""
    carried={};rows=[]
    for row in table.select('tr'):
        line={};next_carried={}
        for column,(text,left) in carried.items():
            line[column]=text
            if left>1:next_carried[column]=(text,left-1)
        column=0
        for cell in row.find_all(['th','td'],recursive=False):
            while column in line:column+=1
            text=clean(str(cell))
            def span_value(name,maximum):
                try:return min(maximum,max(1,int(cell.get(name,1))))
                except (ValueError,TypeError):return 1
            span=span_value('colspan',20);height=span_value('rowspan',200)
            for offset in range(span):
                line[column+offset]=text
                if height>1:next_carried[column+offset]=(text,height-1)
            column+=span
        carried=next_carried
        if line:rows.append([line.get(i,'') for i in range(max(line)+1)])
    return rows

def extract_html(html,url,expected_title=None):
    soup=BeautifulSoup(html,'html.parser')
    headings=[clean(h.get_text(' ',strip=True)) for h in soup.select('h1')]
    # Public album pages often put their identity and tracks in JSON-LD.
    structured=[]
    def collect(value):
        if isinstance(value,list):
            for item in value:collect(item)
        elif isinstance(value,dict):
            if value.get('@type') in ('Movie','MusicAlbum'):structured.append(value)
            for field in ('@graph','mainEntity'):collect(value.get(field,[]))
    for script in soup.select('script[type="application/ld+json"]'):
        try:collect(json.loads(script.get_text()))
        except (ValueError,TypeError):pass
    headings += [clean(v.get('name','')) for v in structured if v.get('name')]
    title=next((h for h in headings if expected_title and same_title(h,expected_title)),headings[-1] if headings else expected_title or '')
    if expected_title and not same_title(title,expected_title):return None
    doc=document(title,url,urlsplit(url).hostname or 'Source')
    album_page=any(v.get('@type')=='MusicAlbum' for v in structured) and not any(v.get('@type')=='Movie' for v in structured)
    album_page=album_page or (urlsplit(url).hostname or '') in {'music.apple.com','open.spotify.com','music.amazon.com','www.qobuz.com'}
    date_field='release_year' if album_page else 'film_year'
    if expected_title and key(film_title(title))!=key(film_title(expected_title)):
        doc['warnings'].append('Tamil title spelling differs from the archive. Check the film and edition before saving.')
    for row in soup.select('table tr'):
        cells=row.find_all(['th','td'],recursive=False)
        if len(cells)!=2:continue
        label,value=key(cells[0].get_text(' ',strip=True)),clean(str(cells[1]))
        if label in COMPOSER and value:doc['composer']=value[:200]
        if label in YEAR:doc[date_field]=doc[date_field] or year_of(value)
    for row in soup.select('.crew-group'):
        role=row.select_one('.role');names=row.select_one('.name')
        if role and names and key(role.get_text()) in COMPOSER:doc['composer']=clean(str(names))[:200]
    for term in soup.select('dt'):
        value=term.find_next_sibling('dd')
        if value and key(term.get_text()) in COMPOSER:doc['composer']=clean(str(value))[:200]
        if value and key(term.get_text()) in YEAR:doc[date_field]=doc[date_field] or year_of(value.get_text())
    # Short, labelled music-credit blocks (heading/list/paragraph); never "Director" alone.
    for node in soup.select('h2,h3,h4,p,li'):
        text=clean(node.get_text(' ',strip=True))
        match=re.fullmatch(r'(?:Music(?: Director| By)?|Composer|Composed By)\s*[:–—-]\s*(.{2,120})',text,re.I)
        if match and not doc['composer']:doc['composer']=match[1]
    # Structured film credit roles. Album artists remain distinct from composers.
    def visit(value):
        if isinstance(value,list):
            for item in value:visit(item)
        elif isinstance(value,dict):
            kind=value.get('@type',[]);kind=[kind] if isinstance(kind,str) else kind
            if expected_title and any(k in kind for k in ('Movie','MusicAlbum')) and value.get('name') and not same_title(value['name'],expected_title):return
            if any(k in kind for k in ('Movie','MusicAlbum','MusicComposition','MusicRecording')):
                def names(raw):
                    raw=raw if isinstance(raw,list) else [raw]
                    return ', '.join(clean(x.get('name','') if isinstance(x,dict) else x) for x in raw if x)
                credit=value.get('musicBy') or value.get('composer')
                if credit and not doc['composer']:doc['composer']=names(credit)[:200]
                if 'Movie' in kind:doc['film_year']=doc['film_year'] or year_of(value.get('datePublished'))
                elif not doc['release_year']:doc['release_year']=year_of(value.get('datePublished') or value.get('dateReleased'))
                if value.get('byArtist'):doc['album_artist']=names(value['byArtist'])[:200]
                if 'MusicAlbum' in kind and (not expected_title or same_title(value.get('name',title),expected_title)):
                    songs=value.get('track') or value.get('tracks') or value.get('hasPart') or []
                    if isinstance(songs,dict):songs=songs.get('itemListElement',songs.get('item',[]))
                    if not isinstance(songs,list):songs=[songs]
                    for index,song in enumerate(songs[:100]):
                        if not isinstance(song,dict):continue
                        number=song.get('position',index+1);song=song.get('item',song)
                        if not isinstance(song,dict) or not song.get('name'):continue
                        duration=str(song.get('duration',''));m=re.fullmatch(r'PT(?:(\d+)M)?(?:(\d+)S)?',duration)
                        if m:duration=f'{int(m[1] or 0)}:{int(m[2] or 0):02}'
                        doc['tracks'].append(track(number,song['name'],performers=names(song.get('byArtist')),length=duration,source_url=url))
            for name in ('@graph','mainEntity'):visit(value.get(name,[]))
    for value in structured:visit(value)
    # Some sources publish only a MusicComposition at the root.
    if not structured:
        for script in soup.select('script[type="application/ld+json"]'):
            try:visit(json.loads(script.get_text()))
            except (ValueError,TypeError):pass
    structured_tracks=doc['tracks'];doc['tracks']=[]
    shared_lyricist=''
    # Wikipedia often credits the lyricist once above the table rather than
    # repeating it in a Lyrics column. Restrict this to the soundtrack section.
    heading=next((h for h in soup.select('h2,h3') if 'soundtrack' in key(h.get_text())),None)
    if heading:
        section=heading.parent if heading.parent.name=='section' else heading
        paragraphs=list(section.select('p')) if section.name=='section' else []
        if not paragraphs:
            for node in heading.next_elements:
                if getattr(node,'name',None) in {'h2','table'}:break
                if getattr(node,'name',None)=='p':paragraphs.append(node)
        for paragraph in paragraphs:
            text=clean(paragraph.get_text(' ',strip=True))
            match=re.search(r'(?:all\s+(?:the\s+)?)?lyrics\s+(?:were\s+|was\s+)?(?:written|penned)\s+by\s+([^.;]+)',text,re.I)
            if match:shared_lyricist=match[1].strip()[:200]
    for table in soup.select('table'):
        grid=table_grid(table);headers=None
        for row in grid:
            labels=[key(x) for x in row]
            if any(x in {'title','song','songtitle','track','tracktitle'} for x in labels) and any(x in {'lyrics','lyricist','lyricists','singers','singer','length','duration'} for x in labels):
                headers=labels;continue
            if not headers or len(row)!=len(headers):continue
            values=dict(zip(headers,row));name=next((values[k] for k in ('title','songtitle','tracktitle','song','track') if values.get(k)),'')
            if not name or re.search(r'^total\s+length',name,re.I):continue
            number=values.get('no') or values.get('number') or str(len(doc['tracks'])+1)
            if not re.search(r'\d',number):continue
            doc['tracks'].append(track(number.strip('.'),name,lyrics=values.get('lyrics') or values.get('lyricist') or values.get('lyricists') or shared_lyricist,
                singers=values.get('singers') or values.get('singer'),performers=values.get('performers') or values.get('creditedperformers'),length=values.get('length') or values.get('duration'),source_url=url))
        if doc['tracks']:break
    if not doc['tracks']:doc['tracks']=structured_tracks
    # Moviebuff lists labelled roles per song, separately from its film cast/crew.
    if not doc['tracks']:
        for row in soup.select('#tracklist .track')[:100]:
            title_node=row.select_one('.track-detail a')
            if not title_node:continue
            text=clean(title_node.get_text());match=re.match(r'(\d+)\s+(.+)',text)
            if not match:continue
            values={}
            for detail in row.select('.details'):
                label,_,value=detail.get_text(' ',strip=True).partition(':');values[key(label)]=value
            doc['tracks'].append(track(int(match[1]),match[2],singers=values.get('playbacksinger'),lyrics=values.get('lyricist') or values.get('lyrics'),source_url=url))
    if not doc['film_year'] and not album_page:
        doc['film_year']=year_of(next((h for h in headings if same_title(h,title)),''))
    if doc['tracks'] or doc['composer'] or doc['album_artist'] or doc['film_year']:return doc
    return None

def apple_credits(html):
    soup=BeautifulSoup(html,'html.parser');script=soup.select_one('#serialized-server-data')
    out={'composers':[],'lyricists':[],'singers':[],'performers':[],'track_id':None,'title':''}
    if not script:return out
    data=json.loads(script.get_text())
    for outer in data.get('data',[]):
        identity=outer.get('intent',{}).get('contentDescriptor',{}).get('identifiers',{})
        out['track_id']=str(identity.get('storeAdamID',''))
        for section in outer.get('data',{}).get('sections',[]):
            if section.get('id')=='songDetail':out['title']=next((i.get('title','') for i in section.get('items',[])), '')
            if section.get('id') not in {'performer','composer','composer-and-lyrics'}:continue
            for item in section.get('items',[]):
                name=item.get('name','');roles={key(r) for r in item.get('roleNames',[])}
                for label,accepted in [('composers',{'composer','musicdirector'}),('lyricists',{'lyrics','lyricist'}),('singers',{'vocals','vocalist','singer'}),('performers',{'performer','vocals','vocalist','singer'})]:
                    if name and roles & accepted and name not in out[label]:out[label].append(name)
    return out

class MetadataLookup:
    def __init__(self,settings,fetcher_factory=SafeHTTP,progress=None):
        self.settings=settings;self.factory=fetcher_factory;self.progress=progress or (lambda message,docs:None)
        self.errors=[];self.documents=[];self.fetchers={}
    def get(self,url):
        host=urlsplit(url).hostname
        if host not in self.fetchers:self.fetchers[host]=self.factory(self.settings,{host})
        return self.fetchers[host].get(url)[0]
    def close(self):
        for fetcher in self.fetchers.values():fetcher.close()
    def add(self,doc):
        if doc and (doc.get('composer') or doc.get('tracks') or doc.get('album_artist')):
            if any(d['source_url']==doc['source_url'] for d in self.documents):return
            self.documents.append(doc);self.progress('Found '+doc['provider']+' soundtrack metadata',self.documents)
    def run(self,title,year=None,source_url=None,refresh=False,source_only=False,search_web=False):
        title=film_title(title)
        cache=self.settings.data/'.metadata-cache'/('v10-'+hashlib.sha256((key(title)+':'+str(year)+':'+str(source_url)+':'+str(source_only)+':'+str(search_web)).encode()).hexdigest()+'.json')
        if not refresh and cache.is_file() and time.time()-cache.stat().st_mtime<86400:
            try:
                data=json.loads(cache.read_text(encoding='utf-8'))
                if data.get('documents'):return data['documents'],data.get('errors',[])
            except (ValueError,OSError):pass
        try:
            if source_url:
                source_url=canonical_url(source_url)
                self.progress('Reading your selected metadata source',self.documents)
                try:
                    html=self.get(source_url).decode('utf-8',errors='replace')
                    doc=extract_html(html,source_url,title)
                    if not doc and source_only:
                        doc=extract_html(html,source_url)
                        if doc:doc['warnings'].append('This selected reference uses a different title from the archive. Confirm the film before using these details.')
                    if doc and (source_only or not year or not doc['film_year'] or year==doc['film_year']):
                        if year and doc['film_year'] and year!=doc['film_year']:
                            doc['warnings'].append('The reference film year differs from the current album year. Review this correction before saving.')
                        self.add(doc)
                    else:self.errors.append('Selected source did not publish matching readable credits. Check its title spelling or import a copied table.')
                except Exception as exc:self.errors.append('Selected source: '+str(exc)[:220])
            if source_only:return self.documents,self.errors
            self.progress('Looking up film music credits for '+title,self.documents)
            self.apple(title,year)
            if os.environ.get('SPOTIFY_ACCESS_TOKEN'):
                self.spotify(title,year)
            elif not any(d.get('tracks') for d in self.documents):
                self.spotify_public(title,year)
            # Wikipedia is optional: refused pages are reported, never bypassed.
            wiki_titles=[title]+([f'{title} ({year} film)'] if year else [])
            for wiki_title in wiki_titles:
                url='https://en.wikipedia.org/wiki/'+quote(wiki_title.replace(' ','_'),safe='_')
                try:
                    doc=extract_html(self.get(url).decode('utf-8',errors='replace'),url,title)
                    if doc and (not year or not doc['film_year'] or year==doc['film_year']):
                        self.add(doc);break
                except Exception as exc:
                    self.errors.append('Wikipedia: '+str(exc)[:220])
                    # Do not repeat requests when the source refuses automated access.
                    if '403' in str(exc) or 'robots' in str(exc).casefold():break
            if not any('wikipedia.org' in d['source_url'] for d in self.documents) and not any('Wikipedia:' in e and ('403' in e or 'robots' in e.casefold()) for e in self.errors):
                self.wikipedia_search(title,year)
            if self.settings.search_provider in {'brave','google'} and (search_web or not any(d['tracks'] and d['composer'] and d['film_year'] and all(t['singers'] for t in d['tracks']) for d in self.documents)):
                self.web_sources(title,year)
            elif search_web:
                self.errors.append('Automatic web-wide search needs a configured Brave/Google provider. Use Search and import sources to bring a page from your browser without an API key.')
            if not any(d.get('film_year') for d in self.documents):
                for variant in title_variants(title):
                    url='https://www.moviebuff.com/'+quote(re.sub(r'\s+','-',variant.casefold()),safe='-')
                    try:
                        doc=extract_html(self.get(url).decode('utf-8',errors='replace'),url,title)
                        if doc and (not year or not doc['film_year'] or year==doc['film_year']):
                            self.add(doc);break
                    except Exception as exc:self.errors.append('Moviebuff: '+str(exc)[:220])
            film_years={d.get('film_year') for d in self.documents if d.get('film_year')}
            composers={key(d['composer']) for d in self.documents if d.get('composer')}
            if len(film_years)>1 or len(composers)>1:
                for doc in self.documents:doc['warnings'].append('Sources differ. This title may have multiple films or editions; compare year and composer before applying.')
            self.documents.sort(key=provider_rank)
            if self.documents:
                atomic_write(cache,json.dumps({'documents':self.documents,'errors':self.errors},ensure_ascii=False).encode())
            return self.documents,self.errors
        finally:self.close()

    def wikipedia_search(self,title,year):
        """Find the real article title instead of relying on a guessed URL slug."""
        try:
            self.progress('Searching Wikipedia for Tamil film title variants',self.documents)
            url='https://en.wikipedia.org/w/api.php?'+urlencode({'action':'query','list':'search','srsearch':title+' Tamil film','srlimit':5,'format':'json','utf8':1})
            rows=json.loads(self.get(url)).get('query',{}).get('search',[])
            for row in [r for r in rows if same_title(r.get('title',''),title)][:2]:
                link='https://en.wikipedia.org/wiki/'+quote(row['title'].replace(' ','_'),safe='_')
                doc=extract_html(self.get(link).decode('utf-8',errors='replace'),link,title)
                if doc and (not year or not doc['film_year'] or doc['film_year']==year):self.add(doc)
        except Exception as exc:
            if not any(e.startswith('Wikipedia:') for e in self.errors):self.errors.append('Wikipedia: '+str(exc)[:220])

    def web_sources(self,title,year):
        try:
            from .search import search
            self.progress('Searching the web for additional film and song credits',self.documents)
            for result in search(self.settings,title,purpose='metadata')[:6]:
                try:
                    url=result['url'];doc=extract_html(self.get(url).decode('utf-8',errors='replace'),url,title)
                    if doc and (not year or not doc['film_year'] or doc['film_year']==year):self.add(doc)
                except Exception as exc:self.errors.append((result.get('source') or 'Web source')+': '+str(exc)[:160])
        except Exception as exc:self.errors.append('Web metadata search: '+str(exc)[:180])
    def apple(self,title,year):
        try:
            self.progress('Finding published soundtrack releases',self.documents)
            results=[]
            for variant in title_variants(title):
                url='https://itunes.apple.com/search?'+urlencode({'term':variant,'entity':'album','country':'IN','limit':15})
                results.extend(json.loads(self.get(url)).get('results',[]))
                if any(same_title(a.get('collectionName',''),title) for a in results):break
            results=list({a.get('collectionId'):a for a in results}.values())
            matches=[a for a in results if spelling_match(a.get('collectionName',''),title,None,None)]
            if not matches:
                self.progress('Searching Tamil movie soundtrack catalogue',self.documents)
                for variant in title_variants(title):
                    url='https://itunes.apple.com/search?'+urlencode({'term':variant+' Tamil movie soundtrack','entity':'album','country':'IN','limit':15})
                    rows=json.loads(self.get(url)).get('results',[])
                    matches=[a for a in rows if spelling_match(a.get('collectionName',''),title,None,None)]
                    if matches:break
            matches.sort(key=lambda a:('soundtrack' not in a.get('collectionName','').casefold(),not same_title(a.get('collectionName',''),title)))
            for album in matches[:2]:
                ident=album.get('collectionId')
                if not isinstance(ident,int):continue
                url='https://itunes.apple.com/lookup?'+urlencode({'id':ident,'entity':'song','country':'IN'})
                rows=json.loads(self.get(url)).get('results',[])
                doc=document(album.get('collectionName') or title,album['collectionViewUrl'],'Apple Music','Published digital soundtrack release')
                doc.update(album_artist=album.get('artistName',''),release_year=year_of(album.get('releaseDate')),collection_id=ident,
                    release_title=album.get('collectionName',''))
                if key(film_title(album.get('collectionName','')))!=key(film_title(title)):
                    doc['warnings'].append('Title spelling differs. Matched by transliteration and remaining title words; confirm this edition and year.')
                doc['warnings'].append('This source provides song credits, not a verified film year. Its digital release date is ignored.')
                for row in rows:
                    if row.get('wrapperType')!='track' or row.get('collectionId')!=ident:continue
                    seconds=round(row.get('trackTimeMillis',0)/1000)
                    doc['tracks'].append(track(row.get('trackNumber',len(doc['tracks'])+1),row.get('trackName',''),performers=row.get('artistName',''),length=f'{seconds//60}:{seconds%60:02}' if seconds else '',source_url=row.get('trackViewUrl','')))
                    doc['tracks'][-1]['track_id']=row.get('trackId')
                self.add(doc)
                composers=[]
                for index,song in enumerate(doc['tracks'][:30]):
                    ident=song.get('track_id')
                    if not isinstance(ident,int):continue
                    self.progress(f'Reading song credits {index+1} of {min(30,len(doc["tracks"]))}',self.documents)
                    credit_url=f'https://music.apple.com/us/song/{ident}'
                    try:
                        credits=apple_credits(self.get(credit_url).decode('utf-8',errors='replace'))
                        if credits['track_id']!=str(ident) or not same_title(credits['title'],song['title']):continue
                        song.update(lyrics=', '.join(credits['lyricists']),singers=', '.join(credits['singers']),
                            performers=', '.join(credits['performers']) or song['performers'],credits_url=credit_url)
                        for name in credits['composers']:
                            if name not in composers:composers.append(name)
                        song['composers']=credits['composers']
                        doc['track_composers']=list(composers)
                        # Song-credit feeds sometimes tag film directors as composers.
                        # Propose only names also credited on this album; retain the
                        # complete per-song credits for inspection rather than unioning them.
                        album_names=[key(n) for n in re.split(r'[,;&]',doc['album_artist'])]
                        supported=[name for name in composers if key(name) in album_names]
                        doc['composer']=' & '.join(supported)
                        doc['composer_basis']='Track composer also credited on this soundtrack album'
                    except Exception as exc:doc['warnings'].append(f'Credits unavailable for {song["title"]}: {str(exc)[:120]}')
                    self.progress(f'Read song credits {index+1} of {min(30,len(doc["tracks"]))}',self.documents)
                unmatched=[name for name in composers if name not in (doc.get('composer') or '').split(' & ')]
                if unmatched:doc['warnings'].append('Additional track composition credits are not confirmed as the film music composer: '+', '.join(unmatched)+'. See individual song sources.')
                if len(doc['tracks'])>30:doc['warnings'].append('Detailed credits are limited to the first 30 tracks per lookup.')
        except Exception as exc:self.errors.append('Apple Music: '+str(exc)[:220])

    def spotify(self,title,year):
        """Optional authorised catalogue. Album artists are never labelled composers."""
        token=os.environ.get('SPOTIFY_ACCESS_TOKEN')
        host='api.spotify.com'
        fetcher=self.factory(self.settings,{host})
        self.fetchers[host]=fetcher
        def request(path,params=None):
            url='https://'+host+'/v1/'+path+('?' + urlencode(params) if params else '')
            raw,_,_=fetcher._request(url,3*1024*1024,headers={'Authorization':'Bearer '+token})
            return json.loads(raw)
        try:
            self.progress('Checking authorised Spotify catalogue',self.documents)
            rows=request('search',{'q':'album:'+title,'type':'album','limit':10,'market':'IN'}).get('albums',{}).get('items',[])
            for album in [a for a in rows if same_title(a.get('name',''),title)][:2]:
                ident=album.get('id','')
                if not re.fullmatch(r'[A-Za-z0-9]+',ident):continue
                detail=request('albums/'+ident,{'market':'IN'})
                doc=document(title,detail['external_urls']['spotify'],'Spotify','Published digital release; artist roles supplied by Spotify')
                doc.update(album_artist=', '.join(a['name'] for a in detail.get('artists',[])),release_year=year_of(detail.get('release_date')))
                for song in detail.get('tracks',{}).get('items',[]):
                    seconds=round(song.get('duration_ms',0)/1000)
                    doc['tracks'].append(track(song.get('track_number'),song.get('name',''),performers=', '.join(a['name'] for a in song.get('artists',[])),length=f'{seconds//60}:{seconds%60:02}',source_url=song.get('external_urls',{}).get('spotify','')))
                if detail.get('tracks',{}).get('next'):doc['warnings'].append('This release has additional tracks; the first page is shown.')
                self.add(doc)
        except Exception:self.errors.append('Spotify catalogue unavailable; check the local access token and account eligibility.')

    def spotify_public(self,title,year):
        """Read public album links where Spotify exposes them, without account data."""
        try:
            self.progress('Checking public Spotify album matches',self.documents)
            url='https://open.spotify.com/search/'+quote(title+' Tamil movie',safe='')+'/albums'
            soup=BeautifulSoup(self.get(url).decode('utf-8',errors='replace'),'html.parser')
            links=[]
            for anchor in soup.select('a[href]'):
                href=anchor.get('href','')
                if re.fullmatch(r'(?:https://open\.spotify\.com)?/album/[A-Za-z0-9]+',href):
                    target='https://open.spotify.com'+href if href.startswith('/') else href
                    if target not in links:links.append(target)
            for target in links[:3]:
                doc=extract_html(self.get(target).decode('utf-8',errors='replace'),target,title)
                self.add(doc)
            if not links:self.errors.append('Spotify public search does not expose readable album results here. The authorised Spotify catalogue can be configured; other sources continue.')
        except Exception as exc:self.errors.append('Spotify public catalogue: '+str(exc)[:180])
