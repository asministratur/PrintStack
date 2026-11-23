"""Win32 printer discovery, capability inspection, DEVMODE handling, and spooler management."""

from __future__ import annotations

import base64
import contextlib
import json
from typing import Any, Iterator, Optional

import pythoncom
import pywintypes
import win32con
import win32gui
import win32print


@contextlib.contextmanager
def _com_apartment() -> Iterator[None]:
    """Ensure COM is initialized on the calling worker thread for Win32 print drivers."""
    initialized = False
    try:
        pythoncom.CoInitialize()
        initialized = True
    except pythoncom.com_error:
        # Thread already initialized with a compatible/different COM mode
        initialized = False
    try:
        yield
    finally:
        if initialized:
            pythoncom.CoUninitialize()

from printstack.models import (
    PaperBinOption,
    PaperSizeOption,
    PrintSettings,
    PrinterCapabilities,
    PrinterSummary,
    ResolutionOption,
    SpoolerJob,
)

VIRTUAL_PORTS = {"nul:", "portprompt:", "file:", "xpsport:", "shr:", "foxit"}
VIRTUAL_DRIVERS = ("microsoft print to pdf", "adobe pdf", "onenote", "xps document writer", "nitro pdf")

STATUS_MAP: list[tuple[int, str, str]] = [
    (win32print.PRINTER_STATUS_PAPER_JAM, "Paper Jam", "error"),
    (win32print.PRINTER_STATUS_PAPER_OUT, "Out of Paper", "error"),
    (win32print.PRINTER_STATUS_NO_TONER, "No Toner", "error"),
    (win32print.PRINTER_STATUS_DOOR_OPEN, "Door Open", "error"),
    (win32print.PRINTER_STATUS_OUT_OF_MEMORY, "Out of Memory", "error"),
    (win32print.PRINTER_STATUS_ERROR, "Error", "error"),
    (win32print.PRINTER_STATUS_OFFLINE, "Offline", "error"),
    (win32print.PRINTER_STATUS_NOT_AVAILABLE, "Not Available", "error"),
    (win32print.PRINTER_STATUS_USER_INTERVENTION, "Needs Attention", "warn"),
    (win32print.PRINTER_STATUS_PAPER_PROBLEM, "Paper Problem", "warn"),
    (win32print.PRINTER_STATUS_OUTPUT_BIN_FULL, "Output Bin Full", "warn"),
    (win32print.PRINTER_STATUS_TONER_LOW, "Toner Low", "warn"),
    (win32print.PRINTER_STATUS_MANUAL_FEED, "Manual Feed", "warn"),
    (win32print.PRINTER_STATUS_PAUSED, "Paused", "warn"),
    (win32print.PRINTER_STATUS_PRINTING, "Printing", "ok"),
    (win32print.PRINTER_STATUS_PROCESSING, "Processing", "ok"),
    (win32print.PRINTER_STATUS_SPOOLING if hasattr(win32print, "PRINTER_STATUS_SPOOLING") else 0, "Spooling", "ok"),
    (win32print.PRINTER_STATUS_WARMING_UP, "Warming Up", "ok"),
    (win32print.PRINTER_STATUS_BUSY, "Busy", "ok"),
    (win32print.PRINTER_STATUS_INITIALIZING, "Initializing", "ok"),
    (win32print.PRINTER_STATUS_POWER_SAVE, "Sleep / Ready", "ok"),
    (win32print.PRINTER_STATUS_WAITING, "Ready", "ok"),
]

JOB_STATUS_FLAGS: list[tuple[int, str]] = [
    (win32print.JOB_STATUS_ERROR, "Error"),
    (win32print.JOB_STATUS_OFFLINE, "Offline"),
    (win32print.JOB_STATUS_PAPEROUT, "Out of Paper"),
    (win32print.JOB_STATUS_BLOCKED_DEVQ, "Blocked"),
    (win32print.JOB_STATUS_USER_INTERVENTION, "Needs Attention"),
    (win32print.JOB_STATUS_PAUSED, "Paused"),
    (win32print.JOB_STATUS_PRINTING, "Printing"),
    (win32print.JOB_STATUS_SPOOLING, "Spooling"),
    (win32print.JOB_STATUS_DELETING, "Cancelling"),
    (win32print.JOB_STATUS_PRINTED, "Printed"),
    (win32print.JOB_STATUS_COMPLETE, "Complete"),
]

_DEVMODE_BASE_SIZE = 220  # sizeof(DEVMODEW) on Windows


