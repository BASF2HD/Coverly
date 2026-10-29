from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, field_validator

ROOT = Path(os.environ.get("COVERLY_HOME", Path(__file__).resolve().parent.parent)).resolve()


class Settings(BaseModel):
    storage: str = "archive"
    jpg_quality: int = Field(95, ge=70, le=100)
    request_delay: float = Field(1.5, ge=0.25, le=60)
    request_timeout: int = Field(25, ge=5, le=120)
    max_pages: int = Field(1000, ge=1, le=10000)
    max_depth: int = Field(5, ge=0, le=20)
    max_image_mb: int = Field(40, ge=1, le=100)
    ocr_languages: str = "tam+eng"
    auto_ocr: bool = True
    filesystem_sync: bool = True
    filesystem_poll_seconds: float = Field(5, ge=1, le=300)
    high_confidence: float = Field(.85, ge=.5, le=1)
    worker_concurrency: int = Field(2, ge=1, le=4)
    discovery_concurrency: int = Field(2, ge=1, le=4)
    image_domains: list[str] = Field(default_factory=list)
    naming: str = "{artist}/{title}{year}"
    search_provider: Literal["site", "brave", "google"] = "site"
    search_site: str = "https://newgramophonehouse.com/"

    @field_validator("ocr_languages")
    @classmethod
    def languages(cls, v):
        import re
        if not re.fullmatch(r"[a-z_]+(?:\+[a-z_]+)*", v):
            raise ValueError("Use Tesseract language codes, for example tam+eng")
        return v

    @field_validator("naming")
    @classmethod
    def naming_template(cls, v):
        import string
        fields = []
        for _, name, fmt, conv in string.Formatter().parse(v):
            if name is not None:
                if name not in {"artist", "title", "year", "id"} or fmt or conv:
                    raise ValueError("Naming supports only {artist}, {title}, {year}, {id}")
                fields.append(name)
        if "title" not in fields:
            raise ValueError("Naming must include {title}")
        if "\\" in v or any(p in {"..", ".", ""} for p in v.split("/")) or v.startswith("/"):
            raise ValueError("Use safe relative folder names separated by /")
        # Read legacy settings without carrying IDs into new folder names.
        return "{artist}/{title}{year}"

    @property
    def data(self) -> Path:
        p = Path(self.storage).expanduser()
        return (p if p.is_absolute() else ROOT / p).resolve()


def load_settings() -> Settings:
    path = ROOT / "config.json"
    return Settings.model_validate_json(path.read_text("utf-8")) if path.exists() else Settings()


def save_settings(settings: Settings):
    from .storage import atomic_write
    atomic_write(ROOT / "config.json", json.dumps(settings.model_dump(), indent=2).encode())
