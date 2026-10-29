"""Import originals exported by the browser, without making source-site requests.

Input is the local capture manifest produced during the explicit browser task.
All network requests here target the running Coverly instance on localhost.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
from html import unescape
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from bs4 import BeautifulSoup
from coverly.extraction import adapter_for
from coverly.intelligence import identify
from coverly.storage import decode_image


def capture_files(capture):
    """Keep gallery order; resolve missing originals from browser-observed variants."""
    direct = {f['url']: f for f in capture['files']}
    chosen, missing = [], []
    for group in capture.get('images', []):
        original = group.get('url') or group.get('original') or group.get('link')
        if original in direct:
            chosen.append(direct.pop(original))
            continue
        candidates = []
        for file in capture.get('fallback_candidates', {}).get(original, []):
            try:
                image, _ = decode_image(Path(file['path']).read_bytes())
                candidates.append((image.width * image.height, file))
            except (ValueError, OSError):
                continue
        if candidates:
            _, best = max(candidates, key=lambda pair: pair[0])
            chosen.append({**best, 'fallback_for': original})
        elif original:
            missing.append(original)
    chosen.extend(direct.values())
    capture['_missing_photos'] = missing
    if not chosen:
        raise ValueError('No valid captured images for ' + capture['url'])
    return chosen


def run(captures: Path, report: Path):
    results = json.loads(report.read_text('utf-8')) if report.exists() else []
    completed = {r['source'] for r in results}
    with httpx.Client(base_url='http://localhost:8000', trust_env=False, timeout=120,
                      headers={'X-Coverly-Request':'1'}) as client:
        for path in sorted(captures.glob('album-*.json'), key=lambda p:int(p.stem.split('-')[-1])):
            capture = json.loads(path.read_text('utf-8'))
            if capture['url'] in completed:
                prior = next(r for r in results if r['source'] == capture['url'])
                title = unescape(prior['title'])
                if title != prior['title']:
                    album = client.get('/api/albums/'+prior['album_id']).json()
                    fields = {k:album[k] for k in ('title','tamil_title','artist','year','label','catalogue','notes','compilation','review')}
                    fields.update(title=title, compilation=identify({'title':title})['compilation'], review=True)
                    client.patch('/api/albums/'+prior['album_id'],json=fields).raise_for_status()
                    prior['title'] = title
                    report.write_text(json.dumps(results,ensure_ascii=False,indent=2), encoding='utf-8')
                continue
            metadata = adapter_for(capture['html']).extract(capture['html'], capture['url'])['metadata']
            for field in ('title','artist','label','catalogue'):
                if isinstance(metadata.get(field), str):
                    metadata[field] = unescape(metadata[field])
            soup = BeautifulSoup(capture['html'], 'html.parser')
            years = {row.select('td,th')[1].get_text(strip=True) for row in soup.select('tr')
                     if len(row.select('td,th')) >= 2 and row.select('td,th')[0].get_text(strip=True).lower() == 'album releasing year'}
            if len(years) > 1:
                metadata.pop('year', None)  # Multi-film discs must not inherit one film's year.
            source_map, evidence, uploads = {}, [], []
            with ExitStack() as opened:
                for index, file in enumerate(capture_files(capture), 1):
                    local = Path(file['path'])
                    raw = local.read_bytes()
                    image, _ = decode_image(raw)
                    name = f'{index:02d}_' + unquote(Path(urlsplit(file['url']).path).name)
                    source_map[name] = file['url']
                    evidence.append({'source_url':file['url'], 'sha256':hashlib.sha256(raw).hexdigest(),
                                     'requested_source_url':file.get('fallback_for', file['url']),
                                     'resolution_fallback':bool(file.get('fallback_for')),
                                     'width':image.width, 'height':image.height, 'bytes':len(raw)})
                    uploads.append(('files',(name,opened.enter_context(local.open('rb')),'application/octet-stream')))
                response = client.post('/api/import', files=uploads, data={
                    'title':metadata.get('title', capture['title']), 'artist':metadata.get('artist','Unknown Artist'),
                    'year':str(metadata.get('year','')), 'label':metadata.get('label',''),
                    'catalogue':metadata.get('catalogue',''), 'source_page':capture['url'],
                    'source_images':json.dumps(source_map), 'metadata_confirmed':'false',
                })
                response.raise_for_status()
            result = response.json()
            album = client.get('/api/albums/'+result['album_id']).json()
            stored = {i['sha256']:i for i in album['images']}
            for photo in evidence:
                assert photo['sha256'] in stored, (capture['url'], photo['source_url'])
                image = stored[photo['sha256']]
                original = client.get('/api/images/'+image['id']+'/original')
                original.raise_for_status()
                assert hashlib.sha256(original.content).hexdigest() == photo['sha256']
                photo['image_id'] = image['id']
            results.append({'source':capture['url'], 'album_id':result['album_id'], 'title':album['title'],
                            'images':evidence, 'imported':result['imported'], 'duplicates':result['duplicates'],
                            'review':album['review'], 'original_bytes_verified':True})
            results[-1]['missing_photo_urls'] = capture.get('_missing_photos', [])
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(results,ensure_ascii=False,indent=2), encoding='utf-8')
            print(f"{len(results)} albums imported; {len(evidence)} originals verified: {album['title']}",flush=True)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('captures', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    run(args.captures.resolve(), args.report.resolve())
