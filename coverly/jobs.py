from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import urlsplit
from sqlalchemy import select, update, func
from .db import Job, JobItem, Source, Page, Album, ImageAsset, AlbumImage, OCRResult, Candidate, Restoration, Audit, uid, now
from .network import SafeHTTP, canonical_url
from .extraction import adapter_for, render_page, scoped_page_url, page_number
from .intelligence import create_album, run_ocr, transliterate
from .restoration import LocalProvider
from .storage import add_image, within, decode_image, atomic_write, write_manifest
from .locking import file_lock

log = logging.getLogger("coverly.worker")


def enqueue(session, kind, payload, items=None):
    job = Job(kind=kind, payload=payload)
    session.add(job)
    session.flush()
    for position, (key, data) in enumerate(items or []):
        session.add(JobItem(job_id=job.id, key=str(key), data=data, position=position))
    job.total = len(items or [])
    session.flush()
    return job


def enqueue_download(session, pages, payload=None):
    pages = list({p.id:p for p in pages}.values())
    if not pages:
        raise ValueError('No discovered albums selected')
    payload = dict(payload or {})
    existing=list(session.scalars(select(JobItem).join(Job).where(Job.kind=='download',Job.status.in_(['queued','running','paused']),JobItem.key.in_([p.id+':gallery' for p in pages]))))
    covered={item.data.get('page_id') for item in existing}
    if covered and all(page.id in covered for page in pages):return session.get(Job,existing[0].job_id)
    pages=[page for page in pages if page.id not in covered and not page.metadata_json.get('ignore_download')]
    if not pages:raise ValueError('These albums were permanently removed; select another catalogue')
    def origin(page):
        positions = page.metadata_json.get('catalogue_pages', {})
        source = payload.get('source_url') or next(iter(positions), '')
        return {'catalogue_page':next(iter(positions.get(source, [])), None), 'catalogue_url':source}
    pages.sort(key=lambda p:(origin(p).get('catalogue_page') or 1,p.created,p.id))
    return enqueue(session, 'download', payload, [(p.id+':gallery', {
        'page_id':p.id, 'inspect_gallery':True, 'browser_assisted':bool(p.metadata_json.get('browser_assisted')),
        'source_url':p.url, 'title':p.title, **origin(p)}) for p in pages])


def maybe_enqueue_ocr(session, settings, assets):
    if not settings.auto_ocr or not assets:
        return
    from .intelligence import ocr_capabilities
    caps = ocr_capabilities()
    if not caps["available"] or set(settings.ocr_languages.split("+")) - set(caps["languages"]):
        return
    ids = []
    for asset in assets:
        prior = session.scalar(select(OCRResult.id).where(OCRResult.image_id == asset.id, OCRResult.status == "completed"))
        pending = session.scalar(select(JobItem.id).join(Job).where(Job.kind == "ocr", JobItem.key == asset.id, JobItem.status.in_(["pending", "running"]), Job.status.in_(["queued", "running", "paused"])))
        if not prior and not pending:
            ids.append(asset.id)
    if ids:
        enqueue(session, "ocr", {"automatic": True}, [(i, {}) for i in dict.fromkeys(ids)])


def recover(session, worker_id=None):
    jobs = list(session.scalars(select(Job).where(Job.worker_id == worker_id))) if worker_id else list(session.scalars(select(Job)))
    for job in jobs:
        if job.status == "running":
            job.status, job.message, job.updated = "queued", "Recovered after interruption", now()
        session.execute(update(JobItem).where(JobItem.job_id == job.id, JobItem.status == "running").values(status="pending"))
    session.commit()


