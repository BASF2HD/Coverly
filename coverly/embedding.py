"""Square export copies. Source bytes and archive originals remain untouched."""
import io
import numpy as np
from PIL import Image,ImageOps
from .storage import decode_image


def sleeve_bounds(im):
    """Trim a uniform scanner/table background without guessing internal artwork."""
    sample=im.copy();sample.thumbnail((1000,1000))
    a=np.asarray(sample,dtype=np.int16)
    corners=np.array([a[0,0],a[0,-1],a[-1,0],a[-1,-1]])
    colour=np.median(corners,axis=0)
    if np.max(np.abs(corners-colour))>16:return (0,0,*im.size)
    # Coloured sleeve design is not assumed to be scanner background.
    if np.ptp(colour)>20:return (0,0,*im.size)
    mask=np.max(np.abs(a-colour),axis=2)>24
    # Ignore isolated scan dust. Require content across at least 3% of an axis.
    xs=np.flatnonzero(mask.sum(axis=0)>sample.height*.03)
    ys=np.flatnonzero(mask.sum(axis=1)>sample.width*.03)
    if not len(xs) or not len(ys):return (0,0,*im.size)
    box=(round(xs[0]*im.width/sample.width),round(ys[0]*im.height/sample.height),
         round((xs[-1]+1)*im.width/sample.width),round((ys[-1]+1)*im.height/sample.height))
    width,height=box[2]-box[0],box[3]-box[1]
    if min(width,height)<32 or width*height<im.width*im.height*.25:return (0,0,*im.size)
    return box


def square_jpeg(data,edge=2000,max_bytes=None,mode='crop',position_x=.5,position_y=.5):
    im,fmt=decode_image(data);original=im.size;crop=(0,0,*im.size)
    if mode not in {'crop','pad','trim'}:raise ValueError('Choose crop, pad or trim')
    if not 0<=position_x<=1 or not 0<=position_y<=1:raise ValueError('Crop positions must be between zero and one')
    if mode in {'crop','trim'}:
        crop=sleeve_bounds(im);im=im.crop(crop)
    if mode=='crop':
        side=min(im.size)
        left=round((im.width-side)*position_x);top=round((im.height-side)*position_y)
        crop=(crop[0]+left,crop[1]+top,crop[0]+left+side,crop[1]+top+side)
        im=im.crop((left,top,left+side,top+side))
    edge=min(edge,max(im.size))
    info={'crop':crop,'mode':mode,'source_size':list(original),'position':[position_x,position_y]}
    if im.width==im.height and max(im.size)<=edge and (max_bytes is None or len(data)<max_bytes) and fmt=='JPEG' and crop==(0,0,*original):
        return data,{**info,'size':list(im.size),'bytes':len(data),'passthrough':True}
    if mode=='crop':square=im.resize((edge,edge),Image.Resampling.LANCZOS)
    else:
        colour=tuple(int(v) for v in np.median(np.array([im.getpixel((0,0)),im.getpixel((im.width-1,0)),im.getpixel((0,im.height-1)),im.getpixel((im.width-1,im.height-1))]),axis=0))
        square=ImageOps.pad(im,(edge,edge),method=Image.Resampling.LANCZOS,color=colour)
    if max_bytes is None:
        out=io.BytesIO();square.save(out,'JPEG',quality=97,subsampling=0)
        return out.getvalue(),{**info,'size':[edge,edge],'bytes':out.tell(),'quality':97,'passthrough':False}
    # Explicit legacy size-limited exports remain supported by the Python helper.
    while edge>=128:
        best=None;quality_used=0;low,high=40,95
        while low<=high:
            quality=(low+high)//2;out=io.BytesIO();square.save(out,'JPEG',quality=quality,optimize=True)
            raw=out.getvalue()
            if len(raw)<max_bytes:best,quality_used=raw,quality;low=quality+1
            else:high=quality-1
        if best:return best,{**info,'size':[edge,edge],'bytes':len(best),'quality':quality_used,'passthrough':False}
        edge=int(edge*.85);square=square.resize((edge,edge),Image.Resampling.LANCZOS)
    raise ValueError('Cannot fit this image within the chosen size limit')
