"""Windows Desktop shortcut, SendTo shortcut, and Explorer HKCU right-click integration."""

from __future__ import annotations

import json
import os
import sys
import winreg
from pathlib import Path
from typing import Any

import pythoncom
import win32com.client

from printstack.config import APP_NAME, BASE_DIR, ICON_ICO_PATH, IS_FROZEN, SUPPORTED_EXTS

CONTEXT_KEY_NAME = "Printstack"
CONTEXT_MENU_LABEL = "Print with Printstack"


def get_pythonw_path() -> Path:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.exists():
        return pythonw
    return exe


def get_launch_spec() -> tuple[Path, str, Path]:
    """Return (executable, base_args, working_dir) for both frozen .exe and source runs."""
    if IS_FROZEN:
        exe = Path(sys.executable).resolve()
        return exe, "", exe.parent
    return get_pythonw_path(), "-m printstack", BASE_DIR


def _get_icon_location(target_exe: Path) -> str:
    if ICON_ICO_PATH.exists():
        return f"{ICON_ICO_PATH},0"
    if IS_FROZEN:
        return f"{target_exe},0"
    return ""


def _get_desktop_dir() -> Path:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "Desktop")
            if val:
                return Path(val)
    except Exception:
        pass
    return Path.home() / "Desktop"


def _get_sendto_dir() -> Path:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "SendTo")
            if val:
                return Path(val)
    except Exception:
        pass
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "SendTo"


def get_desktop_shortcut_path() -> Path:
    return _get_desktop_dir() / f"{APP_NAME}.lnk"


def get_sendto_shortcut_path() -> Path:
    return _get_sendto_dir() / f"{APP_NAME}.lnk"


def _create_shortcut(lnk_path: Path, target_path: Path, arguments: str, icon_loc: str, working_dir: Path) -> None:
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    pythoncom.CoInitialize()
    shell = None
    shortcut = None
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(lnk_path))
        shortcut.TargetPath = str(target_path)
        shortcut.Arguments = arguments
        shortcut.WorkingDirectory = str(working_dir)
        if icon_loc:
            shortcut.IconLocation = icon_loc
        shortcut.Description = "Batch print PDF, Word, Excel, and images with native Windows printer settings"
        shortcut.Save()
    finally:
        shortcut = None
        shell = None
        pythoncom.CoUninitialize()


def _context_registry_paths() -> list[str]:
    paths = [rf"Software\Classes\Directory\shell\{CONTEXT_KEY_NAME}"]
    for ext in sorted(SUPPORTED_EXTS):
        paths.append(rf"Software\Classes\SystemFileAssociations\{ext}\shell\{CONTEXT_KEY_NAME}")
    return paths


def _delete_reg_tree(root: int, subkey: str) -> None:
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                    _delete_reg_tree(root, f"{subkey}\\{child}")
                except OSError:
                    break
        winreg.DeleteKey(root, subkey)
    except FileNotFoundError:
        pass


def get_integration_status() -> dict[str, Any]:
    desktop_lnk = get_desktop_shortcut_path()
    sendto_lnk = get_sendto_shortcut_path()

    context_installed = True
    for reg_path in _context_registry_paths():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{reg_path}\\command"):
                pass
        except FileNotFoundError:
            context_installed = False
            break

    return {
        "desktop_shortcut": desktop_lnk.exists(),
        "desktop_shortcut_path": str(desktop_lnk),
        "sendto_shortcut": sendto_lnk.exists(),
        "sendto_shortcut_path": str(sendto_lnk),
        "explorer_context_menu": context_installed,
        "supported_extensions": sorted(SUPPORTED_EXTS),
    }


def install_integration(
    desktop: bool = True, sendto: bool = True, context_menu: bool = True
) -> dict[str, Any]:
    target_exe, base_args, work_dir = get_launch_spec()
    icon_loc = _get_icon_location(target_exe)

    if desktop:
        _create_shortcut(
            lnk_path=get_desktop_shortcut_path(),
            target_path=target_exe,
            arguments=base_args,
            icon_loc=icon_loc,
            working_dir=work_dir,
        )

    if sendto:
        _create_shortcut(
            lnk_path=get_sendto_shortcut_path(),
            target_path=target_exe,
            arguments=base_args,
            icon_loc=icon_loc,
            working_dir=work_dir,
        )

    if context_menu:
        cmd_str = f'"{target_exe}" "%1"' if IS_FROZEN else f'"{target_exe}" {base_args} "%1"'
        icon_val = str(ICON_ICO_PATH) if ICON_ICO_PATH.exists() else str(target_exe)
        for reg_path in _context_registry_paths():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, CONTEXT_MENU_LABEL)
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon_val)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{reg_path}\\command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd_str)

    return get_integration_status()


def uninstall_integration(
    desktop: bool = True, sendto: bool = True, context_menu: bool = True
) -> dict[str, Any]:
    if desktop:
        get_desktop_shortcut_path().unlink(missing_ok=True)
    if sendto:
        get_sendto_shortcut_path().unlink(missing_ok=True)
    if context_menu:
        for reg_path in _context_registry_paths():
            _delete_reg_tree(winreg.HKEY_CURRENT_USER, reg_path)
    return get_integration_status()


def cli_install() -> None:
    """CLI entrypoint for `printstack-shortcuts`."""
    status = install_integration(desktop=True, sendto=True, context_menu=True)
    print(json.dumps(status, indent=2))
