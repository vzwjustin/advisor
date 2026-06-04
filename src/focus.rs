//! Port of `advisor/focus.py` — file-level focus dispatcher (one batch of
//! files per runner). Pure batching + plan-formatting on top of `RankedFile`.

use crate::models::RankedFile;

/// Complexity label for a batch. Mirrors the `Literal["low","medium","high"]`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Complexity {
    Low,
    Medium,
    High,
}

impl Complexity {
    pub fn as_str(self) -> &'static str {
        match self {
            Complexity::Low => "low",
            Complexity::Medium => "medium",
            Complexity::High => "high",
        }
    }
}

/// A single-file task with advisor guidance attached. Mirrors `FocusTask`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FocusTask {
    pub file_path: String,
    pub priority: u8,
    pub prompt: String,
}

/// A bundle of files for a single runner. Mirrors `FocusBatch`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FocusBatch {
    pub batch_id: usize,
    pub tasks: Vec<FocusTask>,
    pub complexity: Complexity,
}

impl FocusBatch {
    /// `FocusBatch.file_paths`.
    pub fn file_paths(&self) -> Vec<&str> {
        self.tasks.iter().map(|t| t.file_path.as_str()).collect()
    }

    /// `FocusBatch.top_priority` (max task priority, or 0 if empty).
    pub fn top_priority(&self) -> u8 {
        self.tasks.iter().map(|t| t.priority).max().unwrap_or(0)
    }
}

/// Default per-file review prompt template. Mirrors `DEFAULT_TASK_PROMPT`.
pub const DEFAULT_TASK_PROMPT: &str = "You are reviewing a single file for issues. Focus exclusively on:\n  `{file_path}` (priority {priority})\n\nRelevant signals: {reasons}\n\nInstructions:\n1. Read the file thoroughly.\n2. Hypothesize potential issues (bugs, security flaws, logic errors).\n3. Trace call paths to confirm or reject each hypothesis.\n4. For each confirmed issue, output:\n   - **File**: path and line number\n   - **Severity**: CRITICAL / HIGH / MEDIUM / LOW\n   - **Description**: what the issue is\n   - **Evidence**: the code path or proof\n   - **Fix**: suggested remediation\n5. If no issues found, state that explicitly.\n\nDo NOT review other files. Stay focused on this one.";

/// Sentinel for `create_focus_batches(complexity=...)` auto-derivation.
pub const AUTO_COMPLEXITY: &str = "auto";