def _decode_printer_status(status_code: int, attributes: int) -> tuple[str, str]:
    work_offline = bool(attributes & getattr(win32print, "PRINTER_ATTRIBUTE_WORK_OFFLINE", 1024))
    if work_offline:
        return "Offline", "error"
    if status_code == 0:
        return "Ready", "ok"
    for flag, label, severity in STATUS_MAP:
        if flag and (status_code & flag):
            return label, severity
    return f"Status ({status_code})", "warn"


def _is_virtual_printer(driver_name: str, port_name: str) -> bool:
    dl = (driver_name or "").lower()
    pl = (port_name or "").lower()
    if any(v in dl for v in VIRTUAL_DRIVERS):
        return True
    if pl in VIRTUAL_PORTS or pl.endswith(".pdf") or pl.endswith("*.pdf"):
        return True
    return False


def list_printers() -> list[PrinterSummary]:
    """Enumerate local and connected Windows printers with live status."""
    try:
        default_printer = win32print.GetDefaultPrinter()
    except Exception:
        default_printer = ""

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    raw_printers = win32print.EnumPrinters(flags, None, 2)
    summaries: list[PrinterSummary] = []

    for info in raw_printers:
        name = info.get("pPrinterName") or ""
        if not name:
            continue
        status_code = int(info.get("Status") or 0)
        attributes = int(info.get("Attributes") or 0)
        status_label, severity = _decode_printer_status(status_code, attributes)
        driver_name = info.get("pDriverName") or ""
        port_name = info.get("pPortName") or ""
        summaries.append(
            PrinterSummary(
                name=name,
                driver_name=driver_name,
                port_name=port_name,
                is_default=(name == default_printer),
                status_code=status_code,
                status_label=status_label,
                status_severity=severity,
                jobs_count=int(info.get("cJobs") or 0),
                is_virtual=_is_virtual_printer(driver_name, port_name),
            )
        )

    # Sort physical printers first, default printer near top
    summaries.sort(key=lambda p: (p.is_virtual, not p.is_default, p.name.lower()))
    return summaries


def _safe_device_cap(printer_name: str, port_name: str, cap: int) -> Any:
    try:
        val = win32print.DeviceCapabilities(printer_name, port_name, cap)
        if val == -1:
            return None
        return val
    except Exception:
        return None


def _get_default_devmode(handle: Any, printer_name: str):
    info = win32print.GetPrinter(handle, 2)
    dm = info.get("pDevMode")
    if dm is not None:
        return dm, info.get("pPortName") or ""

    buf_size = win32print.DocumentProperties(0, handle, printer_name, None, None, 0)
    extra = max(0, int(buf_size) - _DEVMODE_BASE_SIZE)
    dm = pywintypes.DEVMODEType(extra)
    win32print.DocumentProperties(0, handle, printer_name, dm, None, win32con.DM_OUT_BUFFER)
    return dm, info.get("pPortName") or ""


def get_printer_capabilities(printer_name: str) -> PrinterCapabilities:
    """Query dynamic driver capabilities (papers, trays, duplex, color, copies, resolutions)."""
    with _com_apartment():
        return _get_printer_capabilities_impl(printer_name)


