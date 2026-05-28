"""Static HTML/CSS/JS assets for the dashboard.

Kept as Python string constants so the wheel doesn't need extra package-data
wiring and the server can serve them from memory without filesystem lookups.
"""

from __future__ import annotations

INDEX_HTML = """<!doctype html>
<html lang="en" data-theme="dark">
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
    <button class="tab active" data-tab="findings" title="Findings (1)">Findings</button>
    <button class="tab" data-tab="live" title="Live (2)">Live</button>
    <button class="tab" data-tab="plan" title="Plan (3)">Plan</button>
    <button class="tab" data-tab="config" title="Run config (4)">Run config</button>
    <button class="tab" data-tab="cost" title="Cost (5)">Cost</button>
  </nav>
  <div class="header-actions">
    <button id="theme-toggle" class="icon-btn" title="Toggle theme (T)" aria-label="Toggle theme">
      <svg id="theme-icon-dark" width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M6 .278a.768.768 0 0 1 .08.858 7.208 7.208 0 0 0-.878 3.46c0 4.021 3.278 7.277 7.318 7.277.527 0 1.04-.055 1.533-.16a.787.787 0 0 1 .81.316.733.733 0 0 1-.031.893A8.349 8.349 0 0 1 8.344 16C3.734 16 0 12.286 0 7.71 0 4.266 2.114 1.312 5.124.06A.752.752 0 0 1 6 .278z"/></svg>
      <svg id="theme-icon-light" width="16" height="16" viewBox="0 0 16 16" fill="currentColor" hidden><path d="M8 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM8 0a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 0zm0 13a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 13zm8-5a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2a.5.5 0 0 1 .5.5zM3 8a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2A.5.5 0 0 1 3 8zm10.657-5.657a.5.5 0 0 1 0 .707l-1.414 1.415a.5.5 0 1 1-.707-.708l1.414-1.414a.5.5 0 0 1 .707 0zm-9.193 9.193a.5.5 0 0 1 0 .707L3.05 13.657a.5.5 0 0 1-.707-.707l1.414-1.414a.5.5 0 0 1 .707 0zm9.193 2.121a.5.5 0 0 1-.707 0l-1.414-1.414a.5.5 0 0 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .707zM4.464 4.465a.5.5 0 0 1-.707 0L2.343 3.05a.5.5 0 1 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .708z"/></svg>
    </button>
    <button id="shortcuts-btn" class="icon-btn" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M14 5a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h12zM2 4a2 2 0 0 0-2 2v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2H2z"/><path d="M13 10.25a.25.25 0 0 1 .25-.25h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5a.25.25 0 0 1-.25-.25v-.5zm0-2a.25.25 0 0 1 .25-.25h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5a.25.25 0 0 1-.25-.25v-.5zm-5 0A.25.25 0 0 1 8.25 8h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5A.25.25 0 0 1 8 8.75v-.5zm2 0a.25.25 0 0 1 .25-.25h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5a.25.25 0 0 1-.25-.25v-.5zm-5 0A.25.25 0 0 1 5.25 8h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5A.25.25 0 0 1 5 8.75v-.5zm-2 0A.25.25 0 0 1 3.25 8h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5A.25.25 0 0 1 3 8.75v-.5zm-1 2a.25.25 0 0 1 .25-.25h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5a.25.25 0 0 1-.25-.25v-.5zm11-4a.25.25 0 0 1 .25-.25h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5a.25.25 0 0 1-.25-.25v-.5zm-2 0a.25.25 0 0 1 .25-.25h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5a.25.25 0 0 1-.25-.25v-.5zm-2 0A.25.25 0 0 1 6.25 6h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5A.25.25 0 0 1 6 6.75v-.5zm-2 0A.25.25 0 0 1 4.25 6h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5A.25.25 0 0 1 4 6.75v-.5zm-2 0A.25.25 0 0 1 2.25 6h.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-.5A.25.25 0 0 1 2 6.75v-.5zm1 2a.25.25 0 0 1 .25-.25h5.5a.25.25 0 0 1 .25.25v.5a.25.25 0 0 1-.25.25h-5.5a.25.25 0 0 1-.25-.25v-.5z"/></svg>
    </button>
  </div>
  <div class="target">target: <code id="target-path">loading&hellip;</code></div>
</header>

<main>
  <section id="findings" class="panel active">
    <div id="severity-stats" class="severity-stats" hidden></div>
    <div class="controls">
      <input id="findings-search" type="search" placeholder="filter by file or description&hellip;" aria-label="Filter findings">
      <select id="findings-severity" aria-label="Filter by severity">
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
      <button id="export-csv" class="btn-secondary" title="Export findings to CSV (E)">Export CSV</button>
      <span id="findings-count" class="count"></span>
    </div>
    <table id="findings-table">
      <thead><tr>
        <th class="sortable" data-sort="timestamp">When <span class="sort-arrow"></span></th>
        <th class="sortable" data-sort="file_path">File <span class="sort-arrow"></span></th>
        <th class="sortable" data-sort="severity">Severity <span class="sort-arrow"></span></th>
        <th class="sortable" data-sort="status">Status <span class="sort-arrow"></span></th>
        <th>Description</th>
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
    <p class="hint">These fields shape the CLI command preview below — the
      running dashboard stays bound to the directory you launched
      <code>advisor ui</code> with. Restart with a different argument to
      change scope.</p>
    <form id="config-form">
      <label>Target directory
        <input name="target" type="text" value="." readonly aria-readonly="true">
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
      <p class="hint" style="margin-top:0.8rem"><code>advisor plan</code> ranks files locally (what this dashboard shows). To run the full review-and-fix pipeline with live agents, type <code>/advisor &lt;path&gt;</code> in Claude Code instead.</p>
    </div>
  </section>

  <section id="cost" class="panel">
    <div class="controls">
      <button id="cost-refresh">Estimate cost</button>
    </div>
    <p class="hint">Min = explore pass only; Max = full fix wave per runner. Actual cost lands in this range based on how many findings Opus confirms and whether the user asks for fixes.</p>
    <div id="cost-summary"></div>
    <p id="cost-empty" class="empty">Click <strong>Estimate cost</strong> to project token usage and dollar cost for the files in scope. The estimate loads automatically on first visit.</p>
  </section>

  <section id="live" class="panel">
    <div class="controls">
      <span id="live-stream-indicator" class="live idle" title="click to toggle polling" role="button" tabindex="0">
        <span class="live-dot"></span>
        <span class="live-label">IDLE</span>
      </span>
      <button id="live-clear" type="button">Clear feed</button>
      <span id="live-count" class="count"></span>
    </div>
    <ul id="live-feed" class="feed"></ul>
    <p id="live-empty" class="empty">No live events yet. Start a review run with <code>/advisor .</code> in Claude Code &mdash; events appear here in real time as the advisor and runners work. If this tab stays quiet after a run, refresh your skill with <code>advisor install</code>.</p>
  </section>
</main>

<div id="shortcuts-modal" class="modal" hidden>
  <div class="modal-backdrop"></div>
  <div class="modal-content">
    <h2>Keyboard shortcuts</h2>
    <div class="shortcuts-grid">
      <div class="shortcut-group">
        <h3>Navigation</h3>
        <dl>
          <dt><kbd>1</kbd>–<kbd>5</kbd></dt><dd>Switch tabs</dd>
          <dt><kbd>/</kbd></dt><dd>Focus search</dd>
          <dt><kbd>Esc</kbd></dt><dd>Close modal / blur search</dd>
        </dl>
      </div>
      <div class="shortcut-group">
        <h3>Actions</h3>
        <dl>
          <dt><kbd>E</kbd></dt><dd>Export findings to CSV</dd>
          <dt><kbd>T</kbd></dt><dd>Toggle theme</dd>
          <dt><kbd>R</kbd></dt><dd>Refresh current tab</dd>
          <dt><kbd>?</kbd></dt><dd>Toggle this help</dd>
        </dl>
      </div>
    </div>
    <button class="modal-close" aria-label="Close">&times;</button>
  </div>
</div>

<div id="toast-container" class="toast-container"></div>

<script src="/static/app.js"></script>
</body>
</html>
"""


