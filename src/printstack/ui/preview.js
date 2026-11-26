/** Interactive print preview controller (cached sheet stage, Ctrl+Wheel zoom, drag pan, jump/skip thumbnails). */

import { api } from "./api.js";

export class PreviewController {
  constructor(state, callbacks) {
    this.state = state;
    this.callbacks = callbacks;
    this.currentSheetIndex = 0;
    this.zoomMode = "fit"; // "fit" or number (0.35 .. 2.4)
    this.plan = null;
    this._requestSeq = 0;
    this._sheetLoadSeq = 0;
    this._refreshTimer = null;
    this._sheetCache = new Map(); // cacheKey -> objectURL
    this._sheetCacheLimit = 30;
    this._panState = null;

    this.els = {
      scopeSwitch: document.getElementById("preview-scope-switch"),
      btnPrev: document.getElementById("btn-prev-sheet"),
      btnNext: document.getElementById("btn-next-sheet"),
      inpSheet: document.getElementById("input-sheet-num"),
      lblTotal: document.getElementById("label-total-sheets"),
      metaPill: document.getElementById("preview-meta-pill"),
      updatingPill: document.getElementById("preview-updating-pill"),
      btnZoomOut: document.getElementById("btn-zoom-out"),
      btnZoomIn: document.getElementById("btn-zoom-in"),
      btnZoomFit: document.getElementById("btn-zoom-fit"),
      lblZoom: document.getElementById("label-zoom"),
      btnOpenOrig: document.getElementById("btn-open-original"),
      stage: document.getElementById("preview-stage"),
      emptyCard: document.getElementById("preview-empty"),
      stateCard: document.getElementById("preview-state-card"),
      spinner: document.getElementById("preview-spinner"),
      stateTitle: document.getElementById("preview-state-title"),
      stateDesc: document.getElementById("preview-state-desc"),
      btnUnlock: document.getElementById("btn-preview-unlock"),
      sheetWrapper: document.getElementById("sheet-wrapper"),
      sheetImg: document.getElementById("preview-sheet-img"),
      thumbWrap: document.getElementById("thumb-strip-wrap"),
      thumbStrip: document.getElementById("thumb-strip"),
      thumbTitle: document.getElementById("thumb-strip-title"),
      btnIncludeAll: document.getElementById("btn-include-all-pages"),
    };

    this._bindEvents();
  }

