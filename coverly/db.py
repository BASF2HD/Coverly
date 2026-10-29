from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from sqlalchemy import create_engine, event, String, Integer, Float, Text, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def uid():
    return uuid.uuid4().hex


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class Artist(Base):
    __tablename__ = "artists"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String, unique=True)


class Album(Base):
    __tablename__ = "albums"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String, index=True)
    tamil_title: Mapped[str] = mapped_column(String, default="")
    artist_id: Mapped[str | None] = mapped_column(ForeignKey("artists.id"))
    artist: Mapped[str] = mapped_column(String, default="Unknown Artist")
    year: Mapped[int | None] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String, default="")
    catalogue: Mapped[str] = mapped_column(String, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    review: Mapped[bool] = mapped_column(default=True, index=True)
    compilation: Mapped[bool] = mapped_column(default=False)
    folder: Mapped[str] = mapped_column(String, unique=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    soundtrack: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[str] = mapped_column(String, default=now)
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)


class Edition(Base):
    __tablename__ = "editions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    album_id: Mapped[str] = mapped_column(ForeignKey("albums.id"), index=True)
    label: Mapped[str] = mapped_column(String, default="")
    catalogue: Mapped[str] = mapped_column(String, default="")
    source_url: Mapped[str | None] = mapped_column(String, unique=True)


class AlbumRelation(Base):
    __tablename__ = "album_relations"
    parent_id: Mapped[str] = mapped_column(ForeignKey("albums.id"), primary_key=True)
    child_id: Mapped[str] = mapped_column(ForeignKey("albums.id"), primary_key=True)
    evidence: Mapped[str] = mapped_column(Text)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    domain: Mapped[str] = mapped_column(String, unique=True)


class Page(Base):
    __tablename__ = "pages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    url: Mapped[str] = mapped_column(String, unique=True)
    title: Mapped[str] = mapped_column(String, default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    album_id: Mapped[str | None] = mapped_column(ForeignKey("albums.id"))
    created: Mapped[str] = mapped_column(String, default=now)


class ImageAsset(Base):
    __tablename__ = "images"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    sha256: Mapped[str] = mapped_column(String, unique=True)
    phash: Mapped[str] = mapped_column(String, index=True)
    original: Mapped[str] = mapped_column(String, unique=True)
    derivative: Mapped[str] = mapped_column(String)
    thumbnail: Mapped[str] = mapped_column(String)
    format: Mapped[str] = mapped_column(String)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    size: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text, default="")
    page_url: Mapped[str] = mapped_column(Text, default="")
    near_duplicate: Mapped[bool] = mapped_column(default=False)
    created: Mapped[str] = mapped_column(String, default=now)


class AlbumImage(Base):
    __tablename__ = "album_images"
    album_id: Mapped[str] = mapped_column(ForeignKey("albums.id"), primary_key=True)
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id"), primary_key=True)
    classification: Mapped[str] = mapped_column(String, default="additional")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    manual: Mapped[bool] = mapped_column(default=False)
    evidence: Mapped[str] = mapped_column(Text, default="")
    active_restoration_id: Mapped[str | None] = mapped_column(ForeignKey('restorations.id'))
    cover_path: Mapped[str] = mapped_column(String,default='')
    cover_source: Mapped[str] = mapped_column(String,default='')
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)


class ArtistAlias(Base):
    __tablename__='artist_aliases'
    alias: Mapped[str] = mapped_column(String,primary_key=True)
    name: Mapped[str] = mapped_column(String)
    canonical_id: Mapped[str] = mapped_column(ForeignKey('artists.id'))
    evidence: Mapped[dict] = mapped_column(JSON,default=dict)
    created: Mapped[str] = mapped_column(String,default=now)


class FilesystemAlbum(Base):
    __tablename__ = 'filesystem_albums'
    album_id: Mapped[str] = mapped_column(ForeignKey('albums.id', ondelete='CASCADE'), primary_key=True)
    folder: Mapped[str] = mapped_column(String)
    identity: Mapped[str] = mapped_column(String, default='')
    present: Mapped[bool] = mapped_column(default=True)
    covers_seen: Mapped[bool] = mapped_column(default=False)


class FilesystemFile(Base):
    __tablename__ = 'filesystem_files'
    __table_args__ = (UniqueConstraint('album_id', 'path'),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    album_id: Mapped[str] = mapped_column(ForeignKey('albums.id', ondelete='CASCADE'), index=True)
    image_id: Mapped[str] = mapped_column(ForeignKey('images.id'), index=True)
    path: Mapped[str] = mapped_column(String)
    signature: Mapped[str] = mapped_column(String)
    present: Mapped[bool] = mapped_column(default=True)


class OCRResult(Base):
    __tablename__ = "ocr_results"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    languages: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    created: Mapped[str] = mapped_column(String, default=now)


class Candidate(Base):
    __tablename__ = "identification_candidates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    album_id: Mapped[str] = mapped_column(ForeignKey("albums.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    evidence: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    worker_id: Mapped[str] = mapped_column(String, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="Waiting for worker")
    created: Mapped[str] = mapped_column(String, default=now)
    updated: Mapped[str] = mapped_column(String, default=now)


class JobItem(Base):
    __tablename__ = "job_items"
    __table_args__ = (UniqueConstraint("job_id", "key"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    key: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Restoration(Base):
    __tablename__ = "restorations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    album_id: Mapped[str | None] = mapped_column(ForeignKey('albums.id'))
    image_id: Mapped[str] = mapped_column(ForeignKey("images.id"), index=True)
    path: Mapped[str] = mapped_column(String)
    input_path: Mapped[str] = mapped_column(String, default="")
    provider: Mapped[str] = mapped_column(String)
    preset: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")
    generated: Mapped[bool] = mapped_column(default=False)
    created: Mapped[str] = mapped_column(String, default=now)


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    action: Mapped[str] = mapped_column(String, index=True)
    target: Mapped[str] = mapped_column(String, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[str] = mapped_column(String, default=now)


def open_db(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    engine = create_engine("sqlite:///" + str(root / "coverly.sqlite3"), connect_args={"timeout": 30})

    @event.listens_for(engine, "connect")
    def configure(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")

    # Apply packaged SQL migrations in order; never rebuild an existing archive.
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied TEXT NOT NULL)"))
    for migration in sorted((Path(__file__).parent / "migrations").glob("[0-9]*.sql")):
        version = int(migration.name.split("_", 1)[0])
        with engine.begin() as conn:
            if conn.execute(text("SELECT 1 FROM schema_migrations WHERE version=:version"), {"version": version}).first():
                continue
            for statement in migration.read_text("utf-8").split(";"):
                if statement.strip():
                    conn.exec_driver_sql(statement)
            conn.execute(text("INSERT INTO schema_migrations VALUES (:version, :at)"), {"version": version, "at": now()})
    return engine, sessionmaker(engine, expire_on_commit=False)
