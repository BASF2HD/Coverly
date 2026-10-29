"""Coordinated archive moves keep shared originals and all manifests valid."""
from contextlib import contextmanager
from sqlalchemy import select,or_
from .db import Album,AlbumImage,ImageAsset,Restoration,Audit,FilesystemAlbum,FilesystemFile
from .storage import within,available_folder,write_existing_manifest,atomic_write


def _rewrite_index(session,old,new):
    for indexed in session.scalars(select(FilesystemAlbum).where(FilesystemAlbum.folder.startswith(old))):
        if indexed.folder==old or indexed.folder.startswith(old+'/'):indexed.folder=new+indexed.folder[len(old):]
    for file in session.scalars(select(FilesystemFile).where(FilesystemFile.path.startswith(old+'/'))):
        file.path=new+file.path[len(old):]


def _case_rename(source,target,root):
    """Two-hop rename works on APFS/NTFS even when both names are one inode."""
    from .db import uid
    stage=within(root,'.move-stage/'+uid())
    stage.parent.mkdir(parents=True,exist_ok=True)
    source.rename(stage)
    try:stage.rename(target)
    except BaseException:
        stage.rename(source)
        raise


def _rewrite_prefix(session,old,new):
    """Align every DB path when a shared directory changes capitalization."""
    def rewrite(value):
        if value and value[:len(old)+1].casefold()==(old+'/').casefold():return new+value[len(old):]
        if value and value.casefold()==old.casefold():return new
        return value
    affected=set()
    _rewrite_index(session,old,new)
    for album in session.scalars(select(Album).where(Album.folder.startswith(old))):
        value=rewrite(album.folder)
        if value!=album.folder:album.folder=value;affected.add(album.id)
    for image in session.scalars(select(ImageAsset).where(or_(*(getattr(ImageAsset,f).startswith(old+'/') for f in ('original','derivative','thumbnail'))))):
        changed=False
        for field in ('original','derivative','thumbnail'):
            value=rewrite(getattr(image,field))
            if value!=getattr(image,field):setattr(image,field,value);changed=True
        if changed:affected.update(session.scalars(select(AlbumImage.album_id).where(AlbumImage.image_id==image.id)))
    for link in session.scalars(select(AlbumImage).where(AlbumImage.cover_path.startswith(old+'/'))):
        link.cover_path=rewrite(link.cover_path)
    for result in session.scalars(select(Restoration).where(or_(Restoration.path.startswith(old+'/'),Restoration.input_path.startswith(old+'/')))):
        result.path=rewrite(result.path);result.input_path=rewrite(result.input_path)
    session.flush()
    return affected


def _same_directory(source,target):
    return source.exists() and target.exists() and source.samefile(target)

