# Coverly

A local Tamil film artwork archive for macOS and Windows. Collect vinyl sleeves and disc scans, review film music credits, compare similar artwork, and prepare restored square JPEGs for music tags.

## Downloads

- [Latest desktop release](https://github.com/BASF2HD/Coverly/releases/latest)
- [Download page and platform guide](https://basf2hd.github.io/Coverly/)

The first release is being prepared with separate Apple Silicon and Intel Mac DMGs, a Windows Setup installer, and a Windows Portable ZIP. Downloads appear after the native build and verification jobs finish.

## Your archive stays yours

Artwork lives in a folder you choose, outside the app. Coverly notices images you add, rename, edit or remove in Finder or Explorer. Indexing does not restore deleted files, rewrite artwork, rename folders, or replace your saved music credits. Right-click artwork and choose **Show in folder** to reveal its current file.

Album directories use `Composer/Album name (1982)`. Unknown film years use `(YYYY)`, and collisions use readable `_1`, `_2` suffixes. A saved film year updates the folder only when folder renaming is enabled for that save; existing folders can also be changed through a reviewed rename plan.

## Features

- Scoped catalogue discovery and resumable artwork downloads with progress.
- Film-year and soundtrack metadata review, browser-page imports, and Tamil OCR with optional Tesseract.
- Front/back/disc classification, similar-artwork comparison, and reversible removal.
- Local restoration, optional AI enhancement, ChatGPT handoff, and square JPEG export under 500 KB.
- Compact dark and light themes, portable relative archive paths, and local access at `http://localhost:8000`.

## Development

Requires Python 3.12 or later. Run `setup.command`, then `start.command` on macOS; use `setup.ps1` and `start.ps1` on Windows. Docker is optional. Native packages include Python and do not require it to be installed separately.

See [the user guide](docs/USER_GUIDE.md), [desktop installation and building](docs/DESKTOP.md), and [verification notes](docs/DESKTOP_VERIFICATION.md). GitHub Actions builds each desktop package on its matching operating system and architecture. Archives, local settings and credentials are excluded from Git and packages.
