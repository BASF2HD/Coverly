"""Artist alias proposals are reviewable. Only approved aliases affect new imports."""
import re,json
from collections import defaultdict
from sqlalchemy import select,func
from .db import Artist,ArtistAlias,Album,Audit
from .storage import available_folder,within


def key(value):return re.sub(r'[\W_]+','',value.casefold())

CURATED=[{'canonical':'Ilaiyaraaja','aliases':['Ilaiyaraaja','Ilaiyaraaja _Xerox Label_','Ilaiyaraja','Ilayaaaja','Ilayaraaja','Ilayaraaja 10203','Ilayaraaja.jpg 11366','Ilayaraja','Illaiyaraaja','Illayaraja','Illayaraja'],
    'source_url':'https://www.youtube.com/@ilaiyaraajaofficial','evidence':'Official artist channel uses Ilaiyaraaja. These source spellings are proposals until you approve.'},
    {'canonical':'K. Chakravarthy','aliases':['Chakravarthi','Chakaravarthy','Chakravarthy','K. Chakravarthy'],
    'source_url':'https://music.apple.com/us/song/1434913737','evidence':'Apple Music credits K. Chakravarthy as composer. Confirm this is the same person before merging.'}]


def approved_name(session,name):
    alias=session.get(ArtistAlias,key(name))
    artist=session.get(Artist,alias.canonical_id) if alias else None
    return artist.name if artist else name


def proposals(session):
    counts=dict(session.execute(select(Album.artist,func.count()).where(Album.deleted_at.is_(None)).group_by(Album.artist)).all())
    used=set();groups=[]
    for curated in CURATED:
        names=[n for n in counts if key(n) in {key(a) for a in curated['aliases']}]
        if any(n!=curated['canonical'] for n in names):
            groups.append({**curated,'names':[{'name':n,'albums':counts[n]} for n in names]});used.update(names)
    remaining=defaultdict(list)
    for n in counts:
        if n in used or key(n) in {'unknown','unknownartist','various','variousartists'}:continue
        cleaned=re.sub(r'\s+\d+$','',re.sub(r'\.(?:jpg|jpeg|png)(?:\s+\d+)?$','',n,flags=re.I)).strip()
        remaining[key(cleaned)].append(n)
    for names in remaining.values():
        if len(names)<2:continue
        canonical=re.sub(r'\s+\d+$','',re.sub(r'\.(?:jpg|jpeg|png)(?:\s+\d+)?$','',max(names,key=lambda n:counts[n]),flags=re.I)).strip()
        groups.append({'canonical':canonical,'names':[{'name':n,'albums':counts[n]} for n in names],
                       'source_url':'','evidence':'Punctuation / case / trailing inventory-number similarity only. Verify identity and choose the correct name.'})
    return groups


def preview(session,settings,groups):
    rows=[];seen=set();unchanged=0;reserved=set()
    for group in groups:
        target=str(group.get('canonical','')).strip()
        if not 1<=len(target)<=200:raise ValueError('Choose a canonical artist name from 1 to 200 characters')
        names=group.get('names',[])
        if not isinstance(names,list) or len(names)>500:raise ValueError('Invalid alias selection')
        for album in session.scalars(select(Album).where(Album.deleted_at.is_(None),Album.artist.in_(names))):
            if album.id in seen:raise ValueError('An album is included in more than one artist group')
            seen.add(album.id)
            from types import SimpleNamespace
            new=available_folder(session,settings,SimpleNamespace(id=album.id,title=album.title,artist=target,year=album.year,folder=album.folder if album.artist==target else ''),reserved)
            if album.artist==target and album.folder==new and within(settings.data,album.folder).is_dir():
                unchanged+=1
                continue
            rows.append({'id':album.id,'title':album.title,'old_artist':album.artist,'artist':target,'year':album.year,'old_folder':album.folder,'folder':new})
    audit=Audit(action='artist_normalization_preview',detail={'groups':groups,'rows':rows})
    session.add(audit);session.flush()
    return {'plan_id':audit.id,'album_count':len(rows),'rows':rows,'groups':groups,'already_correct':unchanged}
