-- Coverly schema v1. Applied incrementally by coverly.db.open_db.

CREATE TABLE artists (
	id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
);

CREATE TABLE sources (
	id VARCHAR NOT NULL,
	domain VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (domain)
);

CREATE TABLE images (
	id VARCHAR NOT NULL,
	sha256 VARCHAR NOT NULL,
	phash VARCHAR NOT NULL,
	original VARCHAR NOT NULL,
	derivative VARCHAR NOT NULL,
	thumbnail VARCHAR NOT NULL,
	format VARCHAR NOT NULL,
	width INTEGER NOT NULL,
	height INTEGER NOT NULL,
	size INTEGER NOT NULL,
	source_url TEXT NOT NULL,
	page_url TEXT NOT NULL,
	near_duplicate BOOLEAN NOT NULL,
	created VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (sha256),
	UNIQUE (original)
);

CREATE INDEX ix_images_phash ON images (phash);

CREATE TABLE jobs (
	id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	worker_id VARCHAR NOT NULL,
	payload JSON NOT NULL,
	progress INTEGER NOT NULL,
	total INTEGER NOT NULL,
	message TEXT NOT NULL,
	created VARCHAR NOT NULL,
	updated VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_jobs_status ON jobs (status);

CREATE TABLE audit_events (
	id VARCHAR NOT NULL,
	action VARCHAR NOT NULL,
	target VARCHAR NOT NULL,
	detail JSON NOT NULL,
	created VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_audit_events_action ON audit_events (action);

CREATE TABLE albums (
	id VARCHAR NOT NULL,
	title VARCHAR NOT NULL,
	tamil_title VARCHAR NOT NULL,
	artist_id VARCHAR,
	artist VARCHAR NOT NULL,
	year INTEGER,
	label VARCHAR NOT NULL,
	catalogue VARCHAR NOT NULL,
	confidence FLOAT NOT NULL,
	review BOOLEAN NOT NULL,
	compilation BOOLEAN NOT NULL,
	folder VARCHAR NOT NULL,
	notes TEXT NOT NULL,
	created VARCHAR NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(artist_id) REFERENCES artists (id),
	UNIQUE (folder)
);

CREATE INDEX ix_albums_title ON albums (title);

CREATE INDEX ix_albums_review ON albums (review);

CREATE TABLE ocr_results (
	id VARCHAR NOT NULL,
	image_id VARCHAR NOT NULL,
	text TEXT NOT NULL,
	confidence FLOAT NOT NULL,
	languages VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	created VARCHAR NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(image_id) REFERENCES images (id)
);

CREATE INDEX ix_ocr_results_image_id ON ocr_results (image_id);

CREATE TABLE job_items (
	id VARCHAR NOT NULL,
	job_id VARCHAR NOT NULL,
	"key" VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	error TEXT NOT NULL,
	attempts INTEGER NOT NULL,
	data JSON NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (job_id, "key"),
	FOREIGN KEY(job_id) REFERENCES jobs (id)
);

CREATE INDEX ix_job_items_job_id ON job_items (job_id);

CREATE TABLE restorations (
	id VARCHAR NOT NULL,
	job_id VARCHAR,
	image_id VARCHAR NOT NULL,
	path VARCHAR NOT NULL,
	input_path VARCHAR NOT NULL,
	provider VARCHAR NOT NULL,
	preset VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	generated BOOLEAN NOT NULL,
	created VARCHAR NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(job_id) REFERENCES jobs (id),
	FOREIGN KEY(image_id) REFERENCES images (id)
);

CREATE INDEX ix_restorations_image_id ON restorations (image_id);

CREATE TABLE editions (
	id VARCHAR NOT NULL,
	album_id VARCHAR NOT NULL,
	label VARCHAR NOT NULL,
	catalogue VARCHAR NOT NULL,
	source_url VARCHAR,
	PRIMARY KEY (id),
	FOREIGN KEY(album_id) REFERENCES albums (id),
	UNIQUE (source_url)
);

CREATE INDEX ix_editions_album_id ON editions (album_id);

CREATE TABLE album_relations (
	parent_id VARCHAR NOT NULL,
	child_id VARCHAR NOT NULL,
	evidence TEXT NOT NULL,
	PRIMARY KEY (parent_id, child_id),
	FOREIGN KEY(parent_id) REFERENCES albums (id),
	FOREIGN KEY(child_id) REFERENCES albums (id)
);

CREATE TABLE pages (
	id VARCHAR NOT NULL,
	source_id VARCHAR NOT NULL,
	url VARCHAR NOT NULL,
	title VARCHAR NOT NULL,
	metadata_json JSON NOT NULL,
	album_id VARCHAR,
	created VARCHAR NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(source_id) REFERENCES sources (id),
	UNIQUE (url),
	FOREIGN KEY(album_id) REFERENCES albums (id)
);

CREATE TABLE album_images (
	album_id VARCHAR NOT NULL,
	image_id VARCHAR NOT NULL,
	classification VARCHAR NOT NULL,
	confidence FLOAT NOT NULL,
	manual BOOLEAN NOT NULL,
	PRIMARY KEY (album_id, image_id),
	FOREIGN KEY(album_id) REFERENCES albums (id),
	FOREIGN KEY(image_id) REFERENCES images (id)
);

CREATE TABLE identification_candidates (
	id VARCHAR NOT NULL,
	album_id VARCHAR NOT NULL,
	title VARCHAR NOT NULL,
	evidence TEXT NOT NULL,
	confidence FLOAT NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(album_id) REFERENCES albums (id)
);

CREATE INDEX ix_identification_candidates_album_id ON identification_candidates (album_id);
