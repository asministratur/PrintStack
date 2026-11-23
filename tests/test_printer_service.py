"""Tests for Win32 printer enumeration, capability querying, and DEVMODE DriverData roundtrip."""

from printstack.printer_service import (
    build_devmode_for_settings,
    deserialize_devmode,
    get_printer_capabilities,
    list_printers,
    serialize_devmode,
)
from printstack.models import PrintSettings
import win32print


def test_list_printers_detects_local_printers():
    printers = list_printers()
    names = [p.name for p in printers]
    assert len(names) >= 1
    assert "Microsoft Print to PDF" in names


def test_capabilities_on_virtual_and_physical_printers():
    printers = {p.name for p in list_printers()}
    assert "Microsoft Print to PDF" in printers

    pdf_caps = get_printer_capabilities("Microsoft Print to PDF")
    assert pdf_caps.printer_name == "Microsoft Print to PDF"
    assert len(pdf_caps.paper_sizes) > 0
    assert pdf_caps.paper_bins == []
    assert pdf_caps.supports_duplex is False

    fx_name = "FF K533p for ApeosPort-V C5576"
    if fx_name in printers:
        fx_caps = get_printer_capabilities(fx_name)
        assert fx_caps.supports_duplex is True
        assert fx_caps.supports_color is True
        assert fx_caps.max_copies >= 1
        assert len(fx_caps.paper_sizes) > 10
        assert len(fx_caps.paper_bins) >= 4


def test_devmode_serialization_preserves_driver_data():
    printers = {p.name for p in list_printers()}
    target = "FF K533p for ApeosPort-V C5576" if "FF K533p for ApeosPort-V C5576" in printers else "Microsoft Print to PDF"

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
        duplex="long_edge",
        devmode_b64=b64,
        has_custom_driver_settings=True,
    )
    built = build_devmode_for_settings(target, settings)
    assert built.Color == 1  # DMCOLOR_MONOCHROME
    assert len(built.DriverData or b"") == orig_len
