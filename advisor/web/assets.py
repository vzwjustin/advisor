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
    });
  });

  // --- findings ---
  let findingsRaw = [];
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
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(e.timestamp || '')}</td>
        <td><code>${escapeHtml(e.file_path || '')}</code></td>
        <td><span class="sev sev-${escapeHtml((e.severity || '').toUpperCase())}">${escapeHtml(e.severity || '')}</span></td>
        <td>${escapeHtml(e.status || '')}</td>
        <td>${escapeHtml(e.description || '')}</td>
      `;
      tbody.appendChild(tr);
    });
    $('#findings-count').textContent = `${filtered.length} of ${findingsRaw.length}`;
    $('#findings-empty').hidden = findingsRaw.length !== 0;
    $('#findings-table').hidden = findingsRaw.length === 0;
  }
  $('#findings-search').addEventListener('input', renderFindings);
  $('#findings-severity').addEventListener('change', renderFindings);

  // --- plan ---
  async function loadPlan() {
    // Server binds to a single target at launch (see AppState); only the
    // ranking knobs round-trip to /api/plan.
    const form = new FormData($('#config-form'));
    const qs = new URLSearchParams();
    qs.set('file_types', form.get('file_types') || '*.py');
    qs.set('min_priority', form.get('min_priority') || '3');
    const r = await fetch('/api/plan?' + qs.toString());
    if (!r.ok) {
      $('#plan-empty').hidden = false;
      $('#plan-empty').textContent = 'Error loading plan: ' + r.status;
      return;
    }
    const data = await r.json();
    const tbody = $('#plan-table tbody');
    tbody.innerHTML = '';
    (data.tasks || []).forEach((t, i) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td><span class="pri-${t.priority}">P${t.priority}</span></td>
        <td><code>${escapeHtml(t.file_path)}</code></td>
      `;
      tbody.appendChild(tr);
    });
    $('#plan-count').textContent = `${data.task_count || 0} files`;
    const empty = (data.task_count || 0) === 0;
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
  async function loadCost() {
    const form = new FormData($('#config-form'));
    const qs = new URLSearchParams();
    qs.set('file_types', form.get('file_types') || '*.py');
    qs.set('min_priority', form.get('min_priority') || '3');
    qs.set('advisor_model', form.get('advisor_model') || 'opus');
    qs.set('runner_model', form.get('runner_model') || 'sonnet');
    const r = await fetch('/api/cost?' + qs.toString());
    if (!r.ok) {
      $('#cost-summary').innerHTML = '';
      $('#cost-empty').hidden = false;
      $('#cost-empty').textContent = 'Error loading cost: ' + r.status;
      return;
    }
    const data = await r.json();
    if (!data.estimate) {
      $('#cost-summary').innerHTML = '';
      $('#cost-empty').hidden = false;
      return;
    }
    const e = data.estimate;
    $('#cost-empty').hidden = true;
    const minTok = (e.input_tokens_min || 0) + (e.output_tokens_min || 0);
    const maxTok = (e.input_tokens_max || 0) + (e.output_tokens_max || 0);
    $('#cost-summary').innerHTML = `
      <div class="cost-card"><h4>Files</h4><div class="val">${data.task_count}</div></div>
      <div class="cost-card"><h4>Runners</h4><div class="val">${e.runner_count || '—'}</div></div>
      <div class="cost-card"><h4>Min tokens</h4><div class="val">${formatNum(minTok)}</div></div>
      <div class="cost-card"><h4>Max tokens</h4><div class="val">${formatNum(maxTok)}</div></div>
      <div class="cost-card"><h4>Min cost</h4><div class="val">$${formatMoney(e.cost_usd_min)}</div></div>
      <div class="cost-card"><h4>Max cost</h4><div class="val">$${formatMoney(e.cost_usd_max)}</div></div>
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
    try {
      const r = await fetch('/api/history');
      if (r.ok) {
        const data = await r.json();
        findingsRaw = data.entries || [];
        renderFindings();
      }
    } catch (_) {}
  })();
})();
"""
