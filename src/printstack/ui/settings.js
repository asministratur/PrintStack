/** Dynamic printer capability & Global / Per-File print settings controller. */

import { api } from "./api.js";

export class SettingsController {
  constructor(state, callbacks) {
    this.state = state;
    this.callbacks = callbacks;
    this.activeTab = "global"; // "global" | "file"
    this._saveTimer = null;
    this._excelSaveTimer = null;

    this.els = {
      tabs: document.getElementById("settings-scope-tabs"),
      tabFile: document.getElementById("tab-file-settings"),
      overrideDot: document.getElementById("tab-override-dot"),
      overrideBanner: document.getElementById("override-banner"),
      chkUseOverride: document.getElementById("chk-use-override"),
      btnResetOverride: document.getElementById("btn-reset-override"),
      form: document.getElementById("settings-form"),

      selPrinter: document.getElementById("sel-printer"),
      selQuickPreset: document.getElementById("sel-quick-preset"),
      btnQuickManagePresets: document.getElementById("btn-quick-manage-presets"),
      advancedSummaryPill: document.getElementById("advanced-summary-pill"),
      btnRefreshPrinters: document.getElementById("btn-refresh-printers"),
      btnPrinterProps: document.getElementById("btn-printer-properties"),
      driverPill: document.getElementById("driver-custom-pill"),
      btnClearDriver: document.getElementById("btn-clear-driver-props"),

      inpCopies: document.getElementById("inp-copies"),
      wrapCollate: document.getElementById("wrap-collate"),
      chkCollate: document.getElementById("chk-collate"),
      segColor: document.getElementById("seg-color"),
      selDuplex: document.getElementById("sel-duplex"),
      selPaperSize: document.getElementById("sel-paper-size"),
      wrapPaperBin: document.getElementById("wrap-paper-bin"),
      selPaperBin: document.getElementById("sel-paper-bin"),
      segOrientation: document.getElementById("seg-orientation"),
      segPages: document.getElementById("seg-pages"),
      inpPageRange: document.getElementById("inp-page-range"),
      errPageRange: document.getElementById("err-page-range"),
      selScale: document.getElementById("sel-scale"),
      wrapScalePct: document.getElementById("wrap-scale-pct"),
      inpScalePct: document.getElementById("inp-scale-pct"),
      selNup: document.getElementById("sel-nup"),
      selQuality: document.getElementById("sel-quality"),
      wrapNupOpts: document.getElementById("wrap-nup-options"),
      selNupOrder: document.getElementById("sel-nup-order"),
      chkNupBorder: document.getElementById("chk-nup-border"),

      // Excel Page Setup controls
      excelCard: document.getElementById("excel-setup-card"),
      excelStatusPill: document.getElementById("excel-status-pill"),
      wrapExcelSheets: document.getElementById("wrap-excel-sheets"),
      btnExcelAllSheets: document.getElementById("btn-excel-all-sheets"),
      excelSheetChips: document.getElementById("excel-sheet-chips"),
      selExcelFit: document.getElementById("sel-excel-fit"),
      segExcelOrient: document.getElementById("seg-excel-orient"),
      selExcelPaper: document.getElementById("sel-excel-paper"),
      selExcelMargins: document.getElementById("sel-excel-margins"),
      chkExcelGridlines: document.getElementById("chk-excel-gridlines"),
      chkExcelHeadings: document.getElementById("chk-excel-headings"),
      chkExcelCenter: document.getElementById("chk-excel-center"),
    };

    this._bindEvents();
  }