def _get_printer_capabilities_impl(printer_name: str) -> PrinterCapabilities:
    handle = win32print.OpenPrinter(printer_name)
    try:
        dm, port = _get_default_devmode(handle, printer_name)

        paper_ids = _safe_device_cap(printer_name, port, win32con.DC_PAPERS) or []
        paper_names = _safe_device_cap(printer_name, port, win32con.DC_PAPERNAMES) or []
        paper_sizes_raw = _safe_device_cap(printer_name, port, win32con.DC_PAPERSIZE) or []

        paper_sizes: list[PaperSizeOption] = []
        seen_paper_ids: set[int] = set()
        count = min(len(paper_ids), len(paper_names), len(paper_sizes_raw))
        for i in range(count):
            pid = int(paper_ids[i])
            if pid in seen_paper_ids:
                continue
            seen_paper_ids.add(pid)
            pname = str(paper_names[i]).strip() or f"Paper #{pid}"
            raw_sz = paper_sizes_raw[i]
            w_mm = round(float(raw_sz["x"]) / 10.0, 1) if isinstance(raw_sz, dict) else 210.0
            h_mm = round(float(raw_sz["y"]) / 10.0, 1) if isinstance(raw_sz, dict) else 297.0
            if w_mm <= 0 or h_mm <= 0:
                w_mm, h_mm = 210.0, 297.0
            paper_sizes.append(PaperSizeOption(id=pid, name=pname, width_mm=w_mm, height_mm=h_mm))

        bin_ids = _safe_device_cap(printer_name, port, win32con.DC_BINS) or []
        bin_names = _safe_device_cap(printer_name, port, win32con.DC_BINNAMES) or []
        paper_bins: list[PaperBinOption] = []
        seen_bin_ids: set[int] = set()
        for i in range(min(len(bin_ids), len(bin_names))):
            bid = int(bin_ids[i])
            if bid in seen_bin_ids:
                continue
            seen_bin_ids.add(bid)
            bname = str(bin_names[i]).strip() or f"Tray #{bid}"
            paper_bins.append(PaperBinOption(id=bid, name=bname))

        duplex_cap = _safe_device_cap(printer_name, port, win32con.DC_DUPLEX)
        color_cap = _safe_device_cap(printer_name, port, win32con.DC_COLORDEVICE)
        copies_cap = _safe_device_cap(printer_name, port, win32con.DC_COPIES)
        collate_cap = _safe_device_cap(printer_name, port, win32con.DC_COLLATE)
        res_raw = _safe_device_cap(printer_name, port, win32con.DC_ENUMRESOLUTIONS) or []

        resolutions: list[ResolutionOption] = []
        for r in res_raw:
            if isinstance(r, dict) and r.get("xdpi", 0) > 0:
                resolutions.append(ResolutionOption(xdpi=int(r["xdpi"]), ydpi=int(r.get("ydpi") or r["xdpi"])))

        duplex_map = {
            win32con.DMDUP_SIMPLEX: "simplex",
            win32con.DMDUP_VERTICAL: "long_edge",
            win32con.DMDUP_HORIZONTAL: "short_edge",
        }
        defaults = {
            "paper_size_id": int(dm.PaperSize) if dm else 9,
            "paper_source_id": int(dm.DefaultSource) if dm else None,
            "duplex": duplex_map.get(int(dm.Duplex), "simplex") if dm else "simplex",
            "color_mode": "color" if (dm and int(dm.Color) == win32con.DMCOLOR_COLOR and bool(color_cap)) else "grayscale",
            "copies": max(1, int(dm.Copies)) if dm else 1,
            "collate": bool(dm.Collate) if dm else True,
            "orientation": "landscape" if (dm and int(dm.Orientation) == win32con.DMORIENT_LANDSCAPE) else "portrait",
        }

        return PrinterCapabilities(
            printer_name=printer_name,
            supports_duplex=bool(duplex_cap),
            supports_color=bool(color_cap) if color_cap is not None else True,
            max_copies=max(1, int(copies_cap)) if copies_cap else 1,
            supports_collate=bool(collate_cap),
            paper_sizes=paper_sizes,
            paper_bins=paper_bins,
            resolutions=resolutions,
            defaults=defaults,
        )
    finally:
        win32print.ClosePrinter(handle)


def serialize_devmode(dm: Any) -> str:
    """Serialize a PyDEVMODEW (including private DriverData) into a portable base64 JSON string."""
    driver_data = bytes(dm.DriverData or b"")
    payload = {
        "device_name": str(dm.DeviceName or ""),
        "spec_version": int(dm.SpecVersion),
        "driver_version": int(dm.DriverVersion),
        "fields": int(dm.Fields),
        "paper_size": int(dm.PaperSize),
        "paper_length": int(dm.PaperLength),
        "paper_width": int(dm.PaperWidth),
        "orientation": int(dm.Orientation),
        "duplex": int(dm.Duplex),
        "color": int(dm.Color),
        "copies": int(dm.Copies),
        "default_source": int(dm.DefaultSource),
        "collate": int(dm.Collate),
        "print_quality": int(dm.PrintQuality),
        "y_resolution": int(dm.YResolution),
        "driver_data_b64": base64.b64encode(driver_data).decode("ascii"),
    }
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def deserialize_devmode(printer_name: str, b64: str):
    """Reconstruct and validate a PyDEVMODEW for a printer from a serialized payload."""
    payload = json.loads(base64.b64decode(b64.encode("ascii")).decode("utf-8"))
    driver_data = base64.b64decode(payload["driver_data_b64"].encode("ascii"))
    dm = pywintypes.DEVMODEType(len(driver_data))
    if driver_data:
        dm.DriverData = driver_data
    dm.DeviceName = payload.get("device_name") or printer_name[:31]
    dm.SpecVersion = int(payload["spec_version"])
    dm.DriverVersion = int(payload["driver_version"])
    dm.Fields = int(payload["fields"])
    dm.PaperSize = int(payload["paper_size"])
    dm.PaperLength = int(payload["paper_length"])
    dm.PaperWidth = int(payload["paper_width"])
    dm.Orientation = int(payload["orientation"])
    dm.Duplex = int(payload["duplex"])
    dm.Color = int(payload["color"])
    dm.Copies = int(payload["copies"])
    dm.DefaultSource = int(payload["default_source"])
    dm.Collate = int(payload["collate"])
    dm.PrintQuality = int(payload["print_quality"])
    dm.YResolution = int(payload["y_resolution"])

    handle = win32print.OpenPrinter(printer_name)
    try:
        win32print.DocumentProperties(
            0, handle, printer_name, dm, dm, win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER
        )
    finally:
        win32print.ClosePrinter(handle)
    return dm


