"""Explicit permanent Trash cleanup, with staging and shared-file preservation."""
import hashlib,json,shutil
from pathlib import Path
from sqlalchemy import select,delete,or_,update
from .db import Album,AlbumImage,ImageAsset,Restoration,OCRResult,Candidate,Edition,AlbumRelation,Page,JobItem,Audit,FilesystemFile,uid,now
from .storage import within,write_manifest,atomic_write


def preview(session,settings,ids=None):
    albums=list(session.scalars(select(Album).where(Album.id.in_(ids) if ids else Album.deleted_at.is_not(None))))
    if ids and len(albums)!=len(set(ids)):raise ValueError('An album no longer exists')
    if not albums:raise ValueError('Trash is empty')
    if any(not a.deleted_at for a in albums):raise ValueError('Only albums in Trash can be permanently deleted')
    ids={a.id for a in albums}
    assets=list(session.scalars(select(ImageAsset).join(AlbumImage).where(AlbumImage.album_id.in_(ids)).distinct()))
    shared={im.id for im in assets if session.scalar(select(AlbumImage.album_id).where(AlbumImage.image_id==im.id,AlbumImage.album_id.not_in(ids)).limit(1))}
    exclusive={im.id for im in assets}-shared
    files=set();retained=set()
    for im in assets:
        (retained if im.id in shared else files).update([im.original,im.derivative,im.thumbnail])
    for r in session.scalars(select(Restoration)):
        (files if r.image_id in exclusive else retained).update(p for p in (r.path,r.input_path) if p)
    for album in albums:
        folder=within(settings.data,album.folder)
        for other in session.scalars(select(Album).where(Album.id.not_in(ids))):
            if within(settings.data,other.folder).is_relative_to(folder):raise ValueError('Another album is nested inside this folder')
        if folder.exists():
            for path in folder.rglob('*'):
                if path.is_symlink():raise ValueError('Remove symlinks from this album folder before permanent cleanup')
                if path.is_file():files.add(path.relative_to(settings.data).as_posix())
    files-=retained
    fingerprint=hashlib.sha256(json.dumps({'albums':sorted((a.id,a.deleted_at,a.folder) for a in albums),
        'assets':sorted((a.id,a.sha256,a.id in shared) for a in assets),'files':sorted(files)},sort_keys=True).encode()).hexdigest()
    return dict(ids=sorted(ids),fingerprint=fingerprint,albums=[{'id':a.id,'title':a.title,'folder':a.folder} for a in albums],
        album_count=len(albums),originals_removed=len(exclusive),shared_originals_retained=len(shared),
        files_removed=len(files),bytes_removed=sum(within(settings.data,p).stat().st_size for p in files if within(settings.data,p).is_file()))


