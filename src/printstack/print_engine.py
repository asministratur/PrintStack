"""Page selection parser, N-up sheet compositor, preview sheet renderer, and Win32 GDI print engine."""

from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import fitz
import pythoncom
import win32con
import win32gui
import win32ui
from PIL import Image, ImageDraw, ImageWin

from printstack.document_service import DocumentManager
from printstack.models import (
    BatchFileStatus,
    BatchState,
    HistoryEntry,
    HistoryFileRecord,
    PageDimension,
    PrintSettings,
    QueueItem,
)
from printstack.printer_service import build_devmode_for_settings, get_printer_capabilities
from printstack.storage import AppStorage


@dataclass(frozen=True)
class PageRef:
    item_id: str
    item_name: str
    pdf_path: Path
    page_index: int  # 0-based
    width_pt: float
    height_pt: float


def parse_page_selection(
    total_pages: int,
    mode: str,
    custom_expr: str = "",
    skipped_pages: Optional[list[int] | set[int]] = None,
) -> list[int]:
    """Return ordered 0-based page indices matching mode/range minus skipped 1-based pages."""
    if total_pages <= 0:
        return []

    skipped = set(skipped_pages or [])

    if mode == "all":
        candidates = list(range(1, total_pages + 1))
    elif mode == "odd":
        candidates = list(range(1, total_pages + 1, 2))
    elif mode == "even":
        candidates = list(range(2, total_pages + 1, 2))
    elif mode == "custom":
        expr = (custom_expr or "").strip()
        if not expr:
            candidates = list(range(1, total_pages + 1))
        else:
            candidates = []
            seen: set[int] = set()
            tokens = [tok.strip() for tok in expr.split(",")]
            if any(tok == "" for tok in tokens):
                raise ValueError(f"Invalid page range syntax: '{custom_expr}'")
            for tok in tokens:
                if "-" in tok:
                    parts = tok.split("-")
                    if len(parts) != 2 or not parts[0].strip().isdigit() or not parts[1].strip().isdigit():
                        raise ValueError(f"Invalid range token: '{tok}'")
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                    if start <= 0 or end <= 0:
                        raise ValueError(f"Page numbers must be >= 1: '{tok}'")
                    step = 1 if start <= end else -1
                    for p in range(start, end + step, step):
                        if 1 <= p <= total_pages and p not in seen:
                            seen.add(p)
                            candidates.append(p)
                else:
                    if not tok.isdigit():
                        raise ValueError(f"Invalid page number: '{tok}'")
                    p = int(tok)
                    if p <= 0:
                        raise ValueError(f"Page numbers must be >= 1: '{tok}'")
                    if 1 <= p <= total_pages and p not in seen:
                        seen.add(p)
                        candidates.append(p)
    else:
        raise ValueError(f"Unsupported page range mode: {mode}")

    return [p - 1 for p in candidates if p not in skipped]


def resolve_effective_settings(item: QueueItem, global_settings: PrintSettings) -> PrintSettings:
    """Resolve effective settings for an item (per-file override or global)."""
    if item.use_override and item.override_settings is not None:
        return item.override_settings
    return global_settings


def resolve_job_orientation(
    settings: PrintSettings, page_dims: list[tuple[float, float]]
) -> str:
    """Resolve sheet orientation ('portrait' or 'landscape') for a job."""
    if settings.orientation in ("portrait", "landscape"):
        return settings.orientation
    if not page_dims:
        return "portrait"
    landscape_count = sum(1 for w, h in page_dims if w > h)
    portrait_count = len(page_dims) - landscape_count
    if settings.pages_per_sheet in (2, 6):
        # 2-up (2x1 landscape) and 6-up (3x2 landscape) match portrait pages cleanly without rotating text
        return "landscape" if portrait_count >= landscape_count else "portrait"
    if landscape_count == len(page_dims):
        return "landscape"
    return "portrait"


def get_nup_grid(pages_per_sheet: int, sheet_is_landscape: bool) -> tuple[int, int]:
    """Return (cols, rows) for N-up layout based on sheet aspect."""
    if pages_per_sheet == 1:
        return (1, 1)
    if pages_per_sheet == 2:
        return (2, 1) if sheet_is_landscape else (1, 2)
    if pages_per_sheet == 4:
        return (2, 2)
    if pages_per_sheet == 6:
        return (3, 2) if sheet_is_landscape else (2, 3)
    if pages_per_sheet == 9:
        return (3, 3)
    cols = math.ceil(math.sqrt(pages_per_sheet))
    rows = math.ceil(pages_per_sheet / cols)
    return (cols, rows)


