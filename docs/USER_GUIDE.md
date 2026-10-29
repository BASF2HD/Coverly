# Coverly user guide

## Install or run locally

Download the package for your processor from the GitHub release: Apple Silicon DMG, Intel Mac DMG, Windows Setup or Windows Portable ZIP. The desktop packages include Python. Windows needs Microsoft Edge WebView2. Keep the Portable package's `_internal` directory beside `Coverly.exe`. See [desktop installation](DESKTOP.md).

For development, use Python 3.12 or later. Run `setup.command` then `start.command` on macOS, or `setup.ps1` then `start.ps1` on Windows. The interface opens at `http://localhost:8000`. Docker is optional.

## Choose an archive

Choose a directory outside the application, or select an existing archive containing `coverly.sqlite3`. Copy the complete archive when moving computers. Stop workers first or use the backup facility so SQLite and image files remain consistent. Archive references are relative; settings, optional engines and API credentials are configured separately on each computer.

Use Settings to choose a different archive destination. Changing the setting does not move files automatically. Copy an active archive only through the explicit copy/backup workflow with its jobs paused.

## Import and edit artwork

Import multiple images or an album folder containing front, back and disc scans. Files in one import belong to the selected album. Add more artwork from album details. Choose the appropriate front/back/Side A/Side B role and review inferred classifications.

You can edit, rename, add or remove images directly in Finder or Explorer. The background index updates the library, gallery and viewer, with a progress indication during an initial large scan. Current Covers and root-level album images are authoritative. Indexing writes catalogue records and disposable previews; it never recreates missing artwork or rewrites images. **Refresh files** requests an earlier pass.

Right-click an artwork preview and choose **Show in folder**. With a preview focused, Shift+F10 opens the menu and Escape dismisses it. The current file is selected in Finder or File Explorer; Linux opens its directory.

## Album metadata and readable folders

A folder uses `Composer/Album title (1982)`. An unknown film year uses `(YYYY)`. Separate editions with a conflicting name use `_1`, `_2`, etc. Internal album IDs remain in the database and are not appended to new folder names.

Use film release years, never digital soundtrack release dates. Lookup results can include a digital date for source context; that date is excluded from automatic film-year updates.

Choose **Use these details** to fill an album's metadata and soundtrack table, then save. **Save & mark reviewed** closes the dialog and removes the review label. Enable **Keep in review queue** when you want another look. Saving with folder renaming enabled moves the existing album folder to its readable composer/title/year name, preserving edited images. Leave it off to save catalogue data without a folder move. Existing folders can also be changed through a selected rename preview/apply with Undo; no bulk migration runs automatically.

## Bulk metadata lookup across all pages

In Album library or Review queue, use the **All albums · all pages** master checkbox. It follows the current search/filter and supports up to 50,000 selected albums. Choose **Look up selected albums**, then **Import details in background**.

Priority is Apple Music, Spotify, Wikipedia, then configured Google/web search. Moviebuff is an additional film-credit fallback. Sources are matched to the film title; track lists can be supplemented with matching song credits and a verified film year from a film reference. Published performers are not assumed to be composers. Conflicting films/composers and albums you manually edit while queued are retained for your judgment.

The default fills missing composer, film year, track list and credit cells, saves the available details in the catalogue, and keeps Needs review enabled. **Update existing metadata too** explicitly permits correcting current values. **Rename composer / album folders** is a separate opt-in. Without it, the bulk run leaves album folders, artwork and album.json sidecars untouched while you clean the archive manually.

Progress, imported/incomplete counts, Pause/Resume and per-album checkpoints persist after refreshing or restarting. A 24-hour cache avoids repeating identical film lookups. Network time depends on each provider's availability and request pacing; incomplete fields stay empty.

Apple's public catalogue and accessible Wikipedia film pages need no API key. Spotify public pages may not publish readable results; authorised Spotify catalogue access is optional. Google search needs the local Google API key and search-engine ID, or use the configured Brave search provider. The app continues with available sources when another refuses access. Source URLs, field provenance and partial-result notes remain available for manual review.

For a page found in your regular browser, the optional browser bridge can bring the explicitly selected page back into the open album. Copied-table and reference-URL imports are also available. Imported details remain suggestions until you use and save them.

## Catalogue discovery and downloads

Start from a specific permitted category or album URL. Discovery follows numbered pages inside that category and retains language filters. Select albums, download the entire discovered catalogue, or enable automatic download after discovery. Progress includes pages, albums, current images and available byte counts. Source robots policies and access restrictions are respected; browser rendering cannot guarantee access.

Downloads and metadata lookup have separate workers. Pausing or cancelling takes effect at a safe item boundary. Reopening the app resumes unfinished running/queued work from checkpoints; deliberately paused or cancelled jobs stay paused/cancelled. Closing a browser tab alone leaves a separately running local backend active.

Close an individual completed/failed section or use **Close all completed sections**. **Show closed jobs** keeps the retained history accessible.

## Similar artwork and Trash

Compare compact groups of directly similar scans. Front/back and opposite disc roles are kept separate. Select individual images or use **Select all except last · all pages**, inspect the kept images, then confirm removal. Keep-last is a convenience policy, not a quality guarantee. Bulk Undo restores removed image relationships.

**Remove artwork** hides that album's image relationship; **Move album to Trash** retains its files and metadata. Permanent Trash cleanup requires an explicit preview and DELETE confirmation. It accounts for shared files and removes only the confirmed content. No automatic purge or missing-file recovery runs.

## Restoration and music-tag exports

In Restoration studio, select albums and individual images. Basic CPU correction is included. Results are saved separately for comparison and approval. Approval can create a new active restored copy; manually edited source artwork remains preserved.

Square JPEG export provides crop/pad position review and stays below 500 KB for embedding in song metadata. Inspect the preview so important text or artwork is retained. Import external restoration results into the matching image and approve them explicitly.

Advanced AI enhancement is optional through the documented source installation. PyTorch/Real-ESRGAN uses MPS, CUDA or CPU where compatible; compact native packages use the CPU provider. ChatGPT and Codex restoration are interactive handoffs rather than unattended subscription automation. The optional Chrome/Edge bridge transfers selected images and a prompt to ChatGPT without pressing Send; its copied-image fallback avoids searching album folders.

## Optional OCR and search setup

Install Tesseract and Tamil/English language data for OCR. On macOS, Homebrew installations normally expose `tesseract`; on Windows, add the installed engine to PATH. Select `tam+eng` in Settings. Missing optional language data does not disable the rest of the archive.

Search credentials belong in the local environment, never in the archive, repository or browser bundle. See `.env.example` for variable names. The built-in film/catalogue lookups remain available without paid search credentials.

## Backups, privacy and updates

Back up the complete archive or use the app's portable ZIP backup. Application updates replace the app separately from your chosen archive. Public source and desktop packages exclude archives, databases, local configuration, credentials, personal screenshots and internal verification notes.

Use the release's SHA-256 sidecars and platform build reports to verify downloads. The static GitHub Pages site is the download guide; the archive interface runs locally at `localhost:8000`.