def _copy_devmode(dm: Any):
    dd = bytes(dm.DriverData or b"")
    dm_copy = pywintypes.DEVMODEType(len(dd))
    if dd:
        dm_copy.DriverData = dd
    for attr in (
        "DeviceName",
        "SpecVersion",
        "DriverVersion",
        "Fields",
        "PaperSize",
        "PaperLength",
        "PaperWidth",
        "Orientation",
        "Duplex",
        "Color",
        "Copies",
        "DefaultSource",
        "Collate",
        "PrintQuality",
        "YResolution",
    ):
        setattr(dm_copy, attr, getattr(dm, attr))
    return dm_copy


def build_devmode_for_settings(
    printer_name: str,
    settings: PrintSettings,
    forced_orientation: Optional[int] = None,
    software_copies: bool = False,
):
    """Create a driver-validated DEVMODE applying PrintSettings on top of base/stored DriverData."""
    handle = win32print.OpenPrinter(printer_name)
    try:
        if settings.devmode_b64:
            try:
                dm = deserialize_devmode(printer_name, settings.devmode_b64)
            except Exception:
                base_dm, _ = _get_default_devmode(handle, printer_name)
                dm = _copy_devmode(base_dm)
        else:
            base_dm, _ = _get_default_devmode(handle, printer_name)
            dm = _copy_devmode(base_dm)

        fields = int(dm.Fields)

        if settings.paper_size_id is not None:
            dm.PaperSize = int(settings.paper_size_id)
            fields |= win32con.DM_PAPERSIZE

        if settings.paper_source_id is not None:
            dm.DefaultSource = int(settings.paper_source_id)
            fields |= win32con.DM_DEFAULTSOURCE

        if forced_orientation is not None:
            dm.Orientation = int(forced_orientation)
            fields |= win32con.DM_ORIENTATION
        elif settings.orientation == "landscape":
            dm.Orientation = win32con.DMORIENT_LANDSCAPE
            fields |= win32con.DM_ORIENTATION
        elif settings.orientation == "portrait":
            dm.Orientation = win32con.DMORIENT_PORTRAIT
            fields |= win32con.DM_ORIENTATION

        duplex_lookup = {
            "simplex": win32con.DMDUP_SIMPLEX,
            "long_edge": win32con.DMDUP_VERTICAL,
            "short_edge": win32con.DMDUP_HORIZONTAL,
        }
        dm.Duplex = duplex_lookup[settings.duplex]
        fields |= win32con.DM_DUPLEX

        dm.Color = (
            win32con.DMCOLOR_COLOR
            if settings.color_mode == "color"
            else win32con.DMCOLOR_MONOCHROME
        )
        fields |= win32con.DM_COLOR

        dm.Copies = 1 if software_copies else max(1, int(settings.copies))
        fields |= win32con.DM_COPIES

        dm.Collate = win32con.DMCOLLATE_TRUE if settings.collate else win32con.DMCOLLATE_FALSE
        fields |= win32con.DM_COLLATE

        dm.Fields = fields
        win32print.DocumentProperties(
            0, handle, printer_name, dm, dm, win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER
        )
        return dm
    finally:
        win32print.ClosePrinter(handle)


