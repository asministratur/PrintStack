/** Main Printstack application controller. */

import { api, connectWebSocket } from "./api.js";
import { PreviewController } from "./preview.js";
import { SettingsController } from "./settings.js";

const SUPPORTED_RE = /\.(pdf|docx|doc|rtf|jpg|jpeg|png|bmp|webp|tif|tiff)$/i;

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatBytes(bytes) {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const state = {
  printers: [],
  wordEngine: { engine: "none", available: true, label: "" },
  capabilitiesCache: {},
  globalSettings: {},
  mergeMode: false,
  theme: "system",
  previewScope: "file",
  queue: [],
  selectedItemId: null,
  presets: [],
  batchState: { status: "idle", files: [] },
  integration: {},
  lastPlan: null,
  sortMode: "name_asc",
};

let previewCtrl = null;
let settingsCtrl = null;
let unlockTargetItem = null;
let dragQueueId = null;

// --- Toast & Modals ---

function toast(message, type = "info") {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 3800);
}

function openModal(modalId) {
  const backdrop = document.getElementById("modal-backdrop");
  backdrop.classList.remove("hidden");
  for (const card of backdrop.querySelectorAll(".modal-card")) {
    card.classList.toggle("hidden", card.id !== modalId);
  }
}

function closeModals() {
  document.getElementById("modal-backdrop").classList.add("hidden");
}

// --- Theme ---

function applyTheme(theme) {
  state.theme = theme;
  document.documentElement.setAttribute("data-theme", theme);
  const labelMap = { system: "Auto", light: "Light", dark: "Dark" };
  const iconMap = { system: "◐", light: "☀", dark: "☾" };
  document.getElementById("theme-label").textContent = labelMap[theme] || "Auto";
  document.getElementById("theme-icon").textContent = iconMap[theme] || "◐";
  document.getElementById("btn-theme-toggle").title = `Theme: ${labelMap[theme]}`;
}

function cycleTheme() {
  const order = ["system", "light", "dark"];
  const next = order[(order.indexOf(state.theme) + 1) % order.length];
  applyTheme(next);
  api.savePrefs({ theme: next }).catch(() => {});
}

// --- Queue & Selection ---

function getSelectedItem() {
  if (!state.queue.length) return null;
  let found = state.queue.find((q) => q.id === state.selectedItemId);
  if (!found) {
    found = state.queue[0];
    state.selectedItemId = found.id;
  }
  return found;
}

function getEffectiveSettingsForPreview(item) {
  if (state.mergeMode && state.previewScope === "merged") {
    return state.globalSettings;
  }
  if (item && item.use_override && item.override_settings) {
    return item.override_settings;
  }
  return state.globalSettings;
}

function renderQueueToolbar() {
  const toolbar = document.getElementById("queue-toolbar");
  const chkAll = document.getElementById("chk-queue-all");
  const summary = document.getElementById("queue-selected-summary");
  const btnSort = document.getElementById("btn-queue-sort");
  if (!toolbar) return;

  toolbar.classList.toggle("hidden", state.queue.length === 0);
  if (!state.queue.length) return;

  const enabledCount = state.queue.filter((q) => q.enabled).length;
  chkAll.checked = enabledCount === state.queue.length;
  chkAll.indeterminate = enabledCount > 0 && enabledCount < state.queue.length;
  summary.textContent = `${enabledCount}/${state.queue.length} active`;

  const sortLabels = {
    name_asc: "Sort: A–Z",
    name_desc: "Sort: Z–A",
    type: "Sort: Type",
  };
  btnSort.textContent = sortLabels[state.sortMode] || "Sort: A–Z";
}

function refreshDiagnostics() {
  const banner = document.getElementById("diagnostic-banner");
  const textEl = document.getElementById("diagnostic-banner-text");
  if (!banner || !textEl) return;

  const printer = state.printers.find((x) => x.name === state.globalSettings.printer_name);
  const hasWordDocs = state.queue.some((q) => q.file_type === "word");

  if (printer && printer.status_severity === "error") {
    banner.className = "diagnostic-banner error";
    textEl.textContent = `Printer "${printer.name}" reports "${printer.status_label}". Check power, paper, or connection before printing.`;
    return;
  }

  if (hasWordDocs && state.wordEngine && state.wordEngine.available === false) {
    banner.className = "diagnostic-banner";
    textEl.textContent = "Microsoft Word or LibreOffice was not detected. Word files (.docx/.doc/.rtf) require one of them to convert.";
    return;
  }

  banner.className = "diagnostic-banner hidden";
}