APP_CSS = """:root,
[data-theme="dark"] {
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
  --thead-bg: #1f242c;
  --hover-bg: rgba(88, 166, 255, 0.06);
  --overlay: rgba(0, 0, 0, 0.6);
}
[data-theme="light"] {
  --bg: #ffffff;
  --panel: #f6f8fa;
  --border: #d0d7de;
  --text: #1f2328;
  --muted: #656d76;
  --accent: #0969da;
  --crit: #cf222e;
  --high: #bc4c00;
  --med: #9a6700;
  --low: #1a7f37;
  --thead-bg: #f0f3f6;
  --hover-bg: rgba(9, 105, 218, 0.04);
  --overlay: rgba(0, 0, 0, 0.3);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--text);
  transition: background 0.2s, color 0.2s;
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
  transition: color 0.15s, border-color 0.15s;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--text); border-color: var(--accent); }
.header-actions { display: flex; gap: 0.4rem; margin-left: auto; }
.icon-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.icon-btn:hover { color: var(--text); border-color: var(--accent); }
.target { color: var(--muted); font-size: 0.85rem; width: 100%; }
.target code { color: var(--accent); }

main { padding: 1.5rem; max-width: 1100px; margin: 0 auto; }
.panel { display: none; }
.panel.active { display: block; }

/* --- severity stats bar --- */
.severity-stats {
  display: flex;
  gap: 0.6rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}
.stat-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.4rem 0.75rem;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 0.82rem;
  font-weight: 500;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.1s;
  user-select: none;
}
.stat-badge:hover { transform: translateY(-1px); }
.stat-badge.active { border-width: 2px; padding: 0.35rem 0.7rem; }
.stat-badge .stat-count {
  font-weight: 700;
  font-size: 1rem;
  font-variant-numeric: tabular-nums;
}
.stat-badge.sev-CRITICAL { color: var(--crit); }
.stat-badge.sev-CRITICAL.active { border-color: var(--crit); }
.stat-badge.sev-HIGH { color: var(--high); }
.stat-badge.sev-HIGH.active { border-color: var(--high); }
.stat-badge.sev-MEDIUM { color: var(--med); }
.stat-badge.sev-MEDIUM.active { border-color: var(--med); }
.stat-badge.sev-LOW { color: var(--low); }
.stat-badge.sev-LOW.active { border-color: var(--low); }
.stat-badge.sev-total { color: var(--text); }

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
.controls input[type="search"] { min-width: 220px; flex: 1; max-width: 320px; }
.controls button { cursor: pointer; transition: border-color 0.15s; }
.controls button:hover { border-color: var(--accent); }
.btn-secondary {
  background: var(--panel);
  color: var(--muted);
  border: 1px solid var(--border);
  padding: 0.4rem 0.7rem;
  border-radius: 4px;
  font-size: 0.82rem;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.btn-secondary:hover { color: var(--text); border-color: var(--accent); }
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
thead { background: var(--thead-bg); }
th, td {
  text-align: left;
  padding: 0.6rem 0.8rem;
  border-bottom: 1px solid var(--border);
}
tr:last-child td { border-bottom: 0; }
td code { color: var(--accent); }

/* --- sortable columns --- */
th.sortable {
  cursor: pointer;
  user-select: none;
  transition: color 0.15s;
  white-space: nowrap;
}
th.sortable:hover { color: var(--accent); }
th.sortable .sort-arrow { font-size: 0.7rem; margin-left: 0.3rem; opacity: 0.4; }
th.sortable.sort-asc .sort-arrow::after { content: "\\25B2"; opacity: 1; }
th.sortable.sort-desc .sort-arrow::after { content: "\\25BC"; opacity: 1; }

/* --- expandable row detail --- */
tr.expandable { cursor: pointer; transition: background 0.1s; }
tr.expandable:hover { background: var(--hover-bg); }
tr.detail-row td {
  padding: 0;
  border-bottom: 1px solid var(--border);
}
.detail-content {
  padding: 0.8rem 1rem;
  background: var(--bg);
  border-top: 1px solid var(--border);
  font-size: 0.85rem;
  line-height: 1.5;
}
.detail-content dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.3rem 1rem;
  margin: 0;
}
.detail-content dt {
  color: var(--muted);
  font-weight: 600;
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.detail-content dd { margin: 0; word-break: break-word; }
.detail-content dd code {
  background: var(--panel);
  padding: 0.1rem 0.35rem;
  border-radius: 3px;
  font-size: 0.82rem;
}

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

@keyframes row-flash {
  0%   { background: rgba(88, 166, 255, 0.22); }
  100% { background: transparent; }
}
tr.row-new td { animation: row-flash 2s ease-out; }

@media (prefers-reduced-motion: reduce) {
  .live.active .live-dot { animation: none; }
  tr.row-new td { animation: none; }
  .feed-item.feed-new { animation: none; }
  .toast { animation: none; }
}

/* --- live feed (events tab) --- */
.feed {
  list-style: none;
  margin: 0;
  padding: 0;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
  font-size: 0.88rem;
}
.feed-item {
  display: grid;
  grid-template-columns: 8.5rem 7rem 1fr;
  gap: 0.8rem;
  padding: 0.55rem 0.8rem;
  border-top: 1px solid var(--border);
  align-items: baseline;
}
.feed-item:first-child { border-top: none; }
.feed-time {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  font-size: 0.78rem;
  white-space: nowrap;
}
.feed-kind {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 0.78rem;
  color: var(--accent);
  letter-spacing: 0.04em;
}
.feed-kind.kind-run_start, .feed-kind.kind-run_end { color: var(--low); }
.feed-kind.kind-report_relay { color: var(--med); }
.feed-kind.kind-fix_dispatch { color: var(--high); }
.feed-kind.kind-runner_spawn { color: var(--accent); }
.feed-body { color: var(--text); word-break: break-word; }
.feed-body code {
  background: rgba(88, 166, 255, 0.08);
  padding: 0.05rem 0.3rem;
  border-radius: 3px;
  font-size: 0.82rem;
}
.feed-data {
  display: block;
  margin-top: 0.25rem;
  color: var(--muted);
  font-size: 0.78rem;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  white-space: pre-wrap;
  word-break: break-word;
}
@keyframes feed-flash {
  0%   { background: rgba(88, 166, 255, 0.18); }
  100% { background: transparent; }
}
.feed-item.feed-new { animation: feed-flash 2s ease-out; }

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
form input[readonly] {
  color: var(--muted);
  cursor: not-allowed;
}
.panel .hint {
  margin: 0 0 0.8rem;
  color: var(--muted);
  font-size: 0.82rem;
  line-height: 1.4;
}
.panel .hint code {
  background: var(--bg);
  padding: 0 0.25rem;
  border-radius: 3px;
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

/* --- modal --- */
.modal { position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; }
.modal[hidden] { display: none; }
.modal-backdrop { position: absolute; inset: 0; background: var(--overlay); }
.modal-content {
  position: relative;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1.5rem 2rem;
  max-width: 480px;
  width: 90%;
  box-shadow: 0 16px 48px rgba(0,0,0,0.3);
}
.modal-content h2 { margin: 0 0 1rem; font-size: 1.1rem; }
.modal-close {
  position: absolute;
  top: 0.8rem;
  right: 1rem;
  background: transparent;
  border: none;
  color: var(--muted);
  font-size: 1.5rem;
  cursor: pointer;
  line-height: 1;
}
.modal-close:hover { color: var(--text); }
.shortcuts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
.shortcut-group h3 { margin: 0 0 0.5rem; font-size: 0.85rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.shortcut-group dl { margin: 0; }
.shortcut-group dt { float: left; clear: left; margin-right: 0.5rem; margin-bottom: 0.4rem; }
.shortcut-group dd { margin: 0 0 0.4rem; font-size: 0.85rem; color: var(--text); }
kbd {
  display: inline-block;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 0.1rem 0.4rem;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 0.75rem;
  box-shadow: 0 1px 0 var(--border);
}

/* --- toast notifications --- */
.toast-container {
  position: fixed;
  bottom: 1.5rem;
  right: 1.5rem;
  z-index: 900;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  pointer-events: none;
}
.toast {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.6rem 1rem;
  font-size: 0.85rem;
  color: var(--text);
  box-shadow: 0 4px 12px rgba(0,0,0,0.2);
  animation: toast-in 0.2s ease-out;
  pointer-events: auto;
}
.toast.toast-out { animation: toast-out 0.2s ease-in forwards; }
@keyframes toast-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes toast-out {
  from { opacity: 1; transform: translateY(0); }
  to { opacity: 0; transform: translateY(8px); }
}

/* --- responsive --- */
@media (max-width: 768px) {
  header { padding: 0.8rem 1rem; gap: 0.8rem; }
  header h1 { font-size: 1.1rem; }
  nav { flex-wrap: wrap; gap: 0.2rem; }
  .tab { padding: 0.35rem 0.6rem; font-size: 0.8rem; }
  .header-actions { order: -1; margin-left: 0; }
  .target { font-size: 0.78rem; }
  main { padding: 1rem; }
  .controls { gap: 0.5rem; }
  .controls input[type="search"] { min-width: 0; width: 100%; max-width: none; }
  .feed-item { grid-template-columns: 6rem 5.5rem 1fr; gap: 0.4rem; padding: 0.4rem 0.6rem; }
  .shortcuts-grid { grid-template-columns: 1fr; }
  .severity-stats { gap: 0.4rem; }
  .stat-badge { padding: 0.3rem 0.55rem; font-size: 0.75rem; }
  .stat-badge .stat-count { font-size: 0.85rem; }
  .detail-content dl { grid-template-columns: 1fr; }
  .detail-content dt { margin-top: 0.4rem; }
}
"""


