"""FastAPI integration tests (upload, reorder, overrides, preview PNG, presets, history)."""

from __future__ import annotations

import os
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PRINTSTACK_DATA_DIR", str(tmp_path / "appdata"))
    # Reload config/main cleanly against isolated temp data dir
    import importlib
    import printstack.config as cfg
    import printstack.storage as st
    import printstack.main as main_mod

    importlib.reload(cfg)
    importlib.reload(st)
    importlib.reload(main_mod)

    with TestClient(main_mod.create_app()) as c:
        yield c


def _make_pdf_bytes(pages: int = 3) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 96), f"API Test Page {i + 1}", fontsize=18)
    data = doc.tobytes()
    doc.close()
    return data


def test_api_end_to_end_workflow(client: TestClient, tmp_path: Path):
    # Health & Bootstrap
    assert client.get("/api/health").status_code == 200
    boot = client.get("/api/bootstrap").json()
    assert isinstance(boot["printers"], list)
    assert len(boot["presets"]) >= 2

    # Upload 2 PDFs
    pdf_a = _make_pdf_bytes(3)
    pdf_b = _make_pdf_bytes(2)
    up = client.post(
        "/api/queue/upload",
        files=[
            ("files", ("alpha.pdf", pdf_a, "application/pdf")),
            ("files", ("beta.pdf", pdf_b, "application/pdf")),
        ],
    )
    assert up.status_code == 200
    added = up.json()["added"]
    assert len(added) == 2
    id_a, id_b = added[0]["id"], added[1]["id"]

    # Reorder queue
    re = client.post("/api/queue/reorder", json={"item_ids": [id_b, id_a]})
    assert re.status_code == 200
    assert [x["id"] for x in re.json()] == [id_b, id_a]

    # Skip page 2 on alpha and set per-file override
    patch = client.patch(
        f"/api/queue/{id_a}",
        json={
            "skipped_pages": [2],
            "use_override": True,
            "override_settings": {
                "printer_name": "Microsoft Print to PDF",
                "color_mode": "grayscale",
                "pages_per_sheet": 2,
            },
        },
    )
    assert patch.status_code == 200
    assert patch.json()["skipped_pages"] == [2]

    # Preview plan & sheet PNG
    plan = client.get(
        "/api/preview/plan",
        params={"scope": "file", "item_id": id_a, "pages_per_sheet": 2, "color_mode": "grayscale"},
    )
    assert plan.status_code == 200
    assert plan.json()["total_selected_pages"] == 2
    assert plan.json()["sheet_count"] == 1

    sheet_png = client.get(
        "/api/preview/sheet",
        params={"scope": "file", "item_id": id_a, "sheet_index": 0, "pages_per_sheet": 2, "color_mode": "grayscale"},
    )
    assert sheet_png.status_code == 200
    assert sheet_png.content.startswith(b"\x89PNG")

    thumb = client.get(f"/api/preview/thumbnail/{id_a}/0")
    assert thumb.status_code == 200
    assert thumb.content.startswith(b"\x89PNG")

    # Presets CRUD
    created_preset = client.post(
        "/api/presets",
        json={
            "name": "Custom Test Preset",
            "description": "Test",
            "settings": {"printer_name": "Microsoft Print to PDF", "color_mode": "grayscale"},
        },
    )
    assert created_preset.status_code == 200
    pid = created_preset.json()["id"]
    assert any(p["id"] == pid for p in client.get("/api/presets").json())
    assert client.delete(f"/api/presets/{pid}").status_code == 200

    # Clear queue
    assert client.delete("/api/queue").status_code == 200
    assert client.get("/api/queue").json() == []


def test_localhost_origin_and_host_guard(client: TestClient):
    assert client.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/queue/bulk-enable", json={"enabled": False}, headers={"Origin": "http://attacker.local"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/health", headers={"Origin": "http://127.0.0.1:47821"}).status_code == 200


def test_queue_bulk_enable_and_sort(client: TestClient):
    pdf_a = _make_pdf_bytes(1)
    pdf_b = _make_pdf_bytes(1)
    up = client.post(
        "/api/queue/upload",
        files=[
            ("files", ("zeta.pdf", pdf_a, "application/pdf")),
            ("files", ("alpha.pdf", pdf_b, "application/pdf")),
        ],
    )
    assert up.status_code == 200

    # Sort A-Z then Z-A
    res_asc = client.post("/api/queue/sort", json={"mode": "name_asc"})
    assert res_asc.status_code == 200
    assert [x["name"] for x in res_asc.json()] == ["alpha.pdf", "zeta.pdf"]

    res_desc = client.post("/api/queue/sort", json={"mode": "name_desc"})
    assert res_desc.status_code == 200
    assert [x["name"] for x in res_desc.json()] == ["zeta.pdf", "alpha.pdf"]

    # Bulk disable then enable
    res_off = client.post("/api/queue/bulk-enable", json={"enabled": False})
    assert res_off.status_code == 200
    assert all(not x["enabled"] for x in res_off.json())

    res_on = client.post("/api/queue/bulk-enable", json={"enabled": True})
    assert res_on.status_code == 200
    assert all(x["enabled"] for x in res_on.json())

    # Bootstrap exposes word_engine and excel_engine metadata
    boot = client.get("/api/bootstrap").json()
    assert "word_engine" in boot and "available" in boot["word_engine"]
    assert "excel_engine" in boot and "available" in boot["excel_engine"]


def test_update_check_endpoint_handles_newer_and_offline(client: TestClient, monkeypatch):
    import json
    import urllib.request
    import printstack.main as main_mod

    class _FakeResp:
        def __init__(self, payload: dict):
            self._raw = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_kw: _FakeResp(
            {
                "tag_name": "v9.9.0",
                "html_url": "https://github.com/asministratur/PrintStack/releases/tag/v9.9.0",
                "assets": [
                    {
                        "name": "Printstack-Setup-9.9.0.exe",
                        "browser_download_url": "https://github.com/asministratur/PrintStack/releases/download/v9.9.0/Printstack-Setup-9.9.0.exe",
                    }
                ],
            }
        ),
    )
    res = client.get("/api/updates/check")
    assert res.status_code == 200
    body = res.json()
    assert body["update_available"] is True
    assert body["latest_version"] == "9.9.0"
    assert body["installer_url"].endswith("Printstack-Setup-9.9.0.exe")

    # Clear cache and verify offline error degrades gracefully
    client.app.state.ctx._update_cache = None

    def _boom(*_a, **_kw):
        raise OSError("network offline")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    res_off = client.get("/api/updates/check")
    assert res_off.status_code == 200
    assert res_off.json()["update_available"] is False
    assert main_mod._parse_semver("1.10.0") > main_mod._parse_semver("1.2.0")

