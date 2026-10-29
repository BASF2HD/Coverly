Coverly's first public desktop release includes separate Apple Silicon and Intel Mac DMGs, Windows Setup, and a Windows Portable ZIP. The same local archive works across supported systems; application packages contain no personal artwork or configuration.

- Dynamic library indexing reflects images edited, renamed, added or removed directly in Finder and Explorer. It never restores deleted files or rewrites artwork.
- Right-click artwork previews to show the current file in its folder.
- Select matching albums across every page with one master checkbox. Bulk metadata lookup uses Apple Music, Spotify, Wikipedia and then configured Google/web search; it saves available composer, verified film year and track credits while retaining Needs review. Conflicting sources and manual edits are protected, progress survives refresh, and folder moves are an explicit opt-in.
- Readable album folders use `Album (1982)`, `Album (YYYY)` for unknown film years, and `_1`, `_2` suffixes for conflicts. Explicit metadata saves with folder renaming enabled can apply a confirmed film year. Existing folders are not migrated automatically.
- Resumable catalogue downloads, film music credits and soundtrack review, compact duplicate comparison, light/dark themes, and square JPEG export under 500 KB.
- CPU image correction is included. Advanced AI engines, Tamil/English Tesseract data, and browser integrations are optional installations documented in the user guide.

Mac packages are ad-hoc signed development builds and are not Apple notarized. The Windows binaries are not Authenticode signed. Windows requires Microsoft Edge WebView2. Setup installs per user; updating or uninstalling the application does not delete an archive outside its installation directory. Extract the complete Portable ZIP and keep `_internal` beside `Coverly.exe`.

SHA-256 sidecars and build-provenance JSON accompany every platform. Native package and background-worker self-tests run before publication.
