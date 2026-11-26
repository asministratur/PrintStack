"""FastAPI backend and WebSocket hub for Printstack."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import unquote, urlparse

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from printstack.config import (
    APP_VERSION,
    DATA_DIR,
    ICON_ICO_PATH,
    LOGO_SVG_PATH,
    STATIC_DIR,
    SUPPORTED_EXTS,
    ensure_dirs,
)
from printstack.desktop_integration import (
    get_integration_status,
    install_integration,
    uninstall_integration,
)
from printstack.dialogs import pick_files_dialog, pick_folder_dialog
from printstack.document_service import DocumentManager, detect_excel_engine, detect_word_engine
from printstack.models import (
    BatchState,
    ImportPathsRequest,
    PresetCreate,
    PrintSettings,
    PrintStartRequest,
    PrinterPropertiesRequest,
    QueueItem,
    QueueItemUpdate,
    QueueReorderRequest,
    UnlockPdfRequest,
)
from printstack.print_engine import (
    PageRef,
    PrintJobManager,
    build_item_page_refs,
    chunk_sheets,
    render_preview_sheet_png,
    resolve_effective_settings,
    resolve_job_orientation,
)
from printstack.printer_service import (
    cancel_spooler_job,
    focus_app_window,
    get_printer_capabilities,
    list_printers,
    list_spooler_jobs,
    open_printer_properties_dialog,
)
from printstack.storage import AppStorage


class PrefsUpdate(BaseModel):
    theme: Optional[Literal["system", "light", "dark"]] = None
    merge_mode: Optional[bool] = None


class IntegrationToggleRequest(BaseModel):
    desktop: bool = True
    sendto: bool = True
    context_menu: bool = True


class QueueBulkEnableRequest(BaseModel):
    enabled: bool


class QueueSortRequest(BaseModel):
    mode: Literal["name_asc", "name_desc", "type"] = "name_asc"


_ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
_GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/asministratur/PrintStack/releases/latest"
_UPDATE_CACHE_TTL_S = 6 * 3600


def _parse_semver(version_str: str) -> tuple[int, ...]:
    nums = [int(p) for p in re.findall(r"\d+", version_str or "")]
    return tuple(nums) if nums else (0,)


def fetch_latest_release_info(current_version: str = APP_VERSION) -> dict[str, Any]:
    """Query GitHub Releases for a newer version; returns update_available=False on any error."""
    req = urllib.request.Request(
        _GITHUB_LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Printstack/{current_version}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "").strip()
        latest = tag.lstrip("vV")
        release_url = str(data.get("html_url") or "https://github.com/asministratur/PrintStack/releases/latest")
        installer_url = ""
        for asset in data.get("assets") or []:
            name = str(asset.get("name") or "")
            if name.lower().endswith(".exe"):
                installer_url = str(asset.get("browser_download_url") or "")
                break
        is_newer = bool(latest) and _parse_semver(latest) > _parse_semver(current_version)
        return {
            "current_version": current_version,
            "latest_version": latest or current_version,
            "update_available": is_newer,
            "release_url": release_url,
            "installer_url": installer_url or release_url,
        }
    except Exception:
        return {
            "current_version": current_version,
            "latest_version": current_version,
            "update_available": False,
            "release_url": "https://github.com/asministratur/PrintStack/releases/latest",
            "installer_url": "https://github.com/asministratur/PrintStack/releases/latest",
        }


def _is_allowed_origin(origin: str) -> bool:
    try:
        parsed = urlparse(origin)
        return (parsed.hostname or "").lower() in _ALLOWED_LOCAL_HOSTS
    except Exception:
        return False


class AppContext:
    def __init__(self) -> None:
        ensure_dirs()
        self.storage = AppStorage()
        self.doc_manager = DocumentManager()
        self.print_manager = PrintJobManager(self.doc_manager, self.storage)
        self.ws_clients: set[WebSocket] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_lock = threading.Lock()
        self._monitor_task: Optional[asyncio.Task[Any]] = None
        self._last_printers_sig: str = ""
        self._update_cache: Optional[tuple[float, dict[str, Any]]] = None

        self.doc_manager.set_on_update(self._on_queue_updated)
        self.print_manager.set_on_state_change(self._on_batch_updated)

    def broadcast(self, event: str, data: Any) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        msg = json.dumps({"event": event, "data": data}, default=str)
        asyncio.run_coroutine_threadsafe(self._send_all(msg), loop)

    async def _send_all(self, msg: str) -> None:
        with self._ws_lock:
            clients = list(self.ws_clients)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        if dead:
            with self._ws_lock:
                for ws in dead:
                    self.ws_clients.discard(ws)

    def _on_queue_updated(self) -> None:
        items = [i.model_dump() for i in self.doc_manager.list_items()]
        self.broadcast("queue_updated", items)

    def _on_batch_updated(self, state: BatchState) -> None:
        self.broadcast("batch_updated", state.model_dump())

    def get_default_settings(self) -> PrintSettings:
        saved = self.storage.get_global_settings()
        printers = list_printers()
        printer_names = {p.name for p in printers}

        if saved is not None and (not saved.printer_name or saved.printer_name in printer_names):
            if not saved.printer_name and printers:
                saved.printer_name = _pick_preferred_printer(printers)
            return saved

        chosen = _pick_preferred_printer(printers)
        settings = PrintSettings(printer_name=chosen)
        if chosen:
            try:
                caps = get_printer_capabilities(chosen)
                _apply_printer_defaults(settings, caps)
            except Exception:
                pass
        self.storage.save_global_settings(settings)
        return settings

    async def printer_monitor_loop(self) -> None:
        while True:
            try:
                printers = await asyncio.to_thread(list_printers)
                sig = json.dumps([p.model_dump() for p in printers], sort_keys=True)
                if sig != self._last_printers_sig:
                    self._last_printers_sig = sig
                    self.broadcast("printers_updated", [p.model_dump() for p in printers])
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(5.0)


def _pick_preferred_printer(printers: list[Any]) -> str:
    if not printers:
        return ""
    for p in printers:
        if p.is_default and not p.is_virtual:
            return p.name
    for p in printers:
        if not p.is_virtual:
            return p.name
    for p in printers:
        if p.is_default:
            return p.name
    return printers[0].name


def _apply_printer_defaults(settings: PrintSettings, caps: Any) -> None:
    d = caps.defaults or {}
    if d.get("paper_size_id") is not None:
        settings.paper_size_id = d["paper_size_id"]
        for p in caps.paper_sizes:
            if p.id == settings.paper_size_id:
                settings.paper_size_name = p.name
                settings.paper_width_mm = p.width_mm
                settings.paper_height_mm = p.height_mm
                break
    elif caps.paper_sizes:
        p0 = caps.paper_sizes[0]
        settings.paper_size_id = p0.id
        settings.paper_size_name = p0.name
        settings.paper_width_mm = p0.width_mm
        settings.paper_height_mm = p0.height_mm

    if d.get("paper_source_id") is not None:
        settings.paper_source_id = d["paper_source_id"]
        for b in caps.paper_bins:
            if b.id == settings.paper_source_id:
                settings.paper_source_name = b.name
                break
    elif caps.paper_bins:
        settings.paper_source_id = caps.paper_bins[0].id
        settings.paper_source_name = caps.paper_bins[0].name

    if caps.supports_duplex and d.get("duplex") in ("simplex", "long_edge", "short_edge"):
        settings.duplex = d["duplex"]
    else:
        settings.duplex = "simplex"

    settings.color_mode = (
        d["color_mode"] if (caps.supports_color and d.get("color_mode") in ("color", "grayscale")) else "grayscale"
    )
    settings.collate = bool(d.get("collate", True))


def create_app() -> FastAPI:
    ctx = AppContext()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ctx.doc_manager.cleanup_temp_files()
        ctx.loop = asyncio.get_running_loop()
        ctx._monitor_task = asyncio.create_task(ctx.printer_monitor_loop())
        yield
        if ctx._monitor_task:
            ctx._monitor_task.cancel()
        ctx.doc_manager.shutdown()
        ctx.doc_manager.cleanup_temp_files()
        ctx.storage.close()

    app = FastAPI(title="Printstack", version=APP_VERSION, lifespan=lifespan)
    app.state.ctx = ctx

    @app.middleware("http")
    async def localhost_origin_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and not _is_allowed_origin(origin):
            return JSONResponse(status_code=403, content={"detail": "Cross-origin requests are not allowed"})
        host_header = (request.headers.get("host") or "").split(":")[0].lower()
        if host_header and host_header not in _ALLOWED_LOCAL_HOSTS:
            return JSONResponse(status_code=403, content={"detail": "Non-local Host header rejected"})
        return await call_next(request)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            return HTMLResponse("<h1>Printstack</h1><p>UI not found.</p>")
        return HTMLResponse(index_file.read_text(encoding="utf-8"))

    @app.get("/favicon.ico")
    async def favicon():
        if ICON_ICO_PATH.exists():
            return FileResponse(str(ICON_ICO_PATH), media_type="image/x-icon")
        raise HTTPException(status_code=404)

    @app.get("/logo.svg")
    async def logo_svg():
        if LOGO_SVG_PATH.exists():
            return FileResponse(str(LOGO_SVG_PATH), media_type="image/svg+xml")
        raise HTTPException(status_code=404)

    @app.get("/api/health")
    async def health():
        with ctx._ws_lock:
            ws_count = len(ctx.ws_clients)
        return {
            "status": "ok",
            "version": APP_VERSION,
            "data_dir": str(DATA_DIR),
            "ws_clients": ws_count,
            "word_engine": detect_word_engine(),
            "excel_engine": detect_excel_engine(),
        }

    @app.get("/api/updates/check")
    async def api_check_updates():
        now = time.monotonic()
        if ctx._update_cache and (now - ctx._update_cache[0]) < _UPDATE_CACHE_TTL_S:
            return ctx._update_cache[1]
        info = await asyncio.to_thread(fetch_latest_release_info, APP_VERSION)
        ctx._update_cache = (now, info)
        return info

    @app.get("/api/bootstrap")
    async def bootstrap():
        printers = await asyncio.to_thread(list_printers)
        global_settings = ctx.get_default_settings()
        caps = None
        if global_settings.printer_name:
            try:
                caps = await asyncio.to_thread(get_printer_capabilities, global_settings.printer_name)
            except Exception:
                caps = None
        return {
            "version": APP_VERSION,
            "word_engine": await asyncio.to_thread(detect_word_engine),
            "excel_engine": await asyncio.to_thread(detect_excel_engine),
            "printers": [p.model_dump() for p in printers],
            "capabilities": caps.model_dump() if caps else None,
            "global_settings": global_settings.model_dump(),
            "prefs": {
                "theme": ctx.storage.get_pref("theme", "system"),
                "merge_mode": bool(ctx.storage.get_pref("merge_mode", False)),
            },
            "queue": [q.model_dump() for q in ctx.doc_manager.list_items()],
            "presets": [p.model_dump() for p in ctx.storage.list_presets()],
            "batch_state": ctx.print_manager.get_state().model_dump(),
            "integration": await asyncio.to_thread(get_integration_status),
        }

    # --- Printers & Spooler ---

    @app.get("/api/printers")
    async def api_list_printers():
        return await asyncio.to_thread(list_printers)

    @app.get("/api/printers/{printer_name:path}/capabilities")
    async def api_printer_capabilities(printer_name: str):
        name = unquote(printer_name)
        try:
            return await asyncio.to_thread(get_printer_capabilities, name)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Printer error: {exc}")

    @app.get("/api/printers/{printer_name:path}/jobs")
    async def api_printer_jobs(printer_name: str):
        name = unquote(printer_name)
        try:
            return await asyncio.to_thread(list_spooler_jobs, name)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/api/printers/{printer_name:path}/jobs/{job_id}")
    async def api_cancel_printer_job(printer_name: str, job_id: int):
        name = unquote(printer_name)
        try:
            await asyncio.to_thread(cancel_spooler_job, name, job_id)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/printers/{printer_name:path}/properties")
    async def api_open_printer_properties(printer_name: str, body: PrinterPropertiesRequest):
        name = unquote(printer_name)
        try:
            updated = await asyncio.to_thread(open_printer_properties_dialog, name, body.settings)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if updated is None:
            return {"cancelled": True, "settings": None}
        return {"cancelled": False, "settings": updated.model_dump()}

    # --- Queue Management ---

    @app.get("/api/queue")
    async def api_get_queue():
        return ctx.doc_manager.list_items()

    @app.post("/api/queue/upload")
    async def api_upload_files(files: list[UploadFile] = File(...)):
        saved: list[tuple[Path, str]] = []
        skipped: list[str] = []
        for uf in files:
            raw_name = uf.filename or "document"
            ext = Path(raw_name).suffix.lower()
            if ext not in SUPPORTED_EXTS:
                skipped.append(raw_name)
                continue
            data = await uf.read()
            saved_path = ctx.doc_manager.save_uploaded_stream(raw_name, data)
            saved.append((saved_path, Path(raw_name).name))
        items = ctx.doc_manager.add_local_files(saved)
        return {"added": [i.model_dump() for i in items], "skipped": skipped}

    @app.post("/api/queue/import-paths")
    async def api_import_paths(req: ImportPathsRequest):
        items = await asyncio.to_thread(ctx.doc_manager.add_paths, req.paths)
        return {"added": [i.model_dump() for i in items]}

    @app.post("/api/queue/reorder")
    async def api_reorder_queue(req: QueueReorderRequest):
        return ctx.doc_manager.reorder(req.item_ids)

    @app.post("/api/queue/bulk-enable")
    async def api_bulk_enable_queue(req: QueueBulkEnableRequest):
        return ctx.doc_manager.set_all_enabled(req.enabled)

    @app.post("/api/queue/sort")
    async def api_sort_queue(req: QueueSortRequest):
        return ctx.doc_manager.sort_items(req.mode)

    @app.patch("/api/queue/{item_id}")
    async def api_update_queue_item(item_id: str, patch: QueueItemUpdate):
        updated = ctx.doc_manager.update_item(item_id, patch)
        if updated is None:
            raise HTTPException(status_code=404, detail="Queue item not found")
        return updated

    @app.post("/api/queue/{item_id}/unlock")
    async def api_unlock_pdf(item_id: str, req: UnlockPdfRequest):
        try:
            return await asyncio.to_thread(ctx.doc_manager.unlock_pdf, item_id, req.password)
        except KeyError:
            raise HTTPException(status_code=404, detail="Queue item not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/queue/{item_id}/open-original")
    async def api_open_original(item_id: str):
        item = ctx.doc_manager.get_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Queue item not found")
        path = Path(item.original_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Original file no longer exists")
        os.startfile(str(path))
        return {"ok": True}

    @app.delete("/api/queue/{item_id}")
    async def api_delete_queue_item(item_id: str):
        if not ctx.doc_manager.remove_item(item_id):
            raise HTTPException(status_code=404, detail="Queue item not found")
        return {"ok": True}

    @app.delete("/api/queue")
    async def api_clear_queue():
        ctx.doc_manager.clear_items()
        return {"ok": True}

    # --- Native File / Folder Pickers ---

    @app.post("/api/dialog/pick-files")
    async def api_pick_files():
        selected = await asyncio.to_thread(pick_files_dialog)
        if not selected:
            return {"added": []}
        items = await asyncio.to_thread(ctx.doc_manager.add_paths, selected)
        return {"added": [i.model_dump() for i in items]}

    @app.post("/api/dialog/pick-folder")
    async def api_pick_folder():
        folder = await asyncio.to_thread(pick_folder_dialog)
        if not folder:
            return {"added": []}
        items = await asyncio.to_thread(ctx.doc_manager.add_paths, [folder])
        return {"added": [i.model_dump() for i in items]}

    # --- Preview Endpoints ---

    def _build_preview_plan_data(
        scope: Literal["file", "merged"],
        item_id: Optional[str],
        override_params: dict[str, Any],
    ) -> tuple[list[list[PageRef]], PrintSettings, str, Optional[str]]:
        global_settings = ctx.get_default_settings()
        queue_items = ctx.doc_manager.list_items()

        def _apply_query_overrides(base: PrintSettings) -> PrintSettings:
            upd = base.model_copy(deep=True)
            for k, v in override_params.items():
                if v is not None and hasattr(upd, k):
                    setattr(upd, k, v)
            return upd

        if scope == "merged":
            eff = _apply_query_overrides(global_settings)
            all_refs: list[PageRef] = []
            err_msg: Optional[str] = None
            for it in queue_items:
                if it.enabled and it.status == "ready":
                    try:
                        all_refs.extend(build_item_page_refs(it, eff))
                    except ValueError as exc:
                        err_msg = str(exc)
            sheets = chunk_sheets(all_refs, eff.pages_per_sheet)
            orient = resolve_job_orientation(
                eff, [(r.width_pt, r.height_pt) for r in all_refs]
            )
            return sheets, eff, orient, err_msg

        target: Optional[QueueItem] = None
        if item_id:
            target = ctx.doc_manager.get_item(item_id)
        if target is None:
            target = next((x for x in queue_items if x.status == "ready"), None)
        if target is None or target.status != "ready":
            eff = _apply_query_overrides(global_settings)
            return [], eff, "portrait", None

        eff = resolve_effective_settings(target, global_settings)
        eff = _apply_query_overrides(eff)
        err_msg = None
        try:
            refs = build_item_page_refs(target, eff)
        except ValueError as exc:
            refs = []
            err_msg = str(exc)
        sheets = chunk_sheets(refs, eff.pages_per_sheet)
        orient = resolve_job_orientation(eff, [(r.width_pt, r.height_pt) for r in refs])
        return sheets, eff, orient, err_msg

    @app.get("/api/preview/plan")
    async def api_preview_plan(
        scope: Literal["file", "merged"] = "file",
        item_id: Optional[str] = None,
        color_mode: Optional[Literal["color", "grayscale"]] = None,
        orientation: Optional[Literal["auto", "portrait", "landscape"]] = None,
        scale_mode: Optional[Literal["fit", "shrink", "actual", "custom"]] = None,
        scale_percent: Optional[int] = None,
        pages_per_sheet: Optional[int] = None,
        nup_order: Optional[Literal["row", "column"]] = None,
        nup_border: Optional[bool] = None,
        page_range_mode: Optional[Literal["all", "odd", "even", "custom"]] = None,
        page_range_custom: Optional[str] = None,
        paper_width_mm: Optional[float] = None,
        paper_height_mm: Optional[float] = None,
    ):
        params = {
            "color_mode": color_mode,
            "orientation": orientation,
            "scale_mode": scale_mode,
            "scale_percent": scale_percent,
            "pages_per_sheet": pages_per_sheet,
            "nup_order": nup_order,
            "nup_border": nup_border,
            "page_range_mode": page_range_mode,
            "page_range_custom": page_range_custom,
            "paper_width_mm": paper_width_mm,
            "paper_height_mm": paper_height_mm,
        }
        sheets, eff, orient, err_msg = _build_preview_plan_data(scope, item_id, params)
        # Also compute total sheets across whole queue for summary badge
        global_settings = ctx.get_default_settings()
        merge_mode = bool(ctx.storage.get_pref("merge_mode", False))
        total_batch_sheets = 0
        enabled_ready = [x for x in ctx.doc_manager.list_items() if x.enabled and x.status == "ready"]
        if merge_mode:
            merged_refs: list[PageRef] = []
            for x in enabled_ready:
                try:
                    merged_refs.extend(build_item_page_refs(x, global_settings))
                except Exception:
                    pass
            total_batch_sheets = len(chunk_sheets(merged_refs, global_settings.pages_per_sheet))
        else:
            for x in enabled_ready:
                x_eff = resolve_effective_settings(x, global_settings)
                try:
                    x_refs = build_item_page_refs(x, x_eff)
                    total_batch_sheets += len(chunk_sheets(x_refs, x_eff.pages_per_sheet))
                except Exception:
                    pass

        return {
            "sheet_count": len(sheets),
            "total_selected_pages": sum(len(s) for s in sheets),
            "resolved_orientation": orient,
            "paper_width_mm": eff.paper_width_mm,
            "paper_height_mm": eff.paper_height_mm,
            "error": err_msg,
            "total_batch_sheets": total_batch_sheets,
            "enabled_ready_count": len(enabled_ready),
            "sheets": [
                [
                    {"item_id": r.item_id, "item_name": r.item_name, "page_number": r.page_index + 1}
                    for r in s
                ]
                for s in sheets
            ],
        }

    @app.get("/api/preview/sheet")
    async def api_preview_sheet(
        sheet_index: int = Query(default=0, ge=0),
        scope: Literal["file", "merged"] = "file",
        item_id: Optional[str] = None,
        dpi: float = Query(default=110.0, ge=50.0, le=200.0),
        color_mode: Optional[Literal["color", "grayscale"]] = None,
        orientation: Optional[Literal["auto", "portrait", "landscape"]] = None,
        scale_mode: Optional[Literal["fit", "shrink", "actual", "custom"]] = None,
        scale_percent: Optional[int] = None,
        pages_per_sheet: Optional[int] = None,
        nup_order: Optional[Literal["row", "column"]] = None,
        nup_border: Optional[bool] = None,
        page_range_mode: Optional[Literal["all", "odd", "even", "custom"]] = None,
        page_range_custom: Optional[str] = None,
        paper_width_mm: Optional[float] = None,
        paper_height_mm: Optional[float] = None,
    ):
        params = {
            "color_mode": color_mode,
            "orientation": orientation,
            "scale_mode": scale_mode,
            "scale_percent": scale_percent,
            "pages_per_sheet": pages_per_sheet,
            "nup_order": nup_order,
            "nup_border": nup_border,
            "page_range_mode": page_range_mode,
            "page_range_custom": page_range_custom,
            "paper_width_mm": paper_width_mm,
            "paper_height_mm": paper_height_mm,
        }
        sheets, eff, orient, _ = _build_preview_plan_data(scope, item_id, params)
        if not sheets or sheet_index >= len(sheets):
            raise HTTPException(status_code=404, detail="Preview sheet not available")
        png = await asyncio.to_thread(
            render_preview_sheet_png, sheets[sheet_index], eff, orient, dpi
        )
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/preview/thumbnail/{item_id}/{page_idx}")
    async def api_preview_thumbnail(item_id: str, page_idx: int):
        item = ctx.doc_manager.get_item(item_id)
        if item is None or item.status != "ready" or not item.converted_pdf_path:
            raise HTTPException(status_code=404, detail="Document preview unavailable")
        try:
            png = await asyncio.to_thread(
                ctx.doc_manager.render_page_png,
                Path(item.converted_pdf_path),
                page_idx,
                72,
                False,
            )
            return Response(
                content=png,
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )
        except IndexError:
            raise HTTPException(status_code=404, detail="Page out of range")

    # --- Settings & Preferences ---

    @app.get("/api/settings/global")
    async def api_get_global_settings():
        return ctx.get_default_settings()

    @app.put("/api/settings/global")
    async def api_put_global_settings(settings: PrintSettings):
        ctx.storage.save_global_settings(settings)
        return settings

    @app.get("/api/prefs")
    async def api_get_prefs():
        return {
            "theme": ctx.storage.get_pref("theme", "system"),
            "merge_mode": bool(ctx.storage.get_pref("merge_mode", False)),
        }

    @app.put("/api/prefs")
    async def api_put_prefs(body: PrefsUpdate):
        if body.theme is not None:
            ctx.storage.set_pref("theme", body.theme)
        if body.merge_mode is not None:
            ctx.storage.set_pref("merge_mode", body.merge_mode)
        return {
            "theme": ctx.storage.get_pref("theme", "system"),
            "merge_mode": bool(ctx.storage.get_pref("merge_mode", False)),
        }

    # --- Printing ---

    @app.post("/api/print/start")
    async def api_print_start(req: PrintStartRequest):
        settings = req.global_settings or ctx.get_default_settings()
        ctx.storage.save_global_settings(settings)
        ctx.storage.set_pref("merge_mode", req.merge_mode)
        try:
            return ctx.print_manager.start_batch(
                global_settings=settings,
                merge_mode=req.merge_mode,
                item_ids=req.item_ids,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/print/cancel")
    async def api_print_cancel():
        return ctx.print_manager.cancel_batch()

    @app.post("/api/print/retry")
    async def api_print_retry():
        try:
            return ctx.print_manager.retry_failed(ctx.get_default_settings())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/print/status")
    async def api_print_status():
        return ctx.print_manager.get_state()

    # --- Presets ---

    @app.get("/api/presets")
    async def api_list_presets():
        return ctx.storage.list_presets()

    @app.post("/api/presets")
    async def api_create_preset(payload: PresetCreate):
        return ctx.storage.create_preset(payload)

    @app.put("/api/presets/{preset_id}")
    async def api_update_preset(preset_id: str, payload: PresetCreate):
        updated = ctx.storage.update_preset(preset_id, payload)
        if updated is None:
            raise HTTPException(status_code=404, detail="Preset not found")
        return updated

    @app.delete("/api/presets/{preset_id}")
    async def api_delete_preset(preset_id: str):
        if not ctx.storage.delete_preset(preset_id):
            raise HTTPException(status_code=404, detail="Preset not found")
        return {"ok": True}

    # --- History ---

    @app.get("/api/history")
    async def api_list_history():
        return ctx.storage.list_history()

    @app.post("/api/history/{entry_id}/reprint")
    async def api_reprint_history(entry_id: str):
        entry = ctx.storage.get_history_entry(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="History entry not found")
        existing_paths = [f.original_path for f in entry.files if Path(f.original_path).exists()]
        if not existing_paths:
            raise HTTPException(status_code=400, detail="Original files no longer exist on disk")

        ctx.storage.save_global_settings(entry.global_settings)
        ctx.storage.set_pref("merge_mode", entry.merge_mode)
        added = await asyncio.to_thread(ctx.doc_manager.add_paths, existing_paths)

        # Restore per-file skipped pages / overrides where path matches
        by_path = {f.original_path.lower(): f for f in entry.files}
        for item in added:
            rec = by_path.get(item.original_path.lower())
            if rec:
                ctx.doc_manager.update_item(
                    item.id,
                    QueueItemUpdate(
                        skipped_pages=rec.skipped_pages,
                        use_override=(rec.settings_snapshot is not None and rec.settings_snapshot != entry.global_settings),
                        override_settings=rec.settings_snapshot,
                    ),
                )
        return {
            "restored_count": len(added),
            "missing_count": len(entry.files) - len(existing_paths),
            "global_settings": entry.global_settings.model_dump(),
            "merge_mode": entry.merge_mode,
        }

    @app.delete("/api/history/{entry_id}")
    async def api_delete_history(entry_id: str):
        if not ctx.storage.delete_history_entry(entry_id):
            raise HTTPException(status_code=404, detail="History entry not found")
        return {"ok": True}

    @app.delete("/api/history")
    async def api_clear_history():
        count = ctx.storage.clear_history()
        return {"deleted": count}

    # --- Desktop Integration & Window Focus ---

    @app.get("/api/integration/status")
    async def api_integration_status():
        return await asyncio.to_thread(get_integration_status)

    @app.post("/api/integration/install")
    async def api_integration_install(req: IntegrationToggleRequest):
        return await asyncio.to_thread(
            install_integration, req.desktop, req.sendto, req.context_menu
        )

    @app.post("/api/integration/uninstall")
    async def api_integration_uninstall(req: IntegrationToggleRequest):
        return await asyncio.to_thread(
            uninstall_integration, req.desktop, req.sendto, req.context_menu
        )

    @app.post("/api/window/focus")
    async def api_window_focus():
        focused = await asyncio.to_thread(focus_app_window)
        return {"focused": focused}

    # --- WebSocket ---

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        with ctx._ws_lock:
            ctx.ws_clients.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            with ctx._ws_lock:
                ctx.ws_clients.discard(ws)

    return app


app = create_app()
