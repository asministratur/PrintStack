<p align="left">
  <img src="assets/logo.svg" alt="Printstack logo" width="56" height="56" />
</p>

# Printstack

Windows desktop utility for queuing, previewing, and batch-printing PDF, Word, and image files with native Win32 printer settings.

## Download (Windows 10 / 11)

No Python installation is required for prebuilt releases.

1. Download the latest release from [GitHub Releases](https://github.com/asministratur/PrintStack/releases):
   - **Installer** (`Printstack-Setup-*.exe`): per-user install (no admin prompt) with optional Desktop, SendTo, and Explorer right-click integration.
   - **Portable** (`Printstack-*-win64-portable.zip`): extract and run `Printstack.exe`.
2. Drop files or folders into the window, verify the sheet preview, and press `Ctrl+P` to print.

## Features

- **Multi-format queue**: PDF (`.pdf`), Word (`.docx`, `.doc`, `.rtf`), and images (`.jpg`, `.jpeg`, `.png`, `.bmp`, `.webp`, `.tif`, `.tiff`) with natural sort and recursive folder import.
- **Word conversion fallback**: converts Word documents via Microsoft Word COM or headless LibreOffice (`soffice`).
- **Interactive preview**: cached sheet flipping, `Ctrl+Scroll` zoom, drag-to-pan, per-page jump, and click-to-skip thumbnails.
- **Full Win32 printer control**: dynamic paper sizes, trays, duplex, color/grayscale, N-up (2/4/6/9 per sheet), custom page ranges, saved presets, per-file overrides, and native Windows Driver Properties dialog.
- **Batch modes & guardrails**: print as separate jobs or merge into a single job, with pre-print warnings for offline printers or batches over 50 sheets.
- **Windows Explorer integration**: right-click files or folders (*Print with Printstack*) or use *Send to* to append directly to the running instance.

## Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| `Ctrl+O` | Add files |
| `Ctrl+P` | Start batch print |
| `Up` / `Down` | Select previous / next document in queue |
| `Delete` | Remove selected document from queue |
| `Left` / `Right` | Previous / next preview sheet |
| `Ctrl+Scroll` | Zoom preview |

## Run from Source

Requires Windows 10/11 and Python 3.11+.

```powershell
git clone git@github.com:asministratur/PrintStack.git
cd PrintStack
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python -m printstack
```

Run the HTTP server directly during development:

```powershell
uvicorn printstack.main:app --host 127.0.0.1 --port 47821
```

### Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PRINTSTACK_PORT` | `47821` | Preferred local HTTP/WebSocket port (falls back automatically if occupied) |
| `PRINTSTACK_DATA_DIR` | `%LOCALAPPDATA%\Printstack` | Directory for SQLite database, temporary files, and logs |

## Build Installer Locally

```powershell
.\packaging\build.ps1 -Version 1.0.0
```

Produces `dist\Printstack-1.0.0-win64-portable.zip` and (when Inno Setup 6 is installed) `dist\Printstack-Setup-1.0.0.exe`.

## Testing

```powershell
pytest -q
```

## License

MIT
