/** REST & WebSocket API client for Printstack. */

export async function apiJson(url, options = {}) {
  const res = await fetch(url, {
    headers: {
      ...(options.body && !(options.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...(options.headers || {}),
    },
    ...options,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (data && data.detail) detail = data.detail;
    } catch (_) {
      // ignore parse failure
    }
    throw new Error(detail);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return res.json();
  }
  return res;
}

export const api = {
  bootstrap: () => apiJson("/api/bootstrap"),
  checkUpdates: () => apiJson("/api/updates/check"),
  listPrinters: () => apiJson("/api/printers"),
  getCapabilities: (name) =>
    apiJson(`/api/printers/${encodeURIComponent(name)}/capabilities`),
  getSpoolerJobs: (name) =>
    apiJson(`/api/printers/${encodeURIComponent(name)}/jobs`),
  cancelSpoolerJob: (name, jobId) =>
    apiJson(`/api/printers/${encodeURIComponent(name)}/jobs/${jobId}`, {
      method: "DELETE",
    }),
  openPrinterProperties: (name, settings) =>
    apiJson(`/api/printers/${encodeURIComponent(name)}/properties`, {
      method: "POST",
      body: JSON.stringify({ settings }),
    }),

  uploadFiles: (files) => {
    const fd = new FormData();
    for (const f of files) {
      fd.append("files", f, f.name);
    }
    return apiJson("/api/queue/upload", { method: "POST", body: fd });
  },
  pickFiles: () => apiJson("/api/dialog/pick-files", { method: "POST" }),
  pickFolder: () => apiJson("/api/dialog/pick-folder", { method: "POST" }),
  reorderQueue: (itemIds) =>
    apiJson("/api/queue/reorder", {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds }),
    }),
  bulkEnableQueue: (enabled) =>
    apiJson("/api/queue/bulk-enable", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  sortQueue: (mode) =>
    apiJson("/api/queue/sort", {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),
  updateQueueItem: (id, patch) =>
    apiJson(`/api/queue/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  unlockPdf: (id, password) =>
    apiJson(`/api/queue/${encodeURIComponent(id)}/unlock`, {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
  openOriginal: (id) =>
    apiJson(`/api/queue/${encodeURIComponent(id)}/open-original`, {
      method: "POST",
    }),
  deleteQueueItem: (id) =>
    apiJson(`/api/queue/${encodeURIComponent(id)}`, { method: "DELETE" }),
  clearQueue: () => apiJson("/api/queue", { method: "DELETE" }),

  getPreviewPlan: (params) =>
    apiJson(`/api/preview/plan?${new URLSearchParams(params).toString()}`),

  saveGlobalSettings: (settings) =>
    apiJson("/api/settings/global", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  savePrefs: (prefs) =>
    apiJson("/api/prefs", {
      method: "PUT",
      body: JSON.stringify(prefs),
    }),

  startPrint: (payload) =>
    apiJson("/api/print/start", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  cancelPrint: () => apiJson("/api/print/cancel", { method: "POST" }),
  retryPrint: () => apiJson("/api/print/retry", { method: "POST" }),

  listPresets: () => apiJson("/api/presets"),
  createPreset: (payload) =>
    apiJson("/api/presets", { method: "POST", body: JSON.stringify(payload) }),
  deletePreset: (id) =>
    apiJson(`/api/presets/${encodeURIComponent(id)}`, { method: "DELETE" }),

  listHistory: () => apiJson("/api/history"),
  reprintHistory: (id) =>
    apiJson(`/api/history/${encodeURIComponent(id)}/reprint`, {
      method: "POST",
    }),
  deleteHistory: (id) =>
    apiJson(`/api/history/${encodeURIComponent(id)}`, { method: "DELETE" }),
  clearHistory: () => apiJson("/api/history", { method: "DELETE" }),

  getIntegrationStatus: () => apiJson("/api/integration/status"),
  installIntegration: (payload) =>
    apiJson("/api/integration/install", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  uninstallIntegration: (payload) =>
    apiJson("/api/integration/uninstall", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};

export function connectWebSocket(onEvent) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws`;
  let ws = null;
  let closed = false;

  function open() {
    if (closed) return;
    ws = new WebSocket(url);
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        onEvent(msg.event, msg.data);
      } catch (_) {
        // ignore invalid messages
      }
    };
    ws.onclose = () => {
      if (!closed) {
        setTimeout(open, 1500);
      }
    };
  }
  open();
  return () => {
    closed = true;
    if (ws) ws.close();
  };
}
