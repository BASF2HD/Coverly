from sqlalchemy import insert
from coverly.db import Album


def test_library_paginates_5001_album_records(client):
    rows=[{'id':f'{i:032x}','title':f'Test album {i:05d}','folder':f'Test/album-{i:05d}','artist':'Test Artist','review':False,'confidence':1.0} for i in range(5001)]
    with client.app.state.sessions() as s:
        s.execute(insert(Album),rows)
        s.commit()
    response=client.get('/api/albums?sort=title&limit=48&offset=4992')
    assert response.status_code==200
    assert response.json()['total']==5001
    assert len(response.json()['items'])==9
    assert response.json()['items'][-1]['title']=='Test album 05000'
