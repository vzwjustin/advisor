"""Bundled SKILL.md source for the ``$advisor`` Codex skill.

The Codex CLI's subagent model is **fire-and-forget**: workers spawned via
``spawn_agents_on_csv`` emit a single ``report_agent_job_result`` call and
cannot stay in live conversation with the orchestrator the way Claude Code's
mailbox-based ``Agent`` + ``SendMessage`` pipeline allows. This skill is the
Codex-flavored entry point — it drives a batch-mode review using the advisor
CLI as the planning oracle and Codex's native ``spawn_agents_on_csv`` for
parallel dispatch. ``advisor install`` writes this body to
``~/.agents/skills/advisor/SKILL.md`` (Codex USER scope) when the ``codex``
binary is on PATH.

The Claude Code pipeline (``skill_asset.SKILL_MD``) is kept as the canonical
review-and-fix loop because it supports live ``CONFIRM``/``REVISE``/``REDIRECT``
verification mid-run. The Codex variant trades that live verification for
batch parallelism — findings come back consolidated and the user reviews them
in a single turn, which matches the most common ``/advisor`` invocation
(read-only sweep).
"""

from __future__ import annotations

from typing import Any

from ._version import resolve_version

#: HTML-comment badge so ``advisor status`` can parse the installed version
#: without hashing the whole file. Matches ``_BADGE_RE`` in ``install.py``.
VERSION_BADGE = f"<!-- advisor:{resolve_version()} -->"


SKILL_MD_CODEX = """__VERSION_BADGE__

# Advisor — Codex batch review

A read-only, parallel bug-hunt review driven by ``advisor`` and Codex's
``spawn_agents_on_csv``. Use this when the user invokes ``$advisor`` or
``$advisor <path>``. The pipeline is fire-and-forget by design — Codex
spawns one subagent per ranked file batch, each emits findings via
``report_agent_job_result``, and the consolidated results are rendered
back to the user as a single report.

This is the **Codex variant** of the advisor pipeline. The Claude Code
variant (``/advisor`` in Claude Code) keeps a live mailbox between the
advisor and runners; Codex's subagent model does not expose mid-run
messaging, so this skill ships review-only and asks the user to request
fixes in a follow-up turn.

## ACTIVATION SEQUENCE — strict order

**Step 1 (text only):** Write ``**Advisor mode (Codex)**``.

**Step 2:** Run the advisor CLI to produce a per-runner dispatch CSV:

```bash
advisor codex-plan-csv TARGET
```

Where ``TARGET`` is the directory the user named, or the current working
directory if they did not. The command prints the absolute path of the
CSV to stdout — capture it and use it in Step 3.

**Step 3:** Spawn parallel runners. Read the CSV path from Step 2, then
invoke ``spawn_agents_on_csv`` with the dispatch CSV. Each row is one
runner batch; the ``prompt`` column contains the complete per-runner
brief that the advisor already tailored to the files in that batch.

```
spawn_agents_on_csv({
  csv_path:        "<path from Step 2>",
  instruction:     "{prompt}",
  id_column:       "runner_id",
  output_schema:   { ... see schema below ... },
  max_concurrency: 5
})
```

The ``instruction`` is the literal string ``"{prompt}"`` — Codex
substitutes the ``prompt`` column from the CSV per row. Do not edit it.

**Step 4:** Wait for all subagents to report. Codex blocks until every
row has either emitted a result via ``report_agent_job_result`` or hit
the per-row timeout. No mid-run intervention is possible — that is the
Codex subagent contract.

**Step 5:** Render the consolidated report. Collect each subagent's
``findings`` array and format the aggregate as Markdown:

```
## Summary
N findings (X HIGH, Y MEDIUM, Z LOW).

## Top 3 Actions
…

## Findings by severity
…
```

Group findings by severity (HIGH first), then by file path within each
severity bucket. Quote each finding's ``file``, ``description``,
``evidence``, and ``fix`` fields verbatim — the runner already validated
shape; this layer is presentation only.

**Step 6 (closing line):** Tell the user how to ask for fixes:

> Want me to apply remediation? Reply with ``$advisor fix`` (or describe
> which findings to address) and I will queue the changes in a follow-up
> turn. The first pass was review-only.

## Output schema for each runner

Each ``report_agent_job_result`` call MUST emit JSON matching this
schema. The runner prompt in the CSV already instructs the worker to
produce this shape — do not modify the schema or the workers will fail
validation.

```json
{
  "type": "object",
  "properties": {
    "runner_id": { "type": "string" },
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "file":        { "type": "string" },
          "severity":    { "type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"] },
          "description": { "type": "string" },
          "evidence":    { "type": "string" },
          "fix":         { "type": "string" }
        },
        "required": ["file", "severity", "description"]
      }
    }
  },
  "required": ["runner_id", "findings"]
}
```

## Failure modes

- **No CSV produced (empty target).** ``advisor codex-plan-csv`` exits
  with code ``1`` and prints the reason to stderr. Surface the message
  verbatim to the user and stop — there is nothing to dispatch.
- **Codex CLI does not have ``spawn_agents_on_csv``.** The user's Codex
  installation is too old. Tell them to upgrade Codex; do NOT fall back
  to a sequential loop that pretends to be parallel.
- **A subagent times out.** Its row is marked with an error in the
  exported CSV. Include the error in the consolidated report under a
  ``## Incomplete batches`` section so the user knows what was skipped.

## Why not Claude Code's pipeline here

The advisor's Claude Code pipeline relies on ``TeamCreate`` /
``TeamDelete`` / ``Agent`` / ``SendMessage`` for live two-way dialogue
between the Opus advisor and Sonnet runners. Codex exposes none of
those — its subagents are batch workers that emit a single result and
exit. Trying to invoke the Claude Code skill from Codex fails with an
unknown-tool error. This skill is what the user gets when they run
``$advisor`` in Codex; the Claude Code variant remains the canonical
review-and-fix loop for the Claude Code runtime.
"""


