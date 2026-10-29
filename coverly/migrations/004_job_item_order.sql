ALTER TABLE job_items ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
CREATE INDEX ix_job_items_work ON job_items(job_id, status, position);