/// Fill `{file_path}`/`{priority}`/`{reasons}` in one pass, leaving unknown
/// braces intact. Mirrors `_render_task_prompt` + `_PLACEHOLDER_RE`.
fn render_task_prompt(template: &str, file_path: &str, priority: &str, reasons: &str) -> String {
    let mut out = String::with_capacity(template.len() + file_path.len() + reasons.len());
    let bytes = template.as_bytes();
    let mut i = 0;
    while i < template.len() {
        if bytes[i] == b'{' {
            // Try to match one of the three known placeholders at this position.
            let rest = &template[i..];
            if let Some(sub) = ["{file_path}", "{priority}", "{reasons}"]
                .into_iter()
                .find(|p| rest.starts_with(p))
            {
                match sub {
                    "{file_path}" => out.push_str(file_path),
                    "{priority}" => out.push_str(priority),
                    _ => out.push_str(reasons),
                }
                i += sub.len();
                continue;
            }
        }
        // Push one full UTF-8 char to stay on a boundary.
        let ch = template[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

fn complexity_for_priority(top_priority: u8) -> Complexity {
    if top_priority >= 4 {
        Complexity::High
    } else if top_priority <= 2 {
        Complexity::Low
    } else {
        Complexity::Medium
    }
}

/// Generate one [`FocusTask`] per ranked file at/above `min_priority`. Mirrors
/// `create_focus_tasks` (defensive descending sort + early break + soft cap).
pub fn create_focus_tasks(
    ranked_files: &[RankedFile],
    max_tasks: Option<usize>,
    min_priority: u8,
    prompt_template: &str,
) -> Vec<FocusTask> {
    let mut sorted_files: Vec<&RankedFile> = ranked_files.iter().collect();
    // Stable descending sort by priority (Python sorted with reverse=True is stable).
    sorted_files.sort_by_key(|b| std::cmp::Reverse(b.priority));

    let mut tasks = Vec::new();
    for rf in sorted_files {
        if let Some(cap) = max_tasks {
            if tasks.len() >= cap {
                break;
            }
        }
        if rf.priority < min_priority {
            break;
        }
        let reasons_str = if rf.reasons.is_empty() {
            "general review".to_string()
        } else {
            rf.reasons.join(", ")
        };
        let prompt = render_task_prompt(
            prompt_template,
            &rf.path,
            &rf.priority.to_string(),
            &reasons_str,
        );
        tasks.push(FocusTask {
            file_path: rf.path.clone(),
            priority: rf.priority,
            prompt,
        });
    }
    tasks
}

/// Group [`FocusTask`]s into batches. `complexity` of `"auto"` derives each
/// batch's label from its top priority; any other value forces that label
/// (returns an error for an invalid forced label, mirroring `FocusBatch`'s
/// `__post_init__`). Mirrors `create_focus_batches`.
pub fn create_focus_batches(
    tasks: &[FocusTask],
    files_per_batch: usize,
    complexity: &str,
) -> Result<Vec<FocusBatch>, String> {
    if files_per_batch < 1 {
        return Err("files_per_batch must be >= 1".to_string());
    }
    let forced = if complexity == AUTO_COMPLEXITY {
        None
    } else {
        Some(match complexity {
            "low" => Complexity::Low,
            "medium" => Complexity::Medium,
            "high" => Complexity::High,
            other => {
                return Err(format!(
                    "FocusBatch.complexity must be 'low', 'medium', or 'high'; got {other:?}"
                ))
            }
        })
    };

    let mut batches = Vec::new();
    let mut i = 0;
    while i < tasks.len() {
        let chunk: Vec<FocusTask> = tasks[i..(i + files_per_batch).min(tasks.len())].to_vec();
        let batch_complexity = match forced {
            Some(c) => c,
            None => {
                let top = chunk.iter().map(|t| t.priority).max().unwrap_or(1);
                complexity_for_priority(top)
            }
        };
        batches.push(FocusBatch {
            batch_id: batches.len() + 1,
            tasks: chunk,
            complexity: batch_complexity,
        });
        i += files_per_batch;
    }
    Ok(batches)
}

/// Compact priority histogram — e.g. `P5 ×11  P4 ×5`. Mirrors `_priority_mix`.
fn priority_mix(priorities: &[u8]) -> String {
    if priorities.is_empty() {
        return String::new();
    }
    let mut counts: std::collections::BTreeMap<u8, usize> = std::collections::BTreeMap::new();
    for p in priorities {
        *counts.entry(*p).or_insert(0) += 1;
    }
    counts
        .iter()
        .rev()
        .map(|(p, n)| format!("P{p} ×{n}"))
        .collect::<Vec<_>>()
        .join("  ")
}

/// Format tasks into a readable dispatch plan. Mirrors `format_dispatch_plan`.
pub fn format_dispatch_plan(tasks: &[FocusTask]) -> String {
    if tasks.is_empty() {
        return "## Dispatch Plan\nNo files matched — nothing to dispatch.\n".to_string();
    }
    let agent_word = if tasks.len() == 1 { "agent" } else { "agents" };
    let mix = priority_mix(&tasks.iter().map(|t| t.priority).collect::<Vec<_>>());
    let base = format!(
        "Dispatching {} focused {} in parallel",
        tasks.len(),
        agent_word
    );
    let header = if mix.is_empty() {
        format!("{base}:")
    } else {
        format!("{base} ({mix}):")
    };
    let mut lines = vec!["## Dispatch Plan".to_string(), header, String::new()];
    for (i, t) in tasks.iter().enumerate() {
        lines.push(format!("{}. **P{}** `{}`", i + 1, t.priority, t.file_path));
    }
    format!("{}\n", lines.join("\n").trim_end())
}

/// Format batches into a readable dispatch plan. Mirrors `format_batch_plan`.
pub fn format_batch_plan(batches: &[FocusBatch]) -> String {
    if batches.is_empty() {
        return "## Batch Dispatch Plan\nNo files matched — nothing to dispatch.\n".to_string();
    }
    let total_files: usize = batches.iter().map(|b| b.tasks.len()).sum();
    let runner_word = if batches.len() == 1 {
        "runner"
    } else {
        "runners"
    };
    let file_word = if total_files == 1 { "file" } else { "files" };
    let all_pri: Vec<u8> = batches
        .iter()
        .flat_map(|b| b.tasks.iter().map(|t| t.priority))
        .collect();
    let mix = priority_mix(&all_pri);
    let base = format!(
        "Dispatching {} {} across {} {}",
        batches.len(),
        runner_word,
        total_files,
        file_word
    );
    let header = if mix.is_empty() {
        format!("{base}:")
    } else {
        format!("{base} ({mix}):")
    };
    let mut lines = vec!["## Batch Dispatch Plan".to_string(), header, String::new()];
    for b in batches {
        let batch_file_word = if b.tasks.len() == 1 { "file" } else { "files" };
        lines.push(format!(
            "**Batch {}** (complexity: {}, top P{}) — {} {}:",
            b.batch_id,
            b.complexity.as_str(),
            b.top_priority(),
            b.tasks.len(),
            batch_file_word
        ));
        for t in &b.tasks {
            lines.push(format!("  - **P{}** `{}`", t.priority, t.file_path));
        }
        lines.push(String::new());
    }
    format!("{}\n", lines.join("\n").trim_end())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn golden() -> Value {
        serde_json::from_str(include_str!("../tests/parity/focus.json")).unwrap()
    }

    fn ranked() -> Vec<RankedFile> {
        vec![
            RankedFile {
                path: "src/auth.py".into(),
                priority: 5,
                reasons: vec!["auth".into(), "login".into()],
            },
            RankedFile {
                path: "api/routes.py".into(),
                priority: 3,
                reasons: vec!["route".into(), "query".into()],
            },
            RankedFile {
                path: "src/util.py".into(),
                priority: 2,
                reasons: vec![],
            },
            RankedFile {
                path: "low.py".into(),
                priority: 1,
                reasons: vec!["util".into()],
            },
        ]
    }

    #[test]
    fn default_prompt_matches_python() {
        assert_eq!(
            DEFAULT_TASK_PROMPT,
            golden()["default_task_prompt"].as_str().unwrap()
        );
    }

    #[test]
    fn tasks_match_python() {
        let g = golden();
        let tasks = create_focus_tasks(&ranked(), None, 2, DEFAULT_TASK_PROMPT);
        let got: Vec<Value> = tasks
            .iter()
            .map(|t| serde_json::json!({"file_path": t.file_path, "priority": t.priority, "prompt": t.prompt}))
            .collect();
        assert_eq!(Value::Array(got), g["tasks"]);
    }

    #[test]
    fn batches_match_python() {
        let g = golden();
        let tasks = create_focus_tasks(&ranked(), None, 2, DEFAULT_TASK_PROMPT);
        let batches = create_focus_batches(&tasks, 2, AUTO_COMPLEXITY).unwrap();
        let got: Vec<Value> = batches
            .iter()
            .map(|b| {
                serde_json::json!({
                    "batch_id": b.batch_id,
                    "complexity": b.complexity.as_str(),
                    "top_priority": b.top_priority(),
                    "file_paths": b.file_paths(),
                })
            })
            .collect();
        assert_eq!(Value::Array(got), g["batches"]);

        let forced = create_focus_batches(&tasks, 10, "low").unwrap();
        let forced_labels: Vec<&str> = forced.iter().map(|b| b.complexity.as_str()).collect();
        let exp: Vec<&str> = g["batches_forced_low"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(forced_labels, exp);
    }

    #[test]
    fn plans_match_python() {
        let g = golden();
        let tasks = create_focus_tasks(&ranked(), None, 2, DEFAULT_TASK_PROMPT);
        let batches = create_focus_batches(&tasks, 2, AUTO_COMPLEXITY).unwrap();
        assert_eq!(
            format_dispatch_plan(&tasks),
            g["dispatch_plan"].as_str().unwrap()
        );
        assert_eq!(
            format_batch_plan(&batches),
            g["batch_plan"].as_str().unwrap()
        );
        assert_eq!(
            format_dispatch_plan(&[]),
            g["dispatch_plan_empty"].as_str().unwrap()
        );
        assert_eq!(
            format_batch_plan(&[]),
            g["batch_plan_empty"].as_str().unwrap()
        );

        let single = create_focus_tasks(
            &[RankedFile {
                path: "a.py".into(),
                priority: 5,
                reasons: vec![],
            }],
            None,
            2,
            DEFAULT_TASK_PROMPT,
        );
        assert_eq!(
            format_dispatch_plan(&single),
            g["dispatch_single"].as_str().unwrap()
        );
    }

    #[test]
    fn invalid_forced_complexity_errors() {
        let tasks = create_focus_tasks(&ranked(), None, 2, DEFAULT_TASK_PROMPT);
        assert!(create_focus_batches(&tasks, 2, "bogus").is_err());
        assert!(create_focus_batches(&tasks, 0, AUTO_COMPLEXITY).is_err());
    }
}
