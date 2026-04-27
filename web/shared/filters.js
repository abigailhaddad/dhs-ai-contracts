/**
 * Client-side filter manager — adapted from usajobs_historical's
 * ServerSideFilterManager for use with static JSON data and Chart.js.
 *
 * Key differences from the original:
 * - No DataTable / no server AJAX calls
 * - Filter options come from a pre-loaded object (not /api/filter_options)
 * - onFilterChange callback drives chart re-renders
 */

function escapeHtml(text) {
    if (!text) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return String(text).replace(/[&<>"']/g, m => map[m]);
}

function getCleanURL() {
    const url = new URL(window.location);
    if (url.pathname.endsWith('/index.html'))
        url.pathname = url.pathname.replace(/\/index\.html$/, '/');
    return url;
}

function createModal(options = {}) {
    const overlay = document.createElement('div');
    overlay.className = 'filter-modal ' + (options.className || '');
    const inner = document.createElement('div');
    inner.innerHTML = options.content || '';
    while (inner.firstChild) overlay.appendChild(inner.firstChild);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(overlay); });
    const escHandler = e => { if (e.key === 'Escape') { closeModal(overlay); document.removeEventListener('keydown', escHandler); } };
    document.addEventListener('keydown', escHandler);
    document.body.appendChild(overlay);
    return overlay;
}

function closeModal(modal) {
    if (modal && modal.parentNode) modal.parentNode.removeChild(modal);
}

function showToast(message, isError = false) {
    const toast = document.createElement('div');
    toast.className = 'toast' + (isError ? ' toast-error' : ' toast-success');
    toast.textContent = message;
    document.body.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('show'));
    setTimeout(() => { toast.classList.remove('show'); setTimeout(() => toast.remove(), 300); }, 2000);
}

class FilterManager {
    /**
     * @param {Object} options
     * @param {Array}    options.columns        - [{name, field, filterType, options?}]
     *                                            filterType: 'multiselect' | 'range' | 'text'
     * @param {string}   options.filterBarId    - ID of the filter chips container
     * @param {string}   options.toolbarId      - ID of the toolbar button row
     * @param {Object}   options.filterOptions  - {field: [{value, label}, ...]} for multiselect
     * @param {Function} options.onFilterChange - called whenever active filters change
     */
    constructor(options) {
        this.columns       = options.columns || [];
        this.filterBarId   = options.filterBarId;
        this.toolbarId     = options.toolbarId;
        this.filterOptions = options.filterOptions || {};
        this.onChange      = options.onFilterChange || (() => {});
        this.activeFilters = {};
        this._applyFiltersFromURL();
        this._setupToolbar();
    }

    // ── Public API ────────────────────────────────────────────────────────

    /** Returns current active filters as a plain object for callers to inspect. */
    getFilters() { return this.activeFilters; }

    clearAll() {
        this.activeFilters = {};
        this._applyAndNotify();
    }

    // ── Setup ─────────────────────────────────────────────────────────────

    _setupToolbar() {
        const toolbar = document.getElementById(this.toolbarId);
        if (!toolbar) return;

        const addBtn = document.createElement('button');
        addBtn.className = 'add-filter-btn';
        addBtn.textContent = '+ Add Filter';
        addBtn.addEventListener('click', () => this._openColumnPicker());
        toolbar.appendChild(addBtn);

        const clearBtn = document.createElement('button');
        clearBtn.className = 'clear-filters-btn';
        clearBtn.textContent = 'Clear All';
        clearBtn.style.display = 'none';
        clearBtn.addEventListener('click', () => this.clearAll());
        toolbar.appendChild(clearBtn);

        const copyBtn = document.createElement('button');
        copyBtn.className = 'copy-link-btn';
        copyBtn.textContent = 'Copy Link';
        copyBtn.addEventListener('click', () => {
            navigator.clipboard.writeText(window.location.href)
                .then(() => showToast('Link copied to clipboard!'))
                .catch(() => showToast('Failed to copy link', true));
        });
        toolbar.appendChild(copyBtn);
    }