  _bindEvents() {
    this.els.tabs.addEventListener("click", (e) => {
      const tab = e.target.closest("[data-tab]");
      if (!tab || tab.classList.contains("disabled")) return;
      this.activeTab = tab.dataset.tab;
      this.render();
      this.callbacks.onSettingsChanged();
    });

    this.els.chkUseOverride.addEventListener("change", async () => {
      const item = this.callbacks.getSelectedItem();
      if (!item) return;
      const enabled = this.els.chkUseOverride.checked;
      const override = enabled
        ? item.override_settings || structuredClone(this.state.globalSettings)
        : item.override_settings;
      await this.callbacks.updateItemSettings(item.id, enabled, override);
      this.render();
    });

    this.els.btnResetOverride.addEventListener("click", async () => {
      const item = this.callbacks.getSelectedItem();
      if (!item) return;
      await this.callbacks.updateItemSettings(item.id, false, null);
      this.render();
    });

    this.els.selQuickPreset?.addEventListener("change", async () => {
      const presetId = this.els.selQuickPreset.value;
      if (!presetId) return;
      const preset = (this.state.presets || []).find((p) => p.id === presetId);
      if (preset && this.callbacks.applyPreset) {
        const targetScope = this.activeTab === "file" ? "file" : "global";
        await this.callbacks.applyPreset(preset, targetScope);
      }
      this.els.selQuickPreset.value = "";
    });

    this.els.btnQuickManagePresets?.addEventListener("click", () => {
      this.callbacks.openPresetsModal?.();
    });

    this.els.selPrinter.addEventListener("change", async () => {
      const newPrinter = this.els.selPrinter.value;
      const s = this.getEditedSettingsObject();
      s.printer_name = newPrinter;
      s.devmode_b64 = null;
      s.has_custom_driver_settings = false;
      await this.ensureCapabilities(newPrinter);
      const caps = this.state.capabilitiesCache[newPrinter];
      if (caps) this._reconcileSettingsWithCaps(s, caps);
      this._commitEditedSettings(s);
    });

    this.els.btnRefreshPrinters.addEventListener("click", () => {
      this.callbacks.refreshPrinters();
    });

    this.els.btnPrinterProps.addEventListener("click", async () => {
      const s = this.getEditedSettingsObject();
      if (!s.printer_name) return;
      const origText = this.els.btnPrinterProps.textContent;
      this.els.btnPrinterProps.disabled = true;
      this.els.btnPrinterProps.textContent = "Waiting for Windows dialog...";
      try {
        const res = await api.openPrinterProperties(s.printer_name, s);
        if (!res.cancelled && res.settings) {
          Object.assign(s, res.settings);
          await this.ensureCapabilities(s.printer_name);
          this._commitEditedSettings(s);
          this.callbacks.toast("Driver properties updated", "ok");
        }
      } catch (err) {
        this.callbacks.toast(`Printer Properties failed: ${err.message}`, "error");
      } finally {
        this.els.btnPrinterProps.disabled = false;
        this.els.btnPrinterProps.textContent = origText;
      }
    });

    this.els.btnClearDriver.addEventListener("click", () => {
      const s = this.getEditedSettingsObject();
      s.devmode_b64 = null;
      s.has_custom_driver_settings = false;
      this._commitEditedSettings(s);
    });

    this.els.inpCopies.addEventListener("input", () => {
      const s = this.getEditedSettingsObject();
      const val = Math.max(1, parseInt(this.els.inpCopies.value, 10) || 1);
      s.copies = val;
      this._commitEditedSettings(s, true);
    });

    this.els.chkCollate.addEventListener("change", () => {
      const s = this.getEditedSettingsObject();
      s.collate = this.els.chkCollate.checked;
      this._commitEditedSettings(s);
    });

    this._bindSegmented(this.els.segColor, (val) => {
      const s = this.getEditedSettingsObject();
      s.color_mode = val;
      this._commitEditedSettings(s);
    });

    this.els.selDuplex.addEventListener("change", () => {
      const s = this.getEditedSettingsObject();
      s.duplex = this.els.selDuplex.value;
      this._commitEditedSettings(s);
    });

    this.els.selPaperSize.addEventListener("change", () => {
      const s = this.getEditedSettingsObject();
      const pid = parseInt(this.els.selPaperSize.value, 10);
      const caps = this.state.capabilitiesCache[s.printer_name];
      const match = caps?.paper_sizes?.find((p) => p.id === pid);
      if (match) {
        s.paper_size_id = match.id;
        s.paper_size_name = match.name;
        s.paper_width_mm = match.width_mm;
        s.paper_height_mm = match.height_mm;
      }
      this._commitEditedSettings(s);
    });

    this.els.selPaperBin.addEventListener("change", () => {
      const s = this.getEditedSettingsObject();
      const bid = parseInt(this.els.selPaperBin.value, 10);
      const caps = this.state.capabilitiesCache[s.printer_name];
      const match = caps?.paper_bins?.find((b) => b.id === bid);
      s.paper_source_id = Number.isNaN(bid) ? null : bid;
      s.paper_source_name = match ? match.name : null;
      this._commitEditedSettings(s);
    });

    this._bindSegmented(this.els.segOrientation, (val) => {
      const s = this.getEditedSettingsObject();
      s.orientation = val;
      this._commitEditedSettings(s);
    });

    this._bindSegmented(this.els.segPages, (val) => {
      const s = this.getEditedSettingsObject();
      s.page_range_mode = val;
      this.els.inpPageRange.classList.toggle("hidden", val !== "custom");
      this._commitEditedSettings(s);
    });

    this.els.inpPageRange.addEventListener("input", () => {
      const s = this.getEditedSettingsObject();
      s.page_range_custom = this.els.inpPageRange.value;
      this._commitEditedSettings(s, true);
    });

    this.els.selScale.addEventListener("change", () => {
      const s = this.getEditedSettingsObject();
      s.scale_mode = this.els.selScale.value;
      this.els.wrapScalePct.classList.toggle("hidden", s.scale_mode !== "custom");
      this._commitEditedSettings(s);
    });

    this.els.inpScalePct.addEventListener("input", () => {
      const s = this.getEditedSettingsObject();
      s.scale_percent = Math.min(400, Math.max(10, parseInt(this.els.inpScalePct.value, 10) || 100));
      this._commitEditedSettings(s, true);
    });

    this.els.selNup.addEventListener("change", () => {
      const s = this.getEditedSettingsObject();
      s.pages_per_sheet = parseInt(this.els.selNup.value, 10);
      this.els.wrapNupOpts.classList.toggle("hidden", s.pages_per_sheet <= 1);
      this._commitEditedSettings(s);
    });

    this.els.selQuality.addEventListener("change", () => {
      const s = this.getEditedSettingsObject();
      s.quality_dpi = parseInt(this.els.selQuality.value, 10);
      this._commitEditedSettings(s);
    });

    this.els.selNupOrder.addEventListener("change", () => {
      const s = this.getEditedSettingsObject();
      s.nup_order = this.els.selNupOrder.value;
      this._commitEditedSettings(s);
    });

    this.els.chkNupBorder.addEventListener("change", () => {
      const s = this.getEditedSettingsObject();
      s.nup_border = this.els.chkNupBorder.checked;
      this._commitEditedSettings(s);
    });

    // --- Seamless Excel Page Setup bindings ---
    this.els.btnExcelAllSheets?.addEventListener("click", () => {
      this._mutateExcelOptions((opts) => {
        opts.sheet_scope = "all";
        opts.selected_sheets = [];
      });
    });

    this.els.selExcelFit?.addEventListener("change", () => {
      this._mutateExcelOptions((opts) => {
        opts.fit_mode = this.els.selExcelFit.value;
      });
    });

    if (this.els.segExcelOrient) {
      this._bindSegmented(this.els.segExcelOrient, (val) => {
        this._mutateExcelOptions((opts) => {
          opts.orientation = val;
        });
      });
    }

    this.els.selExcelPaper?.addEventListener("change", () => {
      this._mutateExcelOptions((opts) => {
        opts.paper_size = this.els.selExcelPaper.value;
      });
    });

    this.els.selExcelMargins?.addEventListener("change", () => {
      this._mutateExcelOptions((opts) => {
        opts.margins = this.els.selExcelMargins.value;
      });
    });

    this.els.chkExcelGridlines?.addEventListener("change", () => {
      this._mutateExcelOptions((opts) => {
        opts.gridlines = this.els.chkExcelGridlines.checked;
      });
    });

    this.els.chkExcelHeadings?.addEventListener("change", () => {
      this._mutateExcelOptions((opts) => {
        opts.headings = this.els.chkExcelHeadings.checked;
      });
    });

    this.els.chkExcelCenter?.addEventListener("change", () => {
      this._mutateExcelOptions((opts) => {
        opts.center_horizontally = this.els.chkExcelCenter.checked;
      });
    });
  }

