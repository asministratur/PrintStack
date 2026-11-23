"""Pydantic v2 domain models for Printstack."""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class PrintSettings(BaseModel):
    printer_name: str = ""
    copies: int = Field(default=1, ge=1, le=9999)
    collate: bool = True
    color_mode: Literal["color", "grayscale"] = "color"
    duplex: Literal["simplex", "long_edge", "short_edge"] = "simplex"
    orientation: Literal["auto", "portrait", "landscape"] = "auto"
    paper_size_id: Optional[int] = None
    paper_size_name: Optional[str] = None
    paper_width_mm: float = Field(default=210.0, gt=0)
    paper_height_mm: float = Field(default=297.0, gt=0)
    paper_source_id: Optional[int] = None
    paper_source_name: Optional[str] = None
    scale_mode: Literal["fit", "shrink", "actual", "custom"] = "fit"
    scale_percent: int = Field(default=100, ge=10, le=400)
    pages_per_sheet: Literal[1, 2, 4, 6, 9] = 1
    nup_order: Literal["row", "column"] = "row"
    nup_border: bool = False
    page_range_mode: Literal["all", "odd", "even", "custom"] = "all"
    page_range_custom: str = ""
    quality_dpi: Literal[150, 300, 600] = 300
    devmode_b64: Optional[str] = None
    has_custom_driver_settings: bool = False


class PaperSizeOption(BaseModel):
    id: int
    name: str
    width_mm: float
    height_mm: float


class PaperBinOption(BaseModel):
    id: int
    name: str


class ResolutionOption(BaseModel):
    xdpi: int
    ydpi: int


class PrinterSummary(BaseModel):
    name: str
    driver_name: str = ""
    port_name: str = ""
    is_default: bool = False
    status_code: int = 0
    status_label: str = "Ready"
    status_severity: Literal["ok", "warn", "error"] = "ok"
    jobs_count: int = 0
    is_virtual: bool = False


class PrinterCapabilities(BaseModel):
    printer_name: str
    supports_duplex: bool = False
    supports_color: bool = True
    max_copies: int = 1
    supports_collate: bool = False
    paper_sizes: list[PaperSizeOption] = Field(default_factory=list)
    paper_bins: list[PaperBinOption] = Field(default_factory=list)
    resolutions: list[ResolutionOption] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)


class SpoolerJob(BaseModel):
    job_id: int
    document: str
    user: str = ""
    status: str
    total_pages: int = 0
    pages_printed: int = 0
    submitted: str = ""


class PageDimension(BaseModel):
    width_pt: float
    height_pt: float


class QueueItem(BaseModel):
    id: str
    name: str
    original_path: str
    file_ext: str
    file_type: Literal["pdf", "word", "image"]
    size_bytes: int = 0
    status: Literal["converting", "ready", "password_required", "error"] = "converting"
    error_message: Optional[str] = None
    enabled: bool = True
    converted_pdf_path: Optional[str] = None
    page_count: int = 0
    page_dimensions: list[PageDimension] = Field(default_factory=list)
    skipped_pages: list[int] = Field(default_factory=list)
    use_override: bool = False
    override_settings: Optional[PrintSettings] = None
    added_at: str = ""


class QueueItemUpdate(BaseModel):
    enabled: Optional[bool] = None
    skipped_pages: Optional[list[int]] = None
    use_override: Optional[bool] = None
    override_settings: Optional[PrintSettings] = None


class QueueReorderRequest(BaseModel):
    item_ids: list[str]


class ImportPathsRequest(BaseModel):
    paths: list[str]


class UnlockPdfRequest(BaseModel):
    password: str


class Preset(BaseModel):
    id: str
    name: str
    description: str = ""
    settings: PrintSettings
    created_at: str = ""
    updated_at: str = ""


class PresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=200)
    settings: PrintSettings


class HistoryFileRecord(BaseModel):
    name: str
    original_path: str
    file_type: str
    page_count: int
    sheet_count: int
    status: Literal["printed", "failed", "cancelled"]
    error: Optional[str] = None
    settings_snapshot: Optional[PrintSettings] = None
    skipped_pages: list[int] = Field(default_factory=list)


class HistoryEntry(BaseModel):
    id: str
    printed_at: str
    printer_name: str
    merge_mode: bool
    status: Literal["completed", "partial", "failed", "cancelled"]
    total_files: int
    total_sheets: int
    files: list[HistoryFileRecord]
    global_settings: PrintSettings


class BatchFileStatus(BaseModel):
    item_id: str
    name: str
    status: Literal["queued", "printing", "printed", "failed", "cancelled"] = "queued"
    current_sheet: int = 0
    total_sheets: int = 0
    error: Optional[str] = None


class BatchState(BaseModel):
    batch_id: Optional[str] = None
    status: Literal["idle", "printing", "cancelling", "completed", "partial", "failed", "cancelled"] = "idle"
    merge_mode: bool = False
    printer_name: str = ""
    current_file_index: int = 0
    total_files: int = 0
    current_sheet: int = 0
    total_sheets: int = 0
    files: list[BatchFileStatus] = Field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None


class PrintStartRequest(BaseModel):
    merge_mode: bool = False
    item_ids: Optional[list[str]] = None
    global_settings: Optional[PrintSettings] = None


class PrinterPropertiesRequest(BaseModel):
    settings: PrintSettings