    // ── Column picker dialog ──────────────────────────────────────────────

    _openColumnPicker() {
        const items = this.columns
            .filter(c => c.filterType)
            .map(c => `<label class="filter-option">
                <input type="checkbox" value="${escapeHtml(c.field)}" data-type="${c.filterType}">
                ${escapeHtml(c.name)}
            </label>`).join('');

        const content = `<div class="filter-popover">
            <div class="filter-title">Add filter</div>
            <input type="text" class="filter-search filter-options-search" placeholder="Search columns…">
            <div class="filter-options">${items}</div>
        </div>`;

        const modal = createModal({ content });

        modal.querySelector('.filter-options-search').addEventListener('input', function () {
            const q = this.value.toLowerCase();
            modal.querySelectorAll('.filter-option').forEach(el => {
                el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
            });
        });
        modal.querySelector('.filter-options-search').focus();

        modal.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            cb.addEventListener('change', () => {
                if (cb.checked) {
                    closeModal(modal);
                    const col = this.columns.find(c => c.field === cb.value);
                    if (col) this._openFilterDialog(col);
                }
            });
        });
    }

    _openFilterDialog(col) {
        if (col.filterType === 'multiselect') this._openMultiselectDialog(col);
        else if (col.filterType === 'range')   this._openRangeDialog(col);
        else if (col.filterType === 'text')    this._openTextDialog(col);
    }

    // ── Multiselect dialog ────────────────────────────────────────────────

    _openMultiselectDialog(col) {
        const opts  = this.filterOptions[col.field] || [];
        const current = (this.activeFilters[col.field]?.values) || [];

        const items = opts.map(o => {
            const val = escapeHtml(String(o.value));
            const lbl = escapeHtml(o.label || String(o.value));
            const chk = current.includes(String(o.value)) ? ' checked' : '';
            return `<label class="filter-option"><input type="checkbox" value="${val}"${chk}>${lbl}</label>`;
        }).join('');

        const content = `<div class="filter-popover">
            <div class="filter-title">Filter: ${escapeHtml(col.name)}</div>
            <input type="text" class="filter-search filter-options-search" placeholder="Search options…">
            <div class="filter-options">${items}</div>
            <div class="filter-buttons">
                <button class="btn btn-clear">Clear</button>
                <button class="btn btn-apply">Apply</button>
            </div>
        </div>`;

        const modal = createModal({ content });
        modal.querySelector('.filter-options-search').addEventListener('input', function () {
            const q = this.value.toLowerCase();
            modal.querySelectorAll('.filter-option').forEach(el => {
                el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
            });
        });
        modal.querySelector('.filter-options-search').focus();

        modal.querySelector('.btn-clear').addEventListener('click', () => {
            delete this.activeFilters[col.field];
            this._applyAndNotify();
            closeModal(modal);
        });

        modal.querySelector('.btn-apply').addEventListener('click', () => {
            const checked = [...modal.querySelectorAll('input[type="checkbox"]:checked')].map(cb => cb.value);
            if (checked.length > 0)
                this.activeFilters[col.field] = { type: 'multiselect', values: checked, name: col.name };
            else
                delete this.activeFilters[col.field];
            this._applyAndNotify();
            closeModal(modal);
        });
    }

    // ── Range dialog ──────────────────────────────────────────────────────

    _openRangeDialog(col) {
        const cur = this.activeFilters[col.field] || {};
        const content = `<div class="filter-popover">
            <div class="filter-title">Filter: ${escapeHtml(col.name)}</div>
            <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;">
                <input type="number" class="filter-range-min filter-search" placeholder="Min" value="${escapeHtml(String(cur.min ?? ''))}" style="flex:1;">
                <span style="color:var(--color-text-muted);">to</span>
                <input type="number" class="filter-range-max filter-search" placeholder="Max" value="${escapeHtml(String(cur.max ?? ''))}" style="flex:1;">
            </div>
            <div class="filter-buttons">
                <button class="btn btn-clear">Clear</button>
                <button class="btn btn-apply">Apply</button>
            </div>
        </div>`;

        const modal = createModal({ content });
        modal.querySelector('.filter-range-min').focus();
        modal.querySelector('.btn-clear').addEventListener('click', () => {
            delete this.activeFilters[col.field]; this._applyAndNotify(); closeModal(modal);
        });
        modal.querySelector('.btn-apply').addEventListener('click', () => {
            const min = modal.querySelector('.filter-range-min').value.trim();
            const max = modal.querySelector('.filter-range-max').value.trim();
            if (min !== '' || max !== '')
                this.activeFilters[col.field] = { type: 'range', min: min !== '' ? +min : null, max: max !== '' ? +max : null, name: col.name };
            else
                delete this.activeFilters[col.field];
            this._applyAndNotify(); closeModal(modal);
        });
    }

    // ── Text dialog ───────────────────────────────────────────────────────

    _openTextDialog(col) {
        const cur    = this.activeFilters[col.field];
        const terms  = cur?.value ? cur.value.split(',').map(t => t.trim()).filter(t => t) : [];
        const content = `<div class="filter-popover">
            <div class="filter-title">Filter: ${escapeHtml(col.name)}</div>
            <div class="text-tags-container"></div>
            <input type="text" class="filter-text-input filter-search" placeholder="Type and press Enter…">
            <div class="filter-buttons">
                <button class="btn btn-clear">Clear</button>
                <button class="btn btn-apply">Apply</button>
            </div>
        </div>`;

        const modal = createModal({ content });
        const input  = modal.querySelector('.filter-text-input');
        const tagsEl = modal.querySelector('.text-tags-container');
        const active = [...terms];

        const renderTags = () => {
            tagsEl.innerHTML = active.map((t, i) =>
                `<span class="text-tag"><span class="text-tag-label">${escapeHtml(t)}</span><span class="text-tag-remove" data-i="${i}">\u00d7</span></span>`
            ).join('');
            tagsEl.querySelectorAll('.text-tag-remove').forEach(el => {
                el.addEventListener('click', () => { active.splice(+el.dataset.i, 1); renderTags(); input.focus(); });
            });
        };
        renderTags();
        input.focus();

        input.addEventListener('keydown', e => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const v = input.value.trim();
                if (v && !active.includes(v)) { active.push(v); renderTags(); }
                input.value = '';
            } else if (e.key === 'Backspace' && !input.value && active.length) {
                active.pop(); renderTags();
            }
        });

        modal.querySelector('.btn-clear').addEventListener('click', () => {
            delete this.activeFilters[col.field]; this._applyAndNotify(); closeModal(modal);
        });
        modal.querySelector('.btn-apply').addEventListener('click', () => {
            const v = input.value.trim();
            if (v && !active.includes(v)) active.push(v);
            if (active.length)
                this.activeFilters[col.field] = { type: 'text', value: active.join(','), name: col.name };
            else
                delete this.activeFilters[col.field];
            this._applyAndNotify(); closeModal(modal);
        });
    }

    // ── Apply + notify ────────────────────────────────────────────────────

    _applyAndNotify() {
        this._updateFilterBar();
        this._updateURL();
        this.onChange(this.activeFilters);
    }

    // ── Filter bar ────────────────────────────────────────────────────────

    _updateFilterBar() {
        const bar = document.getElementById(this.filterBarId);
        if (!bar) return;

        bar.querySelectorAll('.filter-chip.column-filter-chip, .bar-label.filter-label').forEach(el => el.remove());

        const hasFilters = Object.keys(this.activeFilters).length > 0;
        const emptyMsg   = bar.querySelector('.filters-bar-empty');
        if (emptyMsg) emptyMsg.style.display = hasFilters ? 'none' : '';

        const clearBtn = document.querySelector('.clear-filters-btn');
        if (clearBtn) clearBtn.style.display = hasFilters ? '' : 'none';

        if (!hasFilters) return;

        const label = document.createElement('span');
        label.className = 'bar-label filter-label';
        label.textContent = 'Filtered by:';
        bar.insertBefore(label, bar.firstChild);

        Object.entries(this.activeFilters).forEach(([field, filter]) => {
            let displayValue;
            if (filter.type === 'multiselect') {
                // Show labels not raw values when available
                const opts = this.filterOptions[field] || [];
                const labels = filter.values.map(v => {
                    const opt = opts.find(o => String(o.value) === String(v));
                    return opt ? opt.label : v;
                });
                displayValue = labels.join(', ');
            } else if (filter.type === 'range') {
                const parts = [];
                if (filter.min != null) parts.push(filter.min.toLocaleString());
                parts.push('–');
                if (filter.max != null) parts.push(filter.max.toLocaleString());
                displayValue = parts.join(' ');
            } else {
                displayValue = filter.value;
            }

            const chip = document.createElement('div');
            chip.className = 'filter-chip column-filter-chip';

            const chipLabel = document.createElement('span');
            chipLabel.className = 'filter-chip-label';
            chipLabel.textContent = filter.name + ':';

            const chipValue = document.createElement('span');
            chipValue.className = 'filter-chip-value';
            chipValue.textContent = displayValue;

            const chipRemove = document.createElement('span');
            chipRemove.className = 'filter-chip-remove';
            chipRemove.textContent = '\u00d7';
            chipRemove.addEventListener('click', () => {
                delete this.activeFilters[field];
                this._applyAndNotify();
            });

            const editHandler = () => {
                const col = this.columns.find(c => c.field === field);
                if (col) this._openFilterDialog(col);
            };
            chipLabel.style.cursor = 'pointer';
            chipValue.style.cursor = 'pointer';
            chipLabel.addEventListener('click', editHandler);
            chipValue.addEventListener('click', editHandler);

            chip.append(chipLabel, chipValue, chipRemove);
            bar.appendChild(chip);
        });
    }

    // ── URL sync ──────────────────────────────────────────────────────────

    _updateURL() {
        const url = getCleanURL();
        url.search = '';
        Object.entries(this.activeFilters).forEach(([field, filter]) => {
            if (filter.type === 'multiselect') url.searchParams.set(field, filter.values.join(','));
            else if (filter.type === 'range')  url.searchParams.set(field, `${filter.min ?? ''}-${filter.max ?? ''}`);
            else                               url.searchParams.set(field, filter.value);
        });
        window.history.replaceState({}, '', url);
    }

    _applyFiltersFromURL() {
        const params = new URLSearchParams(window.location.search);
        if (!params.toString()) return;
        const byField = Object.fromEntries(this.columns.filter(c => c.filterType).map(c => [c.field, c]));
        params.forEach((value, key) => {
            const col = byField[key];
            if (!col) return;
            if (col.filterType === 'multiselect') {
                const values = value.split(',').map(v => v.trim()).filter(v => v);
                if (values.length) this.activeFilters[key] = { type: 'multiselect', values, name: col.name };
            } else if (col.filterType === 'range') {
                const parts = value.split('-');
                const min = parts[0] !== '' ? +parts[0] : null;
                const max = parts[1] !== '' ? +parts[1] : null;
                if (min != null || max != null) this.activeFilters[key] = { type: 'range', min, max, name: col.name };
            } else {
                if (value) this.activeFilters[key] = { type: 'text', value, name: col.name };
            }
        });
        // Render chips after constructor finishes (DOM may not be ready yet)
        requestAnimationFrame(() => this._updateFilterBar());
    }
}