@contextmanager
def relocate_album(session,settings,album,target_folder=None):
    old,new=album.folder,target_folder or available_folder(session,settings,album)
    # Capitalization of a shared composer directory and a changed album leaf
    # (for example removing a legacy ID suffix) are two distinct moves. Nest
    # their transactions so either both finish or both roll back.
    old_parent,_,old_leaf=old.rpartition('/')
    new_parent,_,_=new.rpartition('/')
    if (old.casefold()!=new.casefold() and old_parent!=new_parent
            and old_parent.casefold()==new_parent.casefold()
            and within(settings.data,old).is_dir()
            and _same_directory(within(settings.data,old_parent),within(settings.data,new_parent))):
        with relocate_album(session,settings,album,target_folder=new_parent+'/'+old_leaf):
            with relocate_album(session,settings,album,target_folder=new) as movement:
                yield movement
        return
    moved=False;manifest_backups={};case_moves=[]
    source,target=within(settings.data,old),within(settings.data,new)
    try:
        if old!=new:
            if target.exists() and not (old.casefold()==new.casefold() and _same_directory(source,target)):
                raise ValueError('Destination folder exists; no files were overwritten')
            if old.casefold()==new.casefold() and _same_directory(source,target):
                # An artist directory can contain many albums. Rename it once
                # and update all paths, including records not yet processed by
                # this job, so the archive remains portable during the move.
                changed=set();parts=old.split('/');new_parts=new.split('/')
                for index,(before,after) in enumerate(zip(parts,new_parts)):
                    if before==after:continue
                    prefix='/'.join(parts[:index+1]);replacement='/'.join(new_parts[:index+1])
                    related_ids=set(session.scalars(select(Album.id).where(Album.folder.startswith(prefix))))
                    shared_images=select(ImageAsset.id).where(or_(*(getattr(ImageAsset,f).startswith(prefix+'/') for f in ('original','derivative','thumbnail'))))
                    related_ids.update(session.scalars(select(AlbumImage.album_id).where(AlbumImage.image_id.in_(shared_images))))
                    affected_albums=list(session.scalars(select(Album).where(Album.id.in_(related_ids))))
                    for related in affected_albums:
                        path=within(settings.data,related.folder+'/album.json')
                        if path.exists() and path not in manifest_backups:manifest_backups[path]=path.read_bytes()
                    _case_rename(within(settings.data,prefix),within(settings.data,replacement),settings.data)
                    case_moves.append((prefix,replacement));changed.update(_rewrite_prefix(session,prefix,replacement))
                    parts[index]=after
                for key in changed:write_existing_manifest(session,settings,session.get(Album,key),sync=True)
                session.add(Audit(action='album_folder_moved',target=album.id,detail={'old':old,'new':new,'case_only':True,'shared_manifests_updated':len(changed)}))
                yield {'old':old,'new':new}
                return
            affected={album.id}
            affected_images=list(session.scalars(select(ImageAsset).where(or_(ImageAsset.original.startswith(old+'/'),ImageAsset.derivative.startswith(old+'/'),ImageAsset.thumbnail.startswith(old+'/')))))
            for image in affected_images:
                if any(getattr(image,f).startswith(old+'/') for f in ('original','derivative','thumbnail')):
                    affected.update(session.scalars(select(AlbumImage.album_id).where(AlbumImage.image_id==image.id)))
            for key in affected:
                related=session.get(Album,key)
                path=within(settings.data,related.folder+'/album.json')
                if path.exists():manifest_backups[path]=path.read_bytes()
            if source.exists():
                target.parent.mkdir(parents=True,exist_ok=True);source.rename(target);moved=True
            for image in affected_images:
                for field in ('original','derivative','thumbnail'):
                    value=getattr(image,field)
                    if value.startswith(old+'/'):setattr(image,field,new+value[len(old):])
            for restoration in session.scalars(select(Restoration).where(or_(Restoration.path.startswith(old+'/'),Restoration.input_path.startswith(old+'/')))):
                for field in ('path','input_path'):
                    value=getattr(restoration,field)
                    if value.startswith(old+'/'):setattr(restoration,field,new+value[len(old):])
            for link in session.scalars(select(AlbumImage).where(AlbumImage.cover_path.startswith(old+'/'))):
                if link.cover_path and link.cover_path.startswith(old+'/'):link.cover_path=new+link.cover_path[len(old):]
            _rewrite_index(session,old,new)
            album.folder=new;session.flush()
            for key in affected:write_existing_manifest(session,settings,session.get(Album,key),sync=True)
            session.add(Audit(action='album_folder_moved',target=album.id,detail={'old':old,'new':new,'shared_manifests_updated':len(affected)}))
        else:session.flush();write_existing_manifest(session,settings,album,sync=True)
        yield {'old':old,'new':new}
    except BaseException:
        session.rollback()
        for before,after in reversed(case_moves):
            _case_rename(within(settings.data,after),within(settings.data,before),settings.data)
        if moved and target.exists():
            source.parent.mkdir(parents=True,exist_ok=True);target.rename(source)
        for path,content in manifest_backups.items():atomic_write(path,content)
        raise
    else:
        if moved:
            try:source.parent.rmdir()
            except OSError:pass
