"""Tests for document ingestion, folder natural sort, PDF unlock, image & Word COM conversion."""

from __future__ import annotations

import time
from pathlib import Path

import fitz
from docx import Document as DocxDocument
from PIL import Image

from printstack.document_service import (
    DocumentManager,
    convert_image_to_pdf,
    inspect_pdf,
    scan_paths,
)


def _make_sample_pdf(path: Path, pages: int = 2, password: str | None = None) -> Path:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 72), f"Sample PDF Page {i + 1}", fontsize=18)
    if password:
        doc.save(
            str(path),
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw=password + "_owner",
            user_pw=password,
        )
    else:
        doc.save(str(path))
    doc.close()
    return path


def test_scan_paths_natural_sort_and_ignores_temp_files(tmp_path: Path):
    folder = tmp_path / "docs"
    folder.mkdir()
    _make_sample_pdf(folder / "doc10.pdf")
    _make_sample_pdf(folder / "doc2.pdf")
    _make_sample_pdf(folder / "doc1.pdf")
    (folder / "~$doc1.docx").write_bytes(b"lock")
    (folder / "notes.txt").write_text("ignored")

    found = scan_paths([str(folder)])
    assert [p.name for p in found] == ["doc1.pdf", "doc2.pdf", "doc10.pdf"]


def test_image_to_pdf_conversion(tmp_path: Path):
    img_path = tmp_path / "photo.png"
    Image.new("RGBA", (400, 300), (37, 99, 235, 255)).save(img_path)

    out_pdf = tmp_path / "photo.pdf"
    convert_image_to_pdf(img_path, out_pdf)
    needs_pass, count, dims = inspect_pdf(out_pdf)
    assert not needs_pass
    assert count == 1
    assert dims[0].width_pt > dims[0].height_pt


def test_pdf_password_lock_and_unlock(tmp_path: Path):
    locked_pdf = _make_sample_pdf(tmp_path / "secret.pdf", pages=3, password="open123")
    dm = DocumentManager(temp_dir=tmp_path / "temp", upload_dir=tmp_path / "up")
    try:
        items = dm.add_paths([str(locked_pdf)])
        assert len(items) == 1
        assert items[0].status == "password_required"

        try:
            dm.unlock_pdf(items[0].id, "wrong")
            assert False, "Expected ValueError for wrong password"
        except ValueError:
            pass

        unlocked = dm.unlock_pdf(items[0].id, "open123")
        assert unlocked.status == "ready"
        assert unlocked.page_count == 3
        png = dm.render_page_png(Path(unlocked.converted_pdf_path), 0, dpi=72, grayscale=True)
        assert png.startswith(b"\x89PNG")
    finally:
        dm.shutdown()


def test_word_docx_conversion_via_word_com(tmp_path: Path):
    docx_path = tmp_path / "memo.docx"
    doc = DocxDocument()
    doc.add_heading("Printstack Test", level=1)
    doc.add_paragraph("Verifying real headless Microsoft Word COM export to PDF.")
    doc.save(str(docx_path))

    dm = DocumentManager(temp_dir=tmp_path / "temp", upload_dir=tmp_path / "up")
    try:
        items = dm.add_paths([str(docx_path)])
        assert len(items) == 1
        item_id = items[0].id

        deadline = time.monotonic() + 30.0
        ready_item = None
        while time.monotonic() < deadline:
            cur = dm.get_item(item_id)
            if cur and cur.status in ("ready", "error"):
                ready_item = cur
                break
            time.sleep(0.25)

        assert ready_item is not None
        assert ready_item.status == "ready", f"Word COM error: {ready_item.error_message}"
        assert ready_item.page_count >= 1
        assert ready_item.converted_pdf_path is not None
        assert Path(ready_item.converted_pdf_path).exists()
    finally:
        dm.shutdown()