def purge(session,settings,plan):
    current=preview(session,settings,plan['ids'])
    if current['fingerprint']!=plan['fingerprint']:raise ValueError('Trash changed since preview. Preview it again before deleting')
    ids=set(plan['ids']);albums=[session.get(Album,key) for key in ids]
    pages=list(session.scalars(select(Page).where(Page.album_id.in_(ids))));page_ids={p.id for p in pages}
    images=list(session.scalars(select(ImageAsset).join(AlbumImage).where(AlbumImage.album_id.in_(ids)).distinct()))
    exclusive={im.id for im in images if not session.scalar(select(AlbumImage.album_id).where(AlbumImage.image_id==im.id,AlbumImage.album_id.not_in(ids)).limit(1))}
    all_images={im.id for im in images}
    items=list(session.scalars(select(JobItem).where(JobItem.status.in_(['pending','running']))))
    affected=[i for i in items if i.key in ids|exclusive or i.data.get('page_id') in page_ids]
    if any(i.status=='running' for i in affected):raise ValueError('Pause the work using this album and wait for its current image before deleting')
    stage=within(settings.data,'.purge-stage/'+uid());stage.mkdir(parents=True)
    moves=[];backups={};mapping={};committed=False
    folders=[within(settings.data,a.folder) for a in albums]
    def under_folder(relative):return any(within(settings.data,relative).is_relative_to(folder) for folder in folders)
    def move(relative,target_relative):
        if relative in mapping:return mapping[relative]
        source=within(settings.data,relative);target=within(settings.data,target_relative)
        if target.exists():raise ValueError('Cleanup destination already exists; no files were overwritten')
        if source.exists():target.parent.mkdir(parents=True,exist_ok=True);source.rename(target);moves.append((source,target))
        mapping[relative]=target_relative
        return target_relative
    try:
        remaining=set()
        # Promote shared originals out of folders which are about to disappear.
        for im in images:
            if im.id in exclusive:continue
            remaining.update(session.scalars(select(AlbumImage.album_id).where(AlbumImage.image_id==im.id,AlbumImage.album_id.not_in(ids))))
            for field in ('original','derivative','thumbnail'):
                value=getattr(im,field)
                if under_folder(value):setattr(im,field,move(value,f'.shared/{im.id}/{Path(value).name}'))
        restorations=list(session.scalars(select(Restoration)))
        for r in restorations:
            if r.image_id in exclusive:continue
            for field in ('path','input_path'):
                value=getattr(r,field)
                if value in mapping:setattr(r,field,mapping[value])
                elif value and under_folder(value):setattr(r,field,move(value,f'.shared/restorations/{r.id}/{Path(value).name}'))
        for key in remaining:
            album=session.get(Album,key);path=within(settings.data,album.folder+'/album.json')
            if path.exists():backups[path]=path.read_bytes()
        for album in albums:
            folder=within(settings.data,album.folder)
            if folder.exists():
                target=stage/'folders'/album.id;target.parent.mkdir(parents=True,exist_ok=True);folder.rename(target);moves.append((folder,target))
        retained={getattr(im,f) for im in session.scalars(select(ImageAsset).where(ImageAsset.id.not_in(exclusive))) for f in ('original','derivative','thumbnail')}
        retained.update(p for r in restorations if r.image_id not in exclusive for p in (r.path,r.input_path) if p)
        removable={getattr(im,f) for im in images if im.id in exclusive for f in ('original','derivative','thumbnail')}
        removable.update(p for r in restorations if r.image_id in exclusive for p in (r.path,r.input_path) if p)
        for number,relative in enumerate(sorted(removable-retained)):
            path=within(settings.data,relative)
            if path.exists():
                target=stage/'files'/str(number);target.parent.mkdir(parents=True,exist_ok=True);path.rename(target);moves.append((path,target))
        for item in affected:item.status,item.error='ignored','Album permanently removed by user'
        for page in pages:page.album_id=None;page.metadata_json={**page.metadata_json,'ignore_download':True,'purged_at':now()}
        session.execute(delete(AlbumImage).where(AlbumImage.album_id.in_(ids)))
        session.execute(delete(FilesystemFile).where(FilesystemFile.album_id.in_(ids)))
        session.execute(delete(OCRResult).where(OCRResult.image_id.in_(exclusive)))
        session.execute(delete(Restoration).where(Restoration.image_id.in_(exclusive)))
        session.execute(delete(ImageAsset).where(ImageAsset.id.in_(exclusive)))
        session.execute(delete(Edition).where(Edition.album_id.in_(ids)))
        session.execute(delete(Candidate).where(Candidate.album_id.in_(ids)))
        session.execute(delete(AlbumRelation).where(or_(AlbumRelation.parent_id.in_(ids),AlbumRelation.child_id.in_(ids))))
        session.flush()
        session.execute(update(Restoration).where(Restoration.album_id.in_(ids)).values(album_id=None))
        session.execute(delete(Album).where(Album.id.in_(ids)))
        for key in remaining:write_manifest(session,settings,session.get(Album,key))
        session.add(Audit(action='trash_permanently_deleted',detail=current));session.commit();committed=True
    except BaseException:
        session.rollback()
        for source,target in reversed(moves):
            if target.exists():source.parent.mkdir(parents=True,exist_ok=True);target.rename(source)
        for path,data in backups.items():atomic_write(path,data)
        shutil.rmtree(stage,ignore_errors=True)
        raise
    cleanup_pending=False
    try:shutil.rmtree(stage)
    except OSError:cleanup_pending=True
    for folder in folders:
        parent=folder.parent
        while parent!=settings.data:
            try:parent.rmdir()
            except OSError:break
            parent=parent.parent
    return {'ok':True,**current,'cleanup_pending':cleanup_pending}