function renderQueue() {
  const list = document.getElementById("queue-list");
  document.getElementById("queue-count-pill").textContent = String(state.queue.length);
  renderQueueToolbar();
  refreshDiagnostics();

  if (!state.queue.length) {
    state.selectedItemId = null;
    list.innerHTML = `<div class="queue-empty-note">No documents in queue yet.<br/>Drop files or folders above.</div>`;
    updateFooterCTA();
    return;
  }

  getSelectedItem();
  list.innerHTML = "";

  for (const item of state.queue) {
    const row = document.createElement("div");
    row.className = `queue-item${item.id === state.selectedItemId ? " selected" : ""}${
      !item.enabled ? " disabled-item" : ""
    }`;
    row.draggable = true;
    row.dataset.id = item.id;

    let statusHtml = "";
    if (item.status === "converting") {
      statusHtml = `<span class="mini-tag warn">Converting...</span>`;
    } else if (item.status === "password_required") {
      statusHtml = `<button class="btn-xs unlock-inline-btn" type="button">Unlock PDF</button>`;
    } else if (item.status === "error") {
      statusHtml = `<span class="mini-tag err" title="${escapeHtml(item.error_message)}">Error</span>`;
    } else {
      const skippedCount = item.skipped_pages ? item.skipped_pages.length : 0;
      const skipInfo = skippedCount ? ` (${skippedCount} skipped)` : "";
      statusHtml = `<span>${item.page_count} pg${item.page_count === 1 ? "" : "s"}${skipInfo}</span>`;
    }

    const overrideTag = item.use_override ? `<span class="mini-tag override">Custom</span>` : "";

    row.innerHTML = `
      <span class="drag-handle" title="Drag to reorder">&#8942;&#8942;</span>
      <input type="checkbox" class="qi-check" ${item.enabled ? "checked" : ""} title="Include in print batch" />
      <div class="qi-body">
        <div class="qi-top">
          <span class="ext-badge ${item.file_type}">${escapeHtml(item.file_ext.replace(".", ""))}</span>
          <span class="qi-name" title="${escapeHtml(item.original_path)}">${escapeHtml(item.name)}</span>
        </div>
        <div class="qi-sub">
          ${statusHtml}
          <span>${formatBytes(item.size_bytes)}</span>
          ${overrideTag}
        </div>
      </div>
      <button class="qi-remove" title="Remove from queue">&times;</button>
    `;

    row.querySelector(".qi-check").addEventListener("click", (e) => e.stopPropagation());
    row.querySelector(".qi-check").addEventListener("change", async (e) => {
      try {
        await api.updateQueueItem(item.id, { enabled: e.target.checked });
      } catch (err) {
        toast(err.message, "error");
      }
    });

    const unlockBtn = row.querySelector(".unlock-inline-btn");
    if (unlockBtn) {
      unlockBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openUnlockModal(item);
      });
    }

    row.querySelector(".qi-remove").addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await api.deleteQueueItem(item.id);
      } catch (err) {
        toast(err.message, "error");
      }
    });

    row.addEventListener("click", () => {
      state.selectedItemId = item.id;
      if (state.mergeMode && state.previewScope === "merged") {
        state.previewScope = "file";
      }
      previewCtrl.currentSheetIndex = 0;
      renderQueue();
      settingsCtrl.render();
      previewCtrl.refreshNow();
    });

    // Drag reorder
    row.addEventListener("dragstart", (e) => {
      dragQueueId = item.id;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("application/x-bps-item", item.id);
    });
    row.addEventListener("dragover", (e) => {
      if (!dragQueueId || dragQueueId === item.id) return;
      e.preventDefault();
      row.classList.add("drag-over-top");
    });
    row.addEventListener("dragleave", () => row.classList.remove("drag-over-top"));
    row.addEventListener("drop", async (e) => {
      row.classList.remove("drag-over-top");
      if (!dragQueueId || dragQueueId === item.id) return;
      e.preventDefault();
      const ids = state.queue.map((q) => q.id).filter((id) => id !== dragQueueId);
      const targetIdx = ids.indexOf(item.id);
      ids.splice(targetIdx, 0, dragQueueId);
      dragQueueId = null;
      await api.reorderQueue(ids).catch((err) => toast(err.message, "error"));
    });
    row.addEventListener("dragend", () => {
      dragQueueId = null;
      for (const el of list.querySelectorAll(".drag-over-top")) {
        el.classList.remove("drag-over-top");
      }
    });

    list.appendChild(row);
  }
  updateFooterCTA();
}