  _bindEvents() {
    this.els.scopeSwitch.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-scope]");
      if (!btn) return;
      this.state.previewScope = btn.dataset.scope;
      this.currentSheetIndex = 0;
      this.refreshNow();
    });

    this.els.btnPrev.addEventListener("click", () => this.goToSheet(this.currentSheetIndex - 1));
    this.els.btnNext.addEventListener("click", () => this.goToSheet(this.currentSheetIndex + 1));
    this.els.inpSheet.addEventListener("change", () => {
      const n = parseInt(this.els.inpSheet.value, 10);
      if (!Number.isNaN(n)) this.goToSheet(n - 1);
    });

    this.els.btnZoomOut.addEventListener("click", () => this.stepZoom(-0.15));
    this.els.btnZoomIn.addEventListener("click", () => this.stepZoom(0.15));
    this.els.btnZoomFit.addEventListener("click", () => {
      this.zoomMode = "fit";
      this._applyZoomSizing();
    });

    // Ctrl + Wheel zoom on stage
    this.els.stage.addEventListener(
      "wheel",
      (e) => {
        if (!e.ctrlKey || this.els.sheetWrapper.classList.contains("hidden")) return;
        e.preventDefault();
        this.stepZoom(e.deltaY < 0 ? 0.15 : -0.15);
      },
      { passive: false }
    );

    // Drag to pan when zoomed beyond stage bounds
    this.els.stage.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || !this._isPannable()) return;
      if (e.target.closest("button, input, a")) return;
      this._panState = {
        startX: e.clientX,
        startY: e.clientY,
        scrollLeft: this.els.stage.scrollLeft,
        scrollTop: this.els.stage.scrollTop,
      };
      this.els.stage.classList.add("is-panning");
    });

    window.addEventListener("mousemove", (e) => {
      if (!this._panState) return;
      const dx = e.clientX - this._panState.startX;
      const dy = e.clientY - this._panState.startY;
      this.els.stage.scrollLeft = this._panState.scrollLeft - dx;
      this.els.stage.scrollTop = this._panState.scrollTop - dy;
    });

    window.addEventListener("mouseup", () => {
      if (!this._panState) return;
      this._panState = null;
      this.els.stage.classList.remove("is-panning");
    });

    this.els.btnUnlock.addEventListener("click", () => {
      const item = this.callbacks.getSelectedItem();
      if (item) this.callbacks.openUnlockModal(item);
    });

    this.els.btnOpenOrig.addEventListener("click", () => {
      const item = this.callbacks.getSelectedItem();
      if (item) {
        api.openOriginal(item.id).catch((err) => this.callbacks.toast(err.message, "error"));
      }
    });

    this.els.btnIncludeAll.addEventListener("click", () => {
      const item = this.callbacks.getSelectedItem();
      if (!item || !item.skipped_pages.length) return;
      this.callbacks.updateSkippedPages(item.id, []);
    });

    this.els.sheetImg.addEventListener("load", () => {
      this.els.sheetImg.classList.remove("loading");
      this._applyZoomSizing();
    });

    window.addEventListener("resize", () => {
      if (this.zoomMode === "fit") this._applyZoomSizing();
      this._updatePanCursor();
    });
  }

  _isPannable() {
    const st = this.els.stage;
    return st.scrollWidth > st.clientWidth + 2 || st.scrollHeight > st.clientHeight + 2;
  }

  _updatePanCursor() {
    this.els.stage.classList.toggle("can-pan", this._isPannable());
  }

  stepZoom(delta) {
    if (this.zoomMode === "fit") {
      this.zoomMode = Math.min(2.4, Math.max(0.35, Number((1.0 + delta).toFixed(2))));
    } else {
      this.zoomMode = Math.min(2.4, Math.max(0.35, Number((this.zoomMode + delta).toFixed(2))));
    }
    this._applyZoomSizing();
  }

  _applyZoomSizing() {
    const img = this.els.sheetImg;
    if (!img || !img.naturalWidth || !img.naturalHeight) {
      this.els.lblZoom.textContent = this.zoomMode === "fit" ? "Fit" : `${Math.round(this.zoomMode * 100)}%`;
      this._updatePanCursor();
      return;
    }
    if (this.zoomMode === "fit") {
      const availW = Math.max(200, this.els.stage.clientWidth - 44);
      const availH = Math.max(200, this.els.stage.clientHeight - 44);
      const ratio = Math.min(availW / img.naturalWidth, availH / img.naturalHeight, 1.8);
      img.style.width = `${Math.round(img.naturalWidth * ratio)}px`;
      img.style.height = `${Math.round(img.naturalHeight * ratio)}px`;
      this.els.lblZoom.textContent = "Fit";
    } else {
      const baseScale = 96 / 110;
      const w = Math.round(img.naturalWidth * baseScale * this.zoomMode);
      const h = Math.round(img.naturalHeight * baseScale * this.zoomMode);
      img.style.width = `${w}px`;
      img.style.height = `${h}px`;
      this.els.lblZoom.textContent = `${Math.round(this.zoomMode * 100)}%`;
    }
    this._updatePanCursor();
  }

  goToSheet(idx) {
    const total = this.plan ? this.plan.sheet_count : 0;
    if (total <= 0) return;
    this.currentSheetIndex = Math.max(0, Math.min(total - 1, idx));
    this._renderCurrentSheet();
  }

  jumpToPage(itemId, pageNumber) {
    if (!this.plan || !this.plan.sheets) return false;
    const sheetIdx = this.plan.sheets.findIndex((sheet) =>
      sheet.some((slot) => slot.item_id === itemId && slot.page_number === pageNumber)
    );
    if (sheetIdx >= 0) {
      this.goToSheet(sheetIdx);
      return true;
    }
    return false;
  }

  scheduleRefresh(delay = 120) {
    clearTimeout(this._refreshTimer);
    this._refreshTimer = setTimeout(() => this.refreshNow(), delay);
  }

  _buildQueryParams(effectiveSettings, item) {
    const scope = this.state.mergeMode && this.state.previewScope === "merged" ? "merged" : "file";
    const params = {
      scope,
      color_mode: effectiveSettings.color_mode,
      orientation: effectiveSettings.orientation,
      scale_mode: effectiveSettings.scale_mode,
      scale_percent: String(effectiveSettings.scale_percent),
      pages_per_sheet: String(effectiveSettings.pages_per_sheet),
      nup_order: effectiveSettings.nup_order,
      nup_border: String(effectiveSettings.nup_border),
      page_range_mode: effectiveSettings.page_range_mode,
      page_range_custom: effectiveSettings.page_range_custom || "",
      paper_width_mm: String(effectiveSettings.paper_width_mm || 210),
      paper_height_mm: String(effectiveSettings.paper_height_mm || 297),
    };
    if (item && scope === "file") {
      params.item_id = item.id;
    }
    return params;
  }

  _buildSheetCacheKey(params, sheetIdx) {
    const slotSig = (this.plan?.sheets?.[sheetIdx] || [])
      .map((s) => {
        const q = (this.state.queue || []).find((x) => x.id === s.item_id);
        return `${s.item_id}@${q?.layout_rev || 0}:${s.page_number}`;
      })
      .join(",");
    return `${new URLSearchParams(params).toString()}|idx=${sheetIdx}|slots=${slotSig}`;
  }

  async _loadCachedSheetUrl(params, sheetIdx) {
    const cacheKey = this._buildSheetCacheKey(params, sheetIdx);
    const cached = this._sheetCache.get(cacheKey);
    if (cached) {
      // Refresh LRU position
      this._sheetCache.delete(cacheKey);
      this._sheetCache.set(cacheKey, cached);
      return cached;
    }

    const qs = new URLSearchParams({
      ...params,
      sheet_index: String(sheetIdx),
      dpi: "110",
    }).toString();

    const resp = await fetch(`/api/preview/sheet?${qs}`);
    if (!resp.ok) {
      throw new Error(`Preview failed (${resp.status})`);
    }
    const blob = await resp.blob();
    const objUrl = URL.createObjectURL(blob);

    this._sheetCache.set(cacheKey, objUrl);
    while (this._sheetCache.size > this._sheetCacheLimit) {
      const oldestKey = this._sheetCache.keys().next().value;
      const oldestUrl = this._sheetCache.get(oldestKey);
      this._sheetCache.delete(oldestKey);
      if (oldestUrl) URL.revokeObjectURL(oldestUrl);
    }
    return objUrl;
  }

  async refreshNow() {
    const seq = ++this._requestSeq;
    const item = this.callbacks.getSelectedItem();

    this.els.scopeSwitch.classList.toggle("hidden", !this.state.mergeMode);
    for (const btn of this.els.scopeSwitch.querySelectorAll("[data-scope]")) {
      btn.classList.toggle("active", btn.dataset.scope === this.state.previewScope);
    }

    this.els.btnOpenOrig.classList.toggle("hidden", !item);
    const isUpdatingLayout = Boolean(item && item.is_updating_layout);
    this.els.updatingPill?.classList.toggle("hidden", !isUpdatingLayout);
    if (isUpdatingLayout) {
      this.els.sheetImg.classList.add("loading");
    }

    if (!this.state.queue.length) {
      this._showEmpty();
      this.callbacks.onPlanUpdated(null);
      return;
    }

    if (this.state.previewScope === "file" && item) {
      if (item.status === "converting") {
        const engineLabel =
          item.file_type === "excel"
            ? this.state.excelEngine?.label || "Excel"
            : this.state.wordEngine?.label || "document";
        this._showStateCard(true, "Converting document...", `Preparing ${item.name} via ${engineLabel} engine.`, false);
        this.els.thumbWrap.classList.add("hidden");
        return;
      }
      if (item.status === "password_required") {
        this._showStateCard(false, "PDF is password protected", `${item.name} requires a password to preview and print.`, true);
        this.els.thumbWrap.classList.add("hidden");
        return;
      }
      if (item.status === "error") {
        this._showStateCard(false, "Unable to load document", item.error_message || "Failed to process file.", false);
        this.els.thumbWrap.classList.add("hidden");
        return;
      }
    }

    const eff = this.callbacks.getEffectiveSettingsForPreview(item);
    const params = this._buildQueryParams(eff, item);
    try {
      const plan = await api.getPreviewPlan(params);
      if (seq !== this._requestSeq) return;
      this.plan = plan;
      this.callbacks.onPlanUpdated(plan);

      this.callbacks.setPageRangeError(plan.error || "");

      if (plan.sheet_count === 0) {
        this._showStateCard(false, "No pages selected", plan.error || "Check your page range or skipped pages.", false);
      } else {
        if (this.currentSheetIndex >= plan.sheet_count) {
          this.currentSheetIndex = plan.sheet_count - 1;
        }
        this._renderCurrentSheet(params, eff);
      }
      this._renderThumbnailStrip(item);
    } catch (err) {
      if (seq !== this._requestSeq) return;
      this._showStateCard(false, "Preview error", err.message, false);
    }
  }

  _showEmpty() {
    this.els.emptyCard.classList.remove("hidden");
    this.els.stateCard.classList.add("hidden");
    this.els.sheetWrapper.classList.add("hidden");
    this.els.thumbWrap.classList.add("hidden");
    this.els.btnPrev.disabled = true;
    this.els.btnNext.disabled = true;
    this.els.inpSheet.value = 0;
    this.els.lblTotal.textContent = "of 0";
    this.els.metaPill.textContent = "No document selected";
  }

  _showStateCard(showSpinner, title, desc, showUnlock) {
    this.els.emptyCard.classList.add("hidden");
    this.els.sheetWrapper.classList.add("hidden");
    this.els.stateCard.classList.remove("hidden");
    this.els.spinner.classList.toggle("hidden", !showSpinner);
    this.els.stateTitle.textContent = title;
    this.els.stateDesc.textContent = desc;
    this.els.btnUnlock.classList.toggle("hidden", !showUnlock);
    this.els.btnPrev.disabled = true;
    this.els.btnNext.disabled = true;
    this.els.inpSheet.value = 0;
    this.els.lblTotal.textContent = "of 0";
  }

  async _renderCurrentSheet(cachedParams = null, cachedEff = null) {
    const plan = this.plan;
    if (!plan || plan.sheet_count === 0) return;

    const item = this.callbacks.getSelectedItem();
    const eff = cachedEff || this.callbacks.getEffectiveSettingsForPreview(item);
    const params = cachedParams || this._buildQueryParams(eff, item);

    this.els.emptyCard.classList.add("hidden");
    this.els.stateCard.classList.add("hidden");
    this.els.sheetWrapper.classList.remove("hidden");

    const total = plan.sheet_count;
    const idx = Math.max(0, Math.min(total - 1, this.currentSheetIndex));
    this.currentSheetIndex = idx;

    this.els.btnPrev.disabled = idx <= 0;
    this.els.btnNext.disabled = idx >= total - 1;
    this.els.inpSheet.max = String(total);
    this.els.inpSheet.value = String(idx + 1);
    this.els.lblTotal.textContent = `of ${total}`;

    const paperLabel = eff.paper_size_name || `${Math.round(plan.paper_width_mm)}×${Math.round(plan.paper_height_mm)}mm`;
    const orientLabel = plan.resolved_orientation === "landscape" ? "Landscape" : "Portrait";
    const colorLabel = eff.color_mode === "grayscale" ? "B&W" : "Color";
    const nupLabel = eff.pages_per_sheet > 1 ? ` · ${eff.pages_per_sheet}-up` : "";
    this.els.metaPill.textContent = `${paperLabel} · ${orientLabel} · ${colorLabel}${nupLabel}`;

    this._highlightActiveThumbPages();

    const loadSeq = ++this._sheetLoadSeq;
    const cacheKey = this._buildSheetCacheKey(params, idx);
    if (!this._sheetCache.has(cacheKey)) {
      this.els.sheetImg.classList.add("loading");
    }

    try {
      const url = await this._loadCachedSheetUrl(params, idx);
      if (loadSeq !== this._sheetLoadSeq) return;
      if (this.els.sheetImg.src !== url) {
        this.els.sheetImg.src = url;
      } else {
        this.els.sheetImg.classList.remove("loading");
        this._applyZoomSizing();
      }
      // Quietly prefetch next sheet for instant flipping
      if (idx + 1 < total) {
        this._loadCachedSheetUrl(params, idx + 1).catch(() => {});
      }
    } catch (_) {
      if (loadSeq === this._sheetLoadSeq) {
        this.els.sheetImg.classList.remove("loading");
      }
    }
  }

  _renderThumbnailStrip(item) {
    const isMerged = this.state.mergeMode && this.state.previewScope === "merged";
    if (isMerged || !item || item.status !== "ready" || item.page_count <= 0) {
      this.els.thumbWrap.classList.add("hidden");
      return;
    }

    this.els.thumbWrap.classList.remove("hidden");
    const skipped = new Set(item.skipped_pages || []);
    const activePages = this._getActiveSheetPagesForItem(item.id);

    const skippedText = skipped.size ? ` · ${skipped.size} skipped` : "";
    this.els.thumbTitle.textContent = `${item.name} (${item.page_count} pg${item.page_count > 1 ? "s" : ""}${skippedText}) — click page to jump, click ✓/× to skip`;

    const rev = item.layout_rev || 0;
    const sig = `${item.id}:${item.page_count}:${rev}`;
    if (this.els.thumbStrip.dataset.sig !== sig) {
      this.els.thumbStrip.dataset.sig = sig;
      this.els.thumbStrip.innerHTML = "";
      for (let p = 1; p <= item.page_count; p++) {
        const card = document.createElement("div");
        card.className = "thumb-card";
        card.dataset.page = String(p);
        card.innerHTML = `
          <button type="button" class="thumb-skip-pill" title="Toggle skip/include page ${p}">✓</button>
          <img src="/api/preview/thumbnail/${encodeURIComponent(item.id)}/${p - 1}?rev=${rev}" loading="lazy" alt="Page ${p}" />
          <div class="thumb-num">${p}</div>
        `;

        // Click badge -> toggle skip/include
        const pillBtn = card.querySelector(".thumb-skip-pill");
        pillBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const latest = this.callbacks.getSelectedItem();
          if (!latest) return;
          const set = new Set(latest.skipped_pages || []);
          if (set.has(p)) set.delete(p);
          else set.add(p);
          this.callbacks.updateSkippedPages(latest.id, Array.from(set));
        });

        // Click thumbnail card -> jump to sheet containing this page
        card.addEventListener("click", () => {
          const latest = this.callbacks.getSelectedItem();
          if (!latest) return;
          const jumped = this.jumpToPage(latest.id, p);
          if (!jumped) {
            this.callbacks.toast(`Page ${p} is excluded by current page range or skip toggle`, "info");
          }
        });

        this.els.thumbStrip.appendChild(card);
      }
    }

    for (const card of this.els.thumbStrip.querySelectorAll(".thumb-card")) {
      const p = parseInt(card.dataset.page, 10);
      const isSkipped = skipped.has(p);
      card.classList.toggle("skipped", isSkipped);
      card.classList.toggle("active-page", !isSkipped && activePages.has(p));
      const pill = card.querySelector(".thumb-skip-pill");
      if (pill) {
        pill.textContent = isSkipped ? "×" : "✓";
        pill.title = isSkipped ? `Include page ${p}` : `Skip page ${p}`;
      }
    }
  }

  _getActiveSheetPagesForItem(itemId) {
    const pages = new Set();
    if (!this.plan || !this.plan.sheets || !this.plan.sheets[this.currentSheetIndex]) {
      return pages;
    }
    for (const slot of this.plan.sheets[this.currentSheetIndex]) {
      if (slot.item_id === itemId) pages.add(slot.page_number);
    }
    return pages;
  }

  _highlightActiveThumbPages() {
    const item = this.callbacks.getSelectedItem();
    if (!item) return;
    const activePages = this._getActiveSheetPagesForItem(item.id);
    for (const card of this.els.thumbStrip.querySelectorAll(".thumb-card")) {
      const p = parseInt(card.dataset.page, 10);
      card.classList.toggle("active-page", activePages.has(p));
    }
  }
}
