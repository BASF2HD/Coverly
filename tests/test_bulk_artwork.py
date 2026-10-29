from sqlalchemy import select
from coverly.db import AlbumImage, ImageAsset


def import_album(client, raw, title):
    response=client.post('/api/import',data={'title':title},files={'files':('front.jpg',raw,'image/jpeg')})
    assert response.status_code==200,response.text
    return client.get('/api/albums/'+response.json()['album_id']).json()


def test_bulk_keep_last_covers_all_groups_and_undo_preserves_originals(client,image_bytes):
    from conftest import jpeg
    from coverly.duplicates import keep_last_selection
    groups=[]
    for index in range(35):
        groups.append({'members':[{'album_id':str(index),'image_id':'first'}, {'album_id':str(index),'image_id':'last'}]})
    plan=keep_last_selection(groups)
    assert plan['groups']==35 and plan['count']==35
    assert all(m['image_id']=='first' for m in plan['items'])
    assert all(m['image_id']=='last' for m in plan['keep'])
    first=import_album(client,image_bytes,'First edition');last=import_album(client,image_bytes,'Last edition')
    plan=client.get('/api/duplicates/selection').json()
    assert plan['count']==1 and len(plan['keep'])==1
    assert not client.get('/api/albums/'+first['id']).json()['trashed_images'] # Preview is read only.
    response=client.post('/api/artwork/trash',json={'items':plan['items'],'protected':plan['keep']})
    assert response.status_code==200,response.text
    assert len(client.get('/api/albums/'+plan['keep'][0]['album_id']).json()['images'])==1
    removed=client.get('/api/albums/'+plan['items'][0]['album_id']).json()
    assert not removed['images'] and removed['trashed_images']
    assert client.get('/api/images/'+first['images'][0]['id']+'/original').content==image_bytes
    assert client.post('/api/artwork/restore',json={'items':plan['items']}).status_code==200
    assert client.get('/api/duplicates').json()['total']==1


def test_opposite_record_sides_are_never_one_bulk_selection_group(client,image_bytes):
    a=import_album(client,image_bytes,'Same film')
    b=import_album(client,image_bytes,'Same film duplicate')
    image=a['images'][0]['id']
    client.patch(f"/api/albums/{a['id']}/images/{image}",json={'classification':'vinyl_side_a'}).raise_for_status()
    client.patch(f"/api/albums/{b['id']}/images/{image}",json={'classification':'vinyl_side_b'}).raise_for_status()
    assert client.get('/api/duplicates').json()['total']==0
    assert client.get('/api/duplicates/selection').json()['count']==0


def test_bulk_selection_cannot_remove_protected_last_image_or_stale_keeper(client,image_bytes):
    a=import_album(client,image_bytes,'First');b=import_album(client,image_bytes,'Last')
    plan=client.get('/api/duplicates/selection').json()
    assert client.post('/api/artwork/trash',json={'items':plan['keep'],'protected':plan['keep']}).status_code==409
    keeper=plan['keep'][0]
    client.post(f"/api/albums/{keeper['album_id']}/images/{keeper['image_id']}/trash").raise_for_status()
    assert client.post('/api/artwork/trash',json={'items':plan['items'],'protected':plan['keep']}).status_code==409
    selected=plan['items'][0]
    assert client.get('/api/albums/'+selected['album_id']).json()['images']
