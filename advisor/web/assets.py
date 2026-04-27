"""Static HTML/CSS/JS assets for the dashboard.

Kept as Python string constants so the wheel doesn't need extra package-data
wiring and the server can serve them from memory without filesystem lookups.
"""

from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>advisor dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<header>
  <h1>advisor</h1>
  <nav>
    <button class="tab active" data-tab="findings">Findings</button>
    <button class="tab" data-tab="plan">Plan</button>
    <button class="tab" data-tab="config">Run config</button>
    <button class="tab" data-tab="cost">Cost</button>
  </nav>
  <div class="target">target: <code id="target-path">loading&hellip;</code></div>
</header>

<main>
  <section id="findings" class="panel active">
    <div class="controls">
      <input id="findings-search" type="search" placeholder="filter by file or description">
      <select id="findings-severity">
        <option value="">all severities</option>
        <option>CRITICAL</option>
        <option>HIGH</option>
        <option>MEDIUM</option>
        <option>LOW</option>
      </select>
      <span id="live-indicator" class="live idle" title="click to toggle live updates" role="button" tabindex="0">
        <span class="live-dot"></span>
        <span class="live-label">IDLE</span>
      </span>
      <span id="live-updated" class="live-updated"></span>
      <span id="findings-count" class="count"></span>
    </div>
    <table id="findings-table">
      <thead><tr>
        <th>When</th><th>File</th><th>Severity</th><th>Status</th><th>Description</th>
      </tr></thead>
      <tbody></tbody>
    </table>
    <p id="findings-empty" class="empty" hidden>No findings yet. Run the advisor on this target first.</p>
  </section>

  <section id="plan" class="panel">
    <div class="controls">
      <button id="plan-refresh">Refresh plan</button>
      <span id="plan-count" class="count"></span>
    </div>
    <table id="plan-table">
      <thead><tr><th>#</th><th>Priority</th><th>File</th></tr></thead>
      <tbody></tbody>
    </table>
    <p id="plan-empty" class="empty" hidden>No files matched the current min-priority filter.</p>
  </section>

  <section id="config" class="panel">
    <form id="config-form">
      <label>Target directory
        <input name="target" type="text" value=".">
      </label>
      <label>File types
        <input name="file_types" type="text" value="*.py">
      </label>
      <label>Max runners
        <input name="max_runners" type="number" value="5" min="1">
      </label>
      <label>Min priority
        <input name="min_priority" type="number" value="3" min="1" max="5">
      </label>
      <label>Advisor model
        <input name="advisor_model" type="text" value="opus">
      </label>
      <label>Runner model
        <input name="runner_model" type="text" value="sonnet">
      </label>
    </form>
    <div class="cli-preview">
      <h3>CLI command</h3>
      <pre id="cli-command"></pre>
      <button id="copy-cli">Copy to clipboard</button>
      <span id="copy-feedback" class="feedback"></span>
    </div>
  </section>

  <section id="cost" class="panel">
    <div class="controls">
      <button id="cost-refresh">Estimate cost</button>
    </div>
    <div id="cost-summary"></div>
    <p id="cost-empty" class="empty" hidden>No plan yet — cost estimate needs ranked files.</p>
  </section>
</main>

<script src="/static/app.js"></script>
</body>
</html>
"""


APP_CSS = """:root {
  --bg: #0e1116;
  --panel: #161b22;
  --border: #30363d;
  --text: #e6edf3;
  --muted: #8b949e;
  --accent: #58a6ff;
  --crit: #f85149;
  --high: #db6d28;
  --med: #d29922;
  --low: #3fb950;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--text);
}
header {
  padding: 1rem 1.5rem;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 1.5rem;
  flex-wrap: wrap;
}
header h1 { margin: 0; font-size: 1.25rem; letter-spacing: 0.05em; }
nav { display: flex; gap: 0.25rem; }
.tab {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  padding: 0.4rem 0.9rem;
  cursor: pointer;
  font-size: 0.9rem;
  border-radius: 4px;
}
.tab.active { color: var(--text); border-color: var(--accent); }
.target { margin-left: auto; color: var(--muted); font-size: 0.85rem; }
.target code { color: var(--accent); }

main { padding: 1.5rem; max-width: 1100px; margin: 0 auto; }
.panel { display: none; }
.panel.active { display: block; }

