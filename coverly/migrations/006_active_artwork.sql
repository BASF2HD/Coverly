ALTER TABLE album_images ADD COLUMN active_restoration_id TEXT REFERENCES restorations(id);
ALTER TABLE album_images ADD COLUMN cover_path TEXT NOT NULL DEFAULT '';
ALTER TABLE album_images ADD COLUMN cover_source TEXT NOT NULL DEFAULT '';
ALTER TABLE restorations ADD COLUMN album_id TEXT REFERENCES albums(id);
CREATE TABLE artist_aliases (alias TEXT PRIMARY KEY, name TEXT NOT NULL, canonical_id TEXT NOT NULL REFERENCES artists(id), evidence JSON NOT NULL DEFAULT '{}', created TEXT NOT NULL);
UPDATE album_images SET active_restoration_id = (SELECT r.id FROM restorations r WHERE r.image_id=album_images.image_id AND r.status='approved' ORDER BY r.created DESC LIMIT 1);
