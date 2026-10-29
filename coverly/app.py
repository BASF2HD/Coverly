from __future__ import annotations

import hashlib
import asyncio
import io
import json
import os
import secrets
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from sqlalchemy import select, func, or_
from .config import Settings, load_settings, save_settings
from .db import open_db, Album, Artist, AlbumImage, AlbumRelation, ImageAsset, Source, Page, Job, JobItem, OCRResult, Candidate, Restoration, Audit, uid, now
from .storage import KINDS, add_image, within, write_manifest, write_existing_manifest, folder_for, atomic_write, decode_image
from .intelligence import create_album, ocr_capabilities
from .jobs import enqueue, enqueue_download, maybe_enqueue_ocr
from .network import canonical_url
from .restoration import hardware, codex_manifest
from .locking import file_lock
from . import __version__

STATIC = Path(__file__).parent / "static"


def record(obj):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def metadata_version(album):
    fields=('title','tamil_title','artist','year','label','catalogue','notes','compilation','review','soundtrack')
    return hashlib.sha256(json.dumps({key:getattr(album,key) for key in fields},sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def job_snapshot(session, job):
    value = record(job)
    value['payload'] = {key:val for key,val in job.payload.items() if key not in {'seen','queue','found_urls'}}
    counts = dict(session.execute(select(JobItem.status, func.count()).where(JobItem.job_id == job.id).group_by(JobItem.status)).all())
    error = session.scalar(select(JobItem.error).where(JobItem.job_id == job.id, JobItem.status == "failed").order_by(JobItem.id).limit(1))
    value.update(successful_items=counts.get("done", 0), failed_items=counts.get("failed", 0), first_error=error or "")
    if job.kind == "discover":
        value.update(source_url=job.payload.get("url", ""), albums_found=job.payload.get("found", 0))
        value["access_blocked"] = bool(error and any(text in error.lower() for text in ("robots.txt unavailable (403)", "disallowed by robots.txt", "403 forbidden", "401 unauthorized", 'browser access rejected (403)', 'browser verification is still pending')))
        # Historical jobs used a generic completion message. Surface their real
        # failure without rewriting existing records or losing job history.
        if error and job.status in {"failed", "completed_with_errors"} and not counts.get("done"):
            value["message"] = "Discovery could not start: " + error
    if job.kind == 'download':
        items = list(session.scalars(select(JobItem).where(JobItem.job_id == job.id)))
        results = [r for item in items for r in item.data.get('image_results', {}).values()]
        legacy = [item for item in items if 'image' in item.data and not item.data.get('inspect_gallery')]
        value['unit'] = 'images' if legacy else 'albums'
        value['legacy_progress'] = bool(legacy)
        parent=session.get(Job,job.payload.get('parent_job_id')) if job.payload.get('parent_job_id') else None
        value['source_url']=job.payload.get('source_url') or (parent.payload.get('url','') if parent else '')
        active_seconds=job.payload.get('active_seconds',0)
        units=job.progress-job.payload.get('speed_base_progress',job.progress)
        value['albums_per_minute']=round(units/active_seconds*60,2) if active_seconds and units else 0
        value['estimated_remaining_seconds']=round((job.total-job.progress)*active_seconds/units) if active_seconds and units>0 else None
        value.update(images_total=sum(i.data.get('image_total', int('image' in i.data)) for i in items),
                     images_done=sum(r.get('status') == 'done' for r in results) + sum(i.status == 'done' for i in legacy),
                     images_failed=sum(r.get('status') == 'failed' for r in results) + sum(i.status == 'failed' for i in legacy),
                     files_new=sum(r.get('status') == 'done' and not r.get('reused') for r in results),
                     files_reused=sum(r.get('status') == 'done' and r.get('reused', False) for r in results))
    if job.kind == 'discover' and job.payload.get('download_job_id'):
        child = session.get(Job, job.payload['download_job_id'])
        value['download_job'] = job_snapshot(session, child) if child else None
    if job.kind=='identify' and job.payload.get('bulk_apply'):
        value['metadata_applied']=session.scalar(select(func.count()).select_from(JobItem).where(JobItem.job_id==job.id,JobItem.data['metadata_applied'].as_boolean()==True))
        value['metadata_unresolved']=counts.get('done',0)-value['metadata_applied']
        value['metadata_incomplete']=session.scalar(select(func.count()).select_from(JobItem).where(JobItem.job_id==job.id,JobItem.data['metadata_incomplete'].as_boolean()==True))
    return value


class DiscoverInput(BaseModel):
    url: str = Field(max_length=4096)
    max_pages: int = Field(1000, ge=1, le=10000)
    render_js: bool = False
    browser_assisted: bool = False
    auto_download: bool = True


class Selection(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)


class BulkLookupInput(BaseModel):
    ids: list[str] = Field(min_length=1,max_length=50000)
    overwrite_existing: bool = False
    move_folders: bool = False


class DownloadInput(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=1000)
    discovery_job_id: str | None = None
    source_job_id: str | None = None


class TrashPreview(BaseModel):
    ids: list[str] = Field(default_factory=list,max_length=1000)

class TrashPurge(BaseModel):
    plan_id: str
    confirmation: Literal['DELETE']

class ArtworkLink(BaseModel):
    album_id: str
    image_id: str

class ArtworkSelection(BaseModel):
    items: list[ArtworkLink] = Field(min_length=1,max_length=1000)
    protected: list[ArtworkLink] = Field(default_factory=list,max_length=20000)

class BrowserAction(BaseModel):
    action: Literal['click', 'key', 'focus']
    x: float | None = Field(None, ge=0, lt=1280)
    y: float | None = Field(None, ge=0, lt=850)
    key: Literal['Tab', 'Space', 'Enter', 'Escape'] | None = None


class RestoreInput(Selection):
    preset: Literal["gentle", "sleeve", "upscale", "ai"] = "gentle"
    size: int = Field(2000, ge=256, le=8000)
    image_albums: dict[str,str] = Field(default_factory=dict)
    square: bool = False


class MetadataLookupInput(BaseModel):
    title: str | None = Field(None,min_length=1,max_length=300)
    year: int | None = Field(None,ge=1850,le=2100)
    source_url: str | None = Field(None,max_length=4096)
    refresh: bool = False
    source_only: bool = False
    search_web: bool = False

class MetadataPageInput(BaseModel):
    source_url: str = Field(min_length=1,max_length=4096)
    html: str = Field(min_length=1,max_length=500000)
    title: str | None = Field(None,max_length=300)
    allow_title_mismatch: bool = False


class SoundtrackChoice(BaseModel):
    job_id: str = Field(min_length=1, max_length=64)
    index: int = Field(ge=0, le=20)

class CopiedMetadataInput(BaseModel):
    source_url: str = Field(min_length=1,max_length=4096)
    title: str = Field(min_length=1,max_length=300)
    text: str = Field('',max_length=500000)
    html: str = Field('',max_length=500000)
    composer: str = Field('',max_length=200)
    year: int | None = Field(None,ge=1850,le=2100)


class AlbumUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    tamil_title: str = Field("", max_length=1000)
    artist: str = Field("Unknown Artist", min_length=1, max_length=200)
    year: int | None = Field(None, ge=1850, le=2100)
    label: str = Field("", max_length=200)
    catalogue: str = Field("", max_length=200)
    notes: str = Field("", max_length=10000)
    compilation: bool = False
    review: bool = False
    move_folder: bool = False
    metadata_version: str | None = Field(None,max_length=64)
    soundtrack_choice: SoundtrackChoice | None = None


class ClassificationInput(BaseModel):
    classification: str


class RelationInput(BaseModel):
    child_id: str
    evidence: str = Field(min_length=1, max_length=2000)


def create_app(settings=None):
    cfg = settings or load_settings()
    engine, sessions = open_db(cfg.data)
    from .filesystem import FilesystemIndex, current_files, asset_path, preview_path
    from .db import FilesystemAlbum
    file_index = FilesystemIndex(cfg, sessions)
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(app):
        file_index.start()
        try:
            yield
        finally:
            file_index.close()
            engine.dispose()
    app = FastAPI(title="Coverly — Tamil Vinyl Archive", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings, app.state.sessions = cfg, sessions
    app.state.file_index = file_index

    from .chatgpt_transfer import Transfers
    transfers=Transfers()
    bridge_seen={"at":0}

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        host = request.headers.get("host", "")
        allowed = {"localhost:8000", "127.0.0.1:8000", "[::1]:8000"}
        if host not in allowed:
            return JSONResponse({"detail": "Coverly accepts localhost:8000 only"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("x-coverly-request") != "1" or (origin and origin not in {"http://" + h for h in allowed}):
                return JSONResponse({"detail": "Same-origin Coverly request required"}, status_code=403)
        response = await call_next(request)
        if request.url.path=="/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"]="no-cache, must-revalidate"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def require(s, model, identifier):
        obj = s.get(model, identifier)
        if not obj:
            raise HTTPException(404, "Record not found")
        return obj

    def require_idle(s):
        if (s.scalar(select(func.count()).select_from(Job).where(Job.status.in_(["queued", "running"])))
                or s.scalar(select(func.count()).select_from(JobItem).where(JobItem.status == "running"))):
            raise HTTPException(409, "Pause queued and running jobs before changing archive folders or creating a backup")

    def image_ids(s, ids):
        ids = list(dict.fromkeys(ids))
        found = list(s.scalars(select(ImageAsset.id).where(ImageAsset.id.in_(ids))))
        if len(found) != len(ids):
            raise HTTPException(404, "One or more selected images no longer exist")
        return found

    def zip_response(writer, name):
        fd, path = tempfile.mkstemp(suffix=".zip", prefix="coverly-export-")
        os.close(fd)
        try:
            writer(Path(path))
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        return FileResponse(path, media_type="application/zip", filename=name, background=BackgroundTask(Path(path).unlink, missing_ok=True))

    def album_record(s, album):
        value = record(album)
        value["metadata_version"] = metadata_version(album)
        page = s.scalar(select(Page).where(Page.album_id == album.id).limit(1))
        primary = next(iter(page.metadata_json.get('images', [])), {}) if page else {}
        images = list(s.execute(select(ImageAsset, AlbumImage).join(AlbumImage).where(AlbumImage.album_id == album.id, AlbumImage.deleted_at.is_(None))))
        files = current_files(s, album.id) if cfg.filesystem_sync else None
        if files is not None:
            images = [pair for pair in images if pair[0].id in files]
        value['filesystem_indexed'] = files is not None
        value['folder_present'] = within(cfg.data, album.folder).is_dir()
        # A shared disc scan may be older than this edition's sleeve. Prefer
        # the source gallery's primary photo over global asset creation time.
        images.sort(key=lambda pair: (pair[1].classification != "front",
                                     not (pair[0].sha256 == primary.get('sha256') or pair[0].source_url == primary.get('url')),
                                     pair[0].created))
        value["image_count"] = len(images)
        value["cover_id"] = images[0][0].id if images else None
        value['cover_url']=f"/api/albums/{album.id}/images/{images[0][0].id}/cover?thumbnail=true&v={images[0][0].sha256}" if images else None
        value["source"] = urlsplit(page.url).hostname if page else "Local import"
        return value

    @app.get("/api/health")
    def health():
        heartbeat = cfg.data / ".worker-heartbeat"
        import time
        active = heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 120
        return {"status": "ok", "version": __version__, "worker": active, "application": "coverly"}

    @app.get('/api/filesystem/status')
    def filesystem_status():
        return file_index.status()

    @app.post('/api/filesystem/refresh')
    def filesystem_refresh():
        file_index.wake.set()
        return file_index.status()

    @app.get('/api/desktop-info')
    def desktop_info():
        # Same local-host/origin guard as the settings page. Used by the native
        # launcher to reconnect to this archive without copying its contents.
        return {'application':'coverly', 'storage':str(cfg.data), 'version':__version__}

    @app.get("/api/docs", include_in_schema=False)
    def api_docs():
        return JSONResponse(app.openapi())

    @app.get("/api/stats")
    def stats():
        with sessions() as s:
            count = lambda model, *conditions: s.scalar(select(func.count()).select_from(model).where(*conditions))
            return {"albums": count(Album, Album.deleted_at.is_(None)), "images": count(ImageAsset), "review": count(Album, Album.review == True, Album.deleted_at.is_(None)),
                    "trash": count(Album, Album.deleted_at.is_not(None)),
                    "identified": count(Album, Album.confidence >= cfg.high_confidence, Album.deleted_at.is_(None)), "compilations": count(Album, Album.compilation == True, Album.deleted_at.is_(None)),
                    "duplicates": count(ImageAsset, ImageAsset.near_duplicate == True), "duplicate_skips": count(Audit, Audit.action == "duplicate_skipped"),
                    "restored": count(Restoration), "approved": count(Restoration, Restoration.status == "approved"),
                    "failed": count(JobItem, JobItem.status == "failed"), "active_jobs": count(Job, Job.status.in_(["queued", "running"])),
                    "storage_bytes": s.scalar(select(func.sum(ImageAsset.size))) or 0}

    @app.get("/api/albums")
    def albums(q: str = "", unknown: bool = False, review: bool = False, compilation: bool = False, duplicate: bool = False, trashed: bool = False, sort: str = "recent", offset: int = 0, limit: int = 48):
        with sessions() as s:
            query = select(Album).where(Album.deleted_at.is_not(None) if trashed else Album.deleted_at.is_(None))
            if cfg.filesystem_sync and not trashed:
                query = query.where(~Album.id.in_(select(FilesystemAlbum.album_id).where(FilesystemAlbum.present == False)))
            if q:
                query = query.where(or_(Album.title.icontains(q, autoescape=True), Album.tamil_title.icontains(q, autoescape=True), Album.artist.icontains(q, autoescape=True), Album.catalogue.icontains(q, autoescape=True), Album.folder.icontains(q, autoescape=True)))
            if unknown:
                query=query.where(Album.artist.in_(['Unknown Artist','Unknown','']))
            if review:
                query = query.where(Album.review == True)
            if compilation:
                query = query.where(Album.compilation == True)
            if duplicate:
                from .duplicates import artwork_groups
                ids = {m['album_id'] for group in artwork_groups(s) for m in group['members']}
                query = query.where(Album.id.in_(ids))
            total = s.scalar(select(func.count()).select_from(query.subquery()))
            query = query.order_by(Album.title if sort == "title" else Album.year.desc() if sort == "year" else Album.created.desc())
            rows = s.scalars(query.offset(max(0, offset)).limit(min(100, max(1, limit))))
            return {"items": [album_record(s, a) for a in rows], "total": total}

    @app.get('/api/albums/ids')
    def all_album_ids(q: str='',review: bool=False,unknown: bool=False,compilation: bool=False):
        with sessions() as s:
            query=select(Album.id).where(Album.deleted_at.is_(None))
            if cfg.filesystem_sync:query=query.where(~Album.id.in_(select(FilesystemAlbum.album_id).where(FilesystemAlbum.present==False)))
            if q:query=query.where(or_(Album.title.icontains(q,autoescape=True),Album.tamil_title.icontains(q,autoescape=True),Album.artist.icontains(q,autoescape=True),Album.catalogue.icontains(q,autoescape=True),Album.folder.icontains(q,autoescape=True)))
            if review:query=query.where(Album.review==True)
            if unknown:query=query.where(Album.artist.in_(['Unknown Artist','Unknown','']))
            if compilation:query=query.where(Album.compilation==True)
            ids=list(s.scalars(query.limit(50001)))
            if len(ids)>50000:raise HTTPException(409,'Narrow the selection to at most 50,000 albums.')
            return {'ids':ids,'total':len(ids)}

    @app.post('/api/metadata/bulk')
    def bulk_metadata_lookup(body: BulkLookupInput):
        from .bulk_metadata import fingerprint
        ids=list(dict.fromkeys(body.ids))
        with sessions() as s:
            albums=[]
            for offset in range(0,len(ids),500):
                albums.extend(s.scalars(select(Album).where(Album.id.in_(ids[offset:offset+500]),Album.deleted_at.is_(None))))
            if len(albums)!=len(ids):raise HTTPException(409,'Some selected albums were removed. Refresh the selection.')
            digest=hashlib.sha256(json.dumps([sorted(ids),body.overwrite_existing,body.move_folders]).encode()).hexdigest()
            existing=s.scalar(select(Job).where(Job.kind=='identify',Job.status.in_(['queued','running','paused']),Job.payload['selection_digest'].as_string()==digest))
            if existing:return job_snapshot(s,existing)
            payload={'bulk_apply':True,'lookup_internet':True,'auto_apply':False,'overwrite_existing':body.overwrite_existing,
                     'move_folders':body.move_folders,'selection_digest':digest,'provider_order':['Apple Music','Spotify','Wikipedia','Google']}
            job=enqueue(s,'identify',payload,[(a.id,{'title':a.title,'metadata_fingerprint':fingerprint(a)}) for a in albums])
            job.message=f'Queued metadata lookup for {len(albums)} albums · Apple Music → Spotify → Wikipedia → Google'
            s.commit()
            return job_snapshot(s,job)

    @app.get("/api/albums/{album_id}")
    def album_detail(album_id: str):
        file_index.scan_album(album_id)
        with sessions() as s:
            album = require(s, Album, album_id)
            value = album_record(s, album)
            value["images"] = [{**record(a), "classification": link.classification, "classification_confidence": link.confidence, "classification_evidence":link.evidence, "deleted_at":link.deleted_at,
                                "ocr": [record(o) for o in s.scalars(select(OCRResult).where(OCRResult.image_id == a.id))],
                                "display_url":f"/api/albums/{album.id}/images/{a.id}/cover?v={link.active_restoration_id or a.sha256}","active_restoration_id":link.active_restoration_id,"cover_path":link.cover_path,"restorations": [record(r) for r in s.scalars(select(Restoration).where(Restoration.image_id == a.id))]}
                               for a, link in s.execute(select(ImageAsset, AlbumImage).join(AlbumImage).where(AlbumImage.album_id == album.id))]
            value['trashed_images'] = [im for im in value['images'] if im['deleted_at']]
            value['images'] = [im for im in value['images'] if not im['deleted_at']]
            files = current_files(s, album.id) if cfg.filesystem_sync else None
            if files is not None:
                value['images'] = [im for im in value['images'] if im['id'] in files]
                for image in value['images']:
                    image['cover_path'] = files[image['id']].path
                    image['filename'] = Path(image['cover_path']).name
                    image['file_id'] = files[image['id']].id
            for image in value['images']:
                try:
                    image['original_available'] = asset_path(s,cfg,s.get(ImageAsset,image['id']),album.id if files is not None else None).is_file()
                except FileNotFoundError:
                    image['original_available'] = False
            value['images'].sort(key=lambda im:(im['id'] != value['cover_id'], im['classification'] != 'front', im['created']))
            lookup_query=select(JobItem).join(Job).where(Job.kind=='identify',JobItem.key==album_id)
            if album.soundtrack:
                accepted_at=album.soundtrack.get('accepted_at') or s.scalar(select(func.max(Audit.created)).where(Audit.target==album_id,Audit.action=='metadata_updated'))
                if accepted_at:
                    lookup_query=lookup_query.where(Job.created>accepted_at)
            lookups=list(s.scalars(lookup_query.order_by(Job.created.desc()).limit(5)))
            from .soundtracks import film_title
            value['lookup_title']=film_title(album.title)
            value['metadata_suggestions']=[v for item in lookups for v in item.data.get('suggestions',[])]
            value['soundtrack_candidates']=[]
            seen=set()
            for item in lookups:
                for index,doc in enumerate(item.data.get('soundtracks',[])):
                    identity=doc.get('source_url')
                    if identity not in seen:
                        seen.add(identity)
                        value['soundtrack_candidates'].append({'job_id':item.job_id,'index':index,'document':doc})
            latest=s.get(Job,lookups[0].job_id) if lookups else None
            value['metadata_lookup']={'id':latest.id,'status':latest.status,'message':latest.message,'source_url':latest.payload.get('source_url')} if latest else None
            value['metadata_lookup_errors']=[v for item in lookups[:1] for v in item.data.get('lookup_errors',[])]
            value["candidates"] = [record(c) for c in s.scalars(select(Candidate).where(Candidate.album_id == album.id))]
            value["pages"] = [record(p) for p in s.scalars(select(Page).where(Page.album_id == album.id))]
            value["components"] = [{"id": r.child_id, "title": require(s, Album, r.child_id).title, "evidence": r.evidence}
                                   for r in s.scalars(select(AlbumRelation).where(AlbumRelation.parent_id == album.id))]
            return value

    @app.get('/api/duplicates')
    def duplicates(q: str = '', offset: int = 0, limit: int = 32):
        from .duplicates import artwork_groups
        with sessions() as s:
            groups = artwork_groups(s, q)
            return {'items':groups[max(0,offset):max(0,offset)+min(100,max(1,limit))], 'total':len(groups)}

    @app.get('/api/duplicates/selection')
    def duplicate_selection(q: str = ''):
        from .duplicates import artwork_groups, keep_last_selection
        with sessions() as s:
            return keep_last_selection(artwork_groups(s, q))

    @app.post('/api/trash/preview')
    def preview_trash(body: TrashPreview):
        from .purge import preview
        with file_lock(cfg.data / '.archive.lock'), sessions() as s:
            plan=preview(s,cfg,body.ids)
            audit=Audit(action='trash_purge_preview',detail=plan);s.add(audit);s.commit()
            return {'plan_id':audit.id,**plan}

    @app.post('/api/trash/purge')
    def purge_trash(body: TrashPurge):
        from .purge import purge
        with file_lock(cfg.data / '.archive.lock'), sessions() as s:
            audit=require(s,Audit,body.plan_id)
            if audit.action!='trash_purge_preview':raise ValueError('Expected a permanent deletion preview')
            result=purge(s,cfg,audit.detail)
            audit.action='trash_purge_used';s.commit()
            return result

    @app.post('/api/albums/trash')
    def trash_albums(body: Selection):
        return change_trash(body, True)

    @app.post('/api/albums/restore')
    def restore_albums(body: Selection):
        return change_trash(body, False)

    def change_trash(body, removed):
        with file_lock(cfg.data / '.archive.lock'), sessions() as s:
            albums = [require(s, Album, key) for key in dict.fromkeys(body.ids)]
            for album in albums:
                album.deleted_at = now() if removed else None
                s.add(Audit(action='album_trashed' if removed else 'album_restored', target=album.id))
            s.flush()
            for album in albums:
                write_existing_manifest(s, cfg, album)
            s.commit()
            return {'ok':True, 'ids':[a.id for a in albums]}

    @app.post('/api/albums/{album_id}/images/{image_id}/trash')
    @app.post('/api/albums/{album_id}/images/{image_id}/restore')
    def image_trash(album_id: str, image_id: str, request: Request):
        action=request.url.path.rsplit('/',1)[-1]
        with file_lock(cfg.data / '.archive.lock'), sessions() as s:
            album = require(s, Album, album_id)
            link = s.get(AlbumImage, (album_id,image_id))
            if not link:
                raise HTTPException(404, 'Artwork does not belong to this album')
            link.deleted_at = now() if action == 'trash' else None
            s.add(Audit(action='artwork_'+action, target=image_id, detail={'album_id':album_id}))
            s.flush();write_existing_manifest(s, cfg, album);s.commit()
            return {'ok':True}

    @app.patch("/api/albums/{album_id}")
    def edit_album(album_id: str, body: AlbumUpdate):
        with file_lock(cfg.data / ".archive.lock"), sessions() as s:
            album = require(s, Album, album_id)
            if body.metadata_version and body.metadata_version!=metadata_version(album):
                raise HTTPException(409,'Album metadata changed while this window was open. Reopen the album before saving.')
            old = record(album)
            if body.soundtrack_choice:
                choice=body.soundtrack_choice
                item=s.scalar(select(JobItem).join(Job).where(Job.kind=='identify',Job.id==choice.job_id,JobItem.key==album_id))
                documents=item.data.get('soundtracks',[]) if item else []
                if choice.index>=len(documents):raise HTTPException(422,'Soundtrack suggestion is unavailable for this album. Run lookup again.')
                if not item or item.status!='done':raise HTTPException(409,'Wait for the soundtrack lookup to finish before saving its details.')
                album.soundtrack={**documents[choice.index], 'accepted_at':now(), 'accepted_job_id':choice.job_id}
            for key, value in body.model_dump(exclude={'move_folder','metadata_version','soundtrack_choice'}).items():
                setattr(album, key, value)
            artist = s.scalar(select(Artist).where(Artist.name == album.artist))
            if not artist:
                artist = Artist(name=album.artist)
                s.add(artist)
                s.flush()
            album.artist_id = artist.id
            if not album.review:
                album.confidence = 1
            s.add(Audit(action="metadata_updated", target=album.id, detail={"before": old, "after": body.model_dump()}))
            s.flush()
            if body.move_folder:
                from .archive_ops import relocate_album
                with relocate_album(s,cfg,album):s.commit()
            else:
                write_manifest(s, cfg, album);s.commit()
            from .archive_files import cleanup_covers
            cleanup_covers(s,cfg,album)
            return {"ok": True}

    @app.post('/api/albums/{album_id}/lookup')
    def lookup_album(album_id: str, body: MetadataLookupInput | None = None):
        if body and body.source_url:canonical_url(body.source_url)
        with sessions() as s:
            require(s,Album,album_id)
            existing=s.scalar(select(Job).join(JobItem).where(Job.kind=='identify',JobItem.key==album_id,Job.status.in_(['queued','running','paused'])).order_by(Job.created.desc()))
            if existing:return record(existing)
            job=enqueue(s,'identify',{'lookup_internet':True,'auto_apply':False,**((body.model_dump()) if body else {})},[(album_id,{})]);s.commit()
            return record(job)

    @app.post('/api/albums/{album_id}/metadata-import')
    def import_copied_metadata(album_id: str,body: CopiedMetadataInput):
        from .soundtracks import copied_table
        doc=copied_table(body.title,canonical_url(body.source_url),body.text,body.html,body.composer,body.year)
        with sessions() as s:
            require(s,Album,album_id)
            job=enqueue(s,'identify',{'copied_source':body.source_url},[(album_id,{'soundtracks':[doc],'suggestions':[],'lookup_errors':[]})])
            job.status='completed';job.progress=job.total=1;job.message='Copied metadata imported; review before saving'
            s.flush()
            item=s.scalar(select(JobItem).where(JobItem.job_id==job.id));item.status='done'
            s.add(Audit(action='copied_metadata_imported',target=album_id,detail={'source_url':doc['source_url'],'tracks':len(doc['tracks'])}))
            s.commit();return {'job_id':job.id,'document':doc}

    @app.post('/api/albums/{album_id}/metadata-page')
    def import_browser_metadata(album_id: str,body: MetadataPageInput):
        from .soundtracks import extract_html,film_title
        url=canonical_url(body.source_url)
        with sessions() as s:
            album=require(s,Album,album_id)
            doc=extract_html(body.html,url,film_title(body.title or album.title))
            if not doc and body.allow_title_mismatch:
                doc=extract_html(body.html,url)
                if doc:
                    doc['warnings'].append('The selected reference uses a different film title from the archive. Confirm this is the intended film before applying these details.')
            if not doc or not (doc['tracks'] or doc['composer']):
                raise ValueError('No matching readable film credits or track table found on this page. Open the film/album result, check its title, or use Import copied table.')
            doc['provider']='Browser page · '+(urlsplit(url).hostname or 'Source')
            doc['warnings'].append('Captured from the page you opened. Check the film, singer roles and edition before saving.')
            job=enqueue(s,'identify',{'browser_source':url},[(album_id,{'soundtracks':[doc],'suggestions':[],'lookup_errors':[]})])
            job.status='completed';job.progress=job.total=1;job.message='Browser metadata received; review before saving'
            s.flush();s.scalar(select(JobItem).where(JobItem.job_id==job.id)).status='done'
            s.add(Audit(action='browser_metadata_imported',target=album_id,detail={'source_url':url,'tracks':len(doc['tracks'])}));s.commit()
            return {'job_id':job.id,'document':doc}

    @app.get('/api/artists/proposals')
    def artist_proposals():
        from .artists import proposals
        with sessions() as s:return {'groups':proposals(s)}

    @app.post('/api/artists/lookup')
    def artist_lookup(body: dict):
        from .network import SafeHTTP
        from urllib.parse import urlencode
        name=str(body.get('name','')).strip()
        if not 2<=len(name)<=200:raise ValueError('Enter an artist name')
        fetcher=SafeHTTP(cfg,{'itunes.apple.com'})
        try:
            raw,_=fetcher.get('https://itunes.apple.com/search?'+urlencode({'term':name+' Tamil composer','entity':'musicArtist','limit':8,'country':'IN'}))
            return {'items':[{'name':r.get('artistName',''),'url':r.get('artistLinkUrl',''),'evidence':'Apple Music artist profile. Verify this is the same composer.'} for r in json.loads(raw).get('results',[])]}
        finally:fetcher.close()

    @app.post('/api/artists/preview')
    def artist_preview(body: dict):
        from .artists import preview
        with sessions() as s:
            plan=preview(s,cfg,body.get('groups',[]));s.commit();return plan

    @app.post('/api/artists/plans/{plan_id}/apply')
    def artist_apply(plan_id: str):
        from .db import ArtistAlias
        from .artists import key
        with file_lock(cfg.data/'.archive.lock'),sessions() as s:
            plan=require(s,Audit,plan_id)
            if plan.action!='artist_normalization_preview':raise ValueError('Expected an artist preview')
            existing=s.scalar(select(Job).where(Job.kind=='organize',Job.payload['plan_id'].as_string()==plan_id))
            if existing:return record(existing)
            for row in plan.detail['rows']:
                album=require(s,Album,row['id'])
                if album.artist!=row['old_artist'] or album.title!=row['title'] or album.year!=row['year']:raise HTTPException(409,'Preview is stale. Generate a fresh artist plan.')
            for group in plan.detail['groups']:
                artist=s.scalar(select(Artist).where(Artist.name==group['canonical']))
                if not artist:artist=Artist(name=group['canonical']);s.add(artist);s.flush()
                for name in group['names']:
                    alias=s.get(ArtistAlias,key(name))
                    if alias:alias.canonical_id=artist.id;alias.evidence={**group,'plan_id':plan_id}
                    else:s.add(ArtistAlias(alias=key(name),name=name,canonical_id=artist.id,evidence={**group,'plan_id':plan_id}))
            job=enqueue(s,'organize',{'plan_id':plan_id},[(r['id'],{**r,'plan_id':plan_id}) for r in plan.detail['rows']]);s.commit()
            return record(job)

    @app.post('/api/archive/organize')
    def organize_archive(body: Selection | None = None):
        with sessions() as s:
            active=s.scalar(select(Job).where(Job.kind=='organize',Job.status.in_(['queued','running','paused'])))
            if active:return record(active)
            query=select(Album.id).where(Album.deleted_at.is_(None))
            if body and body.ids:query=query.where(Album.id.in_(body.ids))
            ids=list(s.scalars(query));job=enqueue(s,'organize',{},[(ident,{}) for ident in ids]);s.commit();return record(job)

    @app.post('/api/metadata/repair-unknown')
    def repair_unknown_artists():
        with sessions() as s:
            existing=s.scalar(select(Job).where(Job.kind=='identify',Job.status.in_(['queued','running','paused'])))
            if existing and existing.payload.get('auto_apply'):return record(existing)
            ids=list(s.scalars(select(Album.id).where(Album.deleted_at.is_(None),Album.artist.in_(['Unknown Artist','Unknown','']))))
            if not ids: return {'id':None,'total':0,'message':'No Unknown Artist albums'}
            job=enqueue(s,'identify',{'auto_apply':True,'lookup_internet':False},[(key,{}) for key in ids]);s.commit()
            return record(job)

    @app.post('/api/classify-albums')
    def auto_classify(body: Selection):
        with sessions() as s:
            for key in body.ids:require(s,Album,key)
            job=enqueue(s,'classify',{},[(key,{}) for key in dict.fromkeys(body.ids)]);s.commit()
            return record(job)

    @app.post('/api/artwork/trash')
    def bulk_artwork_trash(body: ArtworkSelection):
        with file_lock(cfg.data / '.archive.lock'), sessions() as s:
            protected={(i.album_id,i.image_id) for i in body.protected}
            if protected.intersection((i.album_id,i.image_id) for i in body.items):
                raise HTTPException(409,'A selected image is marked Keep. Clear the automatic selection before choosing a different policy.')
            for album_id,image_id in protected:
                keeper=s.get(AlbumImage,(album_id,image_id))
                album=s.get(Album,album_id)
                if not keeper or keeper.deleted_at or not album or album.deleted_at:
                    raise HTTPException(409,'A kept image changed since selection. Regenerate the keep-last selection before removing anything else.')
                asset=s.get(ImageAsset,image_id)
                try:
                    if not asset:raise FileNotFoundError()
                    asset_path(s,cfg,asset,album_id)
                except FileNotFoundError:
                    raise HTTPException(409,'An image marked Keep is missing on disk. Select all except last again to exclude unavailable groups, then review and remove the remaining selection.')
            links=[]
            for item in body.items:
                link=s.get(AlbumImage,(item.album_id,item.image_id))
                if not link:raise HTTPException(404,'Artwork relationship not found')
                if not link.deleted_at:links.append(link)
            for link in links:link.deleted_at=now()
            s.flush()
            # Removing a relationship must not read or regenerate unrelated
            # originals. Their on-disk availability is independent of removal.
            for key in {link.album_id for link in links}:write_existing_manifest(s,cfg,require(s,Album,key))
            s.add(Audit(action='artwork_bulk_trashed',detail={'count':len(links)}));s.commit()
            return {'ok':True,'count':len(links)}

    @app.post('/api/artwork/restore')
    def bulk_artwork_restore(body: ArtworkSelection):
        with file_lock(cfg.data / '.archive.lock'), sessions() as s:
            albums=set()
            for item in body.items:
                link=require(s,AlbumImage,(item.album_id,item.image_id))
                link.deleted_at=None;albums.add(item.album_id)
            s.flush()
            for key in albums:write_existing_manifest(s,cfg,require(s,Album,key))
            s.add(Audit(action='artwork_bulk_restored',detail={'count':len(body.items)}));s.commit()
            return {'ok':True,'count':len(body.items)}

    @app.post("/api/albums/{album_id}/components")
    def add_component(album_id: str, body: RelationInput):
        with sessions() as s:
            album = require(s, Album, album_id)
            require(s, Album, body.child_id)
            if album_id == body.child_id:
                raise ValueError("An album cannot contain itself")
            album.compilation = True
            relation = s.get(AlbumRelation, (album_id, body.child_id))
            if not relation:
                s.add(AlbumRelation(parent_id=album_id, child_id=body.child_id, evidence=body.evidence))
            s.commit()
            write_manifest(s, cfg, album)
            return {"ok": True}

    @app.patch("/api/albums/{album_id}/images/{image_id}")
    def reclassify(album_id: str, image_id: str, body: ClassificationInput):
        if body.classification not in KINDS:
            raise ValueError("Unknown classification")
        with file_lock(cfg.data/".archive.lock"),sessions() as s:
            link = s.get(AlbumImage, (album_id, image_id))
            if not link:
                raise HTTPException(404, "Image relationship not found")
            link.classification, link.confidence, link.manual = body.classification, 1, True
            link.evidence = "Manually classified"
            s.add(Audit(action="image_classified", target=image_id, detail={"album_id": album_id, "kind": body.classification}))
            s.flush()
            write_manifest(s, cfg, require(s, Album, album_id))
            s.commit()
            from .archive_files import cleanup_covers
            cleanup_covers(s,cfg,require(s,Album,album_id))
            return {"ok": True}

    @app.post("/api/import")
    def manual_import(files: list[UploadFile] = File(...), title: str = Form(""), artist: str = Form("Unknown Artist"), year: str = Form(""), album_id: str = Form(""), source_page: str = Form(""), source_images: str = Form("{}"), label: str = Form(""), catalogue: str = Form(""), metadata_confirmed: bool = Form(True)):
        if len(files) > 100:
            raise ValueError("Import at most 100 images per batch")
        if len(source_page) > 4096 or len(source_images) > 500_000:
            raise ValueError("Source metadata exceeds the import limit")
        page_url = canonical_url(source_page) if source_page.strip() else ""
        source_map = json.loads(source_images)
        if not isinstance(source_map, dict) or len(source_map) > 100 or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 4096 for k, v in source_map.items()):
            raise ValueError("Image sources must map uploaded filenames to source URLs")
        source_map = {name: canonical_url(url) for name, url in source_map.items()}
        metadata = {"title": title[:250], "artist": artist[:200], "year": year, "label": label[:200], "catalogue": catalogue[:200],
                    "manual": metadata_confirmed, 'structured': bool(page_url) and not metadata_confirmed}
        with file_lock(cfg.data / ".archive.lock"), sessions() as s:
            album = require(s, Album, album_id) if album_id else create_album(s, cfg, metadata, source_url=page_url or None)
            if not metadata_confirmed:
                album.review = True
            if page_url:
                domain = urlsplit(page_url).hostname
                source = s.scalar(select(Source).where(Source.domain == domain))
                if not source:
                    source = Source(domain=domain)
                    s.add(source);s.flush()
                page = s.scalar(select(Page).where(Page.url == page_url))
                if page and page.album_id and page.album_id != album.id:
                    raise ValueError("This source page belongs to another album; open that album to add artwork")
                if not page:
                    page = Page(source_id=source.id, url=page_url, title=album.title,
                                metadata_json={"is_product": True, "metadata": metadata, "images": [], "adapter": "browser-saved-import"})
                    s.add(page)
                page.album_id = album.id
            # Validate entire batch before committing any records.
            validated = []
            for file in files:
                data = file.file.read(cfg.max_image_mb * 1024 * 1024 + 1)
                if len(data) > cfg.max_image_mb * 1024 * 1024:
                    raise ValueError("Image exceeds configured size limit")
                decode_image(data)
                validated.append((file, data))
            skipped = 0
            assets = []
            provenance = {}
            for file, data in validated:
                asset, duplicate = add_image(s, cfg, album, data, source_url=source_map.get(file.filename or "", ""), page_url=page_url, caption=file.filename or "")
                assets.append(asset)
                skipped += duplicate
                if source_map.get(file.filename or ''):
                    original_url = source_map[file.filename or '']
                    provenance[original_url] = {'url': original_url, 'sha256': asset.sha256, 'caption': file.filename or '',
                                                'alternatives': [{'url': original_url}]}
            if page_url and provenance:
                previous = {g['url']: g for g in page.metadata_json.get('images', []) if g.get('url')}
                page.metadata_json = {**page.metadata_json, 'images': list({**previous, **provenance}.values()),
                                      'pending_gallery': False, 'browser_assisted': False, 'adapter': 'browser-saved-import'}
            s.flush()
            write_manifest(s, cfg, album)
            maybe_enqueue_ocr(s, cfg, assets)
            s.add(Audit(action="browser_import" if page_url else "manual_import", target=album.id,
                        detail={"images": len(files), "duplicates": skipped, "source_page": page_url, "original_source_urls": len(source_map)}))
            s.commit()
            return {"album_id": album.id, "duplicates": skipped, "imported": len(files) - skipped}

    @app.get("/api/images/{image_id}/{variant}")
    def image_file(image_id: str, variant: Literal["thumbnail", "original", "jpg"]):
        with sessions() as s:
            image = require(s, ImageAsset, image_id)
            if image.original.startswith('.filesystem/'):
                try:
                    path = preview_path(s,cfg,image) if variant == 'thumbnail' else asset_path(s,cfg,image)
                except (FileNotFoundError,OSError):
                    raise HTTPException(404, 'Artwork is no longer in the album folder')
                return FileResponse(path,filename=path.name if variant=='original' else None,headers={'Cache-Control':'no-cache'})
            path = within(cfg.data, image.original if variant == "original" else image.derivative if variant == "jpg" else image.thumbnail)
            if not path.is_file():
                raise HTTPException(404, "Archive file is missing; check storage location")
            return FileResponse(path, filename=path.name if variant == "original" else None)

    @app.post("/api/discover")
    def discover(body: DiscoverInput):
        url = canonical_url(body.url)
        with file_lock(cfg.data / '.discovery-queue.lock'), sessions() as s:
            for existing in s.scalars(select(Job).where(Job.kind == 'discover', Job.status.in_(['queued', 'running', 'paused'])).order_by(Job.created)):
                if existing.payload.get('url') == url and bool(existing.payload.get('browser_assisted')) == body.browser_assisted:
                    if body.auto_download:existing.payload={**existing.payload,'auto_download':True,'max_pages':max(existing.payload.get('max_pages',0),body.max_pages)}
                    if existing.status=='paused':existing.status,existing.updated='queued',now()
                    s.commit()
                    return {**record(existing), 'reused': True}
            job = enqueue(s, "discover", {**body.model_dump(), "url": url})
            s.commit()
            return record(job)

    @app.post('/api/discover/{job_id}/continue')
    def continue_discovery(job_id: str):
        with sessions() as s:
            job = require(s, Job, job_id)
            if job.kind != 'discover' or job.status not in {'completed', 'completed_with_errors'} or not job.payload.get('queue'):
                raise HTTPException(409, 'This catalogue has no remaining queued pages')
            job.payload = {**job.payload, 'max_pages':cfg.max_pages, 'limit_reached':False}
            if len(job.payload.get('seen', [])) >= cfg.max_pages:
                raise HTTPException(409, 'Increase Maximum pages in Settings before continuing')
            job.status, job.message, job.updated = 'queued', 'Continuing remaining catalogue pages', now()
            s.commit()
            return record(job)

    @app.get("/api/discoveries")
    def discoveries(q: str = "", offset: int = 0, job_id: str | None = None):
        with sessions() as s:
            query = select(Page)
            if job_id:
                job = require(s, Job, job_id)
                query = query.where(Page.url.in_(job.payload.get('found_urls', [])))
            if q:
                query = query.where(Page.title.icontains(q, autoescape=True))
            return {"items": [record(p) for p in s.scalars(query.order_by(Page.created.desc()).offset(max(0, offset)).limit(100))],
                    "total": s.scalar(select(func.count()).select_from(query.subquery()))}

    @app.get("/api/discovery-status")
    def discovery_status(job_id: str | None = None):
        with sessions() as s:
            query = select(Job).where(Job.kind == "discover")
            if job_id:
                query = query.where(Job.id == job_id)
            job = s.scalar(query.order_by(Job.created.desc()).limit(1))
            if not job:
                if job_id:
                    raise HTTPException(404, "Discovery job not found")
                return {"job": None}
            return {"job": job_snapshot(s, job)}

    @app.post("/api/download")
    def download(body: DownloadInput):
        with sessions() as s:
            if body.discovery_job_id:
                parent = require(s, Job, body.discovery_job_id)
                if parent.kind != 'discover' or parent.status not in {'completed','completed_with_errors'}:
                    raise HTTPException(409, 'Wait for discovery to finish before downloading all results')
                if parent.payload.get('download_job_id'):
                    prior = s.get(Job, parent.payload['download_job_id'])
                    if prior and prior.status in {'queued','running','paused'} and (prior.payload.get('selection_mode')=='all' or prior.total>=len(parent.payload.get('found_urls',[]))):
                        if prior.status=='paused':prior.status,prior.message,prior.updated='queued','Resuming requested catalogue download',now();s.commit()
                        return {**record(prior),'reused':True}
                pages = list(s.scalars(select(Page).where(Page.url.in_(parent.payload.get('found_urls', [])))))
            else:
                parent = require(s,Job,body.source_job_id) if body.source_job_id else None
                pages = [require(s, Page, key) for key in dict.fromkeys(body.ids)]
                if parent and any(page.url not in parent.payload.get('found_urls',[]) for page in pages):
                    raise ValueError('Selected albums do not belong to this catalogue')
            job = enqueue_download(s, pages, {'parent_job_id':parent.id, 'source_url':parent.payload.get('url'),
                                             'pages_total':parent.payload.get('pages_total'),'selection_mode':'all' if body.discovery_job_id else 'selected'} if parent else {})
            if parent:
                parent.payload = {**parent.payload, 'download_job_id':job.id}
            s.commit()
            return record(job)

    @app.post("/api/ocr")
    def queue_ocr(body: Selection):
        caps = ocr_capabilities()
        if not caps["available"] or set(cfg.ocr_languages.split("+")) - set(caps["languages"]):
            raise HTTPException(409, "Install Tesseract and Tamil/English language data; see Settings → OCR")
        with sessions() as s:
            ids = image_ids(s, body.ids)
            job = enqueue(s, "ocr", {}, [(i, {}) for i in ids])
            s.commit()
            return record(job)

    @app.get("/api/jobs")
    def jobs():
        with sessions() as s:
            recent=select(Job.id).where(Job.status.not_in(['queued','running','paused'])).order_by(Job.created.desc()).limit(75)
            query=select(Job).where(or_(Job.status.in_(['queued','running','paused']),Job.id.in_(recent))).order_by(Job.created.desc())
            return [job_snapshot(s,j) for j in s.scalars(query)]

    @app.get('/api/jobs/finished-ids')
    def finished_job_ids():
        with sessions() as s:
            return list(s.scalars(select(Job.id).where(Job.status.in_(['completed','completed_with_errors','failed','cancelled']))))

    @app.get('/api/jobs/{job_id}')
    def job_status(job_id: str):
        with sessions() as s:return job_snapshot(s,require(s,Job,job_id))

    @app.get("/api/jobs/{job_id}/items")
    def job_items(job_id: str):
        with sessions() as s:
            require(s, Job, job_id)
            return [record(i) for i in s.scalars(select(JobItem).where(JobItem.job_id == job_id).limit(1000))]

    @app.get('/api/jobs/{job_id}/browser-state')
    def browser_state(job_id: str):
        from .verification import state
        with sessions() as s:
            job = require(s, Job, job_id)
            return state(cfg, job_id) if job.status == 'running' else {'waiting': False}

    @app.get('/api/jobs/{job_id}/browser-screen')
    def browser_screen(job_id: str):
        from .verification import state, directory
        with sessions() as s:
            job = require(s, Job, job_id)
            if job.status != 'running' or not state(cfg, job_id).get('waiting'):
                raise HTTPException(409, 'No active verification screen')
        screen = directory(cfg, job_id) / 'screen.png'
        if not screen.is_file():
            raise HTTPException(404, 'Browser screen is preparing')
        # Read an atomic snapshot instead of opening a replaceable file later.
        from fastapi.responses import Response
        return Response(screen.read_bytes(), media_type='image/png', headers={'Cache-Control': 'no-store'})

    @app.post('/api/jobs/{job_id}/browser-input')
    def browser_input(job_id: str, body: BrowserAction):
        from .verification import state, directory
        with sessions() as s:
            job = require(s, Job, job_id)
            if job.status != 'running' or not state(cfg, job_id).get('waiting'):
                raise HTTPException(409, 'This browser is not waiting for verification')
        if body.action == 'click' and (body.x is None or body.y is None):
            raise ValueError('A click needs both coordinates')
        if body.action == 'key' and not body.key:
            raise ValueError('Choose a supported verification key')
        commands = directory(cfg, job_id) / 'commands'
        commands.mkdir(parents=True, exist_ok=True)
        with file_lock(commands / '.input.lock'):
            if len(list(commands.glob('*.json'))) >= 10:
                raise HTTPException(429, 'Wait for the browser to process your previous input')
            atomic_write(commands / (uid() + '.json'), json.dumps({**body.model_dump(), 'created': time.time()}).encode())
        return {'accepted': True}

    @app.post("/api/jobs/{job_id}/{action}")
    def control_job(job_id: str, action: Literal["pause", "resume", "cancel", "retry"]):
        with sessions() as s:
            job = require(s, Job, job_id)
            if action == "pause" and job.status in {"queued", "running"}:
                job.status, job.message = "paused", "Paused after the current item"
            elif action == "resume" and job.status == "paused":
                if s.scalar(select(func.count()).select_from(JobItem).where(JobItem.job_id == job_id, JobItem.status == "running")):
                    raise HTTPException(409, "The current item is still finishing. Resume in a moment.")
                job.status, job.message = "queued", "Resuming from checkpoint"
            elif action == "cancel" and job.status in {"queued", "running", "paused"}:
                job.status, job.message = "cancelled", "Cancelled after the current item"
            elif action == "retry" and job.status in {"failed", "completed_with_errors", "cancelled"}:
                if s.scalar(select(func.count()).select_from(JobItem).where(JobItem.job_id == job_id, JobItem.status == "running")):
                    raise HTTPException(409, "The cancelled job is still finishing its current item. Retry in a moment.")
                failed = list(s.scalars(select(JobItem).where(JobItem.job_id == job_id, JobItem.status == "failed")))
                if job.kind == "discover":
                    payload = dict(job.payload)
                    failures = {i.key for i in failed}
                    payload["seen"] = [u for u in payload.get("seen", []) if u not in failures]
                    payload["queue"] = [[i.key, i.data.get("depth", 0)] for i in failed] + payload.get("queue", [])
                    job.payload = payload
                for item in failed:
                    item.status, item.error = "pending", ""
                job.status, job.message = "queued", "Retrying unfinished items"
            else:
                raise HTTPException(409, f"Cannot {action} a {job.status} job")
            job.updated = now()
            s.add(Audit(action="job_" + action, target=job_id))
            s.commit()
            return record(job)

    @app.get("/api/events")
    async def events(request: Request):
        async def stream():
            while not await request.is_disconnected():
                with sessions() as s:
                    recent=list(s.scalars(select(Job).order_by(Job.created.desc()).limit(50)))
                    active=list(s.scalars(select(Job).where(Job.status.in_(['queued','running','paused']))))
                    jobs={j.id:j for j in [*recent,*active]}
                    snapshot=[{'id':j.id,'kind':j.kind,'status':j.status,'progress':j.progress,'total':j.total,'message':j.message,'updated':j.updated} for j in jobs.values()]
                yield "data: " + json.dumps(snapshot) + "\n\n"
                yield "event: filesystem\ndata: " + json.dumps(file_index.status()) + "\n\n"
                await asyncio.sleep(2)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/search")
    def search_api(q: str = ""):
        if len(q.strip()) < 2 or len(q) > 300:
            raise ValueError("Enter a search between 2 and 300 characters")
        from .search import search
        try:
            results = search(cfg, q.strip())
        except Exception as exc:
            raise HTTPException(502, str(exc)[:1000]) from exc
        with sessions() as s:
            for result in results:
                result["existing"] = bool(s.scalar(select(Page).where(Page.url == result["url"], Page.album_id.is_not(None))))
        return {"items": results, "provider": cfg.search_provider}

    @app.get("/api/search-preview")
    def search_preview(url: str):
        from .network import SafeHTTP
        from fastapi.responses import Response
        fetcher = SafeHTTP(cfg, {urlsplit(canonical_url(cfg.search_site)).hostname, *cfg.image_domains})
        try:
            raw, _ = fetcher.get(canonical_url(url), image=True)
            im, _ = decode_image(raw)
            im.thumbnail((480, 480))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85)
            return Response(out.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
        except Exception as exc:
            raise HTTPException(404, "Preview unavailable under current domain/access policy") from exc
        finally:
            fetcher.close()

    @app.post("/api/restorations")
    def queue_restoration(body: RestoreInput):
        if body.preset=='ai':
            from .ai_restoration import capabilities
            if not capabilities()['available']:raise HTTPException(409,capabilities()['setup'])
        with sessions() as s:
            ids = image_ids(s, body.ids)
            job = enqueue(s, "restore", {"preset": body.preset, "size": body.size, "square":body.square}, [(i, {'album_id':body.image_albums.get(i)}) for i in ids])
            for ident,album_id in body.image_albums.items():
                if ident in ids and not s.get(AlbumImage,(album_id,ident)):raise ValueError('Image does not belong to selected album')
            s.commit()
            return record(job)

    @app.get('/api/restoration-providers')
    def restoration_providers():
        from .ai_restoration import capabilities
        return {'hardware':hardware(),'ai':capabilities(),'local':{'available':True}}

    @app.get("/api/restorations")
    def restorations(status: Literal["all","pending","approved","rejected"] = "all"):
        with sessions() as s:
            values=[]
            for r in s.scalars(select(Restoration).where(Restoration.status==status if status!="all" else True).order_by(Restoration.created.desc()).limit(500)):
                album=s.get(Album,r.album_id) if r.album_id else s.scalar(select(Album).join(AlbumImage).where(AlbumImage.image_id==r.image_id,Album.deleted_at.is_(None)).limit(1))
                values.append({**record(r),'album_id':album.id if album else None,'title':album.title if album else 'Artwork'})
            return values

    @app.get("/api/restorations/{restore_id}/image")
    def restored_image(restore_id: str):
        with sessions() as s:
            result = require(s, Restoration, restore_id)
            return FileResponse(within(cfg.data, result.path), media_type="image/jpeg")

    @app.post('/api/restorations/{restore_id}/{action}')
    def review_restoration(restore_id: str, action: Literal['approve','reject','pending'], body: dict | None = None):
        from .artwork import review
        with file_lock(cfg.data/'.archive.lock'),sessions() as s:
            result=require(s,Restoration,restore_id)
            review(s,cfg,result,action,(body or {}).get('album_id'))
            return record(result)

    @app.get('/api/albums/{album_id}/images/{image_id}/cover')
    def current_cover(album_id: str,image_id: str,thumbnail: bool=False):
        from .archive_files import active_path
        with sessions() as s:
            asset=require(s,ImageAsset,image_id);link=s.get(AlbumImage,(album_id,image_id))
            if not link:raise HTTPException(404,'Artwork is not attached to this album')
            files = current_files(s,album_id) if cfg.filesystem_sync else None
            if files is not None:
                try:
                    source = asset_path(s,cfg,asset,album_id)
                    if thumbnail:
                        # Album-specific current file, even when its source asset
                        # is shared with another edition or an old Original.
                        from .filesystem import signature
                        version=hashlib.sha256((asset.sha256+signature(source)).encode()).hexdigest()
                        target=cfg.data/'.library-cache'/(version+'.jpg')
                        if not target.is_file():
                            im,_=decode_image(source.read_bytes());im.thumbnail((480,480));out=io.BytesIO();im.save(out,'JPEG',quality=86)
                            try:atomic_write(target,out.getvalue(),exclusive=True)
                            except FileExistsError:pass
                        source=target
                    return FileResponse(source,headers={'Cache-Control':'no-cache'},filename=None)
                except (FileNotFoundError,OSError):
                    raise HTTPException(404,'Artwork is no longer in this album folder')
            path=link.cover_path if link.cover_path and within(cfg.data,link.cover_path).is_file() else active_path(s,asset,link)[0]
            if not within(cfg.data,path).is_file():
                raise HTTPException(404,'Artwork file is missing on disk. The library will reflect the current files after its next scan.')
            return FileResponse(within(cfg.data,path),headers={'Cache-Control':'no-cache'})

    @app.post('/api/albums/{album_id}/reveal')
    def reveal_album_artwork(album_id: str, body: dict):
        from .file_manager import reveal
        from .archive_files import active_path
        file_index.scan_album(album_id)
        with sessions() as s:
            album=require(s,Album,album_id)
            image_id=body.get('image_id')
            path=within(cfg.data,album.folder)
            if image_id:
                if not isinstance(image_id,str):raise ValueError('Expected an artwork ID')
                asset=require(s,ImageAsset,image_id)
                link=s.get(AlbumImage,(album_id,image_id))
                if not link or link.deleted_at:raise HTTPException(404,'Artwork is not attached to this album')
                try:
                    files=current_files(s,album_id) if cfg.filesystem_sync else None
                    path=asset_path(s,cfg,asset,album_id) if files is not None else within(cfg.data,active_path(s,asset,link)[0])
                except FileNotFoundError:
                    raise HTTPException(404,'This artwork is no longer on disk. Refresh files and try again.')
            try:reveal(path)
            except FileNotFoundError as exc:raise HTTPException(404,str(exc))
            except OSError:raise HTTPException(503,'The system file manager could not open. Check that a desktop session is available.')
            return {'opened':True,'folder':str(path.parent if path.is_file() else path)}

    @app.post('/api/albums/{album_id}/images/{image_id}/replacement')
    def replace_artwork(album_id: str,image_id: str,file: UploadFile = File(...), generated: bool = Form(True)):
        from .artwork import review
        data=file.file.read(cfg.max_image_mb*1024*1024+1)
        if len(data)>cfg.max_image_mb*1024*1024:raise ValueError('Replacement exceeds image size limit')
        im,fmt=decode_image(data);ident=uid();path=f'Restored/replacements/{ident}.jpg'
        out=io.BytesIO()
        if fmt=='JPEG':raw=data
        else:im.save(out,'JPEG',quality=cfg.jpg_quality);raw=out.getvalue()
        with file_lock(cfg.data/'.archive.lock'),sessions() as s:
            link=s.get(AlbumImage,(album_id,image_id))
            if not link or link.deleted_at:raise HTTPException(404,'Active album artwork not found')
            require(s,ImageAsset,image_id)
            atomic_write(within(cfg.data,path),raw,exclusive=True)
            input_path=path
            if fmt!='JPEG':
                ext={'PNG':'png','WEBP':'webp','TIFF':'tif','BMP':'bmp'}[fmt]
                input_path=f'Restored/replacements/{ident}.input.{ext}'
                atomic_write(within(cfg.data,input_path),data,exclusive=True)
            result=Restoration(id=ident,album_id=album_id,image_id=image_id,path=path,input_path=input_path,provider='user-replacement',preset='replacement',generated=generated)
            s.add(result);s.flush();review(s,cfg,result,'approve',album_id)
            return record(result)

    @app.get('/api/albums/{album_id}/images/{image_id}/embedding')
    def embedding_preview(album_id: str,image_id: str,mode: Literal['crop','pad','trim']='crop',edge: int = 2000,download: bool = False,position_x: float = .5,position_y: float = .5):
        from .embedding import square_jpeg
        from .archive_files import active_path
        if not 128<=edge<=3000:raise ValueError('Choose an edge from 128 to 3000 pixels')
        with sessions() as s:
            asset=require(s,ImageAsset,image_id);link=s.get(AlbumImage,(album_id,image_id))
            if not link:raise HTTPException(404,'Artwork is not attached to this album')
            raw,info=square_jpeg(within(cfg.data,active_path(s,asset,link)[0]).read_bytes(),edge,499000,mode,position_x,position_y)
            headers={'X-Coverly-Embedding':json.dumps(info)}
            if download:
                from urllib.parse import quote
                from .archive_files import LABELS
                from .storage import safe_name
                album=require(s,Album,album_id)
                filename=safe_name(album.title)[:64]+' - '+LABELS.get(link.classification,'Additional')+'.jpg'
                headers['Content-Disposition']="attachment; filename*=UTF-8''"+quote(filename)
            return Response(raw,media_type='image/jpeg',headers=headers)

    @app.post('/api/embedding/export')
    def export_embedding(body: dict):
        from .embedding import square_jpeg
        from .archive_files import active_path,LABELS
        from .storage import safe_name
        items=body.get('items',[])
        if not items or len(items)>1000:raise ValueError('Select 1 to 1000 artwork images')
        mode=body.get('mode','crop');edge=int(body.get('edge',2000))
        position_x=float(body.get('position_x',.5));position_y=float(body.get('position_y',.5))
        if not 128<=edge<=3000:raise ValueError('Choose an edge from 128 to 3000 pixels')
        def write(path):
            with sessions() as s,zipfile.ZipFile(path,'w',zipfile.ZIP_STORED) as z:
                manifest=[]
                for item in items:
                    album=require(s,Album,item['album_id']);asset=require(s,ImageAsset,item['image_id']);link=s.get(AlbumImage,(album.id,asset.id))
                    if not link or link.deleted_at:raise ValueError('An image is no longer attached to this album')
                    raw,info=square_jpeg(within(cfg.data,active_path(s,asset,link)[0]).read_bytes(),edge,499000,mode,position_x,position_y)
                    name=safe_name(album.title)[:64]+' - '+LABELS.get(link.classification,'Additional')+' - '+asset.id[:8]+'.jpg'
                    z.writestr(album.id[:12]+'/'+name,raw);manifest.append({'album_id':album.id,'image_id':asset.id,'file':name,**info})
                z.writestr('embedding-artwork.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        return zip_response(write,'coverly-square-artwork.zip')

    @app.post('/api/chatgpt-transfers')
    def create_transfer(body: dict):
        items=body.get('items',[]);prompt=str(body.get('prompt',''))
        if not prompt or len(prompt)>10000:raise ValueError('Invalid restoration prompt')
        from .chatgpt_transfer import bundle
        token=transfers.create(items,prompt)
        with file_lock(cfg.data/'.archive.lock'),sessions() as s:bundle(s,cfg,transfers.get(token))
        return {'token':token,'expires_in':600}

    @app.get('/api/chatgpt-transfers')
    def transfer_states():
        return {'items':[{'id':token[:8],'status':r['status'],'message':r['message'],'count':len(r['items']),'expires':r['expires']} for token,r in transfers.items.items() if r['expires']>time.time()]}

    @app.get('/api/chatgpt-transfers/{token}')
    def transfer_files(token: str):
        from .chatgpt_transfer import bundle
        with file_lock(cfg.data/'.archive.lock'),sessions() as s:
            return JSONResponse(bundle(s,cfg,transfers.get(token)),headers={'Cache-Control':'no-store'})

    @app.post('/api/chatgpt-transfers/{token}/status')
    def transfer_status(token: str,body: dict):
        transfers.status(token,str(body.get('status','')),body.get('message',''));return {'ok':True}

    @app.post('/api/browser-bridge/presence')
    def bridge_presence():
        bridge_seen['at']=time.time();return {'ok':True}

    @app.get('/api/browser-bridge/status')
    def bridge_status():
        return {'connected':time.time()-bridge_seen['at']<90,'last_seen':bridge_seen['at']}

    @app.get('/api/interface-version')
    def interface_version():
        return JSONResponse({name:hashlib.sha256((STATIC/name).read_bytes()).hexdigest()[:12] for name in ('app.js','style.css','theme.js')},headers={'Cache-Control':'no-store'})

    @app.get('/api/browser-bridge')
    def bridge_package():
        root=Path(__file__).resolve().parents[1]/'browser-bridge'
        def write(path):
            with zipfile.ZipFile(path,'w',zipfile.ZIP_STORED) as z:
                for name in ['manifest.json','background.js','handoff.js','capture.js','popup.html','popup.js','README.md']:z.write(root/name,'coverly-browser-bridge/'+name)
        return zip_response(write,'coverly-browser-bridge.zip')

    @app.get('/api/albums/{album_id}/images/{image_id}/clipboard')
    def clipboard_image(album_id: str,image_id: str):
        from .archive_files import active_path
        with sessions() as s:
            asset=require(s,ImageAsset,image_id);link=s.get(AlbumImage,(album_id,image_id))
            if not link:raise HTTPException(404,'Artwork is not attached to this album')
            im,_=decode_image(within(cfg.data,active_path(s,asset,link)[0]).read_bytes())
            out=io.BytesIO();im.save(out,'PNG')
            return Response(out.getvalue(),media_type='image/png',headers={'Cache-Control':'no-store'})

    @app.post("/api/codex/export")
    def codex_export(body: Selection, target: Literal["codex","chatgpt"] = "codex"):
        with sessions() as s:
            ids = image_ids(s, body.ids)
            assets = list(s.scalars(select(ImageAsset).where(ImageAsset.id.in_(ids))))
            manifest = codex_manifest(assets)
            if target=='chatgpt':
                manifest['provider']='chatgpt-interactive'
                manifest['instructions']='Extract the ZIP. Attach selected files from originals in ChatGPT with each restoration prompt. Keep image IDs in output filenames. Import each finished result into the matching Coverly image for review; originals are preserved.'
            def write(path):
                with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
                    z.writestr("restoration-job.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                    for asset, entry in zip(assets, manifest["images"]):
                        z.write(asset_path(s,cfg,asset), entry["original"])
            return zip_response(write, "coverly-codex-restoration.zip")

    @app.post("/api/codex/import/{image_id}")
    def codex_import(image_id: str, file: UploadFile = File(...),album_id: str = Form("")):
        data = file.file.read(cfg.max_image_mb * 1024 * 1024 + 1)
        if len(data) > cfg.max_image_mb * 1024 * 1024:
            raise ValueError("Result exceeds size limit")
        im, fmt = decode_image(data)
        identifier = uid()
        out = io.BytesIO()
        if fmt=="JPEG":out.write(data)
        else:im.save(out, "JPEG", quality=cfg.jpg_quality, subsampling=0)
        path = f"Restored/imported/{identifier}.jpg"
        with sessions() as s:
            require(s, ImageAsset, image_id)
            if album_id and not s.get(AlbumImage,(album_id,image_id)):raise ValueError("Artwork is not attached to this album")
            ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "TIFF": "tif", "BMP": "bmp"}[fmt]
            input_path=path if fmt=="JPEG" else f"Restored/imported/{identifier}.input.{ext}"
            if fmt!="JPEG":atomic_write(within(cfg.data,input_path),data,exclusive=True)
            atomic_write(within(cfg.data, path), out.getvalue(), exclusive=True)
            result = Restoration(id=identifier, album_id=album_id or None, image_id=image_id, path=path, input_path=input_path, provider="codex-interactive", preset="advanced", generated=True)
            s.add(result)
            s.add(Audit(action="external_restoration_imported", target=identifier, detail={"requires_approval": True}))
            s.commit()
            return record(result)

    @app.get("/api/export/approved")
    def export_approved():
        with sessions() as s:
            approved = list(s.scalars(select(Restoration).where(Restoration.status == "approved")))
            if not approved:
                raise HTTPException(409, "Approve at least one restoration before exporting")
            def write(path):
                with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
                    for item in approved:
                        from .embedding import square_jpeg
                        raw,info=square_jpeg(within(cfg.data,item.path).read_bytes(),max_bytes=499000)
                        z.writestr(f"{item.image_id}/{item.id}.jpg",raw)
                    z.writestr("approved-artwork.json", json.dumps([record(r) for r in approved], indent=2))
            return zip_response(write, "coverly-approved-artwork.zip")

    @app.post('/api/storage/browse')
    def browse_storage(body: dict):
        from .archive_location import browse
        return browse(body.get('path'))

    @app.post('/api/storage/create-folder')
    def create_storage_folder(body: dict):
        parent=Path(body.get('path','')).expanduser().resolve()
        name=str(body.get('name','')).strip()
        if not parent.is_dir() or name in {'','.','..'} or any(c in name for c in '/\\:'):
            raise HTTPException(422,'Enter a single folder name inside an existing directory')
        target=parent/name;target.mkdir(exist_ok=False)
        return {'path':str(target)}

    @app.post('/api/storage/copy')
    def copy_storage(body: dict):
        from .archive_location import copy_archive
        # Only copy an idle archive; file mutations use the same archive lock.
        with file_lock(cfg.data/'.archive.lock'), sessions() as s:
            require_idle(s)
            return copy_archive(cfg.data,body.get('path',''))

    @app.get("/api/settings")
    def settings_api():
        from .ai_restoration import capabilities
        return {"settings": cfg.model_dump(), "hardware": hardware(), "ocr": ocr_capabilities(),
                "providers": {"ai":capabilities(),"local": {"available": True, "description": "Pillow CPU: conservative correction and Lanczos resizing; no generative reconstruction"},
                              "codex": {"available": "interactive", "description": "Export originals and prompts, edit in Codex, import results for approval"}},
                "search_credentials": {"google_configured": bool(os.environ.get("GOOGLE_SEARCH_API_KEY") and os.environ.get("GOOGLE_SEARCH_CX")),
                                       "brave_configured": bool(os.environ.get("BRAVE_SEARCH_API_KEY")),"spotify_configured":bool(os.environ.get("SPOTIFY_ACCESS_TOKEN"))}}

    @app.put("/api/settings")
    def update_settings(body: Settings):
        # A path picker must not accidentally create an empty library over an existing archive.
        if body.data!=cfg.data:
            with sessions() as s:
                require_idle(s)
                if s.scalar(select(func.count()).select_from(Album)) and not (body.data/'coverly.sqlite3').is_file():
                    raise HTTPException(409,'Copy the current archive to this folder first using Copy current archive here, or choose an existing Coverly archive. Pause jobs and wait for their current items to finish.')
        save_settings(body)
        with sessions() as s:
            s.add(Audit(action="settings_saved", detail={"restart_required": True}))
            s.commit()
        return {"ok": True, "restart_required": True, "message": "Saved. Restart Coverly to apply settings. Copy your archive to the new storage folder before restarting if moving it."}

    @app.post("/api/rename/preview")
    def rename_preview(body: Selection):
        with sessions() as s:
            from .storage import available_folder
            plan = [];reserved=set()
            for identifier in dict.fromkeys(body.ids):
                album = require(s, Album, identifier)
                new = available_folder(s,cfg,album,reserved)
                if new != album.folder:
                    exists = within(cfg.data, new).exists()
                    plan.append({"id": album.id, "old": album.folder, "new": new, "conflict": exists})
            audit = Audit(action="rename_preview", detail={"plan": plan})
            s.add(audit)
            s.commit()
            return {"plan_id": audit.id, "items": plan}

    def apply_rename(s, plan, action):
        require_idle(s)
        for item in plan:
            album = require(s, Album, item["id"])
            if album.folder != item["old"]:
                raise HTTPException(409, "Rename preview is stale; generate a new preview")
            source,target=within(cfg.data,item['old']),within(cfg.data,item['new'])
            own=source.exists() and target.exists() and source.samefile(target) and item['old'].casefold()==item['new'].casefold()
            if target.exists() and not own:
                raise HTTPException(409, "Destination already exists")
        from contextlib import ExitStack
        from .archive_ops import relocate_album
        with ExitStack() as moves:
            for item in plan:
                album = require(s, Album, item["id"])
                moves.enter_context(relocate_album(s,cfg,album,target_folder=item['new']))
            audit = Audit(action=action, detail={"plan": plan})
            s.add(audit)
            s.commit()
            return {"ok": True, "operation_id": audit.id}

    @app.post("/api/rename/{plan_id}/apply")
    def rename_apply(plan_id: str):
        with file_lock(cfg.data / ".archive.lock"), sessions() as s:
            audit = require(s, Audit, plan_id)
            if audit.action != "rename_preview":
                raise ValueError("Expected a rename preview")
            return apply_rename(s, audit.detail["plan"], "rename_applied")

    @app.post("/api/rename/{operation_id}/undo")
    def rename_undo(operation_id: str):
        with file_lock(cfg.data / ".archive.lock"), sessions() as s:
            audit = require(s, Audit, operation_id)
            if audit.action != "rename_applied":
                raise ValueError("Expected a completed rename operation")
            return apply_rename(s, [{"id": i["id"], "old": i["new"], "new": i["old"]} for i in audit.detail["plan"]], "rename_undone")

    @app.get("/api/backup")
    def backup_api():
        from .backup import make_backup
        with file_lock(cfg.data / ".archive.lock"), sessions() as s:
            require_idle(s)
            return zip_response(lambda path: make_backup(cfg.data, path), "coverly-archive-backup.zip")

    @app.get("/api/activity")
    def activity():
        with sessions() as s:
            return [record(a) for a in s.scalars(select(Audit).order_by(Audit.created.desc()).limit(50))]

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        content=(STATIC/'index.html').read_text(encoding='utf-8')
        for name in ('app.js','style.css','theme.js'):
            version=hashlib.sha256((STATIC/name).read_bytes()).hexdigest()[:12]
            content=content.replace('/static/'+name,'/static/'+name+'?v='+version)
        return Response(content,media_type='text/html',headers={'Cache-Control':'no-cache, must-revalidate'})

    return app