.controls {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}
.controls input, .controls select, .controls button {
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--border);
  padding: 0.45rem 0.7rem;
  border-radius: 4px;
  font-size: 0.9rem;
}
.controls input[type="search"] { min-width: 260px; }
.controls button { cursor: pointer; }
.controls button:hover { border-color: var(--accent); }
.count { color: var(--muted); font-size: 0.85rem; margin-left: auto; }

table {
  width: 100%;
  border-collapse: collapse;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
  font-size: 0.9rem;
}
thead { background: #1f242c; }
th, td {
  text-align: left;
  padding: 0.6rem 0.8rem;
  border-bottom: 1px solid var(--border);
}
tr:last-child td { border-bottom: 0; }
td code { color: var(--accent); }

.sev { font-weight: 600; padding: 2px 6px; border-radius: 3px; font-size: 0.75rem; }
.sev-CRITICAL { color: var(--crit); border: 1px solid var(--crit); }
.sev-HIGH { color: var(--high); border: 1px solid var(--high); }
.sev-MEDIUM { color: var(--med); border: 1px solid var(--med); }
.sev-LOW { color: var(--low); border: 1px solid var(--low); }

.pri-5 { color: var(--crit); font-weight: 600; }
.pri-4 { color: var(--high); font-weight: 600; }
.pri-3 { color: var(--med); }
.pri-2, .pri-1 { color: var(--muted); }

.empty { color: var(--muted); font-style: italic; padding: 1rem 0; }

/* --- live indicator --- */
.live {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.35rem 0.7rem;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  font-weight: 600;
  cursor: pointer;
  user-select: none;
  color: var(--muted);
  background: var(--panel);
  transition: color 0.15s, border-color 0.15s;
}
.live:hover { border-color: var(--accent); }
.live:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--muted);
  box-shadow: 0 0 0 0 transparent;
}
.live.active { color: var(--low); border-color: var(--low); }
.live.active .live-dot {
  background: var(--low);
  animation: live-pulse 1.4s ease-out infinite;
}
.live.paused { color: var(--med); border-color: var(--med); }
.live.paused .live-dot { background: var(--med); }
.live.error { color: var(--crit); border-color: var(--crit); }
.live.error .live-dot { background: var(--crit); }

@keyframes live-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.55); }
  70%  { box-shadow: 0 0 0 7px rgba(63, 185, 80, 0); }
  100% { box-shadow: 0 0 0 0 rgba(63, 185, 80, 0); }
}

.live-updated {
  color: var(--muted);
  font-size: 0.75rem;
  font-variant-numeric: tabular-nums;
}

/* Newly-arrived rows briefly flash, then settle. The animation runs once
   via JS adding the `.row-new` class; JS removes it after ~2s so a row
   animates again only when it's genuinely new on a later poll. */
@keyframes row-flash {
  0%   { background: rgba(88, 166, 255, 0.22); }
  100% { background: transparent; }
}
tr.row-new td { animation: row-flash 2s ease-out; }

/* Respect users who asked the OS for reduced motion — no pulses or flashes. */
@media (prefers-reduced-motion: reduce) {
  .live.active .live-dot { animation: none; }
  tr.row-new td { animation: none; }
}

form {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 0.8rem;
  background: var(--panel);
  border: 1px solid var(--border);
  padding: 1rem;
  border-radius: 6px;
}
form label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: 0.85rem;
  color: var(--muted);
}
form input {
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  padding: 0.4rem 0.6rem;
  border-radius: 4px;
  font-family: inherit;
  font-size: 0.9rem;
}
.cli-preview { margin-top: 1.2rem; }
.cli-preview h3 { margin: 0 0 0.5rem; font-size: 0.95rem; }
pre {
  background: var(--panel);
  border: 1px solid var(--border);
  padding: 0.8rem 1rem;
  border-radius: 6px;
  overflow-x: auto;
  color: var(--accent);
  font-size: 0.9rem;
}
.feedback { margin-left: 0.6rem; color: var(--low); font-size: 0.85rem; }

