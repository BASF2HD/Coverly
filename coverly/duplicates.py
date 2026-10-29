"""Small, directly verified image groups; weak hash chains never join albums."""
from functools import lru_cache
from pathlib import Path
import re
from difflib import SequenceMatcher
from .classification import disc_shape
import numpy as np
from PIL import Image
from sqlalchemy import select
from .db import Album,AlbumImage,ImageAsset

@lru_cache(maxsize=8192)
def signature(path,stamp):
    with Image.open(path) as im:
        rgb=np.asarray(im.convert('RGB').resize((32,32)),dtype=np.float32)/255
    gray=rgb.mean(axis=2);normal=(gray-gray.mean())/(gray.std()+.01)
    return rgb,normal,disc_shape(Image.fromarray((rgb*255).astype(np.uint8)))

def artwork_groups(session,query=''):
    root=Path(session.get_bind().url.database).parent
    from .filesystem import current_files,asset_path,preview_path
    from types import SimpleNamespace
    settings=SimpleNamespace(data=root)
    indexed={}
    rows=session.execute(select(Album,AlbumImage,ImageAsset).join(AlbumImage,Album.id==AlbumImage.album_id)
        .join(ImageAsset,ImageAsset.id==AlbumImage.image_id).where(Album.deleted_at.is_(None),AlbumImage.deleted_at.is_(None))
        .order_by(ImageAsset.created,ImageAsset.id,Album.created,Album.id)).all()
    assets,members={},{}
    for album,link,asset in rows:
        if album.id not in indexed:indexed[album.id]=current_files(session,album.id)
        files=indexed[album.id]
        if files is not None and asset.id not in files:continue
        try:available=asset_path(session,settings,asset,album.id if files is not None else None).is_file()
        except (OSError,ValueError):available=False
        assets[asset.id]=asset
        members.setdefault(asset.id,[]).append(dict(album_id=album.id,title=album.title,artist=album.artist,catalogue=album.catalogue,
            image_id=asset.id,classification=link.classification,width=asset.width,height=asset.height,sha256=asset.sha256,source_url=asset.page_url,
            added_at=asset.created,album_added_at=album.created,original_available=available))
    hashes={i:int(a.phash,16) for i,a in assets.items() if a.phash}
    sigs={}
    def similar(a,b):
        roles_a={m['classification'] for m in members[a.id]}
        roles_b={m['classification'] for m in members[b.id]}
        if not roles_a.intersection(roles_b):return None
        distance=(hashes[a.id]^hashes[b.id]).bit_count()
        if distance>4 or not .9<=(a.width/a.height)/(b.width/b.height)<=1.1:return None
        try:
            for im in (a,b):
                if im.id not in sigs:
                    path=preview_path(session,settings,im) if im.original.startswith('.filesystem/') else root/im.thumbnail
                    sigs[im.id]=signature(str(path),path.stat().st_mtime_ns)
            rgb_a,n_a,disc_a=sigs[a.id];rgb_b,n_b,disc_b=sigs[b.id]
            if disc_a or disc_b:
                from .metadata import source_metadata
                titles_a=[source_metadata({'title':m['title']})['title'].casefold() for m in members[a.id]]
                titles_b=[source_metadata({'title':m['title']})['title'].casefold() for m in members[b.id]]
                if not any(SequenceMatcher(None,x,y).ratio()>.85 for x in titles_a for y in titles_b):return None
            color=float(np.abs(rgb_a-rgb_b).mean());correlation=float((n_a*n_b).mean())
            if color>.10 or correlation<.90:return None
            return distance
        except (OSError,ValueError):return None  # Exact file matches remain available.
    clusters=[]
    tree=None
    def add_anchor(value,index):
        nonlocal tree
        if tree is None:tree=[value,[index],{}];return
        node=tree
        while True:
            distance=(value^node[0]).bit_count()
            if not distance:node[1].append(index);return
            if distance not in node[2]:node[2][distance]=[value,[index],{}];return
            node=node[2][distance]
    def candidates(value):
        stack=[tree] if tree is not None else [];found=[]
        while stack:
            node=stack.pop();distance=(value^node[0]).bit_count()
            if distance<=4:found.extend(node[1])
            stack.extend(child for edge,child in node[2].items() if distance-4<=edge<=distance+4)
        return sorted(found)
    for asset in assets.values():
        chosen=None
        if asset.id in hashes:
            for index in candidates(hashes[asset.id]):
                cluster=clusters[index]
                if len(cluster)>=8:continue
                if all(other.id in hashes and similar(asset,other) is not None for other in cluster):
                    chosen=cluster;break
        if chosen is None:
            clusters.append([asset])
            if asset.id in hashes:add_anchor(hashes[asset.id],len(clusters)-1)
        else:chosen.append(asset)
    result=[]
    for cluster in clusters:
        roles={m['classification'] for asset in cluster for m in members[asset.id]}
        for role in sorted(roles):
            # Front/back and opposite record sides are distinct artwork even
            # when the record's outline dominates its perceptual fingerprint.
            links=[member for asset in cluster for member in members[asset.id] if member['classification']==role]
            if len(links)<2:continue
            if query and not any(query.casefold() in (m['title']+' '+m['artist']+' '+m['catalogue']).casefold() for m in links):continue
            links.sort(key=lambda m:(m['added_at'],m['album_added_at'],m['image_id'],m['album_id']))
            ids={m['image_id'] for m in links}
            images=[a for a in cluster if a.id in ids]
            distances=[(hashes[a.id]^hashes[b.id]).bit_count() for index,a in enumerate(images) for b in images[index+1:]]
            result.append(dict(id=cluster[0].id+'-'+role,kind='exact' if len(ids)==1 else 'similar',members=links,
                album_count=len({m['album_id'] for m in links}),matches=[],distance_range=[min(distances),max(distances)] if distances else None,
                comparison_count=len(distances)))
    result.sort(key=lambda g:(not any(m['classification']=='front' for m in g['members']),g['kind']!='exact',g['members'][0]['title']))
    return result


def keep_last_selection(groups):
    """A review-only selection across all groups; never removes any artwork."""
    skipped=[g for g in groups if not g['members'][-1].get('original_available',True)]
    groups=[g for g in groups if g['members'][-1].get('original_available',True)]
    keepers={ (g['members'][-1]['album_id'],g['members'][-1]['image_id']) for g in groups }
    selected={}
    for group in groups:
        for member in group['members'][:-1]:
            identity=(member['album_id'],member['image_id'])
            if identity not in keepers:
                selected[identity]={'album_id':identity[0],'image_id':identity[1]}
    return {'items':list(selected.values()),'keep': [{'album_id':a,'image_id':i} for a,i in sorted(keepers)],
            'groups':len(groups),'count':len(selected),'policy':'keep_last','unavailable_groups':len(skipped)}