// --- Header & Footer Status ---

function refreshHeaderPrinter() {
  const name = state.globalSettings.printer_name;
  const p = state.printers.find((x) => x.name === name) || state.printers[0];
  const nameEl = document.getElementById("header-printer-name");
  const statusEl = document.getElementById("header-printer-status");
  const dotEl = document.getElementById("header-status-dot");
  const jobsEl = document.getElementById("header-jobs-count");

  if (!p) {
    nameEl.textContent = "No Printers Found";
    statusEl.textContent = "Unavailable";
    dotEl.className = "status-dot error";
    jobsEl.textContent = "0";
    return;
  }
  nameEl.textContent = p.name;
  statusEl.textContent = p.status_label;
  dotEl.className = `status-dot ${p.status_severity}`;
  jobsEl.textContent = String(p.jobs_count || 0);
  refreshDiagnostics();
}

function updateFooterCTA() {
  const readyEnabled = state.queue.filter((q) => q.enabled && q.status === "ready");
  const converting = state.queue.filter((q) => q.enabled && q.status === "converting");
  const isPrinting = ["printing", "cancelling"].includes(state.batchState.status);
  const hasFailed = (state.batchState.files || []).some((f) => f.status === "failed");

  const btnPrint = document.getElementById("btn-print-now");
  const btnCancel = document.getElementById("btn-cancel-print");
  const btnRetry = document.getElementById("btn-retry-failed");
  const summaryEl = document.getElementById("print-summary-line");

  btnPrint.disabled = isPrinting || readyEnabled.length === 0 || !state.globalSettings.printer_name;
  btnPrint.textContent = isPrinting
    ? "Printing..."
    : state.mergeMode
    ? `Print Merged (${readyEnabled.length} file${readyEnabled.length === 1 ? "" : "s"})`
    : `Print Batch (${readyEnabled.length} file${readyEnabled.length === 1 ? "" : "s"})`;

  btnCancel.classList.toggle("hidden", !isPrinting);
  btnRetry.classList.toggle("hidden", isPrinting || !hasFailed);

  if (!state.queue.length) {
    summaryEl.textContent = "Add PDF, Word, or Image files to start";
  } else if (converting.length) {
    summaryEl.textContent = `Converting ${converting.length} file(s)...`;
  } else {
    const sheets = state.lastPlan?.total_batch_sheets ?? "?";
    const copies = state.globalSettings.copies || 1;
    const modeLabel = state.mergeMode ? "1 merged job" : `${readyEnabled.length} separate jobs`;
    summaryEl.textContent = `${modeLabel} · ${sheets} sheet(s) · ${copies} cop${copies === 1 ? "y" : "ies"}`;
  }
}

function renderBatchProgress() {
  const b = state.batchState;
  const mini = document.getElementById("batch-progress-mini");
  const label = document.getElementById("batch-progress-label");
  const fill = document.getElementById("batch-progress-fill");

  const showMini = b && b.status && b.status !== "idle";
  mini.classList.toggle("hidden", !showMini);
  if (showMini) {
    const total = Math.max(1, b.total_sheets || 1);
    const pct = Math.min(100, Math.round(((b.current_sheet || 0) / total) * 100));
    fill.style.width = `${b.status === "completed" ? 100 : pct}%`;

    const statusWords = {
      printing: `Printing ${b.current_file_index}/${b.total_files} · Sheet ${b.current_sheet}/${b.total_sheets}`,
      cancelling: "Cancelling remaining jobs...",
      completed: `Completed ${b.total_files} file(s) (${b.total_sheets} sheets)`,
      partial: "Batch completed with some failed/cancelled files",
      failed: "Batch print failed",
      cancelled: "Batch cancelled",
    };
    label.textContent = statusWords[b.status] || b.status;
  }

  // Update modal list if visible
  const listEl = document.getElementById("batch-files-list");
  if (listEl) {
    listEl.innerHTML = "";
    for (const f of b.files || []) {
      const card = document.createElement("div");
      card.className = "list-card";
      card.innerHTML = `
        <div class="list-card-main">
          <div class="list-card-title">${escapeHtml(f.name)}</div>
          <div class="list-card-sub">${f.current_sheet}/${f.total_sheets} sheets${f.error ? ` — ${escapeHtml(f.error)}` : ""}</div>
        </div>
        <span class="mini-tag ${f.status === "failed" ? "err" : f.status === "printing" ? "override" : ""}">
          ${escapeHtml(f.status)}
        </span>
      `;
      listEl.appendChild(card);
    }
    const isPrinting = ["printing", "cancelling"].includes(b.status);
    const hasFailed = (b.files || []).some((f) => f.status === "failed");
    document.getElementById("btn-modal-cancel-batch").classList.toggle("hidden", !isPrinting);
    document.getElementById("btn-modal-retry-batch").classList.toggle("hidden", isPrinting || !hasFailed);
  }
  updateFooterCTA();
}

