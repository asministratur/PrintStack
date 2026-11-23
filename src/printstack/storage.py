"""SQLite persistence layer for presets, print history, and user preferences."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from printstack.config import DB_PATH, ensure_dirs
from printstack.models import HistoryEntry, Preset, PresetCreate, PrintSettings


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AppStorage:
    """Thread-safe SQLite repository for Printstack."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        ensure_dirs()
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._seed_defaults()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS presets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    settings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id TEXT PRIMARY KEY,
                    printed_at TEXT NOT NULL,
                    printer_name TEXT NOT NULL,
                    merge_mode INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    total_files INTEGER NOT NULL,
                    total_sheets INTEGER NOT NULL,
                    entry_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_prefs (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def _seed_defaults(self) -> None:
        if self.get_pref("seeded_presets", False):
            return
        now = utc_now_iso()
        starters = [
            Preset(
                id="preset-economy-bw-duplex",
                name="Economy B&W Duplex",
                description="Black & white double-sided printing to save paper and color toner",
                settings=PrintSettings(
                    color_mode="grayscale",
                    duplex="long_edge",
                    pages_per_sheet=1,
                    quality_dpi=300,
                    scale_mode="fit",
                ),
                created_at=now,
                updated_at=now,
            ),
            Preset(
                id="preset-hq-color",
                name="High Quality Color",
                description="Full-color single-sided 600 DPI for presentations and reports",
                settings=PrintSettings(
                    color_mode="color",
                    duplex="simplex",
                    pages_per_sheet=1,
                    quality_dpi=600,
                    scale_mode="fit",
                ),
                created_at=now,
                updated_at=now,
            ),
            Preset(
                id="preset-handout-2up",
                name="2-Up Handout B&W",
                description="Two pages per sheet with borders in black & white",
                settings=PrintSettings(
                    color_mode="grayscale",
                    duplex="long_edge",
                    pages_per_sheet=2,
                    nup_border=True,
                    quality_dpi=300,
                ),
                created_at=now,
                updated_at=now,
            ),
        ]
        with self._lock:
            cur = self._conn.cursor()
            existing = cur.execute("SELECT COUNT(*) FROM presets").fetchone()[0]
            if existing == 0:
                for p in starters:
                    cur.execute(
                        "INSERT INTO presets (id, name, description, settings_json, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            p.id,
                            p.name,
                            p.description,
                            p.settings.model_dump_json(),
                            p.created_at,
                            p.updated_at,
                        ),
                    )
            cur.execute(
                "INSERT OR REPLACE INTO app_prefs (key, value_json) VALUES (?, ?)",
                ("seeded_presets", json.dumps(True)),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- Preferences ---

    def get_pref(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json FROM app_prefs WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return default
            return json.loads(row["value_json"])

    def set_pref(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO app_prefs (key, value_json) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    def get_global_settings(self) -> Optional[PrintSettings]:
        raw = self.get_pref("global_settings")
        if not raw:
            return None
        return PrintSettings.model_validate(raw)

    def save_global_settings(self, settings: PrintSettings) -> None:
        self.set_pref("global_settings", settings.model_dump())

    # --- Presets ---

    def list_presets(self) -> list[Preset]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM presets ORDER BY created_at ASC, name ASC"
            ).fetchall()
        result: list[Preset] = []
        for r in rows:
            result.append(
                Preset(
                    id=r["id"],
                    name=r["name"],
                    description=r["description"],
                    settings=PrintSettings.model_validate_json(r["settings_json"]),
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
            )
        return result

    def get_preset(self, preset_id: str) -> Optional[Preset]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM presets WHERE id = ?", (preset_id,)
            ).fetchone()
        if r is None:
            return None
        return Preset(
            id=r["id"],
            name=r["name"],
            description=r["description"],
            settings=PrintSettings.model_validate_json(r["settings_json"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def create_preset(self, payload: PresetCreate) -> Preset:
        now = utc_now_iso()
        preset = Preset(
            id=f"preset-{uuid.uuid4().hex[:10]}",
            name=payload.name.strip(),
            description=payload.description.strip(),
            settings=payload.settings,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO presets (id, name, description, settings_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    preset.id,
                    preset.name,
                    preset.description,
                    preset.settings.model_dump_json(),
                    preset.created_at,
                    preset.updated_at,
                ),
            )
            self._conn.commit()
        return preset

    def update_preset(self, preset_id: str, payload: PresetCreate) -> Optional[Preset]:
        existing = self.get_preset(preset_id)
        if existing is None:
            return None
        now = utc_now_iso()
        updated = Preset(
            id=preset_id,
            name=payload.name.strip(),
            description=payload.description.strip(),
            settings=payload.settings,
            created_at=existing.created_at,
            updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                "UPDATE presets SET name = ?, description = ?, settings_json = ?, updated_at = ? WHERE id = ?",
                (
                    updated.name,
                    updated.description,
                    updated.settings.model_dump_json(),
                    updated.updated_at,
                    preset_id,
                ),
            )
            self._conn.commit()
        return updated

    def delete_preset(self, preset_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # --- History ---

    def add_history(self, entry: HistoryEntry) -> HistoryEntry:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO history "
                "(id, printed_at, printer_name, merge_mode, status, total_files, total_sheets, entry_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id,
                    entry.printed_at,
                    entry.printer_name,
                    1 if entry.merge_mode else 0,
                    entry.status,
                    entry.total_files,
                    entry.total_sheets,
                    entry.model_dump_json(),
                ),
            )
            self._conn.commit()
        return entry

    def list_history(self, limit: int = 100) -> list[HistoryEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry_json FROM history ORDER BY printed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [HistoryEntry.model_validate_json(r["entry_json"]) for r in rows]

    def get_history_entry(self, entry_id: str) -> Optional[HistoryEntry]:
        with self._lock:
            row = self._conn.execute(
                "SELECT entry_json FROM history WHERE id = ?", (entry_id,)
            ).fetchone()
        if row is None:
            return None
        return HistoryEntry.model_validate_json(row["entry_json"])

    def delete_history_entry(self, entry_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM history WHERE id = ?", (entry_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def clear_history(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM history")
            self._conn.commit()
            return cur.rowcount
