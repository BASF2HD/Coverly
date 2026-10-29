"""Optional, tiled Real-ESRGAN inference on MPS, CUDA or CPU."""
from __future__ import annotations
import hashlib,importlib.util,io,math
from pathlib import Path
from PIL import Image
from .config import ROOT
from .storage import decode_image

MODEL_URL='https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth'
MODEL_SHA256='4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1'
MODEL_PATH=ROOT/'models'/'RealESRGAN_x4plus.pth'


def capabilities():
    ready=MODEL_PATH.is_file() and all(importlib.util.find_spec(m) for m in ('torch','spandrel'))
    return {'available':bool(ready),'model':'Real-ESRGAN x4plus','model_present':MODEL_PATH.is_file(),
            'description':'Local AI detail enhancement; generated details need review. MPS / CUDA / CPU.',
            'setup':'Run setup-ai.command (macOS) or setup-ai.ps1 (Windows), then restart Coverly.'}


def tiled(model,im,device,progress,tile=128,pad=32):
    import numpy as np
    import torch
    arr=np.asarray(im.convert('RGB'),dtype=np.float32)/255
    h,w=arr.shape[:2];scale=int(model.scale)
    output=np.empty((h*scale,w*scale,3),dtype=np.uint8)
    total=math.ceil(h/tile)*math.ceil(w/tile);done=0
    with torch.inference_mode():
        for y in range(0,h,tile):
            for x in range(0,w,tile):
                x1,y1=max(0,x-pad),max(0,y-pad);x2,y2=min(w,x+tile+pad),min(h,y+tile+pad)
                tensor=torch.from_numpy(arr[y1:y2,x1:x2].copy()).permute(2,0,1).unsqueeze(0).to(device)
                result=model(tensor).clamp(0,1)[0].permute(1,2,0).cpu().numpy()
                ch,cw=min(tile,h-y),min(tile,w-x);oy,ox=(y-y1)*scale,(x-x1)*scale
                output[y*scale:(y+ch)*scale,x*scale:(x+cw)*scale]=(result[oy:oy+ch*scale,ox:ox+cw*scale]*255).round().astype(np.uint8)
                done+=1;progress(f'AI enhancement on {device.upper()} · tile {done} of {total}',done,total)
    return Image.fromarray(output)


class AIProvider:
    name='Real-ESRGAN x4plus'
    def restore(self,original,size,progress=None):
        if not capabilities()['available']:raise ValueError(capabilities()['setup'])
        if hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()!=MODEL_SHA256:raise ValueError('AI model checksum failed. Run AI setup again.')
        import torch
        from spandrel import ModelLoader,ImageModelDescriptor
        device='cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
        report=progress or (lambda *args:None)
        report('Loading Real-ESRGAN on '+device.upper(),0,1)
        model=ModelLoader().load_from_file(MODEL_PATH).eval()
        if not isinstance(model,ImageModelDescriptor):raise ValueError('Expected a supported image enhancement model')
        im,_=decode_image(original if isinstance(original,bytes) else Path(original).read_bytes())
        # Bound memory for very large scans while retaining the original unchanged.
        im.thumbnail((1600,1600),Image.Resampling.LANCZOS)
        try:
            model.to(device)
            result=tiled(model,im,device,report)
        except (RuntimeError,NotImplementedError):
            if device=='cpu':raise
            report('GPU operation unavailable; continuing on CPU',0,1)
            model.to('cpu');device='cpu'
            result=tiled(model,im,device,report)
        if size:
            ratio=size/max(result.size)
            target=(max(1,round(result.width*ratio)),max(1,round(result.height*ratio)))
            if target[0]*target[1]>40_000_000:raise ValueError('Output exceeds 40 megapixels. Choose a smaller longest edge.')
            result=result.resize(target,Image.Resampling.LANCZOS)
        out=io.BytesIO();result.save(out,'JPEG',quality=95,subsampling=0)
        return out.getvalue(),device