#cost-summary { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 0.8rem; }
.cost-card {
  background: var(--panel);
  border: 1px solid var(--border);
  padding: 0.9rem 1rem;
  border-radius: 6px;
}
.cost-card h4 { margin: 0 0 0.4rem; font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.cost-card .val { font-size: 1.2rem; font-weight: 600; color: var(--accent); }
"""


APP_JS = r"""(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // --- tab switching ---
  $$('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      $$('.tab').forEach((b) => b.classList.remove('active'));
      $$('.panel').forEach((p) => p.classList.remove('active'));
      btn.classList.add('active');
      $('#' + btn.dataset.tab).classList.add('active');
      // Poll is only useful on the findings tab; kick it immediately when
      // the user switches to that tab so they don't wait a full interval.
      if (btn.dataset.tab === 'findings') schedulePoll(0);
    });
  });

  // --- findings ---
  let findingsRaw = [];
  let findingsErrorMessage = '';
  // Keys of entries we've already seen in a previous render. Entries whose
  // key is NOT in here get the `.row-new` highlight on the next render.
  // See `findingKey` below for the identity contract.
  let seenKeys = new Set();

  // --------------------------------------------------------------
  // USER CONTRIBUTION POINT — identity of a finding for "is this new?"
  //
  // Returns a stable string key for one history entry. The poll loop
  // diffs incoming entries against `seenKeys`; anything whose key isn't
  // in the set is treated as new and gets the yellow-flash animation.
  //
  // Design trade-offs (pick one):
  //   (a) `${entry.timestamp}|${entry.file_path}|${entry.description}`
  //       — safest: two entries collide only if they are effectively the
  //         same finding. Flash is stable across filter changes.
  //   (b) `${entry.run_id}|${entry.file_path}|${entry.severity}`
  //       — groups by run, ignores description edits. Cleaner if a
  //         runner rewrites descriptions mid-run.
  //   (c) `${entry.timestamp}|${entry.file_path}`
  //       — loosest: collides when one run flags the same file twice in
  //         the same second. Cheapest to compute, lossiest correctness.
  //
  // Return "" to disable highlighting for this entry.
  // --------------------------------------------------------------
  function findingKey(entry) {
    // Tight identity: two entries are "the same finding" only when
    // timestamp, file, AND description all match. Rationale:
    //   - timestamp alone collides when two findings land in the same
    //     second (common during a noisy run).
    //   - file_path alone collides across unrelated findings in one file.
    //   - adding description makes description-rewrites re-flash, which
    //     is the correct UX: if the user-visible text changed, the row
    //     really is "new" to the reader.
    // `\u241f` (SYMBOL FOR UNIT SEPARATOR) is a zero-risk delimiter —
    // never appears in timestamps, paths, or natural-language descriptions.
    const ts = entry.timestamp || '';
    const fp = entry.file_path || '';
    const desc = entry.description || '';
    return ts + '\u241f' + fp + '\u241f' + desc;
  }

  function renderFindings() {
    const q = $('#findings-search').value.toLowerCase();
    const sev = $('#findings-severity').value;
    const filtered = findingsRaw.filter((e) => {
      if (sev && (e.severity || '').toUpperCase() !== sev) return false;
      if (!q) return true;
      return (
        (e.file_path || '').toLowerCase().includes(q) ||
        (e.description || '').toLowerCase().includes(q)
      );
    });
    const tbody = $('#findings-table tbody');
    tbody.innerHTML = '';
    filtered.forEach((e) => {
      const key = findingKey(e);
      // seenKeys.size === 0 means initial load — suppress flash so existing
      // findings don't all light up blue on page open.
      const isNew = seenKeys.size > 0 && key && !seenKeys.has(key);
      const tr = document.createElement('tr');
      if (isNew) tr.classList.add('row-new');
      tr.innerHTML = `
        <td>${escapeHtml(e.timestamp || '')}</td>
        <td><code>${escapeHtml(e.file_path || '')}</code></td>
        <td><span class="sev sev-${severityClass(e.severity)}">${escapeHtml(e.severity || '')}</span></td>
        <td>${escapeHtml(e.status || '')}</td>
        <td>${escapeHtml(e.description || '')}</td>
      `;
      tbody.appendChild(tr);
      if (isNew) setTimeout(() => tr.classList.remove('row-new'), 2100);
    });
    // Track ALL raw entries (not just the filtered view) so clearing a filter
    // never re-flashes entries that were merely hidden by the current query.
    findingsRaw.forEach((e) => { const k = findingKey(e); if (k) seenKeys.add(k); });
    $('#findings-count').textContent = `${filtered.length} of ${findingsRaw.length}`;
    const hasVisibleFindings = filtered.length !== 0;
    $('#findings-empty').textContent = findingsErrorMessage || (findingsRaw.length === 0
      ? 'No findings yet. Run the advisor on this target first.'
      : 'No findings match the current filters.');
    $('#findings-empty').hidden = hasVisibleFindings;
    $('#findings-table').hidden = !hasVisibleFindings;
  }
  $('#findings-search').addEventListener('input', renderFindings);
  $('#findings-severity').addEventListener('change', renderFindings);

  function showFindingsError(message) {
    findingsRaw = [];
    findingsErrorMessage = message;
    $('#findings-table tbody').innerHTML = '';
    $('#findings-count').textContent = '0 of 0';
    $('#findings-empty').textContent = message;
    $('#findings-empty').hidden = false;
    $('#findings-table').hidden = true;
  }

  // --- live poll loop ---
  // Every POLL_INTERVAL_MS we hit /api/status (cheap). Only when the
  // server reports a changed mtime do we fetch /api/history (expensive).
  // On consecutive /api/status failures we apply exponential backoff
  // (doubling up to POLL_MAX_BACKOFF_MS) so a machine under disk-sleep
  // or a temporarily-down server doesn't eat bandwidth. Any success
  // resets the backoff to the normal interval.
  const POLL_INTERVAL_MS = 3000;
  const POLL_MAX_BACKOFF_MS = 30000;
  let pollErrorStreak = 0;
  let lastMtime = null;
  let pollTimer = null;
  let liveEnabled = true;
  let lastStatus = null;      // last successful /api/status payload
  let lastStatusTs = null;    // wall-clock time of that fetch (ms)

  function setLiveState(state, labelOverride) {
    const pill = $('#live-indicator');
    pill.classList.remove('idle', 'active', 'paused', 'error');
    pill.classList.add(state);
    // The ``$`` helper takes a single selector and queries from
    // ``document``; the previous ``$('.live-label', pill)`` silently
    // dropped the second argument, so the lookup escaped the pill
    // scope. Use ``pill.querySelector`` directly so the lookup is
    // genuinely scoped — matters once a future page has more than one
    // ``.live-label`` element on it.
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
      setLiveState(data.is_active ? 'active' : 'idle',
                   data.is_active ? 'LIVE' : 'IDLE');
      if (data.last_mtime !== lastMtime) {
        if (await refetchFindings()) {
          lastMtime = data.last_mtime;
        }
      }
      pollErrorStreak = 0;
    } catch (_) {
      setLiveState('error', 'ERROR');
      pollErrorStreak += 1;
      // 3s → 6s → 12s → 24s → cap at 30s. Keep it deterministic (no
      // jitter) since this is a single-client local dashboard; jitter
      // would only obscure the backoff state during debugging.
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

  // "Updated Ns ago" ticker — refreshes once per second, independent of
  // the poll interval, so the label feels live even between fetches.
  function renderUpdatedLabel() {
    const el = $('#live-updated');
    if (!lastStatusTs) { el.textContent = ''; return; }
    const secs = Math.floor((Date.now() - lastStatusTs) / 1000);
    el.textContent = 'updated ' + (secs <= 1 ? 'just now' : secs + 's ago');
  }
  setInterval(renderUpdatedLabel, 1000);

  // Click / keyboard-toggle the LIVE pill to pause and resume polling.
  const pill = $('#live-indicator');
  function togglePolling() {
    liveEnabled = !liveEnabled;
    if (liveEnabled) schedulePoll(0); else setLiveState('paused', 'PAUSED');
  }
  pill.addEventListener('click', togglePolling);
  pill.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); togglePolling(); }
  });

  // Browser pauses setInterval in background tabs anyway, but reacting to
  // visibilitychange lets us resume *immediately* on tab focus rather than
  // waiting for the next throttled tick.
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
    // Server binds to a single target at launch (see AppState); only the
    // ranking knobs round-trip to /api/plan.
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
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td><span class="pri-${pri}">P${pri}</span></td>
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
      parts.push(`--max-runners ${form.get('max_runners')}`);
    }
    if (form.get('min_priority') && form.get('min_priority') !== '3') {
      parts.push(`--min-priority ${form.get('min_priority')}`);
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
      $('#copy-feedback').textContent = 'copied';
      setTimeout(() => { $('#copy-feedback').textContent = ''; }, 1500);
    } catch (_) {
      $('#copy-feedback').textContent = 'copy failed — select manually';
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
  function shellQuote(s) {
    s = String(s);
    if (/^[A-Za-z0-9_./*@:=-]+$/.test(s)) return s;
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
    // Kick the poll loop — it fetches /api/status, then /api/history
    // (only if mtime has changed from the sentinel `null`, which it
    // always has on first load). This replaces the old single-shot
    // /api/history fetch so there's exactly one code path that
    // populates the table.
    schedulePoll(0);
  })();
})();
"""