APP_JS = r"""(() => {
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
    if (e.line_number) fields.push(['Line', String(e.line_number)]);
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
  // Every POLL_INTERVAL_MS we hit /api/status (cheap). Only when the
  // server reports a changed mtime do we fetch /api/history (expensive).
  // On consecutive /api/status failures we apply exponential backoff
  // (doubling up to POLL_MAX_BACKOFF_MS) so a machine under disk-sleep
  // or a temporarily-down server doesn't eat bandwidth. Any success
  // resets the backoff to the normal interval.
  const POLL_INTERVAL_MS = 3000;
  const POLL_MAX_BACKOFF_MS = 30000;
  let pollErrorStreak = 0;
  // Initial value must NOT match what /api/status returns on first
  // load — when history.jsonl is absent the server returns
  // ``{"last_mtime": null, "token": null}`` and a strict ``!== null``
  // check would evaluate false, skipping the initial /api/history
  // fetch. Result: the empty-state message never renders and the user
  // sees a blank Findings tab with no indication the dashboard is
  // connected. A unique sentinel string differs from any real token,
  // so the first poll always triggers refetchFindings() and either
  // populates the table or shows the "No findings yet" copy.
  let lastMtime = '__uninitialized__';
  // Composite ``${st_mtime_ns}:${st_size}`` token from /api/status.
  // Preferred over lastMtime for change detection: nanosecond precision
  // survives same-microsecond writes that lastMtime's ISO rendering can
  // collapse, and the size suffix catches the (rare) case of a file
  // rewrite landing on the same timestamp. Falls back to lastMtime when
  // the server omits the field — keeps older client/server combos
  // working.
  let lastToken = '__uninitialized__';
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
      // Pill label distinguishes "findings being confirmed" (LIVE)
      // from "a /advisor run is firing live events but hasn't
      // confirmed findings yet" (RUNNING). The latter is the common
      // case during the explore phase — without this label the user
      // sees IDLE while the Live tab is busy and concludes the
      // Findings tab is broken. Older servers don't emit
      // ``live_is_active`` / ``history_is_active`` so the fallback
      // matches the prior LIVE/IDLE behavior.
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
      // Prefer the token field when the server emits it (nanosecond +
      // size). Older servers won't return ``token`` — fall back to the
      // mtime comparison so the client stays compatible with them.
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
  // Polls /api/events?since=<token> while the dashboard page is visible.
  // Mirrors the findings poll loop's shape (interval + backoff + pause
  // toggle) but cursors on the server-issued ``next_token`` so the Live
  // tab is already caught up when the user opens it.
  const LIVE_POLL_INTERVAL_MS = 2000;
  const LIVE_POLL_MAX_BACKOFF_MS = 30000;
  // FIFO bound on how many events live in the DOM. A long-running session
  // could otherwise accumulate thousands of rows; 500 is generous for the
  // typical advisor run (a few dozen events) while keeping memory bounded.
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
    // ``ev.data`` is opaque — the server doesn't enforce a schema.
    // Render core kinds with a friendly single-line summary; fall through
    // to a JSON dump for unknown kinds so nothing is silently dropped.
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
    // Generic kinds: append the raw payload below the summary so the user
    // sees everything the team-lead emitted without crowding the row.
    const hasExtras = Object.keys(d).length > 0;
    const extras = hasExtras ? `<span class="feed-data">${escapeHtml(JSON.stringify(d))}</span>` : '';
    return summary + extras;
  }

  function appendLiveEvents(events) {
    const feed = $('#live-feed');
    if (!feed) return;
    let appended = 0;
    events.forEach((ev) => {
      // Deduplicate by seq — the server's cursor protocol shouldn't replay
      // events but a misconfigured client (e.g. cursor reset by clear-feed)
      // could otherwise show duplicates. Set lookup is O(1).
      const seq = typeof ev.seq === 'number' ? ev.seq : null;
      if (seq !== null) {
        if (liveFeedSeenSeqs.has(seq)) return;
        liveFeedSeenSeqs.add(seq);
      }
      const li = document.createElement('li');
      li.className = 'feed-item feed-new';
      const ts = ev.ts || '';
      const tsShort = ts.length >= 19 ? ts.slice(11, 19) : ts;
      // Kind is rendered as a CSS class suffix; allowlist via a regex strip
      // so a hostile or unexpected kind can't inject CSS selectors.
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
    // FIFO trim — newest stays at the top, drop from the bottom.
    while (feed.childElementCount > LIVE_FEED_MAX_ITEMS) {
      const removed = feed.lastElementChild;
      if (!removed) break;
      feed.removeChild(removed);
    }
    // Bound the seen-seq set too so a long session doesn't leak.
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
    // Leave the cursor in place — clearing the visible feed shouldn't
    // cause the next poll to re-replay every event the user just
    // dismissed.
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
    // Defense-in-depth: priority is server-controlled today but mirror
    // severityClass's allowlist so a future API drift can't inject a
    // CSS class fragment via the ${pri} template hole.
    const s = String(p);
    return PRI_ALLOW.has(s) ? s : '0';
  }
  function shellQuote(s) {
    s = String(s);
    // ``*`` is deliberately NOT in the allow-list: the rendered CLI
    // preview surfaces values like ``--file-types *.py`` which the user
    // copy-pastes into a real shell. With ``*`` allowed through unquoted,
    // bash/zsh expand the glob against CWD before advisor sees the arg,
    // so advisor receives a list of file names instead of the literal
    // pattern. Falling through to the single-quote branch keeps the
    // glob intact across copy-paste.
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
  // Kick the live-events poll loop once on startup. It keeps the cursor
  // moving while the page is visible so the Live tab is hot the moment
  // the user clicks it.
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
    // Kick the poll loop — it fetches /api/status, then /api/history
    // (only if mtime has changed from the sentinel `null`, which it
    // always has on first load). This replaces the old single-shot
    // /api/history fetch so there's exactly one code path that
    // populates the table.
    schedulePoll(0);
  })();
})();
"""
