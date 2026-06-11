//! Agent spawn specs for the live Claude Code team pipeline.
//!
//! Mirrors the Python `build_advisor_agent` / `build_runner_pool_agents` helpers.

use serde_json::{json, Value};

use crate::config::TeamConfig;

use super::advisor_prompt::build_advisor_prompt;
use super::agent_types::{ADVISOR_SUBAGENT_TYPE, RUNNER_SUBAGENT_TYPE};
use super::runner_prompts::build_runner_pool_prompt;

const POOL_SIZE_CEILING: i64 = 20;

/// Opus advisor agent spec — spawn first, before any explorers or runners.
pub fn build_advisor_agent(config: &TeamConfig) -> Value {
    json!({
        "name": "advisor",
        "description": "Investigate, rank, and dispatch explorers + coders",
        "model": config.advisor_model,
        "subagent_type": ADVISOR_SUBAGENT_TYPE,
        "team_name": config.team_name,
        "prompt": build_advisor_prompt(config, ""),
    })
}

/// Sonnet runner pool specs (fallback when not using advisor-authored prompts).
pub fn build_runner_pool_agents(config: &TeamConfig, pool_size: Option<i64>) -> Vec<Value> {
    let raw_size = pool_size.unwrap_or(config.max_runners);
    let limit = config.max_runners.min(POOL_SIZE_CEILING);
    let size = if limit < 1 {
        0
    } else {
        raw_size.clamp(1, limit)
    };
    if limit >= 1 && raw_size > limit {
        eprintln!("⚠ pool_size={raw_size} exceeds runner pool limit of {limit}; using {limit}");
    }
    (1..=size)
        .map(|i| {
            json!({
                "name": format!("runner-{i}"),
                "description": format!("Pool runner {i} — reads batch from initial prompt"),
                "model": config.runner_model,
                "subagent_type": RUNNER_SUBAGENT_TYPE,
                "team_name": config.team_name,
                "run_in_background": true,
                "prompt": build_runner_pool_prompt(i, config),
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{default_team_config, TeamConfigInput};

    fn minimal() -> TeamConfig {
        let mut i = TeamConfigInput::new("/repo");
        i.team_name = "review".to_string();
        i.warn_unknown_model = false;
        default_team_config(i)
    }

    #[test]
    fn advisor_agent_uses_builtin_type() {
        let config = minimal();
        let spec = build_advisor_agent(&config);
        assert_eq!(spec["name"], "advisor");
        assert_eq!(spec["subagent_type"], ADVISOR_SUBAGENT_TYPE);
        assert_eq!(spec["model"], config.advisor_model);
        assert_eq!(spec["team_name"], config.team_name);
        assert_eq!(
            spec["prompt"].as_str().unwrap(),
            build_advisor_prompt(&config, "")
        );
    }

    #[test]
    fn runner_pool_empty_when_max_runners_zero() {
        let mut config = minimal();
        config.max_runners = 0;
        assert!(build_runner_pool_agents(&config, None).is_empty());
        assert!(build_runner_pool_agents(&config, Some(5)).is_empty());
    }

    #[test]
    fn runner_pool_agents_use_builtin_type() {
        let config = minimal();
        let specs = build_runner_pool_agents(&config, Some(2));
        assert_eq!(specs.len(), 2);
        for (i, spec) in specs.iter().enumerate() {
            let n = i as i64 + 1;
            assert_eq!(spec["name"], format!("runner-{n}"));
            assert_eq!(spec["subagent_type"], RUNNER_SUBAGENT_TYPE);
            assert_eq!(spec["model"], config.runner_model);
            assert!(spec["run_in_background"].as_bool().unwrap());
            assert_eq!(
                spec["prompt"].as_str().unwrap(),
                build_runner_pool_prompt(n, &config)
            );
        }
    }
}
