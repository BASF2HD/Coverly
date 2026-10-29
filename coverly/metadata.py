"""Source-backed metadata parsing; retail headers are evidence, not guesses."""
import re
from html import unescape

UNKNOWN={'','unknown','unknown artist','various','various artists'}

def source_metadata(metadata,known_artists=()):
    result=dict(metadata)
    title=unescape(str(result.get('source_title') or result.get('title') or '')).strip()
    artist=str(result.get('artist') or '').strip()
    if not result.get('manual'):
        clean_artist=re.split(r'\s+(?:www\.|https?://)|\s+[\w.-]+\.(?:com|in|shop|net|org)\b',artist,maxsplit=1,flags=re.I)[0].strip()
        if clean_artist!=artist:result['artist']=clean_artist
        artist=clean_artist
    if artist.casefold() in UNKNOWN:
        match=re.search(r'\bby\s+(.+?)(?=\s*[|()]|\s+[–—-]\s+|\s+(?:original|condition|cover|lp|ep|vinyl|records?|audio)\b|$)',title,re.I)
        if match:
            value=re.split(r'\s+(?:www\.|https?://)',match[1],maxsplit=1,flags=re.I)[0]
            value=re.sub(r'\s+[\w.-]+\.(?:com|in|shop|net|org)\b.*$','',value,flags=re.I).strip(' .–—-')
            if 2<=len(value)<=100 and not re.search(r'\b(?:buy|online|shop|unknown)\b',value,re.I):
                result['artist']=value
                result['artist_evidence']='Composer named in source header: '+title
    if str(result.get('artist') or '').casefold() in UNKNOWN:
        normalize=lambda value:re.sub(r'[^\w]+','',value,flags=re.UNICODE).casefold()
        names={normalize(name):name for name in known_artists if name.casefold() not in UNKNOWN}
        for part in re.split(r'\s+[–—-]\s+',title):
            if normalize(part) in names:
                result['artist']=names[normalize(part)]
                result['artist_evidence']='Known archive artist named as a source-header component: '+title
                break
    if not result.get('manual'):
        result.setdefault('source_title',title)
        clean=re.split(r'\s*\|\s*(?:Buy|Shop|Vintage)',str(result.get('title') or title),maxsplit=1,flags=re.I)[0]
        clean=re.split(r'\s+(?:Tamil\s+(?:Film\s+)?(?:LP|EP|Super\s*7|Vinyl)|(?:LP|EP)\s+Vinyl)\b',clean,maxsplit=1,flags=re.I)[0]
        if clean.strip():result['title']=clean.strip(' –—-')
    return result
