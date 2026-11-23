"""Configuration constants and directory layout for Printstack."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Printstack"
APP_VERSION = "1.0.0"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = int(os.environ.get("PRINTSTACK_PORT") or os.environ.get("BATCH_PRINT_PORT") or "47821")

IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    BASE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    STATIC_DIR = BUNDLE_DIR / "ui"
    ASSETS_DIR = BUNDLE_DIR / "assets"
else:
    PACKAGE_DIR = Path(__file__).resolve().parent
    BASE_DIR = PACKAGE_DIR.parent.parent
    BUNDLE_DIR = BASE_DIR
    STATIC_DIR = PACKAGE_DIR / "ui"
    ASSETS_DIR = BASE_DIR / "assets"

ICON_ICO_PATH = ASSETS_DIR / "icon.ico"
ICON_PNG_PATH = ASSETS_DIR / "icon.png"
LOGO_SVG_PATH = ASSETS_DIR / "logo.svg"


def get_data_dir() -> Path:
    custom = os.environ.get("PRINTSTACK_DATA_DIR") or os.environ.get("BATCH_PRINT_DATA_DIR")
    if custom:
        return Path(custom)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Printstack"
    return Path.home() / "AppData" / "Local" / "Printstack"


DATA_DIR = get_data_dir()
TEMP_DIR = DATA_DIR / "temp"
UPLOAD_DIR = DATA_DIR / "uploads"
BROWSER_PROFILE_DIR = DATA_DIR / "browser-profile"
DB_PATH = DATA_DIR / "printstack.db"
SERVER_INFO_PATH = DATA_DIR / "server.json"

PDF_EXTS = {".pdf"}
WORD_EXTS = {".docx", ".doc", ".rtf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
SUPPORTED_EXTS = PDF_EXTS | WORD_EXTS | IMAGE_EXTS


def ensure_dirs() -> None:
    """Ensure runtime data directories exist."""
    for folder in (DATA_DIR, TEMP_DIR, UPLOAD_DIR, BROWSER_PROFILE_DIR):
        folder.mkdir(parents=True, exist_ok=True)
