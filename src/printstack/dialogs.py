"""Native Win32 file and folder picker dialogs (no tkinter dependency)."""

from __future__ import annotations

import os
from pathlib import Path

import pythoncom
import pywintypes
import win32con
import win32gui
from win32com.shell import shell

from printstack.config import SUPPORTED_EXTS
from printstack.printer_service import find_app_window_hwnd

BIF_RETURNONLYFSDIRS = 0x0001
BIF_NEWDIALOGSTYLE = 0x0040


def _build_file_filter() -> str:
    all_exts = ";".join(f"*{e}" for e in sorted(SUPPORTED_EXTS))
    parts = [
        "Supported Documents",
        all_exts,
        "PDF Files",
        "*.pdf",
        "Word Documents",
        "*.docx;*.doc;*.rtf",
        "Excel Spreadsheets",
        "*.xlsx;*.xls;*.xlsm;*.xlsb;*.csv",
        "Images",
        "*.jpg;*.jpeg;*.png;*.bmp;*.webp;*.tif;*.tiff",
        "All Files",
        "*.*",
    ]
    return "\0".join(parts) + "\0\0"


def pick_files_dialog() -> list[str]:
    """Open native Windows multi-file picker parented to the app window."""
    owner_hwnd = find_app_window_hwnd()
    flags = (
        win32con.OFN_ALLOWMULTISELECT
        | win32con.OFN_EXPLORER
        | win32con.OFN_FILEMUSTEXIST
        | win32con.OFN_NOCHANGEDIR
    )
    try:
        fname, _custom, _flags = win32gui.GetOpenFileNameW(
            hwndOwner=owner_hwnd,
            Filter=_build_file_filter(),
            FilterIndex=1,
            Title="Select PDF, Word, Excel, or Image Files",
            Flags=flags,
            MaxFile=65535,
        )
    except pywintypes.error:
        # User cancelled dialog (CommDlgExtendedError == 0) or buffer cancelled
        return []

    if not fname:
        return []

    parts = [p for p in fname.split("\0") if p]
    if len(parts) == 1:
        return parts

    base_dir = parts[0]
    return [os.path.join(base_dir, name) for name in parts[1:]]


def pick_folder_dialog() -> str:
    """Open native Windows folder picker parented to the app window."""
    owner_hwnd = find_app_window_hwnd()
    initialized = False
    try:
        pythoncom.CoInitialize()
        initialized = True
    except pythoncom.com_error:
        pass

    try:
        pidl, _display_name, _image_list = shell.SHBrowseForFolder(
            owner_hwnd,
            None,
            "Select Folder Containing Documents",
            BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE,
        )
        if not pidl:
            return ""
        raw_path = shell.SHGetPathFromIDList(pidl)
        if isinstance(raw_path, bytes):
            return raw_path.decode("mbcs", errors="replace")
        return str(Path(raw_path))
    except Exception:
        return ""
    finally:
        if initialized:
            pythoncom.CoUninitialize()
