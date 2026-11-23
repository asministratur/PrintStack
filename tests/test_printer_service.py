"""Tests for Win32 printer enumeration, capability querying, and DEVMODE DriverData roundtrip."""

import pytest
import win32print

from printstack.models import PrintSettings
from printstack.printer_service import (
    build_devmode_for_settings,
    deserialize_devmode,
    get_printer_capabilities,
    list_printers,
    serialize_devmode,
)

_AVAILABLE_PRINTERS = {p.name for p in list_printers()}
_HAS_MS_PDF = "Microsoft Print to PDF" in _AVAILABLE_PRINTERS


@pytest.mark.skipif(not _HAS_MS_PDF, reason="Microsoft Print to PDF printer not installed")
def test_list_printers_detects_local_printers():
    printers = list_printers()
    names = [p.name for p in printers]
    assert len(names) >= 1
    assert "Microsoft Print to PDF" in names


@pytest.mark.skipif(not _HAS_MS_PDF, reason="Microsoft Print to PDF printer not installed")
def test_capabilities_on_virtual_printer():
    pdf_caps = get_printer_capabilities("Microsoft Print to PDF")
    assert pdf_caps.printer_name == "Microsoft Print to PDF"
    assert len(pdf_caps.paper_sizes) > 0
    assert pdf_caps.paper_bins == []
    assert pdf_caps.supports_duplex is False


@pytest.mark.skipif(not _HAS_MS_PDF, reason="Microsoft Print to PDF printer not installed")
def test_devmode_serialization_preserves_driver_data():
    target = "Microsoft Print to PDF"

    h = win32print.OpenPrinter(target)
    try:
        orig_dm = win32print.GetPrinter(h, 2)["pDevMode"]
    finally:
        win32print.ClosePrinter(h)

    orig_len = len(orig_dm.DriverData or b"")
    b64 = serialize_devmode(orig_dm)
    restored = deserialize_devmode(target, b64)
    assert len(restored.DriverData or b"") == orig_len

    settings = PrintSettings(
        printer_name=target,
        copies=3,
        color_mode="grayscale",
        orientation="landscape",
        duplex="long_edge",
        devmode_b64=b64,
        has_custom_driver_settings=True,
    )
    built = build_devmode_for_settings(target, settings)
    assert built.Orientation == 2  # DMORIENT_LANDSCAPE
    assert built.Color in (1, 2)  # Virtual PDF driver may enforce DMCOLOR_COLOR
    assert len(built.DriverData or b"") == orig_len