def build_item_page_refs(item: QueueItem, settings: PrintSettings) -> list[PageRef]:
    if item.status != "ready" or not item.converted_pdf_path:
        return []
    pdf_path = Path(item.converted_pdf_path)
    indices = parse_page_selection(
        item.page_count,
        settings.page_range_mode,
        settings.page_range_custom,
        item.skipped_pages,
    )
    refs: list[PageRef] = []
    for idx in indices:
        if idx < len(item.page_dimensions):
            dim = item.page_dimensions[idx]
            w_pt, h_pt = dim.width_pt, dim.height_pt
        else:
            w_pt, h_pt = 595.0, 842.0
        refs.append(
            PageRef(
                item_id=item.id,
                item_name=item.name,
                pdf_path=pdf_path,
                page_index=idx,
                width_pt=w_pt,
                height_pt=h_pt,
            )
        )
    return refs


def chunk_sheets(refs: list[PageRef], pages_per_sheet: int) -> list[list[PageRef]]:
    if not refs:
        return []
    n = max(1, pages_per_sheet)
    return [refs[i : i + n] for i in range(0, len(refs), n)]


def render_composited_sheet(
    sheet_refs: list[PageRef],
    settings: PrintSettings,
    target_w_px: int,
    target_h_px: int,
    dpi_x: float,
    dpi_y: float,
    sheet_orientation: str,
    draw_page_border_for_preview: bool = False,
) -> Image.Image:
    """Composite 1..9 PDF pages onto a single physical sheet RGB image."""
    sheet = Image.new("RGB", (max(1, target_w_px), max(1, target_h_px)), (255, 255, 255))
    if not sheet_refs:
        return sheet

    sheet_is_landscape = target_w_px > target_h_px
    cols, rows = get_nup_grid(settings.pages_per_sheet, sheet_is_landscape)

    outer_pad = int(round(0.08 * dpi_x)) if settings.pages_per_sheet > 1 else 0
    cell_gap = int(round(0.10 * dpi_x)) if settings.pages_per_sheet > 1 else 0

    avail_w = max(1, target_w_px - 2 * outer_pad - (cols - 1) * cell_gap)
    avail_h = max(1, target_h_px - 2 * outer_pad - (rows - 1) * cell_gap)
    cell_w = max(1, avail_w // cols)
    cell_h = max(1, avail_h // rows)

    grayscale = settings.color_mode == "grayscale"
    draw = ImageDraw.Draw(sheet)

    # Group document opens by path within the sheet for fast N-up rendering
    open_docs: dict[str, fitz.Document] = {}
    try:
        for slot_idx, ref in enumerate(sheet_refs):
            if settings.nup_order == "column":
                col = slot_idx // rows
                row = slot_idx % rows
            else:
                row = slot_idx // cols
                col = slot_idx % cols

            if row >= rows or col >= cols:
                break

            cx = outer_pad + col * (cell_w + cell_gap)
            cy = outer_pad + row * (cell_h + cell_gap)

            path_str = str(ref.pdf_path)
            if path_str not in open_docs:
                open_docs[path_str] = fitz.open(path_str)
            doc = open_docs[path_str]
            page = doc[ref.page_index]
            rect = page.rect
            pw_pt = float(rect.width)
            ph_pt = float(rect.height)

            rotate_90 = (
                settings.orientation == "auto"
                and settings.pages_per_sheet == 1
                and ((pw_pt > ph_pt) != (cell_w > cell_h))
                and not (sheet_orientation == "landscape" and pw_pt > ph_pt)
            )
            eff_w_pt = ph_pt if rotate_90 else pw_pt
            eff_h_pt = pw_pt if rotate_90 else ph_pt

            nat_w = max(1.0, (eff_w_pt / 72.0) * dpi_x)
            nat_h = max(1.0, (eff_h_pt / 72.0) * dpi_y)

            if settings.scale_mode == "fit":
                scale = min(cell_w / nat_w, cell_h / nat_h)
            elif settings.scale_mode == "shrink":
                scale = min(1.0, cell_w / nat_w, cell_h / nat_h)
            elif settings.scale_mode == "actual":
                scale = 1.0
            else:  # custom
                scale = max(0.1, settings.scale_percent / 100.0)

            dest_w = max(1, int(round(nat_w * scale)))
            dest_h = max(1, int(round(nat_h * scale)))

            # Render at sharp target pixel scale (capped at 600 DPI effective for memory safety)
            render_zoom_x = min(600.0 / 72.0, (dpi_x / 72.0) * scale)
            render_zoom_y = min(600.0 / 72.0, (dpi_y / 72.0) * scale)
            mat = fitz.Matrix(max(0.1, render_zoom_x), max(0.1, render_zoom_y))
            if rotate_90:
                mat = mat.prerotate(90)

            cs = fitz.csGRAY if grayscale else fitz.csRGB
            pix = page.get_pixmap(matrix=mat, colorspace=cs, alpha=False)
            pil_mode = "L" if grayscale else "RGB"
            page_img = Image.frombytes(pil_mode, (pix.width, pix.height), pix.samples)
            if pil_mode != "RGB":
                page_img = page_img.convert("RGB")

            if page_img.size != (dest_w, dest_h):
                page_img = page_img.resize((dest_w, dest_h), Image.Resampling.LANCZOS)

            # Center in cell; crop if larger than cell (actual / custom > 100%)
            if dest_w > cell_w or dest_h > cell_h:
                crop_x = max(0, (dest_w - cell_w) // 2)
                crop_y = max(0, (dest_h - cell_h) // 2)
                crop_w = min(dest_w, cell_w)
                crop_h = min(dest_h, cell_h)
                page_img = page_img.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))
                paste_x = cx + max(0, (cell_w - crop_w) // 2)
                paste_y = cy + max(0, (cell_h - crop_h) // 2)
            else:
                paste_x = cx + (cell_w - dest_w) // 2
                paste_y = cy + (cell_h - dest_h) // 2

            sheet.paste(page_img, (paste_x, paste_y))

            if settings.nup_border and settings.pages_per_sheet > 1:
                border_w = max(1, int(round(dpi_x / 150.0)))
                draw.rectangle(
                    [cx, cy, cx + cell_w - 1, cy + cell_h - 1],
                    outline=(140, 148, 160),
                    width=border_w,
                )
    finally:
        for doc in open_docs.values():
            try:
                doc.close()
            except Exception:
                pass

    if draw_page_border_for_preview:
        draw.rectangle([0, 0, target_w_px - 1, target_h_px - 1], outline=(226, 232, 240), width=1)
    return sheet


def render_preview_sheet_png(
    sheet_refs: list[PageRef],
    settings: PrintSettings,
    sheet_orientation: str,
    preview_dpi: float = 110.0,
) -> bytes:
    """Render a sheet preview as PNG bytes matching paper dimensions & orientation."""
    import io

    w_mm = settings.paper_width_mm or 210.0
    h_mm = settings.paper_height_mm or 297.0
    if sheet_orientation == "landscape" and w_mm < h_mm:
        w_mm, h_mm = h_mm, w_mm
    elif sheet_orientation == "portrait" and w_mm > h_mm:
        w_mm, h_mm = h_mm, w_mm

    target_w = max(60, int(round((w_mm / 25.4) * preview_dpi)))
    target_h = max(60, int(round((h_mm / 25.4) * preview_dpi)))

    img = render_composited_sheet(
        sheet_refs=sheet_refs,
        settings=settings,
        target_w_px=target_w,
        target_h_px=target_h,
        dpi_x=preview_dpi,
        dpi_y=preview_dpi,
        sheet_orientation=sheet_orientation,
        draw_page_border_for_preview=False,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class PrintJobManager:
    """Executes separate or merged batch print jobs via Win32 GDI with cancellation and history."""

    def __init__(
        self,
        doc_manager: DocumentManager,
        storage: AppStorage,
        on_state_change: Optional[Callable[[BatchState], None]] = None,
    ) -> None:
        self.doc_manager = doc_manager
        self.storage = storage
        self.on_state_change = on_state_change
        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._state = BatchState()
        self._last_failed_item_ids: list[str] = []
        self._last_merge_mode: bool = False
        self._last_global_settings: Optional[PrintSettings] = None
        self._worker_thread: Optional[threading.Thread] = None

    def set_on_state_change(self, cb: Optional[Callable[[BatchState], None]]) -> None:
        self.on_state_change = cb

    def get_state(self) -> BatchState:
        with self._lock:
            return self._state.model_copy(deep=True)

    def _emit_state(self) -> None:
        if self.on_state_change:
            try:
                self.on_state_change(self.get_state())
            except Exception:
                pass

    def is_printing(self) -> bool:
        with self._lock:
            return self._state.status in ("printing", "cancelling")

    def start_batch(
        self,
        global_settings: PrintSettings,
        merge_mode: bool = False,
        item_ids: Optional[list[str]] = None,
        output_pdf_path: Optional[str] = None,
    ) -> BatchState:
        with self._lock:
            if self._state.status in ("printing", "cancelling"):
                raise RuntimeError("A print batch is already in progress")

            all_items = self.doc_manager.list_items()
            if item_ids is not None:
                id_set = set(item_ids)
                target_items = [x for x in all_items if x.id in id_set and x.status == "ready"]
            else:
                target_items = [x for x in all_items if x.enabled and x.status == "ready"]

            if not target_items:
                raise ValueError("No ready documents selected to print")

            if not global_settings.printer_name:
                raise ValueError("No printer selected")

            self._cancel_event.clear()
            self._last_failed_item_ids = []
            self._last_merge_mode = merge_mode
            self._last_global_settings = global_settings.model_copy(deep=True)

            file_statuses: list[BatchFileStatus] = []
            total_sheets = 0
            for item in target_items:
                eff = global_settings if merge_mode else resolve_effective_settings(item, global_settings)
                refs = build_item_page_refs(item, eff)
                sheets_cnt = len(chunk_sheets(refs, eff.pages_per_sheet))
                total_sheets += sheets_cnt
                file_statuses.append(
                    BatchFileStatus(
                        item_id=item.id,
                        name=item.name,
                        status="queued",
                        current_sheet=0,
                        total_sheets=sheets_cnt,
                    )
                )

            self._state = BatchState(
                batch_id=f"batch-{uuid.uuid4().hex[:10]}",
                status="printing",
                merge_mode=merge_mode,
                printer_name=global_settings.printer_name,
                current_file_index=0,
                total_files=len(target_items),
                current_sheet=0,
                total_sheets=total_sheets,
                files=file_statuses,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            self._emit_state()

            self._worker_thread = threading.Thread(
                target=self._run_batch,
                args=(target_items, global_settings.model_copy(deep=True), merge_mode, output_pdf_path),
                name="PrintBatchWorker",
                daemon=True,
            )
            self._worker_thread.start()
            return self.get_state()

    def cancel_batch(self) -> BatchState:
        with self._lock:
            if self._state.status == "printing":
                self._cancel_event.set()
                self._state.status = "cancelling"
                self._emit_state()
            return self.get_state()

    def retry_failed(self, global_settings: Optional[PrintSettings] = None) -> BatchState:
        with self._lock:
            if not self._last_failed_item_ids:
                raise ValueError("No failed documents from previous batch to retry")
            settings = global_settings or self._last_global_settings
            if settings is None:
                raise ValueError("No print settings available for retry")
            failed_ids = list(self._last_failed_item_ids)
            merge_mode = self._last_merge_mode
        return self.start_batch(
            global_settings=settings,
            merge_mode=merge_mode,
            item_ids=failed_ids,
        )

    def _print_sheets_to_gdi(
        self,
        job_title: str,
        sheets: list[list[PageRef]],
        settings: PrintSettings,
        sheet_orientation: str,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        output_pdf_path: Optional[str] = None,
    ) -> None:
        if not sheets:
            return

        printer_name = settings.printer_name
        caps = get_printer_capabilities(printer_name)
        software_copies = settings.copies > 1 and caps.max_copies < settings.copies

        forced_orient = (
            win32con.DMORIENT_LANDSCAPE
            if sheet_orientation == "landscape"
            else win32con.DMORIENT_PORTRAIT
        )
        dm = build_devmode_for_settings(
            printer_name,
            settings,
            forced_orientation=forced_orient,
            software_copies=software_copies,
        )

        hdc = win32gui.CreateDC("WINSPOOL", printer_name, dm)
        if not hdc:
            raise RuntimeError(f"Failed to create printer device context for '{printer_name}'")

        dc = win32ui.CreateDCFromHandle(hdc)
        doc_started = False
        try:
            horzres = dc.GetDeviceCaps(win32con.HORZRES)
            vertres = dc.GetDeviceCaps(win32con.VERTRES)
            dev_dpi_x = float(dc.GetDeviceCaps(win32con.LOGPIXELSX) or 300)
            dev_dpi_y = float(dc.GetDeviceCaps(win32con.LOGPIXELSY) or 300)

            # Render at capped quality DPI for crisp text without huge GDI memory spikes
            cap_dpi = float(settings.quality_dpi)
            render_dpi_x = min(dev_dpi_x, cap_dpi)
            render_dpi_y = min(dev_dpi_y, cap_dpi)
            scale_x = render_dpi_x / dev_dpi_x
            scale_y = render_dpi_y / dev_dpi_y
            render_w = max(1, int(round(horzres * scale_x)))
            render_h = max(1, int(round(vertres * scale_y)))

            if output_pdf_path:
                dc.StartDoc(job_title, output_pdf_path)
            else:
                dc.StartDoc(job_title)
            doc_started = True

            copy_loops = settings.copies if software_copies else 1
            sheet_sequence: list[tuple[int, list[PageRef]]] = []
            if software_copies and not settings.collate:
                for idx, s in enumerate(sheets):
                    for _ in range(copy_loops):
                        sheet_sequence.append((idx, s))
            else:
                for _ in range(copy_loops):
                    for idx, s in enumerate(sheets):
                        sheet_sequence.append((idx, s))

            total_emit = len(sheet_sequence)
            for emit_idx, (sheet_idx, sheet_refs) in enumerate(sheet_sequence):
                if self._cancel_event.is_set():
                    dc.AbortDoc()
                    doc_started = False
                    raise InterruptedError("Print job cancelled by user")

                img = render_composited_sheet(
                    sheet_refs=sheet_refs,
                    settings=settings,
                    target_w_px=render_w,
                    target_h_px=render_h,
                    dpi_x=render_dpi_x,
                    dpi_y=render_dpi_y,
                    sheet_orientation=sheet_orientation,
                    draw_page_border_for_preview=False,
                )
                dc.StartPage()
                dib = ImageWin.Dib(img)
                dib.draw(dc.GetHandleOutput(), (0, 0, horzres, vertres))
                dc.EndPage()

                if progress_cb:
                    progress_cb(min(sheet_idx + 1, len(sheets)), emit_idx + 1 if total_emit else 0)

            dc.EndDoc()
            doc_started = False
        except Exception:
            if doc_started:
                try:
                    dc.AbortDoc()
                except Exception:
                    pass
            raise
        finally:
            try:
                dc.DeleteDC()
            except Exception:
                pass

    def _run_batch(
        self,
        items: list[QueueItem],
        global_settings: PrintSettings,
        merge_mode: bool,
        output_pdf_path: Optional[str],
    ) -> None:
        com_init = False
        try:
            pythoncom.CoInitialize()
            com_init = True
        except pythoncom.com_error:
            pass

        try:
            self._run_batch_inner(items, global_settings, merge_mode, output_pdf_path)
        finally:
            if com_init:
                pythoncom.CoUninitialize()

    def _run_batch_inner(
        self,
        items: list[QueueItem],
        global_settings: PrintSettings,
        merge_mode: bool,
        output_pdf_path: Optional[str],
    ) -> None:
        history_files: list[HistoryFileRecord] = []
        failed_ids: list[str] = []
        printed_sheets_total = 0

        if merge_mode:
            self._run_merged_batch(
                items, global_settings, history_files, failed_ids, output_pdf_path
            )
        else:
            self._run_separate_batch(
                items, global_settings, history_files, failed_ids, output_pdf_path
            )

        with self._lock:
            printed_sheets_total = sum(f.sheet_count for f in history_files if f.status == "printed")
            self._last_failed_item_ids = failed_ids

            if self._cancel_event.is_set():
                final_status = "cancelled" if not any(f.status == "printed" for f in history_files) else "partial"
            elif failed_ids and any(f.status == "printed" for f in history_files):
                final_status = "partial"
            elif failed_ids:
                final_status = "failed"
            else:
                final_status = "completed"

            self._state.status = final_status  # type: ignore[assignment]
            self._state.finished_at = datetime.now(timezone.utc).isoformat()
            self._emit_state()

            hist_status = (
                "cancelled"
                if final_status == "cancelled"
                else ("completed" if final_status == "completed" else ("failed" if final_status == "failed" else "partial"))
            )
            entry = HistoryEntry(
                id=self._state.batch_id or f"batch-{uuid.uuid4().hex[:10]}",
                printed_at=self._state.started_at or datetime.now(timezone.utc).isoformat(),
                printer_name=global_settings.printer_name,
                merge_mode=merge_mode,
                status=hist_status,  # type: ignore[arg-type]
                total_files=len(items),
                total_sheets=printed_sheets_total,
                files=history_files,
                global_settings=global_settings,
            )
            self.storage.add_history(entry)

    def _run_separate_batch(
        self,
        items: list[QueueItem],
        global_settings: PrintSettings,
        history_files: list[HistoryFileRecord],
        failed_ids: list[str],
        output_pdf_path: Optional[str],
    ) -> None:
        cumulative_sheets = 0
        for idx, item in enumerate(items):
            if self._cancel_event.is_set():
                with self._lock:
                    self._state.files[idx].status = "cancelled"
                    self._emit_state()
                history_files.append(
                    HistoryFileRecord(
                        name=item.name,
                        original_path=item.original_path,
                        file_type=item.file_type,
                        page_count=item.page_count,
                        sheet_count=0,
                        status="cancelled",
                        skipped_pages=list(item.skipped_pages),
                    )
                )
                continue

            eff = resolve_effective_settings(item, global_settings)
            if not eff.printer_name:
                eff = eff.model_copy(update={"printer_name": global_settings.printer_name})

            with self._lock:
                self._state.current_file_index = idx + 1
                self._state.files[idx].status = "printing"
                self._emit_state()

            try:
                refs = build_item_page_refs(item, eff)
                sheets = chunk_sheets(refs, eff.pages_per_sheet)
                if not sheets:
                    raise ValueError("No pages matched page selection")

                sheet_orient = resolve_job_orientation(
                    eff, [(r.width_pt, r.height_pt) for r in refs]
                )

                def _on_prog(sheet_num: int, _emit: int, file_idx: int = idx, base: int = cumulative_sheets) -> None:
                    with self._lock:
                        self._state.files[file_idx].current_sheet = sheet_num
                        self._state.current_sheet = base + sheet_num
                        self._emit_state()

                self._print_sheets_to_gdi(
                    job_title=f"Printstack - {item.name}",
                    sheets=sheets,
                    settings=eff,
                    sheet_orientation=sheet_orient,
                    progress_cb=_on_prog,
                    output_pdf_path=output_pdf_path,
                )
                cumulative_sheets += len(sheets)
                with self._lock:
                    self._state.files[idx].status = "printed"
                    self._state.files[idx].current_sheet = len(sheets)
                    self._emit_state()
                history_files.append(
                    HistoryFileRecord(
                        name=item.name,
                        original_path=item.original_path,
                        file_type=item.file_type,
                        page_count=item.page_count,
                        sheet_count=len(sheets),
                        status="printed",
                        settings_snapshot=eff,
                        skipped_pages=list(item.skipped_pages),
                    )
                )
            except InterruptedError:
                with self._lock:
                    self._state.files[idx].status = "cancelled"
                    self._emit_state()
                history_files.append(
                    HistoryFileRecord(
                        name=item.name,
                        original_path=item.original_path,
                        file_type=item.file_type,
                        page_count=item.page_count,
                        sheet_count=0,
                        status="cancelled",
                        settings_snapshot=eff,
                        skipped_pages=list(item.skipped_pages),
                    )
                )
            except Exception as exc:
                failed_ids.append(item.id)
                with self._lock:
                    self._state.files[idx].status = "failed"
                    self._state.files[idx].error = str(exc)
                    self._emit_state()
                history_files.append(
                    HistoryFileRecord(
                        name=item.name,
                        original_path=item.original_path,
                        file_type=item.file_type,
                        page_count=item.page_count,
                        sheet_count=0,
                        status="failed",
                        error=str(exc),
                        settings_snapshot=eff,
                        skipped_pages=list(item.skipped_pages),
                    )
                )

    def _run_merged_batch(
        self,
        items: list[QueueItem],
        global_settings: PrintSettings,
        history_files: list[HistoryFileRecord],
        failed_ids: list[str],
        output_pdf_path: Optional[str],
    ) -> None:
        all_refs: list[PageRef] = []
        per_item_counts: dict[str, int] = {}

        for idx, item in enumerate(items):
            try:
                refs = build_item_page_refs(item, global_settings)
                all_refs.extend(refs)
                per_item_counts[item.id] = len(refs)
                with self._lock:
                    self._state.files[idx].status = "printing"
            except Exception as exc:
                per_item_counts[item.id] = 0
                failed_ids.append(item.id)
                with self._lock:
                    self._state.files[idx].status = "failed"
                    self._state.files[idx].error = str(exc)

        sheets = chunk_sheets(all_refs, global_settings.pages_per_sheet)
        with self._lock:
            self._state.total_sheets = len(sheets)
            self._state.current_file_index = len(items)
            self._emit_state()

        if not sheets:
            for idx, item in enumerate(items):
                if item.id not in failed_ids:
                    failed_ids.append(item.id)
                    with self._lock:
                        self._state.files[idx].status = "failed"
                        self._state.files[idx].error = "No pages matched selection"
                history_files.append(
                    HistoryFileRecord(
                        name=item.name,
                        original_path=item.original_path,
                        file_type=item.file_type,
                        page_count=item.page_count,
                        sheet_count=0,
                        status="failed",
                        error="No pages matched selection",
                        settings_snapshot=global_settings,
                        skipped_pages=list(item.skipped_pages),
                    )
                )
            return

        sheet_orient = resolve_job_orientation(
            global_settings, [(r.width_pt, r.height_pt) for r in all_refs]
        )

        try:
            def _on_prog(sheet_num: int, _emit: int) -> None:
                with self._lock:
                    self._state.current_sheet = sheet_num
                    self._emit_state()

            self._print_sheets_to_gdi(
                job_title=f"Printstack - Merged ({len(items)} files)",
                sheets=sheets,
                settings=global_settings,
                sheet_orientation=sheet_orient,
                progress_cb=_on_prog,
                output_pdf_path=output_pdf_path,
            )
            for idx, item in enumerate(items):
                if item.id in failed_ids:
                    status_val = "failed"
                else:
                    status_val = "printed"
                    with self._lock:
                        self._state.files[idx].status = "printed"
                        self._state.files[idx].current_sheet = self._state.files[idx].total_sheets
                est_sheets = math.ceil(per_item_counts.get(item.id, 0) / max(1, global_settings.pages_per_sheet))
                history_files.append(
                    HistoryFileRecord(
                        name=item.name,
                        original_path=item.original_path,
                        file_type=item.file_type,
                        page_count=item.page_count,
                        sheet_count=est_sheets if status_val == "printed" else 0,
                        status=status_val,  # type: ignore[arg-type]
                        settings_snapshot=global_settings,
                        skipped_pages=list(item.skipped_pages),
                    )
                )
        except InterruptedError:
            for idx, item in enumerate(items):
                with self._lock:
                    self._state.files[idx].status = "cancelled"
                history_files.append(
                    HistoryFileRecord(
                        name=item.name,
                        original_path=item.original_path,
                        file_type=item.file_type,
                        page_count=item.page_count,
                        sheet_count=0,
                        status="cancelled",
                        settings_snapshot=global_settings,
                        skipped_pages=list(item.skipped_pages),
                    )
                )
        except Exception as exc:
            for idx, item in enumerate(items):
                if item.id not in failed_ids:
                    failed_ids.append(item.id)
                with self._lock:
                    self._state.files[idx].status = "failed"
                    self._state.files[idx].error = str(exc)
                history_files.append(
                    HistoryFileRecord(
                        name=item.name,
                        original_path=item.original_path,
                        file_type=item.file_type,
                        page_count=item.page_count,
                        sheet_count=0,
                        status="failed",
                        error=str(exc),
                        settings_snapshot=global_settings,
                        skipped_pages=list(item.skipped_pages),
                    )
                )
