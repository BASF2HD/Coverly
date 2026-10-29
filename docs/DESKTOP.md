# Coverly desktop

The desktop shell uses the same Coverly archive, interface and background workers as the local web application. It opens a native window (WKWebView on macOS, WebView2 on Windows). The local application remains available at http://localhost:8000.

## Installation and archive safety

On macOS, open the matching DMG and drag **Coverly.app** into Applications. Use the ARM64 build for Apple Silicon and the x64 build for Intel. These development builds are ad-hoc signed, not Apple notarized. For Windows, run the **Setup.exe** installer for a per-user installation, or extract the entire **Portable.zip** into a folder and open **Coverly.exe**. Keep the accompanying `_internal` folder beside the EXE. Windows requires the Microsoft Edge WebView2 runtime; it is normally included with current Windows installations.

Python is included in the desktop package. Docker and a system Python installation are not needed to run the basic app.

If Coverly is already running on port 8000, the desktop window connects to it and remembers that archive location. Closing that window leaves the previously running server and its downloads alone. If the desktop app starts the backend itself, closing the app stops the workers safely; reopening resumes queued jobs from their saved checkpoints. Paused jobs remain paused. A server belonging to another application on port 8000 is never terminated or displayed as Coverly.

On a new installation, choose **Choose existing archive…** and select the directory containing `coverly.sqlite3`, or create a new archive. Settings also let you choose a directory outside the app. Updating or moving the application bundle does not move or delete the artwork.

The desktop Add album folder action uses the native directory picker and transfers up to 100 selected images (256 MB total) to the normal import preview. Regular browser folder import uses its directory-upload control.

Desktop settings and logs live here:

- macOS: `~/Library/Application Support/Coverly/`
- Windows: `%LOCALAPPDATA%/Coverly/`
- Linux source launcher: `${XDG_DATA_HOME:-~/.local/share}/Coverly/`

`COVERLY_HOME` overrides the settings directory, including for portable installations. Album manifests and image references remain relative to the archive. To move computers, copy the complete archive directory and choose it on the new installation. Do not copy individual database files while workers are writing; use Coverly's backup facility or stop Coverly first.

## Optional processing and website support

CPU image correction, square JPEG export under 500 KB, metadata lookup, ordinary website discovery/download, review, duplicate comparison and archive management are included. Native packaging cannot make a website accept access it refuses. Browser-assisted discovery still requires the site's own verification and robots permission.

The build includes Playwright's Python integration, but does not bundle a Chromium download. Browser-assisted discovery can use a normally installed Chrome. For the optional JavaScript renderer, install Chromium with the source environment's `python -m playwright install chromium`; its default per-user cache can be shared with the packaged app. Optional engines are outside the archive and need reinstalling on another OS.

OCR needs a separately installed Tesseract engine plus Tamil and English language data. See the main README for macOS/Windows instructions. Finder launches also search `/opt/homebrew/bin` and `/usr/local/bin`. On Windows, add the Tesseract installation to PATH before opening Coverly.

This compact package uses the CPU restoration provider. Optional Real-ESRGAN/PyTorch acceleration is available through the source installation documented in README; run that backend and the desktop shell will connect to it. This supports MPS/CUDA/CPU where those packages are compatible. Basic restoration remains usable without those model packages. ChatGPT's Chrome/Edge handoff extension runs in the regular browser, not inside WKWebView/WebView2. Open http://localhost:8000 in the profile where the extension is installed for direct ChatGPT attachments or source-page imports.

## Reproducible native builds

Use Python 3.12 or later and build on the target operating system and architecture:

```text
macOS:   ./build-desktop.command
Windows: .\build-desktop.ps1
```

The scripts create an isolated build environment and write packages into `dist/`. They explicitly include only the application, interface, migrations, browser extension and runtime resources. Your archive, credentials, local settings, models and working virtual environments are excluded.

The native executable is tested before packaging:

1. Temporary SQLite archive, artwork import, CPU correction and square JPEG export.
2. Actual frozen server/worker processes on an isolated test port, an HTTP restoration job, and cooperative shutdown.

Reports are written beside the native app. To check native rendering on an interactive desktop:

```text
macOS:   dist/macos-arm64/Coverly.app/Contents/MacOS/Coverly --gui-self-test-report /tmp/coverly-gui.json
Windows: dist\windows-x64\Coverly\Coverly.exe --gui-self-test-report gui-test.json
```

The GitHub workflow `.github/workflows/desktop.yml` builds and tests Apple Silicon, Intel macOS and Windows x64 packages on their native runners. Push to `main` or run **Desktop release and download page** manually. After all builds pass, matching SHA-256/provenance records are verified and the version is published as a public GitHub release, followed by the static GitHub Pages download page. An existing release is preserved; increase the version for the next release. Enable Pages → Source → GitHub Actions once in repository settings. Archives and local configuration are excluded. Windows Setup is compiled with Inno Setup 6; install it for local Windows builds, or use `--portable-only`. A Windows EXE must be built on Windows; an Intel Mac build needs an Intel-compatible Python and dependencies. Merely changing a filename or target flag does not cross-compile these runtimes.
