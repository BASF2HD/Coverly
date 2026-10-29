# Release verification

The release preparation passed **236 local automated tests**, with two environment-dependent skips. A focused set of checksum-publication tests also passed.

Tests use disposable archives and controlled source fixtures. They cover:

- macOS/Windows/Linux path handling, SQLite migrations and backup round trips;
- incremental filesystem indexing, manual image edits, additions, renames and removals without recovery;
- readable film-year folder names, unknown-year placeholders, numbered conflicts and explicit rename/undo;
- right-click folder reveal, gallery/viewer refresh, compact controls, light/dark themes and metadata review;
- all-pages bulk selection, trusted-source priority, field provenance, digital-date exclusion, conflicting-film protection and manual edits during queued lookups;
- persistent metadata progress, fair worker scheduling, pause/resume and album checkpoints;
- source-scoped catalogue pagination, download retries and archive integrity;
- separate native build targets, complete-package requirements and checksum/provenance validation before release.

Each native package is built on its target operating system and processor architecture. Frozen application tests verify packaged resources, artwork import, CPU restoration and square JPEG export below 500 KB in a temporary archive. A separate frozen backend test starts real local server/worker processes, completes a restoration job and checks shutdown. Test archives and local settings are never bundled.

The GitHub release contains the exact SHA-256 sidecar and build-provenance JSON for each platform. Workflow runs provide the test/build logs. macOS packages are ad-hoc signed development builds, without Apple notarization; Windows packages are unsigned. A passing fixture test does not guarantee that an external catalogue will allow automated access or expose complete metadata.
