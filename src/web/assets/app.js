(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // --- toast notifications ---
  function showToast(msg, durationMs) {
    const container = $('#toast-container');
    if (!container) return;
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    container.appendChild(el);
    const dur = durationMs || 2000;
    setTimeout(() => {
      el.classList.add('toast-out');
      setTimeout(() => el.remove(), 200);
    }, dur);
  }

  // --- theme toggle ---
  function getTheme() {
    return localStorage.getItem('advisor-theme') || 'dark';
  }
  function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('advisor-theme', theme);
    const darkIcon = $('#theme-icon-dark');
    const lightIcon = $('#theme-icon-light');
    if (darkIcon && lightIcon) {
      darkIcon.hidden = theme === 'light';
      lightIcon.hidden = theme === 'dark';
    }
  }
  setTheme(getTheme());
  $('#theme-toggle').addEventListener('click', () => {
    const next = getTheme() === 'dark' ? 'light' : 'dark';
    setTheme(next);
    showToast('Theme: ' + next);
  });

  // --- shortcuts modal ---
  function toggleShortcuts() {
    const modal = $('#shortcuts-modal');
    modal.hidden = !modal.hidden;
  }
  $('#shortcuts-btn').addEventListener('click', toggleShortcuts);
  $('#shortcuts-modal .modal-backdrop').addEventListener('click', () => {
    $('#shortcuts-modal').hidden = true;
  });
  $('#shortcuts-modal .modal-close').addEventListener('click', () => {
    $('#shortcuts-modal').hidden = true;
  });

  // --- tab switching ---
  const TAB_ORDER = ['findings', 'live', 'plan', 'config', 'cost'];
  const autoLoaded = new Set();

  function switchTab(tabName) {
    $$('.tab').forEach((b) => b.classList.remove('active'));
    $$('.panel').forEach((p) => p.classList.remove('active'));
    const btn = document.querySelector(`.tab[data-tab="${tabName}"]`);
    if (btn) btn.classList.add('active');
    const panel = $('#' + tabName);
    if (panel) panel.classList.add('active');
    if (tabName === 'findings') schedulePoll(0);
    if (tabName === 'cost' && !autoLoaded.has('cost')) {
      autoLoaded.add('cost');
      loadCost();
    }
    if (tabName === 'plan' && !autoLoaded.has('plan')) {
      autoLoaded.add('plan');
      loadPlan();
    }
    if (tabName === 'live') scheduleLiveStreamPoll(0);
  }

  $$('.tab').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // --- keyboard shortcuts ---
  document.addEventListener('keydown', (e) => {
    // Ignore when typing in inputs
    const tag = (e.target.tagName || '').toLowerCase();
    const isInput = tag === 'input' || tag === 'textarea' || tag === 'select';

    if (e.key === 'Escape') {
      if (!$('#shortcuts-modal').hidden) { $('#shortcuts-modal').hidden = true; return; }
      if (isInput) { e.target.blur(); return; }
      return;
    }
    if (isInput) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    switch (e.key) {
      case '1': case '2': case '3': case '4': case '5':
        e.preventDefault();
        switchTab(TAB_ORDER[parseInt(e.key) - 1]);
        break;
      case '/':
        e.preventDefault();
        switchTab('findings');
        $('#findings-search').focus();
        break;
      case '?':
        e.preventDefault();
        toggleShortcuts();
        break;
      case 't': case 'T':
        e.preventDefault();
        setTheme(getTheme() === 'dark' ? 'light' : 'dark');
        showToast('Theme: ' + getTheme());
        break;
      case 'e': case 'E':
        e.preventDefault();
        exportFindings();
        break;
      case 'r': case 'R':
        e.preventDefault();
        refreshCurrentTab();
        break;
    }
  });

  function refreshCurrentTab() {
    const active = document.querySelector('.tab.active');
    if (!active) return;
    const tab = active.dataset.tab;
    if (tab === 'findings') schedulePoll(0);
    else if (tab === 'plan') loadPlan();
    else if (tab === 'cost') loadCost();
    else if (tab === 'live') scheduleLiveStreamPoll(0);
    showToast('Refreshed');
  }

  // --- findings ---
  let findingsRaw = [];
  let findingsErrorMessage = '';
  let seenKeys = new Set();
  const MAX_SEEN_KEYS = 5000;

  // Sort state
  let sortField = '';
  let sortDir = '';  // 'asc' or 'desc'

  function findingKey(entry) {
    const ts = entry.timestamp || '';
    const fp = entry.file_path || '';
    const desc = entry.description || '';
    return ts + '\u241f' + fp + '\u241f' + desc;
  }

  // --- severity stats ---
  function renderSeverityStats() {
    const stats = { CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 };
    findingsRaw.forEach((e) => {
      const s = (e.severity || '').toUpperCase();
      if (s in stats) stats[s]++;
    });
    const container = $('#severity-stats');
    const total = findingsRaw.length;
    if (total === 0) { container.hidden = true; return; }
    container.hidden = false;
    const currentSev = $('#findings-severity').value;
    container.innerHTML = `
      <span class="stat-badge sev-total${!currentSev ? ' active' : ''}" data-sev="">
        <span class="stat-count">${total}</span> total
      </span>
      <span class="stat-badge sev-CRITICAL${currentSev === 'CRITICAL' ? ' active' : ''}" data-sev="CRITICAL">
        <span class="stat-count">${stats.CRITICAL}</span> critical
      </span>
      <span class="stat-badge sev-HIGH${currentSev === 'HIGH' ? ' active' : ''}" data-sev="HIGH">
        <span class="stat-count">${stats.HIGH}</span> high
      </span>
      <span class="stat-badge sev-MEDIUM${currentSev === 'MEDIUM' ? ' active' : ''}" data-sev="MEDIUM">
        <span class="stat-count">${stats.MEDIUM}</span> medium
      </span>
      <span class="stat-badge sev-LOW${currentSev === 'LOW' ? ' active' : ''}" data-sev="LOW">
        <span class="stat-count">${stats.LOW}</span> low
      </span>
    `;
    container.querySelectorAll('.stat-badge').forEach((badge) => {
      badge.addEventListener('click', () => {
        const sev = badge.dataset.sev;
        $('#findings-severity').value = sev;
        renderFindings();
      });
    });
  }

  // --- sortable columns ---
  const SEV_ORDER = { CRITICAL: 4, HIGH: 3, MEDIUM: 2, LOW: 1 };
  function sortFindings(arr) {
    if (!sortField || !sortDir) return arr;
    const copy = [...arr];
    const dir = sortDir === 'asc' ? 1 : -1;
    copy.sort((a, b) => {
      let va, vb;
      if (sortField === 'severity') {
        va = SEV_ORDER[(a.severity || '').toUpperCase()] || 0;
        vb = SEV_ORDER[(b.severity || '').toUpperCase()] || 0;
        return (va - vb) * dir;
      }
      va = (a[sortField] || '').toLowerCase();
      vb = (b[sortField] || '').toLowerCase();
      if (va < vb) return -1 * dir;
      if (va > vb) return 1 * dir;
      return 0;
    });
    return copy;
  }

  $$('#findings-table th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const field = th.dataset.sort;
      if (sortField === field) {
        sortDir = sortDir === 'asc' ? 'desc' : sortDir === 'desc' ? '' : 'asc';
        if (!sortDir) sortField = '';
      } else {
        sortField = field;
        sortDir = 'asc';
      }
      $$('#findings-table th.sortable').forEach((h) => {
        h.classList.remove('sort-asc', 'sort-desc');
      });
      if (sortDir) th.classList.add('sort-' + sortDir);
      renderFindings();
    });
  });

  // --- expandable finding detail ---
  let expandedKey = null;

  function renderFindingDetail(e) {
    const fields = [];
    if (e.description) fields.push(['Description', escapeHtml(e.description)]);
    if (e.file_path) fields.push(['File', `<code>${escapeHtml(e.file_path)}</code>`]);
    if (e.line_number) fields.push(['Line', escapeHtml(String(e.line_number))]);
    if (e.evidence) fields.push(['Evidence', `<code>${escapeHtml(e.evidence)}</code>`]);
    if (e.fix) fields.push(['Fix', escapeHtml(e.fix)]);
    if (e.rule_id) fields.push(['Rule', escapeHtml(e.rule_id)]);
    if (e.tool) fields.push(['Tool', escapeHtml(e.tool)]);
    if (e.run_id) fields.push(['Run ID', escapeHtml(e.run_id)]);
    let html = '<dl>';
    fields.forEach(([label, val]) => {
      html += `<dt>${escapeHtml(label)}</dt><dd>${val}</dd>`;
    });
    html += '</dl>';
    return html;
  }

  function renderFindings() {
    const q = $('#findings-search').value.toLowerCase();
    const sev = $('#findings-severity').value;
    let filtered = findingsRaw.filter((e) => {
      if (sev && (e.severity || '').toUpperCase() !== sev) return false;
      if (!q) return true;
      return (
        (e.file_path || '').toLowerCase().includes(q) ||
        (e.description || '').toLowerCase().includes(q)
      );
    });
    filtered = sortFindings(filtered);
    const tbody = $('#findings-table tbody');
    tbody.innerHTML = '';
    filtered.forEach((e) => {
      const key = findingKey(e);
      const isNew = seenKeys.size > 0 && key && !seenKeys.has(key);
      const isExpanded = key === expandedKey;
      const tr = document.createElement('tr');
      tr.className = 'expandable';
      if (isNew) tr.classList.add('row-new');
      if (isExpanded) tr.classList.add('expanded');
      tr.innerHTML = `
        <td>${escapeHtml(e.timestamp || '')}</td>
        <td><code>${escapeHtml(e.file_path || '')}</code></td>
        <td><span class="sev sev-${severityClass(e.severity)}">${escapeHtml(e.severity || '')}</span></td>
        <td>${escapeHtml(e.status || '')}</td>
        <td>${escapeHtml(e.description || '')}</td>
      `;
      tr.addEventListener('click', () => {
        if (expandedKey === key) {
          expandedKey = null;
        } else {
          expandedKey = key;
        }
        renderFindings();
      });
      tbody.appendChild(tr);
      if (isExpanded) {
        const detailTr = document.createElement('tr');
        detailTr.className = 'detail-row';
        const td = document.createElement('td');
        td.colSpan = 5;
        td.innerHTML = `<div class="detail-content">${renderFindingDetail(e)}</div>`;
        detailTr.appendChild(td);
        tbody.appendChild(detailTr);
      }
      if (isNew) setTimeout(() => tr.classList.remove('row-new'), 2100);
    });
    findingsRaw.forEach((e) => { const k = findingKey(e); if (k) seenKeys.add(k); });
    if (seenKeys.size > MAX_SEEN_KEYS) {
      const drop = seenKeys.size - MAX_SEEN_KEYS;
      const it = seenKeys.values();
      for (let i = 0; i < drop; i++) seenKeys.delete(it.next().value);
    }
    $('#findings-count').textContent = `${filtered.length} of ${findingsRaw.length}`;
    const hasVisibleFindings = filtered.length !== 0;
    let emptyCopy;
    if (findingsErrorMessage) {
      emptyCopy = findingsErrorMessage;
    } else if (findingsRaw.length === 0) {
      emptyCopy = (lastStatus && lastStatus.live_is_active)
        ? 'A /advisor run is in progress on this target \u2014 confirmed findings will appear here when the run wraps and you accept them. Watch the Live tab for real-time activity.'
        : 'No findings yet. Run the advisor on this target first.';
    } else {
      emptyCopy = 'No findings match the current filters.';
    }
    $('#findings-empty').textContent = emptyCopy;
    $('#findings-empty').hidden = hasVisibleFindings;
    $('#findings-table').hidden = !hasVisibleFindings;
    renderSeverityStats();
  }
  $('#findings-search').addEventListener('input', renderFindings);
  $('#findings-severity').addEventListener('change', renderFindings);

  // --- export to CSV ---
  function exportFindings() {
    if (findingsRaw.length === 0) {
      showToast('No findings to export');
      return;
    }
    const headers = ['timestamp', 'file_path', 'severity', 'status', 'description', 'evidence', 'fix', 'rule_id', 'tool'];
    const csvRows = [headers.join(',')];
    findingsRaw.forEach((e) => {
      const row = headers.map((h) => {
        const val = String(e[h] || '').replace(/"/g, '""');
        return '"' + val + '"';
      });
      csvRows.push(row.join(','));
    });
    const blob = new Blob([csvRows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'advisor-findings.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('Exported ' + findingsRaw.length + ' findings');
  }
  $('#export-csv').addEventListener('click', exportFindings);

  function showFindingsError(message) {
    findingsRaw = [];
    findingsErrorMessage = message;
    $('#findings-table tbody').innerHTML = '';
    $('#findings-count').textContent = '0 of 0';
    $('#findings-empty').textContent = message;
    $('#findings-empty').hidden = false;
    $('#findings-table').hidden = true;
    renderSeverityStats();
  }

  // --- live poll loop ---
  const POLL_INTERVAL_MS = 3000;
  const POLL_MAX_BACKOFF_MS = 30000;
  let pollErrorStreak = 0;
  let lastMtime = '__uninitialized__';
  let lastToken = '__uninitialized__';
  let pollTimer = null;
  let liveEnabled = true;
  let lastStatus = null;
  let lastStatusTs = null;

  function setLiveState(state, labelOverride) {
    const pill = $('#live-indicator');
    pill.classList.remove('idle', 'active', 'paused', 'error');
    pill.classList.add(state);
    pill.querySelector('.live-label').textContent = labelOverride || state.toUpperCase();
  }

  function schedulePoll(delayMs) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(pollTick, Math.max(0, delayMs));
  }

  async function pollTick() {
    pollTimer = null;
    if (!liveEnabled) { setLiveState('paused', 'PAUSED'); return; }
    if (document.hidden) return;
    const activeTab = document.querySelector('.tab.active')?.dataset.tab;
    if (activeTab !== 'findings') return;

    let nextDelay = POLL_INTERVAL_MS;
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      if (!r.ok) throw new Error('status ' + r.status);
      const data = await r.json();
      lastStatus = data;
      lastStatusTs = Date.now();
      const liveActive = data.live_is_active === true;
      const historyActive = data.history_is_active === true;
      if (historyActive) {
        setLiveState('active', 'LIVE');
      } else if (liveActive) {
        setLiveState('active', 'RUNNING');
      } else if (data.is_active) {
        setLiveState('active', 'LIVE');
      } else {
        setLiveState('idle', 'IDLE');
      }
      const hasToken = typeof data.token === 'string';
      const changed = hasToken
        ? data.token !== lastToken
        : data.last_mtime !== lastMtime;
      if (changed) {
        if (await refetchFindings()) {
          lastMtime = data.last_mtime;
          if (hasToken) lastToken = data.token;
        }
      }
      pollErrorStreak = 0;
    } catch (_) {
      setLiveState('error', 'ERROR');
      pollErrorStreak += 1;
      const backoff = POLL_INTERVAL_MS * Math.pow(2, pollErrorStreak - 1);
      nextDelay = Math.min(POLL_MAX_BACKOFF_MS, backoff);
    }
    schedulePoll(nextDelay);
  }

  async function refetchFindings() {
    try {
      const r = await fetch('/api/history', { cache: 'no-store' });
      if (!r.ok) {
        showFindingsError('Error loading findings: ' + r.status);
        return false;
      }
      const data = await r.json();
      findingsErrorMessage = '';
      findingsRaw = data.entries || [];
      renderFindings();
      return true;
    } catch (_) {
      showFindingsError('Error loading findings: network error');
      return false;
    }
  }

  function renderUpdatedLabel() {
    const el = $('#live-updated');
    if (!lastStatusTs) { el.textContent = ''; return; }
    const secs = Math.floor((Date.now() - lastStatusTs) / 1000);
    el.textContent = 'updated ' + (secs <= 1 ? 'just now' : secs + 's ago');
  }
  setInterval(renderUpdatedLabel, 1000);

  const pill = $('#live-indicator');
  function togglePolling() {
    liveEnabled = !liveEnabled;
    if (liveEnabled) schedulePoll(0); else setLiveState('paused', 'PAUSED');
  }
  pill.addEventListener('click', togglePolling);
  pill.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); togglePolling(); }
  });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && liveEnabled) schedulePoll(0);
  });

  // --- plan ---
  function showPlanError(message) {
    $('#plan-table tbody').innerHTML = '';
    $('#plan-table').hidden = true;
    $('#plan-count').textContent = '';
    $('#plan-empty').hidden = false;
    $('#plan-empty').textContent = message;
  }

  async function loadPlan() {
    const form = new FormData($('#config-form'));
    const qs = new URLSearchParams();
    qs.set('file_types', form.get('file_types') || '*.py');
    qs.set('min_priority', form.get('min_priority') || '3');
    let data;
    try {
      const r = await fetch('/api/plan?' + qs.toString());
      if (!r.ok) {
        showPlanError('Error loading plan: ' + r.status);
        return;
      }
      data = await r.json();
    } catch (_) {
      showPlanError('Error loading plan: network error');
      return;
    }
    const tbody = $('#plan-table tbody');
    tbody.innerHTML = '';
    (data.tasks || []).forEach((t, i) => {
      const tr = document.createElement('tr');
      const pri = escapeHtml(String(t.priority));
      const priCls = priorityClass(t.priority);
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td><span class="pri-${priCls}">P${pri}</span></td>
        <td><code>${escapeHtml(t.file_path)}</code></td>
      `;
      tbody.appendChild(tr);
    });
    $('#plan-count').textContent = `${data.task_count || 0} files`;
    const empty = (data.task_count || 0) === 0;
    $('#plan-empty').textContent = 'No files matched the current min-priority filter.';
    $('#plan-empty').hidden = !empty;
    $('#plan-table').hidden = empty;
  }
  $('#plan-refresh').addEventListener('click', loadPlan);

  // --- config form -> CLI command ---
  function buildCli() {
    const form = new FormData($('#config-form'));
    const parts = ['advisor plan', shellQuote(form.get('target') || '.')];
    if (form.get('file_types') && form.get('file_types') !== '*.py') {
      parts.push(`--file-types ${shellQuote(form.get('file_types'))}`);
    }
    if (form.get('max_runners') && form.get('max_runners') !== '5') {
      parts.push(`--max-runners ${shellQuote(form.get('max_runners'))}`);
    }
    if (form.get('min_priority') && form.get('min_priority') !== '3') {
      parts.push(`--min-priority ${shellQuote(form.get('min_priority'))}`);
    }
    if (form.get('advisor_model') && form.get('advisor_model') !== 'opus') {
      parts.push(`--advisor-model ${shellQuote(form.get('advisor_model'))}`);
    }
    if (form.get('runner_model') && form.get('runner_model') !== 'sonnet') {
      parts.push(`--runner-model ${shellQuote(form.get('runner_model'))}`);
    }
    $('#cli-command').textContent = parts.join(' ');
  }
  $('#config-form').addEventListener('input', buildCli);
  $('#copy-cli').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('#cli-command').textContent);
      showToast('Copied to clipboard');
    } catch (_) {
      showToast('Copy failed \u2014 select manually');
    }
  });

  // --- cost ---
  function showCostError(message) {
    $('#cost-summary').innerHTML = '';
    $('#cost-empty').hidden = false;
    $('#cost-empty').textContent = message;
  }

  async function loadCost() {
    const form = new FormData($('#config-form'));
    const qs = new URLSearchParams();
    qs.set('file_types', form.get('file_types') || '*.py');
    qs.set('min_priority', form.get('min_priority') || '3');
    qs.set('advisor_model', form.get('advisor_model') || 'opus');
    qs.set('runner_model', form.get('runner_model') || 'sonnet');
    qs.set('max_runners', form.get('max_runners') || '5');
    let data;
    try {
      const r = await fetch('/api/cost?' + qs.toString());
      if (!r.ok) {
        showCostError('Error loading cost: ' + r.status);
        return;
      }
      data = await r.json();
    } catch (_) {
      showCostError('Error loading cost: network error');
      return;
    }
    if (!data.estimate) {
      $('#cost-summary').innerHTML = '';
      $('#cost-empty').hidden = false;
      $('#cost-empty').textContent = 'No plan yet — cost estimate needs ranked files.';
      return;
    }
    const e = data.estimate;
    $('#cost-empty').hidden = true;
    const minTok = (e.input_tokens_min || 0) + (e.output_tokens_min || 0);
    const maxTok = (e.input_tokens_max || 0) + (e.output_tokens_max || 0);
    $('#cost-summary').innerHTML = `
      <div class="cost-card"><h4>Files</h4><div class="val">${escapeHtml(data.task_count)}</div></div>
      <div class="cost-card"><h4>Runners</h4><div class="val">${escapeHtml(e.runner_count || '—')}</div></div>
      <div class="cost-card"><h4>Min tokens</h4><div class="val">${escapeHtml(formatNum(minTok))}</div></div>
      <div class="cost-card"><h4>Max tokens</h4><div class="val">${escapeHtml(formatNum(maxTok))}</div></div>
      <div class="cost-card"><h4>Min cost</h4><div class="val">$${escapeHtml(formatMoney(e.cost_usd_min))}</div></div>
      <div class="cost-card"><h4>Max cost</h4><div class="val">$${escapeHtml(formatMoney(e.cost_usd_max))}</div></div>
    `;
  }
  $('#cost-refresh').addEventListener('click', loadCost);

  // --- live events tab ---
  const LIVE_POLL_INTERVAL_MS = 2000;
  const LIVE_POLL_MAX_BACKOFF_MS = 30000;
  const LIVE_FEED_MAX_ITEMS = 500;
  let liveStreamCursor = 0;
  let liveStreamTimer = null;
  let liveStreamEnabled = true;
  let liveStreamErrorStreak = 0;
  let liveFeedCount = 0;
  let liveFeedSeenSeqs = new Set();

  function setLiveStreamState(state, labelOverride) {
    const pill = $('#live-stream-indicator');
    if (!pill) return;
    pill.classList.remove('idle', 'active', 'paused', 'error');
    pill.classList.add(state);
    pill.querySelector('.live-label').textContent = labelOverride || state.toUpperCase();
  }

  function scheduleLiveStreamPoll(delayMs) {
    if (liveStreamTimer) clearTimeout(liveStreamTimer);
    liveStreamTimer = setTimeout(liveStreamTick, Math.max(0, delayMs));
  }

  function renderEventBody(ev) {
    const d = (ev && typeof ev.data === 'object' && ev.data !== null) ? ev.data : {};
    const kind = ev.kind || '';
    const parts = [];
    if (kind === 'run_start') {
      if (d.run_id) parts.push(`run <code>${escapeHtml(d.run_id)}</code>`);
      if (d.pool_size_advisory) parts.push(`pool ${escapeHtml(d.pool_size_advisory)}`);
      if (d.advisor_model) parts.push(`advisor=${escapeHtml(d.advisor_model)}`);
      if (d.runner_model) parts.push(`runner=${escapeHtml(d.runner_model)}`);
    } else if (kind === 'runner_spawn') {
      if (d.runner_name) parts.push(`<code>${escapeHtml(d.runner_name)}</code>`);
      if (d.model) parts.push(`(${escapeHtml(d.model)})`);
      if (Array.isArray(d.batch_files)) parts.push(`${d.batch_files.length} files`);
    } else if (kind === 'report_relay') {
      if (d.runner_name) parts.push(`<code>${escapeHtml(d.runner_name)}</code>`);
      if (typeof d.finding_count === 'number') parts.push(`${d.finding_count} findings`);
      if (d.summary) parts.push(escapeHtml(d.summary));
    } else if (kind === 'fix_dispatch') {
      if (d.runner_name) parts.push(`<code>${escapeHtml(d.runner_name)}</code>`);
      if (d.file) parts.push(`<code>${escapeHtml(d.file)}</code>`);
      if (d.problem) parts.push(escapeHtml(d.problem));
    } else if (kind === 'run_end') {
      if (typeof d.findings_confirmed === 'number') parts.push(`${d.findings_confirmed} confirmed`);
      if (typeof d.findings_rejected === 'number') parts.push(`${d.findings_rejected} rejected`);
      if (typeof d.fixes_landed === 'number') parts.push(`${d.fixes_landed} fixes`);
    }
    let summary = parts.length ? parts.join(' · ') : escapeHtml(kind || 'event');
    const hasExtras = Object.keys(d).length > 0;
    const extras = hasExtras ? `<span class="feed-data">${escapeHtml(JSON.stringify(d))}</span>` : '';
    return summary + extras;
  }

  function appendLiveEvents(events) {
    const feed = $('#live-feed');
    if (!feed) return;
    let appended = 0;
    events.forEach((ev) => {
      const seq = typeof ev.seq === 'number' ? ev.seq : null;
      if (seq !== null) {
        if (liveFeedSeenSeqs.has(seq)) return;
        liveFeedSeenSeqs.add(seq);
      }
      const li = document.createElement('li');
      li.className = 'feed-item feed-new';
      const ts = ev.ts || '';
      const tsShort = ts.length >= 19 ? ts.slice(11, 19) : ts;
      const safeKind = String(ev.kind || 'event').replace(/[^A-Za-z0-9_-]/g, '');
      li.innerHTML = `
        <span class="feed-time" title="${escapeHtml(ts)}">${escapeHtml(tsShort)}</span>
        <span class="feed-kind kind-${safeKind}">${escapeHtml(ev.kind || 'event')}</span>
        <span class="feed-body">${renderEventBody(ev)}</span>
      `;
      feed.insertBefore(li, feed.firstChild);
      setTimeout(() => li.classList.remove('feed-new'), 2100);
      appended += 1;
    });
    liveFeedCount += appended;
    while (feed.childElementCount > LIVE_FEED_MAX_ITEMS) {
      const removed = feed.lastElementChild;
      if (!removed) break;
      feed.removeChild(removed);
    }
    if (liveFeedSeenSeqs.size > LIVE_FEED_MAX_ITEMS * 2) {
      const arr = Array.from(liveFeedSeenSeqs);
      liveFeedSeenSeqs = new Set(arr.slice(-LIVE_FEED_MAX_ITEMS));
    }
    $('#live-count').textContent = `${liveFeedCount} event${liveFeedCount === 1 ? '' : 's'}`;
    $('#live-empty').hidden = liveFeedCount > 0;
  }

  async function liveStreamTick() {
    liveStreamTimer = null;
    if (!liveStreamEnabled) { setLiveStreamState('paused', 'PAUSED'); return; }
    if (document.hidden) { scheduleLiveStreamPoll(LIVE_POLL_INTERVAL_MS); return; }
    let nextDelay = LIVE_POLL_INTERVAL_MS;
    try {
      const qs = new URLSearchParams();
      if (liveStreamCursor > 0) qs.set('since', String(liveStreamCursor));
      qs.set('limit', '200');
      const r = await fetch('/api/events?' + qs.toString(), { cache: 'no-store' });
      if (!r.ok) throw new Error('events ' + r.status);
      const data = await r.json();
      if (Array.isArray(data.events) && data.events.length) {
        appendLiveEvents(data.events);
        setLiveStreamState('active', 'LIVE');
      } else if (liveFeedCount === 0) {
        setLiveStreamState('idle', 'IDLE');
      } else {
        setLiveStreamState('active', 'LIVE');
      }
      if (typeof data.next_token === 'number' && data.next_token >= 0) {
        liveStreamCursor = data.next_token;
      }
      liveStreamErrorStreak = 0;
    } catch (_) {
      setLiveStreamState('error', 'ERROR');
      liveStreamErrorStreak += 1;
      const backoff = LIVE_POLL_INTERVAL_MS * Math.pow(2, liveStreamErrorStreak - 1);
      nextDelay = Math.min(LIVE_POLL_MAX_BACKOFF_MS, backoff);
    }
    scheduleLiveStreamPoll(nextDelay);
  }

  function clearLiveFeed() {
    const feed = $('#live-feed');
    if (feed) feed.innerHTML = '';
    liveFeedCount = 0;
    liveFeedSeenSeqs = new Set();
    $('#live-count').textContent = '';
    $('#live-empty').hidden = false;
  }
  $('#live-clear').addEventListener('click', clearLiveFeed);

  const liveStreamPill = $('#live-stream-indicator');
  function toggleLiveStream() {
    liveStreamEnabled = !liveStreamEnabled;
    if (liveStreamEnabled) scheduleLiveStreamPoll(0);
    else setLiveStreamState('paused', 'PAUSED');
  }
  liveStreamPill.addEventListener('click', toggleLiveStream);
  liveStreamPill.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleLiveStream(); }
  });

  // --- helpers ---
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
  const SEV_ALLOW = new Set(['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']);
  function severityClass(s) {
    const upper = (s || '').toUpperCase();
    return SEV_ALLOW.has(upper) ? upper : 'LOW';
  }
  const PRI_ALLOW = new Set(['1', '2', '3', '4', '5']);
  function priorityClass(p) {
    const s = String(p);
    return PRI_ALLOW.has(s) ? s : '0';
  }
  function shellQuote(s) {
    s = String(s);
    if (/^[A-Za-z0-9_./@:=-]+$/.test(s)) return s;
    return "'" + s.replace(/'/g, "'\\''") + "'";
  }
  function formatNum(n) {
    if (n == null) return '—';
    return Number(n).toLocaleString();
  }
  function formatMoney(usd) {
    if (usd == null) return '—';
    return Number(usd).toFixed(4);
  }

  // --- init ---
  scheduleLiveStreamPoll(0);

  (async () => {
    try {
      const r = await fetch('/api/target');
      if (r.ok) {
        const t = await r.json();
        $('#target-path').textContent = t.target || '.';
        $('input[name="target"]').value = t.target || '.';
        buildCli();
      }
    } catch (_) {}
    buildCli();
    schedulePoll(0);
  })();
})();
