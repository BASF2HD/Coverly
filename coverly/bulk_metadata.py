"""Priority-based metadata fusion. Never treats a digital date as a film year."""
import copy
import hashlib
import json
from urllib.parse import urlsplit
from .soundtracks import provider_rank,same_title,key
from .metadata import UNKNOWN
from .db import Artist,Audit,now
from sqlalchemy import select


def fingerprint(album):
    value={field:getattr(album,field) for field in ('title','artist','year','soundtrack','notes','review')}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def merge_documents(documents,title):
    docs=sorted([d for d in documents if same_title(d.get('title',''),title)],key=provider_rank)
    if not docs:return None
    composers={key(d['composer']) for d in docs if d.get('composer')}
    # Only an actual film-reference date is eligible for automatic folder years.
    films=[d for d in docs if d.get('film_year') and ((urlsplit(d.get('source_url','')).hostname or '').endswith('.wikipedia.org') or urlsplit(d.get('source_url','')).hostname in {'www.moviebuff.com','moviebuff.com'})]
    years={d['film_year'] for d in films}
    if len(composers)>1 or len(years)>1:return {'conflict':True,'sources':[d['source_url'] for d in docs]}
    main=next((d for d in docs if d.get('tracks')),docs[0])
    result=copy.deepcopy(main)
    result['composer']=next((d['composer'] for d in docs if d.get('composer')),'')
    result['film_year']=next(iter(years),None)
    result['field_sources']={}
    if result['composer']:result['field_sources']['composer']=next(d['source_url'] for d in docs if d.get('composer'))
    if films:result['field_sources']['film_year']=films[0]['source_url']
    for song in result.get('tracks',[]):
        for doc in docs:
            match=next((other for other in doc.get('tracks',[]) if key(other.get('title',''))==key(song.get('title',''))),None)
            if not match:continue
            for field in ('lyrics','singers','performers','length'):
                if not song.get(field) and match.get(field):
                    song[field]=match[field]
                    song.setdefault('field_sources',{})[field]=doc['source_url']
    result['supporting_sources']=[{'provider':d.get('provider','Source'),'url':d['source_url']} for d in docs]
    result['bulk_imported_at']=now()
    result['accepted_at']=now()
    return result


def apply_result(session,settings,album,documents,payload,expected):
    from .archive_ops import relocate_album
    if fingerprint(album)!=expected:return {'metadata_applied':False,'reason':'Saved metadata changed during lookup; suggestions retained for manual review.'}
    document=merge_documents(documents,album.title)
    if not document:return {'metadata_applied':False,'reason':'No matching published metadata found.'}
    if document.get('conflict'):return {'metadata_applied':False,'reason':'Sources disagree about the composer or film year; choose the correct film manually.'}
    old={'artist':album.artist,'year':album.year,'folder':album.folder}
    overwrite=payload.get('overwrite_existing',False)
    if not overwrite and album.artist.casefold() not in UNKNOWN and document.get('composer') and key(album.artist)!=key(document['composer']):
        return {'metadata_applied':False,'reason':'The source composer differs from saved credits; review the correction or enable updating existing metadata.'}
    if document.get('composer') and (overwrite or album.artist.casefold() in UNKNOWN):
        from .artists import approved_name
        name=approved_name(session,document['composer'])
        artist=session.scalar(select(Artist).where(Artist.name==name))
        if not artist:artist=Artist(name=name);session.add(artist);session.flush()
        album.artist,album.artist_id=artist.name,artist.id
    if document.get('film_year') and (overwrite or not album.year):album.year=document['film_year']
    if overwrite or not album.soundtrack.get('tracks'):
        album.soundtrack=document
    else:
        # Preserve accepted track ordering; fill genuinely missing credit cells.
        saved=copy.deepcopy(album.soundtrack)
        for song in saved.get('tracks',[]):
            match=next((row for row in document.get('tracks',[]) if key(row['title'])==key(song['title'])),None)
            if match:
                for field in ('lyrics','singers','performers','length'):
                    if not song.get(field) and match.get(field):song[field]=match[field]
        saved.setdefault('supporting_sources',[])
        saved['supporting_sources']+=document['supporting_sources']
        saved['bulk_imported_at']=now();album.soundtrack=saved
    album.review=True
    session.add(Audit(action='bulk_metadata_imported',target=album.id,detail={'before':old,'sources':document['supporting_sources'],'provider_order':['Apple Music','Spotify','Wikipedia','Google/web search'],'folder_rename_requested':payload.get('move_folders',False)}))
    if payload.get('move_folders'):
        with relocate_album(session,settings,album):session.commit()
    else:
        # Metadata is held in SQLite; even album.json is left untouched during
        # the default bulk run while the user cleans/edits folders manually.
        session.commit()
    missing=[]
    if album.artist.casefold() in UNKNOWN:missing.append('composer')
    if not album.year:missing.append('film year')
    if not album.soundtrack.get('tracks'):missing.append('track list')
    elif any(not song.get('singers') or not song.get('lyrics') for song in album.soundtrack['tracks']):missing.append('some song credits')
    return {'metadata_applied':True,'metadata_incomplete':bool(missing),'missing_fields':missing,'reason':'Details imported; kept in Needs review.','metadata_source':document['source_url']}