  _defaultExcelOptions() {
    return {
      sheet_scope: "all",
      selected_sheets: [],
      fit_mode: "fit_columns",
      orientation: "auto",
      paper_size: "auto",
      margins: "default",
      gridlines: false,
      headings: false,
      center_horizontally: true,
    };
  }

  _mutateExcelOptions(mutator) {
    const item = this.callbacks.getSelectedItem();
    if (!item || item.file_type !== "excel") return;
    const opts = structuredClone(item.excel_options || this._defaultExcelOptions());
    mutator(opts);
    item.excel_options = opts;
    this._renderExcelCard(item);

    clearTimeout(this._excelSaveTimer);
    this._excelSaveTimer = setTimeout(() => {
      this.callbacks.updateExcelOptions?.(item.id, opts);
    }, 180);
  }

  _renderExcelCard(item) {
    if (!this.els.excelCard) return;
    const isExcel = Boolean(item && item.file_type === "excel");
    this.els.excelCard.classList.toggle("hidden", !isExcel);
    if (!isExcel) return;

    const opts = item.excel_options || this._defaultExcelOptions();
    const sheets = item.excel_sheets || [];

    if (this.els.excelStatusPill) {
      if (item.is_updating_layout || item.status === "converting") {
        this.els.excelStatusPill.textContent = "Updating...";
        this.els.excelStatusPill.classList.add("updating");
      } else if (opts.sheet_scope === "selected" && opts.selected_sheets?.length) {
        this.els.excelStatusPill.textContent = `${opts.selected_sheets.length}/${sheets.length || opts.selected_sheets.length} sheets`;
        this.els.excelStatusPill.classList.remove("updating");
      } else {
        this.els.excelStatusPill.textContent = sheets.length > 1 ? `All (${sheets.length} sheets)` : "1 sheet";
        this.els.excelStatusPill.classList.remove("updating");
      }
    }

    // Worksheets chip strip (show when > 1 sheet)
    if (this.els.wrapExcelSheets && this.els.excelSheetChips) {
      this.els.wrapExcelSheets.classList.toggle("hidden", sheets.length <= 1);
      if (sheets.length > 1) {
        const selectedSet = new Set(opts.selected_sheets || []);
        const isAll = opts.sheet_scope !== "selected" || selectedSet.size === 0;
        this.els.excelSheetChips.innerHTML = "";
        for (const name of sheets) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = `sheet-chip${isAll || selectedSet.has(name) ? " active" : ""}`;
          btn.textContent = name;
          btn.title = name;
          btn.addEventListener("click", () => {
            this._mutateExcelOptions((o) => {
              let next;
              if (o.sheet_scope !== "selected" || !(o.selected_sheets || []).length) {
                // Switching from "All" to single sheet focus
                next = [name];
              } else {
                const cur = new Set(o.selected_sheets);
                if (cur.has(name)) {
                  cur.delete(name);
                } else {
                  cur.add(name);
                }
                next = sheets.filter((s) => cur.has(s));
              }
              if (!next.length || next.length === sheets.length) {
                o.sheet_scope = "all";
                o.selected_sheets = [];
              } else {
                o.sheet_scope = "selected";
                o.selected_sheets = next;
              }
            });
          });
          this.els.excelSheetChips.appendChild(btn);
        }
      }
    }

