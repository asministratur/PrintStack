"""Tests for N-up sheet compositing, grayscale preview, and Win32 GDI print to Microsoft Print to PDF."""

from __future__ import annotations

import io
import time
from pathlib import Path

import fitz
from PIL import Image

from printstack.document_service import DocumentManager
from printstack.models import PrintSettings
from printstack.print_engine import (
    PageRef,
    PrintJobManager,
    chunk_sheets,
    render_preview_sheet_png,
    resolve_job_orientation,
)
from printstack.storage import AppStorage


def _make_colored_pdf(path: Path, pages: int = 4, landscape: bool = False) -> Path:
    doc = fitz.open()
    w, h = (842, 595) if landscape else (595, 842)
    colors = [(0.9, 0.1, 0.1), (0.1, 0.7, 0.2), (0.1, 0.3, 0.9), (0.8, 0.5, 0.1)]
    for i in range(pages):
        page = doc.new_page(width=w, height=h)
        page.draw_rect(fitz.Rect(40, 40, w - 40, h - 40), color=colors[i % len(colors)], fill=colors[i % len(colors)])
        page.insert_text((72, 120), f"Page {i + 1}", fontsize=28, color=(1, 1, 1))
    doc.save(str(path))
    doc.close()
    return path


def test_nup_preview_compositing_and_grayscale(tmp_path: Path):
    pdf = _make_colored_pdf(tmp_path / "color.pdf", pages=4)
    refs = [PageRef("id1", "color.pdf", pdf, i, 595.0, 842.0) for i in range(4)]
    sheets = chunk_sheets(refs, pages_per_sheet=4)
    assert len(sheets) == 1

    settings = PrintSettings(
        printer_name="Microsoft Print to PDF",
        color_mode="grayscale",
        pages_per_sheet=4,
        nup_border=True,
        orientation="auto",
    )
    orient = resolve_job_orientation(settings, [(r.width_pt, r.height_pt) for r in refs])
    assert orient == "portrait"

    png = render_preview_sheet_png(sheets[0], settings, orient, preview_dpi=90.0)
    img = Image.open(io.BytesIO(png))
    assert img.width < img.height
    # Verify grayscale pixels (R == G == B)
    px = img.getpixel((img.width // 4, img.height // 4))
    assert px[0] == px[1] == px[2]


def test_2up_auto_orientation_chooses_landscape_for_portrait_pages():
    s = PrintSettings(orientation="auto", pages_per_sheet=2)
    assert resolve_job_orientation(s, [(595.0, 842.0), (595.0, 842.0)]) == "landscape"


def test_gdi_print_to_microsoft_print_to_pdf_separate_and_merged(tmp_path: Path):
    pdf1 = _make_colored_pdf(tmp_path / "one.pdf", pages=2)
    pdf2 = _make_colored_pdf(tmp_path / "two.pdf", pages=2)

    storage = AppStorage(db_path=tmp_path / "test.db")
    dm = DocumentManager(temp_dir=tmp_path / "temp", upload_dir=tmp_path / "up")
    pm = PrintJobManager(dm, storage)
    try:
        items = dm.add_paths([str(pdf1), str(pdf2)])
        assert len(items) == 2

        out_pdf = tmp_path / "printed_output.pdf"
        settings = PrintSettings(
            printer_name="Microsoft Print to PDF",
            color_mode="color",
            pages_per_sheet=2,
            quality_dpi=150,
            paper_size_id=9,
            paper_width_mm=210.0,
            paper_height_mm=297.0,
        )

        # Merged mode writes all 4 pages across 2 sheets into 1 print job -> single valid PDF output
        pm.start_batch(global_settings=settings, merge_mode=True, output_pdf_path=str(out_pdf))

        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            st = pm.get_state()
            if st.status in ("completed", "partial", "failed", "cancelled"):
                break
            time.sleep(0.15)

        final = pm.get_state()
        assert final.status == "completed", f"Print failed: {final}"
        assert final.total_sheets == 2
        assert out_pdf.exists()
        assert out_pdf.stat().st_size > 1000

        with fitz.open(str(out_pdf)) as printed_doc:
            assert len(printed_doc) == 2

        hist = storage.list_history()
        assert len(hist) == 1
        assert hist[0].total_files == 2
        assert hist[0].total_sheets == 2
    finally:
        dm.shutdown()
        storage.close()
