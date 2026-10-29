"""Per-album active artwork selection and restoration review."""
from sqlalchemy import select
from .db import AlbumImage,Album,Restoration,Audit,now
from .storage import write_manifest
from .archive_files import cleanup_covers


def review(session,settings,result,action,album_id=None):
    if action not in {'approve','reject','pending'}:raise ValueError('Invalid review action')
    links=list(session.scalars(select(AlbumImage).join(Album).where(AlbumImage.image_id==result.image_id,Album.deleted_at.is_(None),AlbumImage.deleted_at.is_(None))))
    target=album_id or result.album_id
    if target:
        links=[link for link in links if link.album_id==target]
        if not links and action=='approve':raise ValueError('Restoration does not belong to active album artwork')
    elif links:links=links[:1]
    result.status={'approve':'approved','reject':'rejected','pending':'pending'}[action]
    for link in links:
        if action=='approve':
            link.active_restoration_id=result.id
            # Only this explicit approval may export a new restored copy.
            # Ordinary metadata saves must never regenerate indexed user files.
            if link.cover_source=='filesystem':link.cover_source='restore-request:'+result.id
        elif link.active_restoration_id==result.id:link.active_restoration_id=None
        write_manifest(session,settings,session.get(Album,link.album_id))
    session.add(Audit(action='restoration_'+action,target=result.id,detail={'album_ids':[link.album_id for link in links],'generated':result.generated}))
    session.commit()
    for link in links:cleanup_covers(session,settings,session.get(Album,link.album_id))