// --- Drag & Drop (Files + Folders via webkitGetAsEntry) ---

async function collectDroppedFiles(dataTransfer) {
  const items = dataTransfer.items ? Array.from(dataTransfer.items) : [];
  const entries = items
    .map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null))
    .filter(Boolean);

  if (!entries.length) {
    return Array.from(dataTransfer.files || []).filter((f) => SUPPORTED_RE.test(f.name));
  }

  const collected = [];
  async function walkEntry(entry) {
    if (entry.isFile) {
      await new Promise((resolve) => {
        entry.file(
          (file) => {
            if (!file.name.startsWith("~$") && SUPPORTED_RE.test(file.name)) {
              collected.push(file);
            }
            resolve();
          },
          () => resolve()
        );
      });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const readAll = async () => {
        const batch = await new Promise((res) => reader.readEntries(res, () => res([])));
        if (!batch.length) return;
        for (const child of batch) {
          await walkEntry(child);
        }
        await readAll();
      };
      await readAll();
    }
  }

  for (const entry of entries) {
    await walkEntry(entry);
  }
  return collected;
}

function setupGlobalDropzone() {
  const overlay = document.getElementById("global-drop-overlay");
  let dragDepth = 0;

  const isExternalFileDrag = (e) => {
    if (dragQueueId) return false;
    const types = e.dataTransfer?.types ? Array.from(e.dataTransfer.types) : [];
    return types.includes("Files");
  };

  window.addEventListener("dragenter", (e) => {
    if (!isExternalFileDrag(e)) return;
    e.preventDefault();
    dragDepth++;
    overlay.classList.remove("hidden");
  });

  window.addEventListener("dragover", (e) => {
    if (!isExternalFileDrag(e)) return;
    e.preventDefault();
  });

  window.addEventListener("dragleave", (e) => {
    if (!isExternalFileDrag(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) overlay.classList.add("hidden");
  });

  window.addEventListener("drop", async (e) => {
    if (!isExternalFileDrag(e)) return;
    e.preventDefault();
    dragDepth = 0;
    overlay.classList.add("hidden");
    try {
      const files = await collectDroppedFiles(e.dataTransfer);
      if (!files.length) {
        toast("No supported PDF, Word, or Image files found in drop", "error");
        return;
      }
      const res = await api.uploadFiles(files);
      toast(`Added ${res.added.length} file(s)`, "ok");
    } catch (err) {
      toast(`Upload failed: ${err.message}`, "error");
    }
  });
}

// --- Modals Actions ---

function openUnlockModal(item) {
  unlockTargetItem = item;
  document.getElementById("password-modal-filename").textContent = item.name;
  document.getElementById("inp-pdf-password").value = "";
  document.getElementById("err-pdf-password").classList.add("hidden");
  openModal("modal-password");
  setTimeout(() => document.getElementById("inp-pdf-password").focus(), 50);
}

async function applyPreset(preset, scope = "global") {
  const next = structuredClone(preset.settings);
  if (!next.printer_name) next.printer_name = state.globalSettings.printer_name;

  if (scope === "file") {
    const item = getSelectedItem();
    if (!item) return;
    await settingsCtrl.ensureCapabilities(next.printer_name);
    await api.updateQueueItem(item.id, { use_override: true, override_settings: next });
    settingsCtrl.activeTab = "file";
    settingsCtrl.render();
    previewCtrl.refreshNow();
    toast(`Applied "${preset.name}" to ${item.name}`, "ok");
    return;
  }

  state.globalSettings = next;
  await settingsCtrl.ensureCapabilities(next.printer_name);
  await api.saveGlobalSettings(next);
  settingsCtrl.activeTab = "global";
  settingsCtrl.render();
  refreshHeaderPrinter();
  previewCtrl.refreshNow();
  toast(`Applied preset "${preset.name}"`, "ok");
}

async function renderPresetsModal() {
  state.presets = await api.listPresets();
  settingsCtrl?.render();
  const list = document.getElementById("presets-list");
  list.innerHTML = "";
  if (!state.presets.length) {
    list.innerHTML = `<div class="muted">No saved presets yet.</div>`;
    return;
  }
  for (const p of state.presets) {
    const card = document.createElement("div");
    card.className = "list-card";
    const s = p.settings;
    const summary = `${s.color_mode === "grayscale" ? "B&W" : "Color"} · ${s.duplex} · ${s.pages_per_sheet}-up · ${s.quality_dpi} DPI${
      s.has_custom_driver_settings ? " · Custom Driver" : ""
    }`;
    card.innerHTML = `
      <div class="list-card-main">
        <div class="list-card-title">${escapeHtml(p.name)}</div>
        <div class="list-card-sub">${escapeHtml(p.description || summary)}</div>
      </div>
      <div class="list-card-actions">
        <button class="btn-sm primary-subtle apply-global">Apply Global</button>
        <button class="btn-sm apply-file" ${getSelectedItem() ? "" : "disabled"}>Apply File</button>
        <button class="btn-sm danger-subtle delete-preset">Delete</button>
      </div>
    `;
    card.querySelector(".apply-global").addEventListener("click", async () => {
      await applyPreset(p, "global");
      closeModals();
    });
    card.querySelector(".apply-file").addEventListener("click", async () => {
      await applyPreset(p, "file");
      closeModals();
    });
    card.querySelector(".delete-preset").addEventListener("click", async () => {
      await api.deletePreset(p.id);
      renderPresetsModal();
    });
    list.appendChild(card);
  }
}

async function renderHistoryModal() {
  const entries = await api.listHistory();
  const list = document.getElementById("history-list");
  list.innerHTML = "";
  if (!entries.length) {
    list.innerHTML = `<div class="muted">No print history recorded yet.</div>`;
    return;
  }
  for (const h of entries) {
    const dt = new Date(h.printed_at).toLocaleString();
    const names = h.files.map((f) => f.name).slice(0, 3).join(", ");
    const more = h.files.length > 3 ? ` +${h.files.length - 3} more` : "";
    const card = document.createElement("div");
    card.className = "list-card";
    card.innerHTML = `
      <div class="list-card-main">
        <div class="list-card-title">${escapeHtml(h.printer_name)} · ${h.total_files} file(s), ${h.total_sheets} sheet(s)</div>
        <div class="list-card-sub">${escapeHtml(dt)} · ${escapeHtml(h.status.toUpperCase())} · ${escapeHtml(names + more)}</div>
      </div>
      <div class="list-card-actions">
        <button class="btn-sm primary-subtle reprint-btn">Reprint</button>
        <button class="btn-sm danger-subtle delete-btn">Delete</button>
      </div>
    `;
    card.querySelector(".reprint-btn").addEventListener("click", async () => {
      try {
        const res = await api.reprintHistory(h.id);
        state.globalSettings = res.global_settings;
        state.mergeMode = res.merge_mode;
        updateMergeToggleUI();
        await settingsCtrl.ensureCapabilities(state.globalSettings.printer_name);
        settingsCtrl.render();
        refreshHeaderPrinter();
        closeModals();
        toast(
          `Restored ${res.restored_count} file(s) from history${
            res.missing_count ? ` (${res.missing_count} missing on disk)` : ""
          }`,
          "ok"
        );
      } catch (err) {
        toast(err.message, "error");
      }
    });
    card.querySelector(".delete-btn").addEventListener("click", async () => {
      await api.deleteHistory(h.id);
      renderHistoryModal();
    });
    list.appendChild(card);
  }
}

async function renderSpoolerModal() {
  const printerName = state.globalSettings.printer_name;
  document.getElementById("spooler-modal-title").textContent = `Windows Queue — ${printerName}`;
  const list = document.getElementById("spooler-jobs-list");
  list.innerHTML = `<div class="muted">Loading Windows spooler jobs...</div>`;
  try {
    const jobs = await api.getSpoolerJobs(printerName);
    list.innerHTML = "";
    if (!jobs.length) {
      list.innerHTML = `<div class="muted">No active jobs in Windows spooler for this printer.</div>`;
      return;
    }
    for (const j of jobs) {
      const card = document.createElement("div");
      card.className = "list-card";
      card.innerHTML = `
        <div class="list-card-main">
          <div class="list-card-title">#${j.job_id} · ${escapeHtml(j.document)}</div>
          <div class="list-card-sub">${escapeHtml(j.status)} · ${j.pages_printed}/${j.total_pages} pages · ${escapeHtml(j.user)}</div>
        </div>
        <button class="btn-sm danger-subtle">Cancel Job</button>
      `;
      card.querySelector("button").addEventListener("click", async () => {
        try {
          await api.cancelSpoolerJob(printerName, j.job_id);
          renderSpoolerModal();
        } catch (err) {
          toast(err.message, "error");
        }
      });
      list.appendChild(card);
    }
  } catch (err) {
    list.innerHTML = `<div class="field-error">${escapeHtml(err.message)}</div>`;
  }
}

async function renderIntegrationModal() {
  const st = await api.getIntegrationStatus();
  state.integration = st;
  document.getElementById("chk-int-desktop").checked = Boolean(st.desktop_shortcut);
  document.getElementById("chk-int-sendto").checked = Boolean(st.sendto_shortcut);
  document.getElementById("chk-int-context").checked = Boolean(st.explorer_context_menu);
}

function updateMergeToggleUI() {
  document.getElementById("btn-mode-separate").classList.toggle("active", !state.mergeMode);
  document.getElementById("btn-mode-merge").classList.toggle("active", state.mergeMode);
  if (!state.mergeMode) state.previewScope = "file";
}

// --- Bootstrap & Event Wiring ---

async function init() {
  const boot = await api.bootstrap();
  state.printers = boot.printers || [];
  state.wordEngine = boot.word_engine || { engine: "none", available: true, label: "" };
  state.globalSettings = boot.global_settings || {};
  if (boot.capabilities && state.globalSettings.printer_name) {
    state.capabilitiesCache[state.globalSettings.printer_name] = boot.capabilities;
  }
  state.queue = boot.queue || [];
  state.presets = boot.presets || [];
  state.batchState = boot.batch_state || { status: "idle", files: [] };
  state.integration = boot.integration || {};
  state.mergeMode = Boolean(boot.prefs?.merge_mode);
  applyTheme(boot.prefs?.theme || "system");

  previewCtrl = new PreviewController(state, {
    getSelectedItem,
    getEffectiveSettingsForPreview,
    openUnlockModal,
    toast,
    setPageRangeError: (msg) => settingsCtrl?.setPageRangeError(msg),
    onPlanUpdated: (plan) => {
      state.lastPlan = plan;
      updateFooterCTA();
    },
    updateSkippedPages: async (itemId, pages) => {
      try {
        await api.updateQueueItem(itemId, { skipped_pages: pages });
      } catch (err) {
        toast(err.message, "error");
      }
    },
  });

  settingsCtrl = new SettingsController(state, {
    getSelectedItem,
    toast,
    refreshHeaderPrinter,
    applyPreset,
    openPresetsModal: () => {
      openModal("modal-presets");
      renderPresetsModal();
    },
    refreshPrinters: async () => {
      state.printers = await api.listPrinters();
      settingsCtrl.render();
      refreshHeaderPrinter();
      toast("Printer list refreshed", "ok");
    },
    updateItemSettings: async (itemId, useOverride, overrideSettings, quiet = false) => {
      await api.updateQueueItem(itemId, {
        use_override: useOverride,
        override_settings: overrideSettings,
      });
      if (!quiet) previewCtrl.scheduleRefresh(40);
    },
    onSettingsChanged: () => previewCtrl.scheduleRefresh(120),
  });

  updateMergeToggleUI();
  refreshHeaderPrinter();
  renderQueue();
  settingsCtrl.render();
  previewCtrl.refreshNow();
  renderBatchProgress();
  setupGlobalDropzone();

  // Header buttons
  document.getElementById("btn-theme-toggle").addEventListener("click", cycleTheme);

  const handleModeClick = (merge) => {
    state.mergeMode = merge;
    state.previewScope = merge ? "merged" : "file";
    updateMergeToggleUI();
    api.savePrefs({ merge_mode: merge }).catch(() => {});
    previewCtrl.currentSheetIndex = 0;
    previewCtrl.refreshNow();
    updateFooterCTA();
  };
  document.getElementById("btn-mode-separate").addEventListener("click", () => handleModeClick(false));
  document.getElementById("btn-mode-merge").addEventListener("click", () => handleModeClick(true));

  // Add files / folders
  const pickFiles = () => api.pickFiles().catch((err) => toast(err.message, "error"));
  const pickFolder = () => api.pickFolder().catch((err) => toast(err.message, "error"));
  document.getElementById("btn-add-files").addEventListener("click", pickFiles);
  document.getElementById("btn-empty-add-files").addEventListener("click", pickFiles);
  document.getElementById("queue-drop-zone").addEventListener("click", pickFiles);
  document.getElementById("btn-add-folder").addEventListener("click", pickFolder);
  document.getElementById("btn-empty-add-folder").addEventListener("click", pickFolder);

  document.getElementById("btn-clear-queue").addEventListener("click", async () => {
    if (!state.queue.length) return;
    await api.clearQueue();
  });

  document.getElementById("chk-queue-all")?.addEventListener("change", async (e) => {
    try {
      await api.bulkEnableQueue(e.target.checked);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  document.getElementById("btn-queue-sort")?.addEventListener("click", async () => {
    const cycle = ["name_asc", "name_desc", "type"];
    state.sortMode = cycle[(cycle.indexOf(state.sortMode) + 1) % cycle.length];
    try {
      await api.sortQueue(state.sortMode);
      renderQueueToolbar();
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // Print CTA with pre-print guardrail
  const executePrint = async () => {
    try {
      await api.startPrint({
        merge_mode: state.mergeMode,
        global_settings: state.globalSettings,
      });
    } catch (err) {
      toast(`Print failed: ${err.message}`, "error");
    }
  };

  const triggerPrint = () => {
    const readyEnabled = state.queue.filter((q) => q.enabled && q.status === "ready");
    if (!readyEnabled.length || !state.globalSettings.printer_name) return;

    const printer = state.printers.find((p) => p.name === state.globalSettings.printer_name);
    const copies = Math.max(1, state.globalSettings.copies || 1);
    const baseSheets = state.lastPlan?.total_batch_sheets || 0;
    const totalPhysicalSheets = baseSheets * copies;

    const warnings = [];
    if (printer && printer.status_severity === "error") {
      warnings.push(`Printer "${printer.name}" currently reports "${printer.status_label}".`);
    }
    if (totalPhysicalSheets > 50) {
      warnings.push(`This batch will print ${totalPhysicalSheets} total sheets (${baseSheets} sheets × ${copies} copies).`);
    }

    if (warnings.length) {
      document.getElementById("confirm-print-title").textContent =
        printer?.status_severity === "error" ? "Printer Offline or Error" : "Large Print Batch";
      document.getElementById("confirm-print-message").textContent = warnings.join(" ");
      document.getElementById("confirm-print-meta").innerHTML = `
        <div><strong>Printer:</strong> ${escapeHtml(state.globalSettings.printer_name)}</div>
        <div><strong>Files:</strong> ${readyEnabled.length} (${state.mergeMode ? "Merged 1 job" : "Separate jobs"})</div>
        <div><strong>Output:</strong> ${totalPhysicalSheets || baseSheets} sheet(s)</div>
      `;
      openModal("modal-confirm-print");
      return;
    }

    executePrint();
  };

  document.getElementById("btn-confirm-print-go")?.addEventListener("click", () => {
    closeModals();
    executePrint();
  });
  document.getElementById("btn-print-now").addEventListener("click", triggerPrint);
  document.getElementById("btn-cancel-print").addEventListener("click", () => api.cancelPrint());
  document.getElementById("btn-modal-cancel-batch").addEventListener("click", () => api.cancelPrint());
  document.getElementById("btn-retry-failed").addEventListener("click", () =>
    api.retryPrint().catch((e) => toast(e.message, "error"))
  );
  document.getElementById("btn-modal-retry-batch").addEventListener("click", () =>
    api.retryPrint().catch((e) => toast(e.message, "error"))
  );

  // Modals wiring
  document.getElementById("modal-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "modal-backdrop" || e.target.hasAttribute("data-close")) {
      closeModals();
    }
  });
  document.getElementById("btn-open-presets").addEventListener("click", () => {
    openModal("modal-presets");
    renderPresetsModal();
  });
  document.getElementById("btn-save-preset").addEventListener("click", async () => {
    const nameEl = document.getElementById("inp-preset-name");
    const descEl = document.getElementById("inp-preset-desc");
    const name = nameEl.value.trim();
    if (!name) return toast("Enter a preset name", "error");
    await api.createPreset({
      name,
      description: descEl.value.trim(),
      settings: state.globalSettings,
    });
    nameEl.value = "";
    descEl.value = "";
    renderPresetsModal();
    toast(`Preset "${name}" saved`, "ok");
  });

  document.getElementById("btn-open-history").addEventListener("click", () => {
    openModal("modal-history");
    renderHistoryModal();
  });
  document.getElementById("btn-clear-history").addEventListener("click", async () => {
    await api.clearHistory();
    renderHistoryModal();
  });

  document.getElementById("btn-open-spooler").addEventListener("click", () => {
    openModal("modal-spooler");
    renderSpoolerModal();
  });
  document.getElementById("btn-refresh-spooler").addEventListener("click", renderSpoolerModal);

  document.getElementById("btn-open-batch-modal").addEventListener("click", () => {
    openModal("modal-batch");
    renderBatchProgress();
  });

  document.getElementById("btn-open-integration").addEventListener("click", () => {
    openModal("modal-integration");
    renderIntegrationModal();
  });
  document.getElementById("btn-install-integration").addEventListener("click", async () => {
    await api.installIntegration({
      desktop: document.getElementById("chk-int-desktop").checked,
      sendto: document.getElementById("chk-int-sendto").checked,
      context_menu: document.getElementById("chk-int-context").checked,
    });
    toast("Windows shortcuts & Explorer integration updated", "ok");
    closeModals();
  });
  document.getElementById("btn-uninstall-integration").addEventListener("click", async () => {
    await api.uninstallIntegration({
      desktop: document.getElementById("chk-int-desktop").checked,
      sendto: document.getElementById("chk-int-sendto").checked,
      context_menu: document.getElementById("chk-int-context").checked,
    });
    await renderIntegrationModal();
    toast("Selected integrations removed", "ok");
  });

  // Unlock PDF password submit
  const submitPw = async () => {
    if (!unlockTargetItem) return;
    const pw = document.getElementById("inp-pdf-password").value;
    try {
      await api.unlockPdf(unlockTargetItem.id, pw);
      closeModals();
      toast(`Unlocked ${unlockTargetItem.name}`, "ok");
    } catch (err) {
      const errEl = document.getElementById("err-pdf-password");
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
    }
  };
  document.getElementById("btn-submit-password").addEventListener("click", submitPw);
  document.getElementById("inp-pdf-password").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitPw();
  });

  // Keyboard shortcuts
  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "p") {
      e.preventDefault();
      if (!document.getElementById("btn-print-now").disabled) triggerPrint();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "o") {
      e.preventDefault();
      pickFiles();
      return;
    }
    if (e.key === "Escape") {
      closeModals();
      return;
    }
    if (!document.getElementById("modal-backdrop").classList.contains("hidden")) return;
    const tag = document.activeElement?.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

    if (e.key === "ArrowLeft") {
      previewCtrl.goToSheet(previewCtrl.currentSheetIndex - 1);
    } else if (e.key === "ArrowRight") {
      previewCtrl.goToSheet(previewCtrl.currentSheetIndex + 1);
    } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      if (!state.queue.length) return;
      e.preventDefault();
      const idx = state.queue.findIndex((q) => q.id === state.selectedItemId);
      const nextIdx =
        e.key === "ArrowDown"
          ? Math.min(state.queue.length - 1, idx + 1)
          : Math.max(0, idx - 1);
      if (nextIdx !== idx && state.queue[nextIdx]) {
        state.selectedItemId = state.queue[nextIdx].id;
        previewCtrl.currentSheetIndex = 0;
        renderQueue();
        settingsCtrl.render();
        previewCtrl.refreshNow();
      }
    } else if (e.key === "Delete") {
      const item = getSelectedItem();
      if (item) {
        api.deleteQueueItem(item.id).catch((err) => toast(err.message, "error"));
      }
    }
  });

  // Live WebSocket updates
  connectWebSocket((event, data) => {
    if (event === "queue_updated") {
      state.queue = data || [];
      renderQueue();
      settingsCtrl.render();
      previewCtrl.refreshNow();
    } else if (event === "printers_updated") {
      state.printers = data || [];
      settingsCtrl.render();
      refreshHeaderPrinter();
    } else if (event === "batch_updated") {
      state.batchState = data || { status: "idle", files: [] };
      renderBatchProgress();
      if (state.batchState.status === "completed") {
        toast("Print batch sent to printer!", "ok");
      } else if (state.batchState.status === "failed") {
        toast("Print batch failed — check details", "error");
      }
    }
  });
}

init().catch((err) => {
  toast(`Failed to initialize app: ${err.message}`, "error");
});