def render_codex_skill_md(*, version_badge: str = VERSION_BADGE) -> str:
    """Return the Codex SKILL.md body with the version badge substituted."""
    return SKILL_MD_CODEX.replace("__VERSION_BADGE__", version_badge)


#: Pre-rendered body — what ``install_codex_skill`` writes to disk by default.
SKILL_MD_CODEX_RENDERED = render_codex_skill_md()


#: JSON schema each Codex subagent must emit via ``report_agent_job_result``.
#: Mirrored in :data:`SKILL_MD_CODEX` so the runner prompt and the
#: ``spawn_agents_on_csv`` schema arg stay in sync — change one, update the
#: other. The required-field set is intentionally minimal so a runner that
#: omits ``evidence`` or ``fix`` (because it judged them not load-bearing
#: for a given finding) still passes validation.
RUNNER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "runner_id": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                    },
                    "description": {"type": "string"},
                    "evidence": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["file", "severity", "description"],
            },
        },
    },
    "required": ["runner_id", "findings"],
}


def build_codex_runner_prompt(
    runner_id: str,
    file_lines: list[str],
) -> str:
    """Build the per-runner prompt that Codex's ``spawn_agents_on_csv`` runs.

    ``file_lines`` is the pre-formatted batch listing — one entry per file
    in the form ``- `path/to/file.py` (P3)``. Callers that want richer
    per-file guidance can pre-format the list with the guidance appended;
    this helper does not impose a structure beyond the leading bullet.

    The prompt mirrors the shape ``build_runner_prompt`` uses for Claude
    Code but trades the live ``SendMessage`` mailbox for a single
    ``report_agent_job_result`` emission — Codex's subagent contract.
    Read-only by design: the Codex variant ships review-only and the
    user asks for fixes in a follow-up turn.
    """
    files_block = "\n".join(file_lines) if file_lines else "(no files in batch)"
    return f"""You are {runner_id} — a Codex subagent on a read-only bug-hunt review.

## Your batch
Review the following files for correctness defects, race conditions, error-handling gaps, off-by-ones, resource leaks, and any logic flaws. **Read-only — do NOT edit, write, or modify files.**

{files_block}

## How to work
1. Read each file in the batch.
2. For each defect you find, build a finding object with these fields:
   - `file`: path:line_number (e.g. ``advisor/foo.py:42``)
   - `severity`: one of ``CRITICAL``, ``HIGH``, ``MEDIUM``, ``LOW``
   - `description`: one paragraph on what the bug is
   - `evidence`: the relevant code or proof (1–3 lines)
   - `fix`: suggested remediation as plain text (not a diff)
3. When you have finished reviewing every file, call ``report_agent_job_result`` **exactly once** with JSON matching the required schema below. If you find no issues, emit ``findings: []`` — do not skip the call.

## Required output schema
```json
{{
  "runner_id": "{runner_id}",
  "findings": [
    {{
      "file": "advisor/foo.py:42",
      "severity": "HIGH",
      "description": "Brief description of the bug.",
      "evidence": "Lines of code or trace proving the defect.",
      "fix": "Plain-text remediation suggestion."
    }}
  ]
}}
```

Always include ``runner_id`` so the orchestrator can attribute findings. Do not produce any other output — the single ``report_agent_job_result`` call is the entire contract.

## Scope
- Work ONLY on the files listed above. Do not pivot to other files or open new ones.
- Do not run shell commands, write new files, or edit existing ones.
- If a file is unreadable or empty, emit no finding for it and proceed; do not raise.
"""
