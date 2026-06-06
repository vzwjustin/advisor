//! Port of `advisor/orchestrate/explorer_prompts.py` — Haiku read-only explorer prompts.

use std::collections::HashMap;

use crate::config::{TeamConfig, POOL_SIZE_CEILING};
use crate::fence::sanitize_inline;

const EXPLORER_TEMPLATE: &str = include_str!("../assets/explorer.txt");

fn format_target_files(target_files: &[String], guidance: &HashMap<String, String>) -> String {
    let mut lines = Vec::with_capacity(target_files.len());
    for path in target_files {
        let g = sanitize_inline(
            guidance
                .get(path)
                .map(|s| s.trim())
                .filter(|s| !s.is_empty())
                .unwrap_or(""),
        );
        let suffix = if g.is_empty() {
            String::new()
        } else {
            format!(" — {g}")
        };
        lines.push(format!("- `{}`{suffix}", sanitize_inline(path)));
    }
    lines.join("\n")
}

/// Build a read-only exploration prompt for one Haiku explorer.
pub fn build_explorer_prompt(
    config: &TeamConfig,
    target_files: &[String],
    guidance: &HashMap<String, String>,
    explorer_id: i64,
) -> String {
    let files_block = if target_files.is_empty() {
        "_(No files yet — announce ready to team-lead and wait for the \
         advisor's explore dispatch with your file batch.)_"
            .to_string()
    } else {
        format_target_files(target_files, guidance)
    };
    EXPLORER_TEMPLATE
        .replace("{explorer_id}", &explorer_id.to_string())
        .replace("{team_name}", &sanitize_inline(&config.team_name))
        .replace("{files_block}", &files_block)
}

/// Agent specs for the initial explorer pool (Haiku, read-only).
pub fn build_explorer_pool_agents(
    config: &TeamConfig,
    pool_size: Option<i64>,
) -> Vec<serde_json::Value> {
    let raw_size = pool_size.unwrap_or(config.max_explorers);
    let limit = config.max_explorers.min(POOL_SIZE_CEILING);
    let mut size = raw_size;
    if size < 0 {
        eprintln!("⚠ pool_size={size} is < 0; using 0");
        size = 0;
    }
    if size > limit {
        eprintln!("⚠ pool_size={size} exceeds explorer pool limit of {limit}; using {limit}");
        size = limit;
    }
    size = size.clamp(0, limit);
    (1..=size)
        .map(|i| {
            serde_json::json!({
                "description": format!("Pool explorer {i} — read-only file exploration"),
                "name": format!("explorer-{i}"),
                "subagent_type": "explorer",
                "model": config.explorer_model,
                "team_name": config.team_name,
                "run_in_background": true,
                "prompt": build_explorer_prompt(config, &[], &HashMap::new(), i),
            })
        })
        .collect()
}
