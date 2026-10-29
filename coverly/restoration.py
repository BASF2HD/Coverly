from __future__ import annotations

import importlib.util
import io
import json
import platform
from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
from .storage import atomic_write, within, decode_image


@lru_cache(maxsize=1)
def hardware():
    result = {"platform": platform.system(), "architecture": platform.machine(), "device": "cpu", "cuda": False, "mps": False,
              "local_provider": "Pillow CPU", "torch_installed": bool(importlib.util.find_spec("torch"))}
    if result["torch_installed"]:
        try:
            import torch
            result["cuda"] = bool(torch.cuda.is_available())
            result["mps"] = bool(hasattr(torch.backends, "mps") and torch.backends.mps.is_available())
            result["device"] = "cuda" if result["cuda"] else "mps" if result["mps"] else "cpu"
        except (ImportError, OSError, RuntimeError) as exc:
            result["runtime_error"] = str(exc)
    return result


class LocalProvider:
    name = "local-pillow"
    presets = {"gentle", "sleeve", "upscale"}

    def restore(self, original: Path, preset: str, size: int):
        if preset not in self.presets:
            raise ValueError("Advanced reconstruction uses the interactive Codex export/import workflow")
        im, _ = decode_image(original if isinstance(original,bytes) else original.read_bytes())
        if preset == "gentle":
            im = ImageEnhance.Contrast(im.filter(ImageFilter.MedianFilter(3))).enhance(1.06)
            im = ImageEnhance.Color(im).enhance(1.04)
        elif preset == "sleeve":
            # Conservative correction: no guessed perspective or generated damage repair.
            im = ImageOps.autocontrast(im, cutoff=.2)
            im = im.filter(ImageFilter.MedianFilter(3))
        if size:
            scale = size / max(im.size)
            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.Resampling.LANCZOS)
        if preset == "upscale":
            im = im.filter(ImageFilter.UnsharpMask(radius=1, percent=65, threshold=4))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=95, subsampling=0)
        return out.getvalue()


def codex_manifest(assets):
    return {"schema": 1, "provider": "codex-interactive", "requires_approval": True,
            "instructions": "Open each Original in Codex. Use the installed imagegen skill to edit. Preserve faces, composition and Tamil lettering. Do not crop. Clearly flag generated details. Import results into Coverly by original image ID for approval.",
            "images": [{"image_id": a.id, "sha256": a.sha256, "original": f"originals/{a.id}.{a.original.rsplit('.', 1)[-1]}",
                        "prompt": "Restore this vinyl sleeve conservatively. Preserve the complete artwork, all text, faces, and aspect ratio. Repair only visible physical damage. Flag uncertain reconstructed details."} for a in assets]}
