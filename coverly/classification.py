"""Portable artwork heuristics. Printed evidence wins; manual choices are kept."""
import re
import numpy as np
from PIL import Image
from .storage import classify,within,write_manifest
from .db import AlbumImage,ImageAsset,Page,Album,OCRResult,Audit
from sqlalchemy import select


def disc_shape(im):
    rgb=np.asarray(im.convert('RGB').resize((128,128)),dtype=np.float32)/255
    yy,xx=np.mgrid[:128,:128];r=np.sqrt(((xx-63.5)/64)**2+((yy-63.5)/64)**2)
    gray=rgb.mean(axis=2);outside=gray[r>1.1];ring=gray[(r>.55)&(r<.85)]
    # Black vinyl on a plain light background: require both a dark annulus
    # and consistent outer background, rather than any circular cover motif.
    return bool(outside.mean()>.65 and outside.std()<.13 and ring.mean()<.40 and outside.mean()-ring.mean()>.34)


def decide(im,caption='',index=0,disc_index=0,text=''):
    kind,confidence=classify(caption)
    if confidence:return kind,confidence,'Source caption explicitly names '+kind.replace('_',' ')
    if disc_shape(im):
        if re.search(r'(?:side|பக்கம்)\s*(?:b|2|two)\b',text,re.I):return 'vinyl_side_b',.92,'Printed side B / 2 marker'
        if re.search(r'(?:side|பக்கம்)\s*(?:a|1|one)\b',text,re.I):return 'vinyl_side_a',.92,'Printed side A / 1 marker'
        if disc_index<2:return ('vinyl_side_a' if disc_index==0 else 'vinyl_side_b'),.58,'Round vinyl silhouette; side inferred from gallery order, please review'
        return 'vinyl',.75,'Round vinyl silhouette; printed side is unknown'
    if index==0:return 'front',.76,'First non-disc source gallery image; please review'
    if re.search(r'(?:track|song|lyrics|singer|catalogue|side\s*[ab12])',text,re.I):return 'back',.82,'Track-list or catalogue text on sleeve'
    if index==1:return 'back',.64,'Second sleeve image in source gallery; please review'
    return 'additional',.25,'No clear front/back or disc evidence'


def classify_album(session,settings,album_id):
    album=session.get(Album,album_id)
    if not album or album.deleted_at:return 0
    rows=list(session.execute(select(ImageAsset,AlbumImage).join(AlbumImage).where(AlbumImage.album_id==album_id,AlbumImage.deleted_at.is_(None))))
    from .filesystem import current_files,preview_path
    files=current_files(session,album_id) if settings.filesystem_sync else None
    if files is not None:rows=[pair for pair in rows if pair[0].id in files]
    source=[im for page in session.scalars(select(Page).where(Page.album_id==album_id)) for im in page.metadata_json.get('images',[])]
    def position(asset):
        return next((i for i,m in enumerate(source) if m.get('sha256')==asset.sha256 or m.get('url')==asset.source_url),len(source))
    rows.sort(key=lambda pair:(position(pair[0]),pair[0].created))
    discs=0;updated=0
    for index,(asset,link) in enumerate(rows):
        if link.manual:continue
        caption=next((m.get('caption','') for m in source if m.get('sha256')==asset.sha256 or m.get('url')==asset.source_url),'')
        text=' '.join(session.scalars(select(OCRResult.text).where(OCRResult.image_id==asset.id,OCRResult.status=='completed')))
        path=preview_path(session,settings,asset) if asset.original.startswith('.filesystem/') else within(settings.data,asset.thumbnail)
        with Image.open(path) as im:
            kind,confidence,evidence=decide(im,caption,index,discs,text)
        if kind.startswith('vinyl'):discs+=1
        link.classification,link.confidence,link.evidence=kind,confidence,evidence;updated+=1
    session.flush();write_manifest(session,settings,album)
    session.add(Audit(action='artwork_auto_classified',target=album_id,detail={'updated':updated,'manual_preserved':True}))
    return updated
