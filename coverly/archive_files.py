"""Readable, per-album cover copies. Canonical originals remain immutable."""
from pathlib import Path
from collections import Counter,defaultdict
from sqlalchemy import select,or_
from .db import AlbumImage,ImageAsset,Restoration,Album,uid
from .storage import within,safe_name,atomic_write

LABELS={'front':'Front','back':'Back','vinyl_side_a':'Side A','vinyl_side_b':'Side B','vinyl':'Disc','inner':'Inner','additional':'Additional'}


def active_path(session,asset,link,include_cover=True):
    if include_cover and link.cover_source == 'filesystem':
        from types import SimpleNamespace
        from .filesystem import asset_path
        root=Path(session.get_bind().url.database).parent
        path=asset_path(session,SimpleNamespace(data=root),asset,link.album_id)
        return path.relative_to(root).as_posix(),'filesystem:'+asset.sha256
    if include_cover and link.cover_path:
        root=Path(session.get_bind().url.database).parent
        if within(root,link.cover_path).is_file():
            return link.cover_path,link.cover_source or 'original:'+asset.sha256
    result=session.get(Restoration,link.active_restoration_id) if link.active_restoration_id else None
    if result and result.status=='approved' and result.image_id==asset.id:
        return result.path,'restoration:'+result.id
    return (asset.original if asset.format=='JPEG' else asset.derivative),'original:'+asset.sha256


def sync_covers(session,settings,album):
    if album.deleted_at:return set()
    rows=list(session.execute(select(ImageAsset,AlbumImage).join(AlbumImage).where(AlbumImage.album_id==album.id,AlbumImage.deleted_at.is_(None)).order_by(ImageAsset.created,ImageAsset.id)))
    from .filesystem import current_files
    files=current_files(session,album.id)
    if files is not None:
        rows=[pair for pair in rows if pair[0].id in files or not pair[1].cover_source]
    affected=set()
    totals=Counter(link.classification for asset,link in rows);seen=defaultdict(int)
    for asset,link in rows:
        if link.cover_source == 'filesystem':
            continue  # Finder/editor files are indexed, never regenerated.
        # Legacy JPEG derivatives were needlessly re-encoded. Point the canonical
        # JPEG view back to the byte-identical source; keep old files recoverable.
        if asset.format=='JPEG' and asset.derivative!=asset.original and within(settings.data,asset.original).is_file():
            asset.derivative=asset.original
            affected.update(session.scalars(select(AlbumImage.album_id).where(AlbumImage.image_id==asset.id)))
        source,version=active_path(session,asset,link,include_cover=False)
        previous=within(settings.data,link.cover_path) if link.cover_path else None
        changed_version=bool(link.cover_source and link.cover_source!=version)
        if previous and not previous.is_file() and not changed_version:
            continue  # A manually removed cover is not regenerated.
        if previous and previous.is_file() and not changed_version:
            source=link.cover_path  # Preserve hand-edited Covers pixels.
        seen[link.classification]+=1
        label=LABELS.get(link.classification,'Additional')
        number=f' {seen[link.classification]}' if totals[link.classification]>1 else ''
        filename=safe_name(album.title)[:64]+' - '+label+number+'.jpg'
        path=f'{album.folder}/Covers/{filename}'
        target=within(settings.data,path)
        if target.is_file() and (changed_version or (previous and previous.is_file() and not target.samefile(previous))):
            index=2
            while target.exists():
                path=f'{album.folder}/Covers/{safe_name(album.title)[:64]} - {label} {index}.jpg'
                target=within(settings.data,path);index+=1
        elif target.is_file() and not link.cover_path:
            index=2
            while target.exists():
                path=f'{album.folder}/Covers/{safe_name(album.title)[:64]} - {label} {index}.jpg'
                target=within(settings.data,path);index+=1
        if not target.is_file():
            source_path=within(settings.data,source)
            if not source_path.is_file():
                # Metadata and folder repairs must remain usable when an old
                # image was moved externally. Report availability in the API;
                # never replace an original with a thumbnail or another scan.
                continue
            raw=source_path.read_bytes()
            atomic_write(target,raw,exclusive=True)
        link.cover_path,link.cover_source=path,version
    session.flush()
    return affected-{album.id}


def cleanup_covers(session,settings,album):
    """Covers are user-owned; automatic cleanup must never move/remove them."""
    return 0
