CREATE TABLE filesystem_albums (album_id TEXT PRIMARY KEY REFERENCES albums(id) ON DELETE CASCADE, folder TEXT NOT NULL, identity TEXT NOT NULL DEFAULT '', present BOOLEAN NOT NULL DEFAULT 1, covers_seen BOOLEAN NOT NULL DEFAULT 0);
CREATE TABLE filesystem_files (id TEXT PRIMARY KEY, album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE, image_id TEXT NOT NULL REFERENCES images(id), path TEXT NOT NULL, signature TEXT NOT NULL, present BOOLEAN NOT NULL DEFAULT 1, UNIQUE(album_id,path));
CREATE INDEX ix_filesystem_files_album_id ON filesystem_files(album_id);
CREATE INDEX ix_filesystem_files_image_id ON filesystem_files(image_id);
