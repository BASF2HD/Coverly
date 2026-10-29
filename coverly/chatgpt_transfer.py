"""Short-lived, selected-file handoffs to an explicitly installed browser bridge."""
import base64,secrets,time,threading,io
from .storage import within,decode_image,safe_name
from .archive_files import active_path,LABELS
from .db import Album,AlbumImage,ImageAsset

class Transfers:
    def __init__(self):self.items={};self.lock=threading.RLock()
    def create(self,items,prompt):
        if not isinstance(items,list) or not 1<=len(items)<=10:raise ValueError('Choose 1 to 10 images per ChatGPT handoff')
        if any(not isinstance(i,dict) or not isinstance(i.get('album_id'),str) or not isinstance(i.get('image_id'),str) for i in items):raise ValueError('Select album/image references')
        token=secrets.token_urlsafe(32)
        with self.lock:
            self.items={k:v for k,v in self.items.items() if v['expires']>time.time()}
            self.items[token]={'items':items,'prompt':prompt,'expires':time.time()+600,'status':'ready','message':'Waiting for browser bridge'}
        return token
    def get(self,token):
        with self.lock:
            value=self.items.get(token)
            if not value or value['expires']<time.time():raise ValueError('Handoff expired. Select the images again.')
            return dict(value)
    def status(self,token,status,message):
        self.get(token)
        with self.lock:self.items[token].update(status=status,message=str(message)[:500])


def bundle(session,settings,record):
    files=[];total=0
    for item in record['items']:
        album=session.get(Album,item['album_id']);asset=session.get(ImageAsset,item['image_id']);link=session.get(AlbumImage,(item['album_id'],item['image_id']))
        if not album or album.deleted_at or not asset or not link or link.deleted_at:raise ValueError('Selected artwork is no longer available')
        raw=within(settings.data,active_path(session,asset,link)[0]).read_bytes()
        im,fmt=decode_image(raw)
        if fmt not in {'JPEG','PNG'}:
            out=io.BytesIO();im.save(out,'JPEG',quality=95);raw=out.getvalue();fmt='JPEG'
        total+=len(raw)
        if len(raw)>20_000_000 or total>30_000_000:raise ValueError('Selected images exceed the handoff limit. Choose fewer images or use square embedding copies.')
        ext='jpg' if fmt=='JPEG' else 'png'
        files.append({'name':safe_name(album.title)[:64]+' - '+LABELS.get(link.classification,'Additional')+' - '+asset.id[:8]+'.'+ext,
                      'mimeType':'image/jpeg' if fmt=='JPEG' else 'image/png','data':base64.b64encode(raw).decode('ascii')})
    return {'files':files,'prompt':record['prompt'],'expires':record['expires']}
