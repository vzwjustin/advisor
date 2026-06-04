use crate::version::resolve_version;

pub fn version_badge() -> String {
    format!("<!-- advisor:{} -->", resolve_version())
}

const SKILL_MD_CODEX_TEMPLATE: &str = include_str!("assets/codex_skill_md.txt");

pub fn render_codex_skill_md() -> String {
    SKILL_MD_CODEX_TEMPLATE.replace("__VERSION_BADGE__", &version_badge())
}

pub fn build_codex_runner_prompt(runner_id: &str, file_lines: &[&str]) -> String {
    let files_block = if file_lines.is_empty() {
        "(no files in batch)".to_string()
    } else {
        file_lines.join("\n")
    };
    format!(
        r#"You are {runner_id} — a Codex subagent on a read-only bug-hunt review.

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
"#
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn golden() -> serde_json::Value {
        let s = include_str!("../tests/parity/skill_strings.json");
        serde_json::from_str(s).unwrap()
    }

    #[test]
    fn parity_codex_skill_md_rendered() {
        let g = golden();
        assert_eq!(
            render_codex_skill_md(),
            g["SKILL_MD_CODEX_RENDERED"].as_str().unwrap()
        );
    }

    #[test]
    fn parity_codex_runner_prompt_basic() {
        let g = golden();
        let result =
            build_codex_runner_prompt("runner-1", &["- `src/foo.py` (P1)", "- `src/bar.py` (P2)"]);
        assert_eq!(result, g["codex_runner_prompt_basic"].as_str().unwrap());
    }

    #[test]
    fn parity_codex_runner_prompt_empty() {
        let g = golden();
        let result = build_codex_runner_prompt("runner-2", &[]);
        assert_eq!(result, g["codex_runner_prompt_empty"].as_str().unwrap());
    }
}