class Worker:
    def __init__(self, settings, sessions, fetcher_factory=SafeHTTP, allowed_kinds=None, quantum=None):
        self.settings = settings
        self.sessions = sessions
        self.fetcher_factory = fetcher_factory
        self.worker_id = str(os.getpid())
        self.allowed_kinds, self.quantum = allowed_kinds, quantum

    def active(self, job_id):
        with self.sessions() as s:
            job = s.get(Job, job_id)
            return job.status == "running" and job.worker_id == self.worker_id

    def fetcher(self, domains, browser=False, job_id=None):
        if not browser:
            return self.fetcher_factory(self.settings, domains)
        from .browser import BrowserHTTP
        def waiting(message):
            if job_id:
                with self.sessions() as s:
                    job = s.get(Job, job_id)
                    if job.status == 'running':
                        job.message, job.updated = message, now()
                        s.commit()
        return BrowserHTTP(self.settings, domains, on_wait=waiting,
                           active=(lambda: self.active(job_id)) if job_id else None, job_id=job_id)

    def tick(self):
        with self.sessions() as s:
            query = select(Job).where(Job.status == 'queued')
            if self.allowed_kinds:
                query = query.where(Job.kind.in_(self.allowed_kinds))
            job = s.scalar(query.order_by(Job.updated, Job.created).limit(1))
            if not job:
                return False
            claimed = s.execute(update(Job).where(Job.id == job.id, Job.status == "queued")
                                .values(status="running", worker_id=self.worker_id, message="Processing", updated=now())).rowcount
            s.commit()
            if not claimed:
                return True
            identifier, kind = job.id, job.kind
            if kind=='download' and 'speed_base_progress' not in job.payload:
                job.payload={**job.payload,'speed_base_progress':job.progress};s.commit()
        started=time.monotonic()
        try:
            if kind == "discover":
                self.discover(identifier)
            else:
                self.process_items(identifier)
            with self.sessions() as s:
                job = s.get(Job, identifier)
                if job.status == "running" and job.worker_id == self.worker_id:
                    errors = s.scalar(select(func.count()).select_from(JobItem).where(JobItem.job_id == identifier, JobItem.status == "failed"))
                    successes = s.scalar(select(func.count()).select_from(JobItem).where(JobItem.job_id == identifier, JobItem.status == "done"))
                    job.status = "failed" if kind == "discover" and errors and not successes else "completed_with_errors" if errors else "completed"
                    first_error = s.scalar(select(JobItem.error).where(JobItem.job_id == identifier, JobItem.status == "failed").order_by(JobItem.id).limit(1))
                    if kind == "discover":
                        found = job.payload.get("found", 0)
                        job.message = (f"Discovery failed: {first_error}" if job.status == "failed" else
                                       f"Found {found} album page(s); {errors} page(s) failed: {first_error}" if errors else
                                       f"Discovery finished: {found} album page(s) found" if found else
                                       "No album pages found. This page may need a site adapter or manual artwork import.")
                    else:
                        job.message = f"Finished with {errors} failed item(s)" if errors else "Completed successfully"
                        if kind == 'download':
                            job.payload = {**job.payload, 'stage':'Finished with errors' if errors else 'Finished'}
                    job.updated = now()
                    s.commit()
        except Exception as exc:
            log.exception("Job failed", extra={"job_id": identifier})
            with self.sessions() as s:
                job = s.get(Job, identifier)
                if job.status == "running":
                    job.status, job.message, job.updated = "failed", str(exc)[:2000], now()
                s.add(Audit(action="job_failed", target=identifier, detail={"error": str(exc)[:2000]}))
                s.commit()
        finally:
            with self.sessions() as s:
                job=s.get(Job,identifier)
                if job and job.kind=='download':
                    job.payload={**job.payload,'active_seconds':job.payload.get('active_seconds',0)+time.monotonic()-started}
                    job.updated=now();s.commit()
        return True

    def discover(self, job_id):
        with self.sessions() as s:
            payload = dict(s.get(Job, job_id).payload)
        url = canonical_url(payload['url'])
        url = scoped_page_url(url, url)
        domain = urlsplit(url).hostname
        # Older jobs followed sidebars and unrelated media. Rebuild their queue
        # from the chosen catalogue instead of resuming that unsafe navigation.
        fresh_scope = payload.get('scope_version') != 2
        queue = [[url, 0]] if fresh_scope else payload.get('queue', [[url, 0]])
        seen = set() if fresh_scope else set(payload.get('seen', []))
        found_urls = set() if fresh_scope else set(payload.get('found_urls', []))
        known_total = 0 if fresh_scope else payload.get('pages_total', 0)
        limit = min(payload.get('max_pages', self.settings.max_pages), self.settings.max_pages)
        queue = [[target, 0] for candidate, _ in queue if (target := scoped_page_url(candidate, url)) and target not in seen]
        payload = {**payload, 'url':url, 'scope_version':2, 'found_urls':sorted(found_urls)}
        with self.sessions() as s:
            if fresh_scope:
                for item in s.scalars(select(JobItem).where(JobItem.job_id == job_id)):
                    if not scoped_page_url(item.key, url):
                        item.status, item.error = 'ignored', 'Outside the selected catalogue; ignored after scope correction'
            s.get(Job, job_id).payload = payload
            s.commit()
        fetcher = self.fetcher({domain, *self.settings.image_domains}, payload.get('browser_assisted'), job_id)
        processed = 0
        try:
            while queue and len(seen) < limit and self.active(job_id) and (not self.quantum or processed < self.quantum*3):
                queue.sort(key=lambda entry: page_number(entry[0]))
                page_url, depth = queue.pop(0)
                if page_url in seen:
                    continue
                number = page_number(page_url)
                with self.sessions() as s:
                    item = s.scalar(select(JobItem).where(JobItem.job_id == job_id, JobItem.key == page_url))
                    if not item:
                        item = JobItem(job_id=job_id, key=page_url, data={'catalogue_page':number})
                        s.add(item)
                    item.status, item.error = 'running', ''
                    item.attempts = (item.attempts or 0) + 1
                    job = s.get(Job, job_id)
                    job.payload = {**payload, 'stage':'Reading catalogue', 'current_page':number,
                                   'current_url':page_url, 'pages_total':known_total}
                    job.message, job.updated = f'Reading catalogue page {number}' + (f' of {known_total}' if known_total else ''), now()
                    s.commit()
                try:
                    raw, final = fetcher.get(page_url)
                    if not scoped_page_url(final, url):
                        raise ValueError('The server redirected outside the selected catalogue')
                    html = raw.decode('utf-8', errors='replace')
                    if payload.get('render_js') and not payload.get('browser_assisted'):
                        html = render_page(final, fetcher)
                    result = adapter_for(html).extract(html, final)
                    known_total = max(known_total, result.get('total_pages', number))
                    records = [dict(result, url=final)] if result['is_product'] else result.get('products', [])
                    with self.sessions() as s:
                        source = s.scalar(select(Source).where(Source.domain == domain))
                        if not source:
                            source = Source(domain=domain);s.add(source);s.flush()
                        for discovered in records:
                            page = s.scalar(select(Page).where(Page.url == discovered['url']))
                            if not page:
                                page = Page(source_id=source.id, url=discovered['url']);s.add(page)
                            previous = page.metadata_json or {}
                            metadata = previous if discovered.get('pending_gallery') and previous.get('images') else discovered
                            page.title = metadata['metadata']['title']
                            page.metadata_json = {**metadata, 'browser_assisted':bool(payload.get('browser_assisted')),
                                                  'catalogue_urls':sorted({*previous.get('catalogue_urls', []), url}),
                                                  'catalogue_pages':{**previous.get('catalogue_pages', {}),
                                                      url:sorted({*previous.get('catalogue_pages', {}).get(url, []), number})}}
                            found_urls.add(discovered['url'])
                        item = s.scalar(select(JobItem).where(JobItem.job_id == job_id, JobItem.key == page_url))
                        item.status, item.error = 'done', ''
                        item.data = {**item.data, 'album_urls':[record['url'] for record in records]}
                        s.commit()
                    queued = {entry[0] for entry in queue}
                    for link in result.get('pagination', []):
                        target = scoped_page_url(link['url'], url)
                        if target and target not in seen and target != page_url and target not in queued:
                            queue.append([target, 0]);queued.add(target)
                except Exception as exc:
                    from .browser import BrowserSessionClosed
                    if isinstance(exc, BrowserSessionClosed):
                        # A closed verification/browser session is recoverable.
                        # Keep this page pending instead of failing every URL
                        # against the same dead Playwright context.
                        with self.sessions() as s:
                            item = s.scalar(select(JobItem).where(JobItem.job_id == job_id, JobItem.key == page_url))
                            item.status, item.error = 'pending', ''
                            job = s.get(Job, job_id)
                            job.status, job.message, job.updated = 'paused', str(exc), now()
                            job.payload = {**payload, 'queue':[[page_url, depth], *queue], 'seen':sorted(seen),
                                           'found_urls':sorted(found_urls), 'found':len(found_urls), 'pages_total':known_total}
                            s.commit()
                        break
                    with self.sessions() as s:
                        item = s.scalar(select(JobItem).where(JobItem.job_id == job_id, JobItem.key == page_url))
                        item.status, item.error = 'failed', str(exc)[:2000];s.commit()
                seen.add(page_url)
                processed += 1
                queue = [entry for entry in queue if entry[0] not in seen]
                payload = {**payload, 'stage':'Reading catalogue', 'current_page':number, 'current_url':page_url,
                           'queue':queue, 'seen':sorted(seen), 'found':len(found_urls), 'found_urls':sorted(found_urls),
                           'pages_total':known_total, 'limit_reached':bool(queue and len(seen) >= limit)}
                with self.sessions() as s:
                    job = s.get(Job, job_id)
                    job.payload = payload
                    job.progress, job.total = len(seen), max(known_total - page_number(url) + 1, len(seen) + len(queue))
                    if job.status == 'running':
                        job.message = f'Catalogue page {number} of {known_total or "?"} · {len(found_urls)} albums found'
                    job.updated = now();s.commit()
            with self.sessions() as s:
                job = s.get(Job, job_id)
                if job.status == 'running' and queue and len(seen) < limit:
                    job.status, job.message, job.updated = 'queued', 'Continuing catalogue from checkpoint', now()
                    s.commit()
                if job.status == 'running' and payload.get('auto_download') and found_urls and not payload.get('download_job_id'):
                    child = enqueue_download(s, list(s.scalars(select(Page).where(Page.url.in_(found_urls)))),
                                             {'parent_job_id':job_id, 'source_url':url, 'pages_total':known_total})
                    payload['download_job_id'] = child.id
                    job.payload = payload;s.commit()
        finally:
            fetcher.close()

    def process_items(self, job_id):
        processed = 0
        while self.active(job_id):
            with self.sessions() as s:
                job = s.get(Job, job_id)
                item = s.scalar(select(JobItem).where(JobItem.job_id == job_id, JobItem.status == "pending").order_by(JobItem.position,JobItem.id).limit(1))
                if not item:
                    break
                item.status, item.attempts = "running", item.attempts + 1
                item_id, key, data, kind, payload = item.id, item.key, item.data, job.kind, job.payload
                if job.kind == 'download':
                    job.payload = {**job.payload, 'current_album':data.get('title', ''), 'stage':'Reading album gallery',
                                   'current_image':0, 'current_image_total':0,
                                   'catalogue_page':data.get('catalogue_page'), 'catalogue_url':data.get('catalogue_url')}
                    job.message, job.updated = f'Reading album {job.progress+1} of {job.total}: ' + data.get('title', ''), now()
                s.commit()
            try:
                if kind == "download":
                    if data.get('inspect_gallery'):
                        self.download_album(data, job_id, item_id)
                    else:
                        self.download(data, job_id=job_id)
                elif kind == 'identify':
                    result = self.identify_album(key, {**payload,**data}, job_id, item_id)
                    with self.sessions() as s:
                        item=s.get(JobItem,item_id);item.data={**item.data, **result};s.commit()
                elif kind == 'organize':
                    self.organize_album(key,data)
                elif kind == 'classify':
                    from .classification import classify_album
                    with file_lock(self.settings.data / '.archive.lock'), self.sessions() as s:
                        classify_album(s,self.settings,key);s.commit()
                        from .archive_files import cleanup_covers
                        cleanup_covers(s,self.settings,s.get(Album,key))
                elif kind == "ocr":
                    self.ocr(key)
                elif kind == "restore":
                    self.restore(job_id, key, {**payload,**data})
                else:
                    raise ValueError("Unknown job type")
                with self.sessions() as s:
                    s.get(JobItem, item_id).status = "done"
                    s.commit()
            except Exception as exc:
                with self.sessions() as s:
                    item = s.get(JobItem, item_id)
                    paused = s.get(Job, job_id).status in {"paused", "cancelled"}
                    item.status, item.error = ("pending", "") if paused else ("failed", str(exc)[:2000])
                    s.commit()
            processed += 1
            with self.sessions() as s:
                job = s.get(Job, job_id)
                job.progress = s.scalar(select(func.count()).select_from(JobItem).where(JobItem.job_id == job_id, JobItem.status.in_(["done", "failed"])))
                job.message, job.updated = f"Processed {job.progress} of {job.total}", now()
                if self.quantum and processed >= self.quantum*(20 if kind in {'identify','classify'} and not payload.get('bulk_apply') else 1) and job.status == 'running' and s.scalar(select(JobItem.id).where(JobItem.job_id == job_id, JobItem.status == 'pending').limit(1)):
                    job.status, job.message = 'queued', 'Continuing from saved album checkpoint'
                s.commit()

    def download_progress(self, job_id, **details):
        if not job_id:
            return
        with self.sessions() as s:
            job = s.get(Job, job_id)
            job.payload = {**job.payload, **details}
            job.message = f"Album {min(job.progress+1,job.total)} of {job.total} · " + details.get('stage', job.payload.get('stage','Downloading'))
            job.updated = now();s.commit()

    def download_album(self, data, job_id, item_id):
        with self.sessions() as s:
            page = s.get(Page, data['page_id'])
            if not page:
                raise ValueError('Source page no longer exists')
            if page.album_id:
                album=s.get(Album,page.album_id)
                if album and not within(self.settings.data,album.folder).is_dir():
                    raise ValueError('This album folder was removed from the archive. Background downloading will not recreate it; import artwork explicitly if wanted.')
            page_url = page.url
        fetcher = self.fetcher({urlsplit(page_url).hostname, *self.settings.image_domains}, data.get('browser_assisted'), job_id)
        try:
            raw, final = fetcher.get(page_url)
            html = raw.decode('utf-8', errors='replace')
            result = adapter_for(html).extract(html, final)
            if not result['is_product'] or not result['images']:
                raise ValueError('No gallery images found on the selected album page')
            with self.sessions() as s:
                page = s.get(Page, data['page_id'])
                page.title = result['metadata']['title']
                page.metadata_json = {**page.metadata_json, **result, 'pending_gallery':False, 'browser_assisted': bool(data.get('browser_assisted'))}
                item = s.get(JobItem, item_id)
                item.data = {**item.data, 'image_total':len(result['images'])}
                completed = dict(item.data.get('image_results', {}))
                s.commit()
            failures = []
            for index, image in enumerate(result['images'], 1):
                key = image['url']
                if completed.get(key, {}).get('status') == 'done':
                    continue
                self.download_progress(job_id, stage='Downloading artwork', current_album=result['metadata']['title'],
                                       current_image=index, current_image_total=len(result['images']))
                if not self.active(job_id):
                    raise ValueError('Gallery download interrupted; retry to complete remaining images')
                try:
                    saved = self.download({**data, 'image': image}, fetcher, job_id)
                    completed[key] = {'status':'done', **saved}
                except Exception as exc:
                    completed[key] = {'status':'failed', 'error':str(exc)[:1500]}
                    failures.append(str(exc))
                with self.sessions() as s:
                    item = s.get(JobItem, item_id)
                    item.data = {**item.data, 'image_results':dict(completed)}
                    page = s.get(Page, data['page_id'])
                    page.metadata_json = {**page.metadata_json, 'images':[
                        {**im, **({'sha256':completed[im['url']]['sha256']} if completed.get(im['url'], {}).get('sha256') else {})}
                        for im in result['images']]}
                    if page.album_id:
                        from .storage import write_existing_manifest
                        s.flush();write_existing_manifest(s, self.settings, s.get(Album, page.album_id))
                    job = s.get(Job, job_id);job.updated = now();s.commit()
            if failures:
                raise ValueError('Some gallery images failed: ' + '; '.join(failures)[:1500])
        finally:
            fetcher.close()

    def download(self, data, shared_fetcher=None, job_id=None):
        with file_lock(self.settings.data / ".archive.lock"), self.sessions() as s:
            page = s.get(Page, data["page_id"])
            if not page:
                raise ValueError("Source page no longer exists")
            if page.album_id:
                existing_album=s.get(Album,page.album_id)
                if existing_album and not within(self.settings.data,existing_album.folder).is_dir():
                    raise ValueError('This album folder was removed from the archive. Background downloading will not recreate it; import artwork explicitly if wanted.')
            album = create_album(s, self.settings, page.metadata_json["metadata"], page.url)
            page.album_id = album.id
            write_manifest(s, self.settings, album)
            s.commit()
            if album.deleted_at:
                raise ValueError('This album is in Trash. Restore it before downloading more artwork.')
            album_id, page_url = album.id, page.url
        group = data["image"]
        domains = {urlsplit(page_url).hostname, *self.settings.image_domains}
        fetcher = shared_fetcher or self.fetcher(domains, data.get('browser_assisted'), job_id)
        last_progress = [0]
        def received(url, count, total):
            if time.monotonic() - last_progress[0] >= .75:
                self.download_progress(job_id, stage='Receiving artwork', current_url=url,
                                       received_bytes=count, expected_bytes=total)
                last_progress[0] = time.monotonic()
        fetcher.on_progress = received if job_id else None
        best, best_area, selected_url = None, 0, None
        errors = []
        try:
            # Validate all listed resolutions, retaining largest actual pixel dimensions. No invented URLs.
            candidates = group.get('alternatives', [{'url':group['url']}])
            advertised_width = max((c.get('width',0) for c in candidates), default=0)
            for candidate in candidates:
                try:
                    raw, final = fetcher.get(candidate["url"], image=True)
                    im, _ = decode_image(raw)
                    area = im.width * im.height
                    if area > best_area:
                        best, best_area, selected_url = raw, area, final
                    # Original/full-resolution URLs are preferred. Once actual
                    # dimensions meet the largest advertised width, thumbnails
                    # cannot improve this image and need not be fetched.
                    if im.width >= advertised_width and (candidate.get('priority',100) >= 90 or candidate.get('width',0) >= advertised_width):
                        break
                except Exception as exc:
                    errors.append(str(exc))
            if best is None:
                raise ValueError("No accessible valid image: " + "; ".join(errors)[:1500])
            self.download_progress(job_id, stage='Saving artwork', current_url=selected_url)
            with file_lock(self.settings.data / ".archive.lock"), self.sessions() as s:
                album=s.get(Album,album_id)
                if not within(self.settings.data,album.folder).is_dir():
                    raise ValueError('The album folder was removed during downloading. No artwork was recreated.')
                asset, duplicate = add_image(s, self.settings, album, best, selected_url, page_url, group.get("caption", ""))
                from .classification import classify_album
                classify_album(s, self.settings, album_id)
                maybe_enqueue_ocr(s, self.settings, [asset])
                s.commit()
                from .archive_files import cleanup_covers
                cleanup_covers(s,self.settings,album)
                return {"image_id":asset.id, "sha256":asset.sha256, "reused":duplicate, "bytes":asset.size, "source_url":selected_url}
        finally:
            fetcher.on_progress = None
            if shared_fetcher is None:
                fetcher.close()

    def identify_album(self, album_id, payload, job_id=None, item_id=None):
        from .metadata import source_metadata, UNKNOWN
        from .archive_ops import relocate_album
        from .db import Artist
        with self.sessions() as s:
            album=s.get(Album,album_id)
            if not album or album.deleted_at:return {'skipped':'Album removed'}
            pages=list(s.scalars(select(Page).where(Page.album_id==album_id)))
            title,year=album.title,album.year
            from .bulk_metadata import fingerprint
            expected=payload.get('metadata_fingerprint') or fingerprint(album)
            if payload.get('bulk_apply') and expected!=fingerprint(album):
                return {'metadata_applied':False,'reason':'Metadata changed since selection; this album was left for manual review.'}
            known_artists=list(s.scalars(select(Artist.name)))
            sources=[(page.url,dict(page.metadata_json.get('metadata',{}))) for page in pages]
        suggestions=[]
        for url,metadata in sources:
            inferred=source_metadata(metadata,known_artists)
            if inferred.get('artist','').casefold() not in UNKNOWN:
                suggestions.append({'field':'artist','value':inferred['artist'],'source_url':url,
                    'evidence':inferred.get('artist_evidence','Composer in source metadata'),'confidence':.95})
        errors=[];documents=[]
        def publish(message,docs):
            if not job_id or not item_id:return
            # Snapshot a fresh JSON object: SQLAlchemy does not track nested mutations.
            with self.sessions() as s:
                job,item=s.get(Job,job_id),s.get(JobItem,item_id)
                if job and item:
                    job.message,job.updated=(f'Album {job.progress+1} of {job.total} · {title} · '+message if payload.get('bulk_apply') else message),now()
                    item.data={**item.data,'suggestions':suggestions,'soundtracks':json.loads(json.dumps(docs)),'lookup_errors':errors}
                    s.commit()
        if payload.get('lookup_internet'):
            from .soundtracks import MetadataLookup
            documents,lookup_errors=MetadataLookup(self.settings,fetcher_factory=lambda settings,domains:self.fetcher(domains),progress=publish).run(payload.get('title') or title,payload.get('year') if 'year' in payload else None if payload.get('overwrite_existing') else year,source_url=payload.get('source_url'),refresh=payload.get('refresh',False),source_only=payload.get('source_only',False),search_web=payload.get('search_web',False))
            errors.extend(lookup_errors)
            for doc in documents:
                if doc.get('composer'):
                    suggestions.append({'field':'artist','value':doc['composer'],'source_url':doc['source_url'],
                        'evidence':doc['provider']+' · '+doc.get('composer_basis','explicit film composer / music director credit'),'confidence':.85 if doc.get('composer_basis') else .95})
            # Product metadata remains a useful fallback when soundtrack providers have no match.
            if not suggestions and not payload.get('source_only'):
                for url,_ in sources[:2]:
                    fetcher=self.fetcher({urlsplit(url).hostname,*self.settings.image_domains})
                    try:
                        raw,final=fetcher.get(url)
                        html=raw.decode('utf-8',errors='replace')
                        metadata=adapter_for(html).extract(html,final)['metadata']
                        inferred=source_metadata(metadata,known_artists)
                        if inferred.get('artist','').casefold() not in UNKNOWN:
                            suggestions.append({'field':'artist','value':inferred['artist'],'source_url':final,'evidence':'Current source product metadata','confidence':.95})
                    except Exception as exc:errors.append(str(exc)[:350])
                    finally:fetcher.close()
            if not suggestions and not payload.get('source_only'):
                try:
                    from .search import search
                    search_settings=self.settings
                    if self.settings.search_provider=='site' and sources:
                        origin=urlsplit(sources[0][0]);search_settings=self.settings.model_copy(update={'search_site':f'{origin.scheme}://{origin.netloc}/'})
                    for item in search(search_settings,title,purpose="metadata"):
                        metadata=source_metadata({'title':item['title']})
                        if metadata.get('artist','').casefold() not in UNKNOWN:
                            suggestions.append({'field':'artist','value':metadata['artist'],'source_url':item['url'],'evidence':'Search result: '+item['title']+'; confirm this is the same film','confidence':.65})
                except Exception as exc:errors.append(str(exc)[:350])
            publish('Soundtrack lookup finished; review source details before saving',documents)
        if payload.get('bulk_apply'):
            from .bulk_metadata import apply_result
            with file_lock(self.settings.data/'.archive.lock'),self.sessions() as s:
                album=s.get(Album,album_id)
                if not album or album.deleted_at:applied={'metadata_applied':False,'reason':'Album removed during lookup.'}
                elif job_id and not self.active(job_id):raise ValueError('Metadata job paused or cancelled before saving')
                else:applied=apply_result(s,self.settings,album,documents,payload,expected)
            return {'suggestions':suggestions,'lookup_errors':errors,'soundtracks':documents,**applied}
        if suggestions and payload.get('auto_apply'):
            # Apply only explicit source evidence, never an unverified web result.
            match=next((m for m in suggestions if m['confidence']>=.9),None)
            if match:
                with file_lock(self.settings.data / '.archive.lock'), self.sessions() as s:
                    album=s.get(Album,album_id)
                    if album and not album.deleted_at and album.artist.casefold() in UNKNOWN:
                        old=album.artist;artist=s.scalar(select(Artist).where(Artist.name==match['value']))
                        if not artist:artist=Artist(name=match['value']);s.add(artist);s.flush()
                        album.artist,album.artist_id=artist.name,artist.id
                        with relocate_album(s,self.settings,album) as movement:
                            s.add(Audit(action='artist_repaired',target=album_id,detail={'old':old,'artist':artist.name,'source':match,'move':movement}));s.commit()
        return {'suggestions':suggestions,'lookup_errors':errors,'soundtracks':documents}

    def organize_album(self,album_id,data):
        from .archive_ops import relocate_album
        from .archive_files import cleanup_covers
        from .db import Artist
        with file_lock(self.settings.data/'.archive.lock'),self.sessions() as s:
            album=s.get(Album,album_id)
            if not album or album.deleted_at:return
            if data.get('old_artist') is not None and album.artist not in {data['old_artist'],data.get('artist')}:
                raise ValueError('Artist changed since preview. Generate a fresh proposal.')
            old_artist=album.artist
            if data.get('artist'):
                artist=s.scalar(select(Artist).where(Artist.name==data['artist']))
                if not artist:artist=Artist(name=data['artist']);s.add(artist);s.flush()
                album.artist,album.artist_id=artist.name,artist.id
            with relocate_album(s,self.settings,album,target_folder=data.get('target_folder')):
                s.add(Audit(action='archive_organized',target=album.id,detail={'old_artist':old_artist,'artist':album.artist,'plan_id':data.get('plan_id')}));s.commit()
            cleanup_covers(s,self.settings,album)

    def ocr(self, image_id):
        with self.sessions() as s:
            image = s.get(ImageAsset, image_id)
            if not image:
                raise ValueError("Unknown image")
            try:
                from .filesystem import asset_path
                text, confidence = run_ocr(asset_path(s,self.settings,image), self.settings.ocr_languages)
                s.add(OCRResult(image_id=image.id, text=text, confidence=confidence, languages=self.settings.ocr_languages, status="completed"))
                for link in s.scalars(select(AlbumImage).where(AlbumImage.image_id == image_id)):
                    album = s.get(Album, link.album_id)
                    album.review = True
                    # Full OCR text is evidence, never automatically assumed to be a film title.
                    for tamil, conventional in __import__("coverly.intelligence", fromlist=["CONVENTIONAL"]).CONVENTIONAL.items():
                        if tamil in text:
                            s.add(Candidate(album_id=album.id, title=conventional, evidence="Tamil OCR: " + text, confidence=min(confidence, .8)))
                    import re
                    for tamil in re.findall(r"[\u0b80-\u0bff]+(?:\s+[\u0b80-\u0bff]+){0,4}", text):
                        if len(tamil) > 2:
                            s.add(Candidate(album_id=album.id, title=transliterate(tamil), evidence="Mechanical transliteration of OCR fragment (not a verified film title): " + tamil, confidence=min(confidence, .55)))
                s.commit()
            except Exception as exc:
                s.add(OCRResult(image_id=image.id, text=str(exc), confidence=0, languages=self.settings.ocr_languages, status="failed"))
                s.commit()
                raise

    def restore(self, job_id, image_id, payload):
        # Read the original under the archive lock, then release it for inference.
        with file_lock(self.settings.data / ".archive.lock"), self.sessions() as s:
            existing=s.scalar(select(Restoration).where(Restoration.job_id==job_id,Restoration.image_id==image_id))
            if existing:return
            image=s.get(ImageAsset,image_id)
            if not image:raise ValueError('Unknown image')
            from .filesystem import asset_path
            original=asset_path(s,self.settings,image,payload.get('album_id')).read_bytes()
        def progress(message,done,total):
            with self.sessions() as s:
                job=s.get(Job,job_id)
                if job:
                    job.message,job.updated=message,now()
                    job.payload={**job.payload,'restore_done':done,'restore_total':total,'restore_image_id':image_id}
                    s.commit()
        progress('Preparing restoration',0,1)
        if payload.get('square'):
            from .embedding import square_jpeg
            # Crop before enhancement so the surrounding scan is not enlarged.
            original,_=square_jpeg(original,edge=8000,mode='crop')
        if payload['preset']=='ai':
            from .ai_restoration import AIProvider
            result,device=AIProvider().restore(original,payload.get('size',2000),progress)
            provider=AIProvider.name+' · '+device.upper();generated=True
        else:
            result=LocalProvider().restore(original,payload['preset'],payload.get('size',2000))
            provider=LocalProvider.name;generated=False
        with file_lock(self.settings.data / '.archive.lock'),self.sessions() as s:
            if not s.get(ImageAsset,image_id):return
            path=f'Restored/{job_id}/{image_id}.jpg'
            atomic_write(within(self.settings.data,path),result)
            s.add(Restoration(job_id=job_id,album_id=payload.get('album_id'),image_id=image_id,path=path,provider=provider,preset=payload['preset'],generated=generated))
            s.commit()