    if (this.els.selExcelFit) this.els.selExcelFit.value = opts.fit_mode || "fit_columns";
    if (this.els.segExcelOrient) {
      for (const btn of this.els.segExcelOrient.querySelectorAll("[data-val]")) {
        btn.classList.toggle("active", btn.dataset.val === (opts.orientation || "auto"));
      }
    }
    if (this.els.selExcelPaper) this.els.selExcelPaper.value = opts.paper_size || "auto";
    if (this.els.selExcelMargins) this.els.selExcelMargins.value = opts.margins || "default";
    if (this.els.chkExcelGridlines) this.els.chkExcelGridlines.checked = Boolean(opts.gridlines);
    if (this.els.chkExcelHeadings) this.els.chkExcelHeadings.checked = Boolean(opts.headings);
    if (this.els.chkExcelCenter) this.els.chkExcelCenter.checked = opts.center_horizontally !== false;
  }

  _bindSegmented(container, onPick) {
    container.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-val]");
      if (!btn || btn.disabled) return;
      onPick(btn.dataset.val);
    });
  }

  async ensureCapabilities(printerName) {
    if (!printerName || this.state.capabilitiesCache[printerName]) return;
    try {
      const caps = await api.getCapabilities(printerName);
      this.state.capabilitiesCache[printerName] = caps;
    } catch (_) {
      // ignore unavailable printer
    }
  }

  _reconcileSettingsWithCaps(s, caps) {
    if (!caps) return;
    if (!caps.supports_color) s.color_mode = "grayscale";
    if (!caps.supports_duplex) s.duplex = "simplex";
    s.copies = Math.min(s.copies || 1, Math.max(1, caps.max_copies));

    if (caps.paper_sizes.length) {
      const hasPaper = caps.paper_sizes.some((p) => p.id === s.paper_size_id);
      if (!hasPaper) {
        const defId = caps.defaults?.paper_size_id;
        const picked = caps.paper_sizes.find((p) => p.id === defId) || caps.paper_sizes[0];
        s.paper_size_id = picked.id;
        s.paper_size_name = picked.name;
        s.paper_width_mm = picked.width_mm;
        s.paper_height_mm = picked.height_mm;
      }
    }
    if (caps.paper_bins.length) {
      const hasBin = caps.paper_bins.some((b) => b.id === s.paper_source_id);
      if (!hasBin) {
        const defBin = caps.defaults?.paper_source_id;
        const picked = caps.paper_bins.find((b) => b.id === defBin) || caps.paper_bins[0];
        s.paper_source_id = picked.id;
        s.paper_source_name = picked.name;
      }
    } else {
      s.paper_source_id = null;
      s.paper_source_name = null;
    }
  }

  getEditedSettingsObject() {
    const item = this.callbacks.getSelectedItem();
    if (this.activeTab === "file" && item && item.use_override && item.override_settings) {
      return item.override_settings;
    }
    return this.state.globalSettings;
  }

  _commitEditedSettings(settingsObj, debounceSave = false) {
    const item = this.callbacks.getSelectedItem();
    const isFileOverride = this.activeTab === "file" && item && item.use_override;

    this.render();
    this.callbacks.onSettingsChanged();

    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(
      () => {
        if (isFileOverride) {
          this.callbacks.updateItemSettings(item.id, true, settingsObj, true);
        } else {
          api.saveGlobalSettings(settingsObj).catch(() => {});
          this.callbacks.refreshHeaderPrinter();
        }
      },
      debounceSave ? 250 : 20
    );
  }

  setPageRangeError(msg) {
    this.els.errPageRange.textContent = msg || "";
    this.els.errPageRange.classList.toggle("hidden", !msg);
  }

  render() {
    const item = this.callbacks.getSelectedItem();
    const hasItem = Boolean(item);
    if (!hasItem && this.activeTab === "file") {
      this.activeTab = "global";
    }

    // Scope Tabs
    this.els.tabFile.classList.toggle("disabled", !hasItem);
    this.els.overrideDot.classList.toggle("hidden", !(item && item.use_override));
    for (const t of this.els.tabs.querySelectorAll("[data-tab]")) {
      t.classList.toggle("active", t.dataset.tab === this.activeTab);
    }

    const inFileTab = this.activeTab === "file" && hasItem;
    this.els.overrideBanner.classList.toggle("hidden", !inFileTab);
    if (inFileTab) {
      this.els.chkUseOverride.checked = Boolean(item.use_override);
      this.els.form.classList.toggle("locked-readonly", !item.use_override);
    } else {
      this.els.form.classList.remove("locked-readonly");
    }

    this._renderExcelCard(item);

    const s =
      inFileTab && item.use_override && item.override_settings
        ? item.override_settings
        : this.state.globalSettings;

    // Printer dropdown
    this.els.selPrinter.innerHTML = "";
    for (const p of this.state.printers) {
      const opt = document.createElement("option");
      opt.value = p.name;
      const tag = p.is_virtual ? " [Virtual]" : p.is_default ? " [Default]" : "";
      opt.textContent = `${p.name}${tag} (${p.status_label})`;
      this.els.selPrinter.appendChild(opt);
    }
    if (s.printer_name) {
      this.els.selPrinter.value = s.printer_name;
    }

    this.els.driverPill.classList.toggle("hidden", !s.has_custom_driver_settings);

    const caps = this.state.capabilitiesCache[s.printer_name];

    // Copies & Collate
    const maxCopies = caps ? Math.max(1, caps.max_copies) : 999;
    this.els.inpCopies.max = String(maxCopies);
    if (document.activeElement !== this.els.inpCopies) {
      this.els.inpCopies.value = String(s.copies || 1);
    }
    this.els.chkCollate.checked = Boolean(s.collate);
    this.els.wrapCollate.classList.toggle("hidden", (s.copies || 1) <= 1 && maxCopies <= 1);

    // Color
    const supportsColor = caps ? caps.supports_color : true;
    for (const btn of this.els.segColor.querySelectorAll("[data-val]")) {
      btn.classList.toggle("active", btn.dataset.val === s.color_mode);
      if (btn.dataset.val === "color") btn.disabled = !supportsColor;
    }

    // Duplex
    const supportsDuplex = caps ? caps.supports_duplex : true;
    this.els.selDuplex.disabled = !supportsDuplex;
    this.els.selDuplex.value = supportsDuplex ? s.duplex || "simplex" : "simplex";

    // Paper Sizes
    this.els.selPaperSize.innerHTML = "";
    const sizes = caps?.paper_sizes || [];
    if (sizes.length) {
      for (const ps of sizes) {
        const opt = document.createElement("option");
        opt.value = String(ps.id);
        opt.textContent = `${ps.name} (${ps.width_mm}×${ps.height_mm} mm)`;
        this.els.selPaperSize.appendChild(opt);
      }
      if (s.paper_size_id != null) {
        this.els.selPaperSize.value = String(s.paper_size_id);
      }
    } else {
      const opt = document.createElement("option");
      opt.value = "9";
      opt.textContent = "A4 (210×297 mm)";
      this.els.selPaperSize.appendChild(opt);
    }

    // Paper Bins
    const bins = caps?.paper_bins || [];
    this.els.wrapPaperBin.classList.toggle("hidden", bins.length === 0);
    this.els.selPaperBin.innerHTML = "";
    for (const b of bins) {
      const opt = document.createElement("option");
      opt.value = String(b.id);
      opt.textContent = b.name;
      this.els.selPaperBin.appendChild(opt);
    }
    if (bins.length && s.paper_source_id != null) {
      this.els.selPaperBin.value = String(s.paper_source_id);
    }

    // Orientation
    for (const btn of this.els.segOrientation.querySelectorAll("[data-val]")) {
      btn.classList.toggle("active", btn.dataset.val === (s.orientation || "auto"));
    }

    // Page Range
    for (const btn of this.els.segPages.querySelectorAll("[data-val]")) {
      btn.classList.toggle("active", btn.dataset.val === (s.page_range_mode || "all"));
    }
    this.els.inpPageRange.classList.toggle("hidden", s.page_range_mode !== "custom");
    if (document.activeElement !== this.els.inpPageRange) {
      this.els.inpPageRange.value = s.page_range_custom || "";
    }

    // Scaling & N-Up & Quality
    this.els.selScale.value = s.scale_mode || "fit";
    this.els.wrapScalePct.classList.toggle("hidden", s.scale_mode !== "custom");
    if (document.activeElement !== this.els.inpScalePct) {
      this.els.inpScalePct.value = String(s.scale_percent || 100);
    }

    this.els.selNup.value = String(s.pages_per_sheet || 1);
    this.els.wrapNupOpts.classList.toggle("hidden", (s.pages_per_sheet || 1) <= 1);
    this.els.selNupOrder.value = s.nup_order || "row";
    this.els.chkNupBorder.checked = Boolean(s.nup_border);
    this.els.selQuality.value = String(s.quality_dpi || 300);

    // Quick Preset dropdown
    if (this.els.selQuickPreset) {
      const presets = this.state.presets || [];
      this.els.selQuickPreset.innerHTML = `<option value="">Apply a saved preset...</option>`;
      for (const p of presets) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.name;
        this.els.selQuickPreset.appendChild(opt);
      }
    }

    // Advanced section summary pill
    if (this.els.advancedSummaryPill) {
      const tags = [];
      if (s.orientation && s.orientation !== "auto") {
        tags.push(s.orientation === "landscape" ? "Landscape" : "Portrait");
      }
      if (s.page_range_mode && s.page_range_mode !== "all") {
        tags.push(s.page_range_mode === "custom" ? `Pages: ${s.page_range_custom || "custom"}` : s.page_range_mode);
      }
      if (s.scale_mode && s.scale_mode !== "fit") {
        tags.push(s.scale_mode === "custom" ? `${s.scale_percent || 100}%` : s.scale_mode);
      }
      if ((s.pages_per_sheet || 1) > 1) {
        tags.push(`${s.pages_per_sheet}-up`);
      }
      if (s.quality_dpi && s.quality_dpi !== 300) {
        tags.push(`${s.quality_dpi} DPI`);
      }
      const isModified = tags.length > 0;
      this.els.advancedSummaryPill.textContent = isModified ? tags.join(" · ") : "Default";
      this.els.advancedSummaryPill.classList.toggle("modified", isModified);
    }
  }
}
