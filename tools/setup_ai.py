"""Fetch the pinned official Real-ESRGAN model; dependencies are installed by setup-ai scripts."""
import hashlib,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from coverly.ai_restoration import MODEL_URL,MODEL_SHA256,MODEL_PATH
import httpx

def main():
    if MODEL_PATH.is_file() and hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()==MODEL_SHA256:
        print('Verified AI model already installed.');return
    print('Downloading official Real-ESRGAN model (about 64 MB)…',flush=True)
    with httpx.Client(follow_redirects=True,trust_env=False,timeout=120) as client:
        response=client.get(MODEL_URL);response.raise_for_status();raw=response.content
    if hashlib.sha256(raw).hexdigest()!=MODEL_SHA256:raise ValueError('Model checksum does not match; file was not installed')
    MODEL_PATH.parent.mkdir(parents=True,exist_ok=True)
    from coverly.storage import atomic_write
    atomic_write(MODEL_PATH,raw)
    print('AI model verified. Restart Coverly to enable MPS / CUDA / CPU restoration.')

if __name__=='__main__':main()
