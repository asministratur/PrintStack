"""Document ingestion, MS Word/Excel COM & LibreOffice conversion, image-to-PDF, and preview rendering."""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import winreg
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import fitz
import pythoncom
import win32com.client
from PIL import Image, ImageOps, ImageSequence

from printstack.config import (
    EXCEL_EXTS,
    IMAGE_EXTS,
    PDF_EXTS,
    SUPPORTED_EXTS,
    TEMP_DIR,
    UPLOAD_DIR,
    WORD_EXTS,
    ensure_dirs,
)
from printstack.models import ExcelPrintOptions, PageDimension, QueueItem, QueueItemUpdate


def natural_sort_key(value: str) -> list[int | str]:
    parts = re.split(r"(\d+)", value.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def _is_com_progid_registered(progid: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID"):
            return True
    except OSError:
        return False


def _is_msword_registered() -> bool:
    return _is_com_progid_registered("Word.Application")


def _is_excel_registered() -> bool:
    return _is_com_progid_registered("Excel.Application")


def find_libreoffice_exe() -> Optional[Path]:
    which = shutil.which("soffice") or shutil.which("soffice.exe")
    if which:
        return Path(which)
    candidates = [
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def detect_word_engine() -> dict[str, Any]:
    """Detect available engine for converting .docx/.doc/.rtf files to PDF."""
    if _is_msword_registered():
        return {"engine": "msword", "available": True, "label": "Microsoft Word"}
    soffice = find_libreoffice_exe()
    if soffice is not None:
        return {"engine": "libreoffice", "available": True, "label": "LibreOffice", "path": str(soffice)}
    return {"engine": "none", "available": False, "label": "None detected"}


def detect_excel_engine() -> dict[str, Any]:
    """Detect available engine for converting Excel/CSV spreadsheets to PDF."""
    if _is_excel_registered():
        return {"engine": "excel", "available": True, "label": "Microsoft Excel"}
    soffice = find_libreoffice_exe()
    if soffice is not None:
        return {"engine": "libreoffice", "available": True, "label": "LibreOffice Calc", "path": str(soffice)}
    return {"engine": "none", "available": False, "label": "None detected"}


def _read_xlsx_sheet_names(src_path: Path) -> list[str]:
    """Inspect visible worksheet names from an OpenXML (.xlsx/.xlsm) archive without Excel."""
    if src_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        return [src_path.stem]
    try:
        with zipfile.ZipFile(src_path, "r") as zf:
            xml_bytes = zf.read("xl/workbook.xml")
        root = ET.fromstring(xml_bytes)
        names: list[str] = []
        for elem in root.iter():
            if elem.tag.endswith("}sheet") or elem.tag == "sheet":
                state = elem.attrib.get("state", "visible")
                name = elem.attrib.get("name")
                if name and state == "visible":
                    names.append(name)
        return names
    except Exception:
        return []


def convert_office_with_libreoffice(src_path: Path, dst_pdf: Path, timeout: float = 90.0) -> Path:
    """Fallback Office-to-PDF conversion using headless LibreOffice."""
    soffice = find_libreoffice_exe()
    if soffice is None:
        raise RuntimeError("Microsoft Office or LibreOffice is required to convert Office documents.")

    dst_pdf.parent.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="ps_lo_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        profile_uri = (tmp_path / "profile").resolve().as_uri()
        cmd = [
            str(soffice),
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(tmp_path),
            str(src_path.resolve()),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"LibreOffice conversion failed: {detail or f'exit {proc.returncode}'}")

        produced = tmp_path / f"{src_path.stem}.pdf"
        if not produced.exists():
            pdfs = list(tmp_path.glob("*.pdf"))
            if not pdfs:
                raise RuntimeError("LibreOffice did not produce a PDF file")
            produced = pdfs[0]
        shutil.move(str(produced), str(dst_pdf))
    return dst_pdf


def convert_word_with_libreoffice(src_path: Path, dst_pdf: Path, timeout: float = 90.0) -> Path:
    return convert_office_with_libreoffice(src_path, dst_pdf, timeout=timeout)


def scan_paths(paths: list[str]) -> list[Path]:
    """Recursively collect supported files from files/folders in natural sort order."""
    collected: list[Path] = []
    seen: set[str] = set()

    def _add_file(fp: Path) -> None:
        if not fp.is_file():
            return
        name = fp.name
        if name.startswith("~$") or name.startswith("."):
            return
        if fp.suffix.lower() not in SUPPORTED_EXTS:
            return
        resolved = str(fp.resolve())
        key = resolved.lower()
        if key not in seen:
            seen.add(key)
            collected.append(fp.resolve())

    for raw in paths:
        if not raw:
            continue
        p = Path(raw)
        if not p.exists():
            continue
        if p.is_file():
            _add_file(p)
        elif p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs.sort(key=natural_sort_key)
                files.sort(key=natural_sort_key)
                for fname in files:
                    _add_file(Path(root) / fname)

    return collected


class _WordTask:
    def __init__(self, src: Path, dst: Path) -> None:
        self.src = src
        self.dst = dst
        self.done = threading.Event()
        self.error: Optional[Exception] = None


class WordConverterWorker:
    """Single-STA-thread headless Microsoft Word COM worker with LibreOffice fallback."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Optional[_WordTask]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._word_app = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run_loop, name="WordCOMConverter", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            t = self._thread
        if t and t.is_alive():
            self._queue.put(None)
            t.join(timeout=5.0)

    def convert(self, src_path: Path, dst_pdf: Path, timeout: float = 90.0) -> Path:
        if not _is_msword_registered():
            if find_libreoffice_exe() is not None:
                return convert_office_with_libreoffice(src_path, dst_pdf, timeout=timeout)
            raise RuntimeError(
                "Microsoft Word or LibreOffice is required to convert Word documents (.docx/.doc/.rtf)."
            )

        self.start()
        task = _WordTask(src_path, dst_pdf)
        self._queue.put(task)
        if not task.done.wait(timeout=timeout):
            raise TimeoutError(f"Word conversion timed out for {src_path.name}")
        if task.error is not None:
            if find_libreoffice_exe() is not None:
                return convert_office_with_libreoffice(src_path, dst_pdf, timeout=timeout)
            raise task.error
        return dst_pdf

    def _ensure_word(self):
        if self._word_app is not None:
            try:
                _ = self._word_app.Version
                return self._word_app
            except Exception:
                self._quit_word()

        app = win32com.client.DispatchEx("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0  # wdAlertsNone
        try:
            app.Options.ConfirmConversions = False
        except Exception:
            pass
        try:
            app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
        except Exception:
            pass
        self._word_app = app
        return app

    def _quit_word(self) -> None:
        if self._word_app is not None:
            try:
                self._word_app.Quit(SaveChanges=0)
            except Exception:
                pass
            self._word_app = None

    def _convert_once(self, src_path: Path, dst_pdf: Path) -> None:
        word = self._ensure_word()
        doc = None
        try:
            doc = word.Documents.Open(
                FileName=str(src_path.resolve()),
                ConfirmConversions=False,
                ReadOnly=True,
                AddToRecentFiles=False,
                PasswordDocument="",
                Visible=False,
                OpenAndRepair=False,
                NoEncodingDialog=True,
            )
            dst_pdf.parent.mkdir(parents=True, exist_ok=True)
            doc.ExportAsFixedFormat(
                OutputFileName=str(dst_pdf.resolve()),
                ExportFormat=17,  # wdExportFormatPDF
                OpenAfterExport=False,
                OptimizeFor=0,  # wdExportOptimizeForPrint
            )
        finally:
            if doc is not None:
                try:
                    doc.Close(SaveChanges=0)
                except Exception:
                    pass

    def _run_loop(self) -> None:
        pythoncom.CoInitialize()
        try:
            while True:
                task = self._queue.get()
                if task is None:
                    break
                try:
                    try:
                        self._convert_once(task.src, task.dst)
                    except Exception:
                        self._quit_word()
                        self._convert_once(task.src, task.dst)
                except Exception as exc:
                    task.error = exc
                finally:
                    task.done.set()
        finally:
            self._quit_word()
            pythoncom.CoUninitialize()


_EXCEL_PAPER_SIZES = {
    "letter": 1,  # xlPaperLetter
    "legal": 5,   # xlPaperLegal
    "a3": 8,      # xlPaperA3
    "a4": 9,      # xlPaperA4
}

_EXCEL_MARGINS_IN = {
    "narrow": (0.25, 0.25, 0.5, 0.5, 0.25, 0.25),
    "normal": (0.7, 0.7, 0.75, 0.75, 0.3, 0.3),
    "wide": (1.0, 1.0, 1.0, 1.0, 0.5, 0.5),
}


class _ExcelTask:
    def __init__(self, src: Path, dst: Path, options: ExcelPrintOptions) -> None:
        self.src = src
        self.dst = dst
        self.options = options
        self.sheets: list[str] = []
        self.done = threading.Event()
        self.error: Optional[Exception] = None


class ExcelConverterWorker:
    """Single-STA-thread headless Microsoft Excel COM worker with PageSetup control & LibreOffice fallback."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Optional[_ExcelTask]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._excel_app = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run_loop, name="ExcelCOMConverter", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            t = self._thread
        if t and t.is_alive():
            self._queue.put(None)
            t.join(timeout=5.0)

    def convert(
        self,
        src_path: Path,
        dst_pdf: Path,
        options: ExcelPrintOptions,
        timeout: float = 90.0,
    ) -> tuple[Path, list[str]]:
        if not _is_excel_registered():
            if find_libreoffice_exe() is not None:
                convert_office_with_libreoffice(src_path, dst_pdf, timeout=timeout)
                return dst_pdf, _read_xlsx_sheet_names(src_path)
            raise RuntimeError(
                "Microsoft Excel or LibreOffice Calc is required to convert Excel/CSV files."
            )

        self.start()
        task = _ExcelTask(src_path, dst_pdf, options)
        self._queue.put(task)
        if not task.done.wait(timeout=timeout):
            raise TimeoutError(f"Excel conversion timed out for {src_path.name}")
        if task.error is not None:
            if find_libreoffice_exe() is not None:
                convert_office_with_libreoffice(src_path, dst_pdf, timeout=timeout)
                return dst_pdf, _read_xlsx_sheet_names(src_path)
            raise task.error
        return dst_pdf, task.sheets

    def _ensure_excel(self):
        if self._excel_app is not None:
            try:
                _ = self._excel_app.Version
                return self._excel_app
            except Exception:
                self._quit_excel()

        app = win32com.client.DispatchEx("Excel.Application")
        app.Visible = False
        app.DisplayAlerts = False
        for attr, val in (
            ("ScreenUpdating", False),
            ("EnableEvents", False),
            ("AskToUpdateLinks", False),
            ("AutomationSecurity", 3),  # msoAutomationSecurityForceDisable
        ):
            try:
                setattr(app, attr, val)
            except Exception:
                pass
        self._excel_app = app
        return app

    def _quit_excel(self) -> None:
        if self._excel_app is not None:
            try:
                self._excel_app.Quit()
            except Exception:
                pass
            self._excel_app = None

    def _apply_page_setup(self, excel: Any, ws: Any, options: ExcelPrintOptions) -> None:
        ps = ws.PageSetup
        if options.orientation == "portrait":
            try:
                ps.Orientation = 1  # xlPortrait
            except Exception:
                pass
        elif options.orientation == "landscape":
            try:
                ps.Orientation = 2  # xlLandscape
            except Exception:
                pass

        if options.paper_size in _EXCEL_PAPER_SIZES:
            try:
                ps.PaperSize = _EXCEL_PAPER_SIZES[options.paper_size]
            except Exception:
                pass

        if options.margins in _EXCEL_MARGINS_IN:
            left_i, right_i, top_i, bottom_i, hdr_i, ftr_i = _EXCEL_MARGINS_IN[options.margins]
            try:
                ps.LeftMargin = excel.InchesToPoints(left_i)
                ps.RightMargin = excel.InchesToPoints(right_i)
                ps.TopMargin = excel.InchesToPoints(top_i)
                ps.BottomMargin = excel.InchesToPoints(bottom_i)
                ps.HeaderMargin = excel.InchesToPoints(hdr_i)
                ps.FooterMargin = excel.InchesToPoints(ftr_i)
            except Exception:
                pass

        try:
            if options.fit_mode == "none":
                ps.FitToPagesWide = False
                ps.FitToPagesTall = False
                ps.Zoom = 100
            elif options.fit_mode == "fit_columns":
                ps.Zoom = False
                ps.FitToPagesWide = 1
                ps.FitToPagesTall = False
            elif options.fit_mode == "fit_rows":
                ps.Zoom = False
                ps.FitToPagesWide = False
                ps.FitToPagesTall = 1
            elif options.fit_mode == "fit_sheet":
                ps.Zoom = False
                ps.FitToPagesWide = 1
                ps.FitToPagesTall = 1
        except Exception:
            pass

        try:
            ps.PrintGridlines = bool(options.gridlines)
        except Exception:
            pass
        try:
            ps.PrintHeadings = bool(options.headings)
        except Exception:
            pass
        try:
            ps.CenterHorizontally = bool(options.center_horizontally)
        except Exception:
            pass

    def _convert_once(self, src_path: Path, dst_pdf: Path, options: ExcelPrintOptions) -> list[str]:
        excel = self._ensure_excel()
        wb = None
        try:
            wb = excel.Workbooks.Open(
                Filename=str(src_path.resolve()),
                UpdateLinks=0,
                ReadOnly=True,
                IgnoreReadOnlyRecommended=True,
            )

            visible_sheets: list[tuple[str, Any]] = []
            for ws in wb.Worksheets:
                try:
                    # -1 is xlSheetVisible
                    if int(ws.Visible) == -1:
                        visible_sheets.append((str(ws.Name), ws))
                except Exception:
                    visible_sheets.append((str(ws.Name), ws))

            sheet_names = [name for name, _ in visible_sheets]
            if not visible_sheets:
                raise RuntimeError("Workbook has no visible worksheets")

            if options.sheet_scope == "selected" and options.selected_sheets:
                wanted = set(options.selected_sheets)
                target_sheets = [(n, ws) for n, ws in visible_sheets if n in wanted]
                if not target_sheets:
                    target_sheets = visible_sheets
            else:
                target_sheets = visible_sheets

            # Disable printer communication during PageSetup edits for fast COM turnaround
            try:
                excel.PrintCommunication = False
            except Exception:
                pass
            try:
                for _, ws in target_sheets:
                    self._apply_page_setup(excel, ws, options)
            finally:
                try:
                    excel.PrintCommunication = True
                except Exception:
                    pass

            dst_pdf.parent.mkdir(parents=True, exist_ok=True)
            out_str = str(dst_pdf.resolve())

            if len(target_sheets) == len(visible_sheets):
                wb.ExportAsFixedFormat(
                    Type=0,  # xlTypePDF
                    Filename=out_str,
                    Quality=0,  # xlQualityStandard
                    IncludeDocProperties=True,
                    IgnorePrintAreas=False,
                    OpenAfterPublish=False,
                )
            elif len(target_sheets) == 1:
                target_sheets[0][1].ExportAsFixedFormat(
                    Type=0,
                    Filename=out_str,
                    Quality=0,
                    IncludeDocProperties=True,
                    IgnorePrintAreas=False,
                    OpenAfterPublish=False,
                )
            else:
                target_names = [n for n, _ in target_sheets]
                wb.Worksheets(target_names).Select()
                wb.ActiveSheet.ExportAsFixedFormat(
                    Type=0,
                    Filename=out_str,
                    Quality=0,
                    IncludeDocProperties=True,
                    IgnorePrintAreas=False,
                    OpenAfterPublish=False,
                )

            return sheet_names
        finally:
            if wb is not None:
                try:
                    wb.Close(SaveChanges=False)
                except Exception:
                    pass

    def _run_loop(self) -> None:
        pythoncom.CoInitialize()
        try:
            while True:
                task = self._queue.get()
                if task is None:
                    break
                try:
                    try:
                        task.sheets = self._convert_once(task.src, task.dst, task.options)
                    except Exception:
                        self._quit_excel()
                        task.sheets = self._convert_once(task.src, task.dst, task.options)
                except Exception as exc:
                    task.error = exc
                finally:
                    task.done.set()
        finally:
            self._quit_excel()
            pythoncom.CoUninitialize()


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    transposed = ImageOps.exif_transpose(img)
    if transposed.mode in ("RGBA", "LA") or (
        transposed.mode == "P" and "transparency" in transposed.info
    ):
        rgba = transposed.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    if transposed.mode != "RGB":
        return transposed.convert("RGB")
    return transposed


def convert_image_to_pdf(image_path: Path, out_pdf: Path) -> Path:
    """Convert single or multi-frame image into a properly oriented PDF."""
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as im:
        dpi_info = im.info.get("dpi")
        res = 150.0
        if isinstance(dpi_info, (tuple, list)) and len(dpi_info) >= 1:
            try:
                val = float(dpi_info[0])
                if 36.0 <= val <= 600.0:
                    res = val
            except Exception:
                pass
        frames = [_flatten_to_rgb(frame.copy()) for frame in ImageSequence.Iterator(im)]

    if not frames:
        raise ValueError(f"No readable frames in image {image_path.name}")
    first, rest = frames[0], frames[1:]
    first.save(
        str(out_pdf),
        "PDF",
        resolution=res,
        save_all=True,
        append_images=rest,
    )
    return out_pdf


def inspect_pdf(pdf_path: Path) -> tuple[bool, int, list[PageDimension]]:
    """Return (needs_password, page_count, page_dimensions)."""
    with fitz.open(str(pdf_path)) as doc:
        if doc.needs_pass:
            return True, 0, []
        dims = [
            PageDimension(width_pt=float(page.rect.width), height_pt=float(page.rect.height))
            for page in doc
        ]
        return False, len(dims), dims


def unlock_pdf_file(src_pdf: Path, password: str, out_pdf: Path) -> tuple[bool, int, list[PageDimension]]:
    """Attempt to authenticate a password-protected PDF and write decrypted copy."""
    with fitz.open(str(src_pdf)) as doc:
        if doc.needs_pass:
            ok = doc.authenticate(password)
            if not ok:
                return False, 0, []
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_pdf))
    _, count, dims = inspect_pdf(out_pdf)
    return True, count, dims


def _excel_options_hash(options: ExcelPrintOptions) -> str:
    return hashlib.sha1(options.model_dump_json().encode("utf-8")).hexdigest()[:12]


class DocumentManager:
    """Manages document queue, background conversions, unlocking, and cached page rendering."""

    def __init__(
        self,
        temp_dir: Optional[Path] = None,
        upload_dir: Optional[Path] = None,
        on_update: Optional[Callable[[], None]] = None,
    ) -> None:
        ensure_dirs()
        self.temp_dir = temp_dir or TEMP_DIR
        self.upload_dir = upload_dir or UPLOAD_DIR
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._items: list[QueueItem] = []
        self._excel_reconvert_seq: dict[str, int] = {}
        self._on_update = on_update
        self._word_worker = WordConverterWorker()
        self._excel_worker = ExcelConverterWorker()
        self._img_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="ImgConvert"
        )
        self._render_cache: OrderedDict[tuple[str, int, int, bool], bytes] = OrderedDict()
        self._cache_limit = 80

    def set_on_update(self, cb: Optional[Callable[[], None]]) -> None:
        self._on_update = cb

    def _notify(self) -> None:
        if self._on_update:
            try:
                self._on_update()
            except Exception:
                pass

    def shutdown(self) -> None:
        self._word_worker.stop()
        self._excel_worker.stop()
        self._img_pool.shutdown(wait=False)

    def list_items(self) -> list[QueueItem]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._items]

    def get_item(self, item_id: str) -> Optional[QueueItem]:
        with self._lock:
            for item in self._items:
                if item.id == item_id:
                    return item.model_copy(deep=True)
        return None

    def _classify_ext(self, ext: str) -> str:
        ext = ext.lower()
        if ext in PDF_EXTS:
            return "pdf"
        if ext in WORD_EXTS:
            return "word"
        if ext in EXCEL_EXTS:
            return "excel"
        if ext in IMAGE_EXTS:
            return "image"
        raise ValueError(f"Unsupported file type: {ext}")

    def add_paths(self, paths: list[str]) -> list[QueueItem]:
        resolved_files = scan_paths(paths)
        return self._enqueue_file_entries(
            [(fp, fp.name) for fp in resolved_files]
        )

    def save_uploaded_stream(self, filename: str, data: bytes) -> Path:
        safe_name = Path(filename).name
        target_dir = self.upload_dir / uuid.uuid4().hex[:8]
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / safe_name
        dst.write_bytes(data)
        return dst

    def add_local_files(self, file_tuples: list[tuple[Path, str]]) -> list[QueueItem]:
        valid = [
            (p, name)
            for p, name in file_tuples
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        ]
        return self._enqueue_file_entries(valid)

    def _enqueue_file_entries(self, entries: list[tuple[Path, str]]) -> list[QueueItem]:
        created: list[QueueItem] = []
        now = datetime.now(timezone.utc).isoformat()

        for fp, display_name in entries:
            ext = fp.suffix.lower()
            ftype = self._classify_ext(ext)
            item_id = f"doc-{uuid.uuid4().hex[:10]}"
            size_bytes = fp.stat().st_size if fp.exists() else 0

            excel_opts = ExcelPrintOptions() if ftype == "excel" else None
            item = QueueItem(
                id=item_id,
                name=display_name,
                original_path=str(fp.resolve()),
                file_ext=ext,
                file_type=ftype,  # type: ignore[arg-type]
                size_bytes=size_bytes,
                status="converting",
                excel_options=excel_opts,
                added_at=now,
            )

            if ftype == "pdf":
                try:
                    needs_pass, count, dims = inspect_pdf(fp)
                    if needs_pass:
                        item.status = "password_required"
                    else:
                        item.status = "ready"
                        item.converted_pdf_path = str(fp.resolve())
                        item.page_count = count
                        item.page_dimensions = dims
                except Exception as exc:
                    item.status = "error"
                    item.error_message = str(exc)
            else:
                with self._lock:
                    self._items.append(item)
                    if ftype == "excel":
                        self._excel_reconvert_seq[item_id] = 1
                created.append(item.model_copy(deep=True))
                if ftype == "word":
                    threading.Thread(
                        target=self._process_word_item,
                        args=(item_id, fp),
                        daemon=True,
                    ).start()
                elif ftype == "excel":
                    threading.Thread(
                        target=self._process_excel_item,
                        args=(item_id, fp, excel_opts or ExcelPrintOptions(), 1),
                        daemon=True,
                    ).start()
                else:
                    self._img_pool.submit(self._process_image_item, item_id, fp)
                continue

            with self._lock:
                self._items.append(item)
            created.append(item.model_copy(deep=True))

        if created:
            self._notify()
        return created

    def _process_word_item(self, item_id: str, src_path: Path) -> None:
        out_pdf = self.temp_dir / f"{item_id}.pdf"
        try:
            self._word_worker.convert(src_path, out_pdf)
            needs_pass, count, dims = inspect_pdf(out_pdf)
            with self._lock:
                for item in self._items:
                    if item.id == item_id:
                        if needs_pass:
                            item.status = "password_required"
                        else:
                            item.status = "ready"
                            item.converted_pdf_path = str(out_pdf)
                            item.page_count = count
                            item.page_dimensions = dims
                        break
        except Exception as exc:
            with self._lock:
                for item in self._items:
                    if item.id == item_id:
                        item.status = "error"
                        item.error_message = f"Word conversion failed: {exc}"
                        break
        self._notify()

    def _process_excel_item(
        self,
        item_id: str,
        src_path: Path,
        options: ExcelPrintOptions,
        seq: int,
    ) -> None:
        opts_hash = _excel_options_hash(options)
        out_pdf = self.temp_dir / f"{item_id}-{opts_hash}.pdf"
        try:
            sheets: list[str]
            if out_pdf.exists() and out_pdf.stat().st_size > 0:
                existing = self.get_item(item_id)
                sheets = list(existing.excel_sheets) if existing and existing.excel_sheets else _read_xlsx_sheet_names(src_path)
            else:
                _, sheets = self._excel_worker.convert(src_path, out_pdf, options)

            needs_pass, count, dims = inspect_pdf(out_pdf)
            with self._lock:
                if self._excel_reconvert_seq.get(item_id) != seq:
                    return
                for item in self._items:
                    if item.id == item_id:
                        item.is_updating_layout = False
                        item.excel_sheets = sheets or item.excel_sheets
                        if needs_pass:
                            item.status = "password_required"
                        else:
                            item.status = "ready"
                            item.error_message = None
                            item.converted_pdf_path = str(out_pdf)
                            item.page_count = count
                            item.page_dimensions = dims
                            item.skipped_pages = [p for p in item.skipped_pages if 1 <= p <= count]
                            item.layout_rev += 1
                        break
        except Exception as exc:
            with self._lock:
                if self._excel_reconvert_seq.get(item_id) != seq:
                    return
                for item in self._items:
                    if item.id == item_id:
                        item.is_updating_layout = False
                        if not item.converted_pdf_path:
                            item.status = "error"
                        item.error_message = f"Excel conversion failed: {exc}"
                        break
        self._notify()

    def _process_image_item(self, item_id: str, src_path: Path) -> None:
        out_pdf = self.temp_dir / f"{item_id}.pdf"
        try:
            convert_image_to_pdf(src_path, out_pdf)
            _, count, dims = inspect_pdf(out_pdf)
            with self._lock:
                for item in self._items:
                    if item.id == item_id:
                        item.status = "ready"
                        item.converted_pdf_path = str(out_pdf)
                        item.page_count = count
                        item.page_dimensions = dims
                        break
        except Exception as exc:
            with self._lock:
                for item in self._items:
                    if item.id == item_id:
                        item.status = "error"
                        item.error_message = f"Image conversion failed: {exc}"
                        break
        self._notify()

    def unlock_pdf(self, item_id: str, password: str) -> QueueItem:
        item = self.get_item(item_id)
        if item is None:
            raise KeyError("Queue item not found")
        src = Path(item.original_path)
        out_pdf = self.temp_dir / f"{item_id}-unlocked.pdf"
        ok, count, dims = unlock_pdf_file(src, password, out_pdf)
        if not ok:
            raise ValueError("Incorrect PDF password")

        with self._lock:
            for q in self._items:
                if q.id == item_id:
                    q.status = "ready"
                    q.error_message = None
                    q.converted_pdf_path = str(out_pdf)
                    q.page_count = count
                    q.page_dimensions = dims
                    updated = q.model_copy(deep=True)
                    break
            else:
                raise KeyError("Queue item not found")
        self._notify()
        return updated

    def update_item(self, item_id: str, patch: QueueItemUpdate) -> Optional[QueueItem]:
        reconvert_args: Optional[tuple[Path, ExcelPrintOptions, int]] = None
        with self._lock:
            for item in self._items:
                if item.id == item_id:
                    if patch.enabled is not None:
                        item.enabled = patch.enabled
                    if patch.skipped_pages is not None:
                        item.skipped_pages = sorted(
                            {p for p in patch.skipped_pages if 1 <= p <= max(item.page_count, 9999)}
                        )
                    if patch.use_override is not None:
                        item.use_override = patch.use_override
                    if patch.override_settings is not None:
                        item.override_settings = patch.override_settings
                    if patch.excel_options is not None and item.file_type == "excel":
                        if item.excel_options != patch.excel_options:
                            item.excel_options = patch.excel_options
                            opts_hash = _excel_options_hash(patch.excel_options)
                            cached_pdf = self.temp_dir / f"{item_id}-{opts_hash}.pdf"
                            next_seq = self._excel_reconvert_seq.get(item_id, 0) + 1
                            self._excel_reconvert_seq[item_id] = next_seq
                            if cached_pdf.exists() and cached_pdf.stat().st_size > 0:
                                _, count, dims = inspect_pdf(cached_pdf)
                                item.status = "ready"
                                item.error_message = None
                                item.is_updating_layout = False
                                item.converted_pdf_path = str(cached_pdf)
                                item.page_count = count
                                item.page_dimensions = dims
                                item.skipped_pages = [p for p in item.skipped_pages if 1 <= p <= count]
                                item.layout_rev += 1
                            else:
                                item.is_updating_layout = True
                                reconvert_args = (Path(item.original_path), patch.excel_options, next_seq)
                    res = item.model_copy(deep=True)
                    break
            else:
                return None

        self._notify()
        if reconvert_args is not None:
            src_path, opts, seq = reconvert_args
            threading.Thread(
                target=self._process_excel_item,
                args=(item_id, src_path, opts, seq),
                daemon=True,
            ).start()
        return res

    def set_all_enabled(self, enabled: bool) -> list[QueueItem]:
        with self._lock:
            for item in self._items:
                item.enabled = enabled
            snapshot = [item.model_copy(deep=True) for item in self._items]
        self._notify()
        return snapshot

    def sort_items(self, mode: str) -> list[QueueItem]:
        with self._lock:
            if mode == "name_asc":
                self._items.sort(key=lambda x: natural_sort_key(x.name))
            elif mode == "name_desc":
                self._items.sort(key=lambda x: natural_sort_key(x.name), reverse=True)
            elif mode == "type":
                self._items.sort(key=lambda x: (x.file_type, natural_sort_key(x.name)))
            else:
                raise ValueError(f"Unsupported sort mode: {mode}")
            snapshot = [item.model_copy(deep=True) for item in self._items]
        self._notify()
        return snapshot

    def reorder(self, item_ids: list[str]) -> list[QueueItem]:
        with self._lock:
            by_id = {item.id: item for item in self._items}
            new_order: list[QueueItem] = []
            used: set[str] = set()
            for iid in item_ids:
                if iid in by_id and iid not in used:
                    new_order.append(by_id[iid])
                    used.add(iid)
            for item in self._items:
                if item.id not in used:
                    new_order.append(item)
            self._items = new_order
            snapshot = [item.model_copy(deep=True) for item in self._items]
        self._notify()
        return snapshot

    def _cleanup_item_files(self, item: QueueItem) -> None:
        self._excel_reconvert_seq.pop(item.id, None)
        try:
            for temp_file in self.temp_dir.glob(f"{item.id}*.pdf"):
                key_str = str(temp_file)
                for k in [k for k in self._render_cache if k[0] == key_str]:
                    self._render_cache.pop(k, None)
                temp_file.unlink(missing_ok=True)
        except Exception:
            pass

        if item.converted_pdf_path:
            key_str = str(Path(item.converted_pdf_path))
            for k in [k for k in self._render_cache if k[0] == key_str]:
                self._render_cache.pop(k, None)

        orig = Path(item.original_path)
        try:
            if orig.resolve().is_relative_to(self.upload_dir.resolve()):
                parent = orig.resolve().parent
                if parent != self.upload_dir.resolve():
                    shutil.rmtree(parent, ignore_errors=True)
                else:
                    orig.unlink(missing_ok=True)
        except Exception:
            pass

    def remove_item(self, item_id: str) -> bool:
        removed: Optional[QueueItem] = None
        with self._lock:
            idx = next((i for i, x in enumerate(self._items) if x.id == item_id), None)
            if idx is None:
                return False
            removed = self._items.pop(idx)
            self._cleanup_item_files(removed)
        self._notify()
        return True

    def clear_items(self) -> None:
        with self._lock:
            old_items = list(self._items)
            self._items.clear()
            self._render_cache.clear()
            for item in old_items:
                self._cleanup_item_files(item)
        self._notify()

    def cleanup_temp_files(self) -> None:
        for folder in (self.temp_dir, self.upload_dir):
            if folder.exists():
                for child in folder.iterdir():
                    try:
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
                    except Exception:
                        pass

    def render_page_png(
        self, pdf_path: Path, page_index: int, dpi: int = 110, grayscale: bool = False
    ) -> bytes:
        key = (str(pdf_path), page_index, dpi, grayscale)
        with self._lock:
            cached = self._render_cache.get(key)
            if cached is not None:
                self._render_cache.move_to_end(key)
                return cached

        with fitz.open(str(pdf_path)) as doc:
            if page_index < 0 or page_index >= len(doc):
                raise IndexError(f"Page {page_index} out of bounds")
            page = doc[page_index]
            cs = fitz.csGRAY if grayscale else fitz.csRGB
            pix = page.get_pixmap(dpi=dpi, colorspace=cs, alpha=False)
            png_bytes = pix.tobytes("png")

        with self._lock:
            self._render_cache[key] = png_bytes
            while len(self._render_cache) > self._cache_limit:
                self._render_cache.popitem(last=False)
        return png_bytes