def sync_settings_from_devmode(
    printer_name: str, settings: PrintSettings, dm: Any
) -> PrintSettings:
    """Reflect standard fields from a modified DEVMODE back onto PrintSettings."""
    caps = get_printer_capabilities(printer_name)
    updated = settings.model_copy(deep=True)
    updated.printer_name = printer_name
    updated.devmode_b64 = serialize_devmode(dm)
    updated.has_custom_driver_settings = True

    paper_id = int(dm.PaperSize)
    updated.paper_size_id = paper_id
    for p in caps.paper_sizes:
        if p.id == paper_id:
            updated.paper_size_name = p.name
            updated.paper_width_mm = p.width_mm
            updated.paper_height_mm = p.height_mm
            break

    bin_id = int(dm.DefaultSource)
    updated.paper_source_id = bin_id
    for b in caps.paper_bins:
        if b.id == bin_id:
            updated.paper_source_name = b.name
            break

    duplex_rev = {
        win32con.DMDUP_SIMPLEX: "simplex",
        win32con.DMDUP_VERTICAL: "long_edge",
        win32con.DMDUP_HORIZONTAL: "short_edge",
    }
    if int(dm.Duplex) in duplex_rev:
        updated.duplex = duplex_rev[int(dm.Duplex)]  # type: ignore[assignment]

    updated.color_mode = (
        "color" if int(dm.Color) == win32con.DMCOLOR_COLOR and caps.supports_color else "grayscale"
    )
    if int(dm.Copies) >= 1:
        updated.copies = min(int(dm.Copies), max(1, caps.max_copies))
    updated.collate = bool(dm.Collate)
    if updated.orientation != "auto":
        updated.orientation = (
            "landscape" if int(dm.Orientation) == win32con.DMORIENT_LANDSCAPE else "portrait"
        )
    return updated


def find_app_window_hwnd(title_substring: str = "Printstack") -> int:
    """Locate the running desktop app window HWND to parent modal dialogs or focus it."""
    found: list[int] = []

    def _cb(hwnd: int, _extra: Any) -> bool:
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd) or ""
            if title_substring.lower() in title.lower():
                found.append(hwnd)
                return False
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return found[0] if found else 0


def focus_app_window(title_substring: str = "Printstack") -> bool:
    """Restore and bring the app window to the foreground."""
    hwnd = find_app_window_hwnd(title_substring)
    if not hwnd:
        return False
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def open_printer_properties_dialog(
    printer_name: str, current_settings: PrintSettings, owner_hwnd: int = 0
) -> Optional[PrintSettings]:
    """Open native Windows DocumentProperties dialog; returns updated PrintSettings or None if cancelled."""
    with _com_apartment():
        if not owner_hwnd:
            owner_hwnd = find_app_window_hwnd()

        dm_in = build_devmode_for_settings(printer_name, current_settings)
        dm_out = _copy_devmode(dm_in)

        handle = win32print.OpenPrinter(printer_name)
        try:
            result = win32print.DocumentProperties(
                owner_hwnd,
                handle,
                printer_name,
                dm_out,
                dm_in,
                win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER | win32con.DM_IN_PROMPT,
            )
        finally:
            win32print.ClosePrinter(handle)

        if result != win32con.IDOK:
            return None
        return sync_settings_from_devmode(printer_name, current_settings, dm_out)


def list_spooler_jobs(printer_name: str) -> list[SpoolerJob]:
    """List current Windows spooler jobs queued on the specified printer."""
    handle = win32print.OpenPrinter(printer_name)
    try:
        raw_jobs = win32print.EnumJobs(handle, 0, -1, 2)
    finally:
        win32print.ClosePrinter(handle)

    jobs: list[SpoolerJob] = []
    for j in raw_jobs:
        status_code = int(j.get("Status") or 0)
        labels = [label for bit, label in JOB_STATUS_FLAGS if status_code & bit]
        status_text = ", ".join(labels) if labels else (j.get("pStatus") or "Queued")
        submitted_dt = j.get("Submitted")
        submitted_str = str(submitted_dt) if submitted_dt else ""
        jobs.append(
            SpoolerJob(
                job_id=int(j.get("JobId") or 0),
                document=str(j.get("pDocument") or "Document"),
                user=str(j.get("pUserName") or ""),
                status=status_text,
                total_pages=int(j.get("TotalPages") or 0),
                pages_printed=int(j.get("PagesPrinted") or 0),
                submitted=submitted_str,
            )
        )
    return jobs


def cancel_spooler_job(printer_name: str, job_id: int) -> None:
    """Cancel a Windows spooler print job by ID."""
    handle = win32print.OpenPrinter(printer_name)
    try:
        win32print.SetJob(handle, job_id, 0, None, win32print.JOB_CONTROL_CANCEL)
    finally:
        win32print.ClosePrinter(handle)
