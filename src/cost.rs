//! Port of the pricing model constants and family resolution from
//! `advisor/cost.py`.
//!
//! This slice ports the pricing table, token-overhead constants, and
//! [`family_of`]. The full `estimate_cost` / `load_pricing` / `format_estimate`
//! pipeline is tracked in PORT_NOTES.md.

/// Default pricing in **cents per million tokens**: `(input, output)` per
/// family. Mirrors `DEFAULT_PRICING_CENTS_PER_MTOK`.
pub const OPUS_CENTS_PER_MTOK: (i64, i64) = (1500, 7500);
pub const SONNET_CENTS_PER_MTOK: (i64, i64) = (300, 1500);
pub const HAIKU_CENTS_PER_MTOK: (i64, i64) = (25, 125);

/// Fixed token overheads used by the estimator.
pub const ADVISOR_SYSTEM_TOKENS: i64 = 4_500;
pub const RUNNER_SYSTEM_TOKENS: i64 = 2_000;
pub const EXPLORER_SYSTEM_TOKENS: i64 = 1_500;
pub const PER_MESSAGE_OVERHEAD_TOKENS: i64 = 300;

/// Characters-per-token heuristic (English ~4, code ~3.5). Mirrors `CHARS_PER_TOKEN`.
pub const CHARS_PER_TOKEN: f64 = 3.5;

/// Pricing snapshot date `PRICING_AS_OF` (year, month, day).
pub const PRICING_AS_OF: (i32, u32, u32) = (2026, 5, 22);

/// Days after which the bundled default pricing is considered stale.
pub const PRICING_STALE_DAYS: i64 = 180;

/// Return the canonical family (`opus`, `sonnet`, `haiku`) for a model name,
/// substring-matched case-insensitively. Unknown names fall back to `sonnet`.
/// Mirrors Python `_family_of` (minus the one-shot stderr warning, which the
/// caller path will reintroduce when the estimator is ported).
pub fn family_of(model: &str) -> &'static str {
    let m = model.to_lowercase();
    if m.contains("opus") {
        "opus"
    } else if m.contains("sonnet") {
        "sonnet"
    } else if m.contains("haiku") {
        "haiku"
    } else {
        "sonnet"
    }
}

/// Default cents-per-Mtok `(input, output)` for a family name.
pub fn default_pricing_for(family: &str) -> (i64, i64) {
    match family {
        "opus" => OPUS_CENTS_PER_MTOK,
        "haiku" => HAIKU_CENTS_PER_MTOK,
        _ => SONNET_CENTS_PER_MTOK,
    }
}

use std::collections::HashMap;
use std::path::Path;

use crate::focus::{FocusBatch, FocusTask};

/// Token + USD range for a planned advisor run. Mirrors `CostEstimate`.
#[derive(Debug, Clone, PartialEq)]
pub struct CostEstimate {
    pub input_tokens_min: i64,
    pub input_tokens_max: i64,
    pub output_tokens_min: i64,
    pub output_tokens_max: i64,
    pub cost_usd_min: f64,
    pub cost_usd_max: f64,
    pub runner_count: i64,
    pub file_count: i64,
    pub advisor_model: String,
    pub runner_model: String,
    pub explorer_model: String,
    pub explorer_input_tokens_min: i64,
    pub explorer_input_tokens_max: i64,
    pub explorer_output_tokens_min: i64,
    pub explorer_output_tokens_max: i64,
    pub explorer_cost_usd_min: f64,
    pub explorer_cost_usd_max: f64,
    pub advisor_cost_usd_min: f64,
    pub advisor_cost_usd_max: f64,
    pub coder_cost_usd_min: f64,
    pub coder_cost_usd_max: f64,
}

fn round4(x: f64) -> f64 {
    (x * 10_000.0).round() / 10_000.0
}

impl CostEstimate {
    /// Round-trippable dict for JSON output (key order matches Python `to_dict`).
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "runner_count": self.runner_count,
            "file_count": self.file_count,
            "advisor_model": self.advisor_model,
            "runner_model": self.runner_model,
            "explorer_model": self.explorer_model,
            "explorer_input_tokens_min": self.explorer_input_tokens_min,
            "explorer_input_tokens_max": self.explorer_input_tokens_max,
            "explorer_output_tokens_min": self.explorer_output_tokens_min,
            "explorer_output_tokens_max": self.explorer_output_tokens_max,
            "explorer_cost_usd_min": round4(self.explorer_cost_usd_min),
            "explorer_cost_usd_max": round4(self.explorer_cost_usd_max),
            "advisor_cost_usd_min": round4(self.advisor_cost_usd_min),
            "advisor_cost_usd_max": round4(self.advisor_cost_usd_max),
            "coder_cost_usd_min": round4(self.coder_cost_usd_min),
            "coder_cost_usd_max": round4(self.coder_cost_usd_max),
            "input_tokens_min": self.input_tokens_min,
            "input_tokens_max": self.input_tokens_max,
            "output_tokens_min": self.output_tokens_min,
            "output_tokens_max": self.output_tokens_max,
            "cost_usd_min": round4(self.cost_usd_min),
            "cost_usd_max": round4(self.cost_usd_max),
        })
    }
}

/// Best-effort token estimate for a file (single stat; `size / CHARS_PER_TOKEN`).
/// Returns 0 for missing files or paths outside `target`. Mirrors `_tokens_for_file`.
fn tokens_for_file(path: &str, target: Option<&Path>) -> i64 {
    if let Some(t) = target {
        match (Path::new(path).canonicalize(), t.canonicalize()) {
            (Ok(p), Ok(tr)) if p.starts_with(&tr) => {}
            _ => return 0,
        }
    }
    match std::fs::metadata(path) {
        Ok(m) => (m.len() as f64 / CHARS_PER_TOKEN) as i64,
        Err(_) => 0,
    }
}

/// Estimate token usage + USD cost for a planned run. Mirrors `estimate_cost`.
#[allow(clippy::too_many_arguments)]
pub fn estimate_cost(
    tasks: &[FocusTask],
    batches: Option<&[FocusBatch]>,
    advisor_model: &str,
    runner_model: &str,
    max_fixes_per_runner: i64,
    max_runners: Option<i64>,
    explorer_model: &str,
    max_explorers: i64,
    pricing: Option<&HashMap<String, (i64, i64)>>,
    target: Option<&Path>,
) -> Result<CostEstimate, String> {
    let default_pricing: HashMap<String, (i64, i64)> = [
        ("opus", OPUS_CENTS_PER_MTOK),
        ("sonnet", SONNET_CENTS_PER_MTOK),
        ("haiku", HAIKU_CENTS_PER_MTOK),
    ]
    .iter()
    .map(|(k, v)| (k.to_string(), *v))
    .collect();
    let pricing = pricing.unwrap_or(&default_pricing);

    for fam in ["sonnet", "opus", "haiku"] {
        if !pricing.contains_key(fam) {
            return Err(
                "pricing= is missing required keys; supply entries for sonnet/opus/haiku"
                    .to_string(),
            );
        }
    }
    if max_fixes_per_runner < 0 {
        return Err(format!(
            "max_fixes_per_runner must be >= 0 (got {max_fixes_per_runner}); 0 disables fix waves, negative is not meaningful"
        ));
    }

    let runner_limit = max_runners.map(|m| m.max(0)).unwrap_or(5);
    let runner_count: i64 = if let Some(b) = batches.filter(|b| !b.is_empty()) {
        if runner_limit > 0 {
            (b.len() as i64).min(runner_limit)
        } else {
            0
        }
    } else if runner_limit == 0 || tasks.is_empty() {
        0
    } else {
        runner_limit.min(tasks.len() as i64)
    };
    let file_count = tasks.len() as i64;

    let mut token_cache: HashMap<&str, i64> = HashMap::new();
    let mut content_tokens: i64 = 0;
    for task in tasks {
        let t = *token_cache
            .entry(task.file_path.as_str())
            .or_insert_with(|| tokens_for_file(&task.file_path, target));
        content_tokens += t;
    }

    let explorer_limit = max_explorers.max(0);
    let explorer_count = if explorer_limit > 0 && file_count > 0 {
        explorer_limit.min(file_count)
    } else {
        0
    };
    let three_tier = explorer_count > 0;

    // MIN scenario.
    let mut advisor_in_min = ADVISOR_SYSTEM_TOKENS + PER_MESSAGE_OVERHEAD_TOKENS * runner_count * 2;
    let (explorer_in_min, explorer_out_min, runner_in_min, runner_out_min) = if three_tier {
        advisor_in_min += PER_MESSAGE_OVERHEAD_TOKENS * explorer_count * 2;
        let explorer_in = explorer_count * EXPLORER_SYSTEM_TOKENS
            + content_tokens
            + explorer_count * PER_MESSAGE_OVERHEAD_TOKENS;
        let explorer_out = explorer_count * 600;
        let runner_in =
            runner_count * RUNNER_SYSTEM_TOKENS + runner_count * PER_MESSAGE_OVERHEAD_TOKENS;
        (explorer_in, explorer_out, runner_in, runner_count * 800)
    } else {
        let runner_in = runner_count * RUNNER_SYSTEM_TOKENS
            + content_tokens
            + runner_count * PER_MESSAGE_OVERHEAD_TOKENS;
        (0, 0, runner_in, runner_count * 800)
    };
    let mut advisor_out_min = runner_count * 400;
    if three_tier {
        advisor_out_min += explorer_count * 300;
    }

    // MAX scenario.
    let fix_rounds = max_fixes_per_runner.max(0) * runner_count;
    let advisor_in_max = advisor_in_min + fix_rounds * PER_MESSAGE_OVERHEAD_TOKENS * 2;
    let avg_file_tokens = content_tokens / file_count.max(1);
    let (explorer_in_max, explorer_out_max, runner_in_max, runner_out_max) = if three_tier {
        let runner_in =
            runner_in_min + fix_rounds * (avg_file_tokens / 2 + PER_MESSAGE_OVERHEAD_TOKENS);
        (
            explorer_in_min,
            explorer_out_min,
            runner_in,
            runner_out_min + fix_rounds * 600,
        )
    } else {
        let runner_in =
            runner_in_min + fix_rounds * (avg_file_tokens + PER_MESSAGE_OVERHEAD_TOKENS);
        (0, 0, runner_in, runner_out_min + fix_rounds * 600)
    };
    let advisor_out_max = advisor_out_min + fix_rounds * 200;

    let (adv_in_c, adv_out_c) = *pricing
        .get(family_of(advisor_model))
        .unwrap_or(&pricing["sonnet"]);
    let (run_in_c, run_out_c) = *pricing
        .get(family_of(runner_model))
        .unwrap_or(&pricing["sonnet"]);
    let exp_fam = if three_tier {
        family_of(explorer_model)
    } else {
        "haiku"
    };
    let (exp_in_c, exp_out_c) = *pricing.get(exp_fam).unwrap_or(&pricing["haiku"]);

    let dollars = |it: i64, ot: i64, ic: i64, oc: i64| -> f64 {
        (it * ic + ot * oc) as f64 / 1_000_000.0 / 100.0
    };
    let adv_cost_min = dollars(advisor_in_min, advisor_out_min, adv_in_c, adv_out_c);
    let adv_cost_max = dollars(advisor_in_max, advisor_out_max, adv_in_c, adv_out_c);
    let run_cost_min = dollars(runner_in_min, runner_out_min, run_in_c, run_out_c);
    let run_cost_max = dollars(runner_in_max, runner_out_max, run_in_c, run_out_c);
    let exp_cost_min = if three_tier {
        dollars(explorer_in_min, explorer_out_min, exp_in_c, exp_out_c)
    } else {
        0.0
    };
    let exp_cost_max = if three_tier {
        dollars(explorer_in_max, explorer_out_max, exp_in_c, exp_out_c)
    } else {
        0.0
    };
    let cost_min = adv_cost_min + run_cost_min + exp_cost_min;
    let cost_max = adv_cost_max + run_cost_max + exp_cost_max;

    Ok(CostEstimate {
        input_tokens_min: advisor_in_min + runner_in_min + explorer_in_min,
        input_tokens_max: advisor_in_max + runner_in_max + explorer_in_max,
        output_tokens_min: advisor_out_min + runner_out_min + explorer_out_min,
        output_tokens_max: advisor_out_max + runner_out_max + explorer_out_max,
        cost_usd_min: cost_min,
        cost_usd_max: cost_max,
        runner_count,
        file_count,
        advisor_model: advisor_model.to_string(),
        runner_model: runner_model.to_string(),
        explorer_model: if three_tier {
            explorer_model.to_string()
        } else {
            String::new()
        },
        explorer_input_tokens_min: explorer_in_min,
        explorer_input_tokens_max: explorer_in_max,
        explorer_output_tokens_min: explorer_out_min,
        explorer_output_tokens_max: explorer_out_max,
        explorer_cost_usd_min: exp_cost_min,
        explorer_cost_usd_max: exp_cost_max,
        advisor_cost_usd_min: adv_cost_min,
        advisor_cost_usd_max: adv_cost_max,
        coder_cost_usd_min: run_cost_min,
        coder_cost_usd_max: run_cost_max,
    })
}

/// Group an integer with thousands commas (Python `{:,}`).
fn group_thousands(n: i64) -> String {
    let s = n.abs().to_string();
    let bytes = s.as_bytes();
    let mut out = String::new();
    for (i, b) in bytes.iter().enumerate() {
        if i > 0 && (bytes.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(*b as char);
    }
    if n < 0 {
        format!("-{out}")
    } else {
        out
    }
}

/// Human-readable summary. Mirrors `format_estimate`.
pub fn format_estimate(est: &CostEstimate) -> String {
    let as_of = format!(
        "{:04}-{:02}-{:02}",
        PRICING_AS_OF.0, PRICING_AS_OF.1, PRICING_AS_OF.2
    );
    let mut lines = vec![
        "## Cost estimate".to_string(),
        String::new(),
        format!("- Files: {}", est.file_count),
        format!("- Runners: {}", est.runner_count),
    ];
    if !est.explorer_model.is_empty() {
        lines.push(format!(
            "- Models: advisor={}, explorers={}, coders={}",
            est.advisor_model, est.explorer_model, est.runner_model
        ));
        lines.push(format!(
            "- Explorer cost: ${:.2} – ${:.2}",
            est.explorer_cost_usd_min, est.explorer_cost_usd_max
        ));
        lines.push(format!(
            "- Advisor cost: ${:.2} – ${:.2}",
            est.advisor_cost_usd_min, est.advisor_cost_usd_max
        ));
        lines.push(format!(
            "- Coder cost: ${:.2} – ${:.2}",
            est.coder_cost_usd_min, est.coder_cost_usd_max
        ));
    } else {
        lines.push(format!(
            "- Models: advisor={}, runners={}",
            est.advisor_model, est.runner_model
        ));
    }
    lines.push(format!(
        "- Input tokens: {} – {}",
        group_thousands(est.input_tokens_min),
        group_thousands(est.input_tokens_max)
    ));
    lines.push(format!(
        "- Output tokens: {} – {}",
        group_thousands(est.output_tokens_min),
        group_thousands(est.output_tokens_max)
    ));
    lines.push(format!(
        "- **Est. cost: ${:.2} – ${:.2}**",
        est.cost_usd_min, est.cost_usd_max
    ));
    lines.push(String::new());
    lines.push(format!(
        "_Range covers review-only (min) to full fix waves (max). Actual cost depends on dialogue depth and fix count. Pricing as of {as_of} — verify at https://www.anthropic.com/pricing._"
    ));
    lines.join("\n")
}

/// Load a pricing override table from a JSON file. Mirrors `load_pricing`.
pub fn load_pricing(path: &Path) -> Result<HashMap<String, (i64, i64)>, String> {
    let text = crate::fs::read_text_capped(path, 1_048_576)
        .map_err(|e| format!("could not read pricing file {}: {e}", path.display()))?;
    let raw: serde_json::Value = serde_json::from_str(&text)
        .map_err(|e| format!("pricing file {} is not valid JSON: {e}", path.display()))?;
    let Some(obj) = raw.as_object() else {
        return Err(format!(
            "pricing file {} must be a JSON object at the top level",
            path.display()
        ));
    };
    let mut out = HashMap::new();
    for family in ["opus", "sonnet", "haiku"] {
        let entry = obj.get(family).ok_or_else(|| {
            format!(
                "pricing file {} is missing family '{family}'; all three of opus/sonnet/haiku must be present",
                path.display()
            )
        })?;
        let (in_c, out_c) = if let Some(m) = entry.as_object() {
            let getint = |k: &str| -> Result<i64, String> {
                match m.get(k) {
                    Some(serde_json::Value::Number(n)) if n.is_i64() || n.is_u64() => {
                        Ok(n.as_i64().unwrap_or(0))
                    }
                    _ => Err(format!(
                        "pricing file {}: family '{family}' {k:?} must be an integer",
                        path.display()
                    )),
                }
            };
            (getint("input")?, getint("output")?)
        } else if let Some(arr) = entry.as_array().filter(|a| a.len() == 2) {
            let getint = |v: &serde_json::Value, label: &str| -> Result<i64, String> {
                match v {
                    serde_json::Value::Number(n) if n.is_i64() || n.is_u64() => {
                        Ok(n.as_i64().unwrap_or(0))
                    }
                    _ => Err(format!(
                        "pricing file {}: family '{family}' array {label:?} must be an integer",
                        path.display()
                    )),
                }
            };
            (getint(&arr[0], "input")?, getint(&arr[1], "output")?)
        } else {
            return Err(format!(
                "pricing file {}: family '{family}' must be an object {{input, output}} or a [input, output] array",
                path.display()
            ));
        };
        if in_c < 0 || out_c < 0 {
            return Err(format!(
                "pricing file {}: family '{family}' cents values must be non-negative",
                path.display()
            ));
        }
        out.insert(family.to_string(), (in_c, out_c));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn family_resolution() {
        assert_eq!(family_of("claude-opus-4-7"), "opus");
        assert_eq!(family_of("claude-sonnet-4-6"), "sonnet");
        assert_eq!(family_of("claude-haiku-4-5"), "haiku");
        assert_eq!(family_of("gpt-4"), "sonnet"); // unknown -> sonnet fallback
        assert_eq!(family_of("OPUS"), "opus"); // case-insensitive
    }

    #[test]
    fn pricing_table() {
        assert_eq!(default_pricing_for("opus"), (1500, 7500));
        assert_eq!(default_pricing_for("sonnet"), (300, 1500));
        assert_eq!(default_pricing_for("haiku"), (25, 125));
        assert_eq!(default_pricing_for("unknown"), (300, 1500));
    }

    fn golden() -> serde_json::Value {
        serde_json::from_str(include_str!("../tests/parity/cost.json")).unwrap()
    }

    fn task(p: &str, pri: u8) -> FocusTask {
        FocusTask {
            file_path: p.into(),
            priority: pri,
            prompt: "p".into(),
        }
    }

    #[test]
    fn estimate_matches_python() {
        let g = golden();
        let tasks = vec![task("nope_a.py", 5), task("nope_b.py", 3)];
        let est = estimate_cost(
            &tasks,
            None,
            "cc/claude-opus-4-8",
            "cc/claude-sonnet-4-6",
            2,
            Some(5),
            "claude-haiku-4-5",
            0,
            None,
            None,
        )
        .unwrap();
        assert_eq!(est.to_dict(), g["to_dict"]);
        assert_eq!(format_estimate(&est), g["format"].as_str().unwrap());

        let batches = vec![
            FocusBatch {
                batch_id: 1,
                tasks: vec![task("nope_a.py", 5)],
                complexity: crate::focus::Complexity::High,
            },
            FocusBatch {
                batch_id: 2,
                tasks: vec![task("nope_b.py", 3)],
                complexity: crate::focus::Complexity::Medium,
            },
        ];
        let est2 = estimate_cost(
            &tasks,
            Some(&batches),
            "opus",
            "haiku",
            3,
            Some(5),
            "claude-haiku-4-5",
            0,
            None,
            None,
        )
        .unwrap();
        assert_eq!(est2.to_dict(), g["to_dict_batches"]);
    }

    #[test]
    fn load_pricing_shapes_and_errors() {
        let g = golden();
        let dir = std::env::temp_dir().join(format!("advisor_cost_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let obj = dir.join("obj.json");
        std::fs::write(&obj, r#"{"opus":{"input":1500,"output":7500},"sonnet":{"input":300,"output":1500},"haiku":{"input":25,"output":125}}"#).unwrap();
        let arr = dir.join("arr.json");
        std::fs::write(
            &arr,
            r#"{"opus":[1500,7500],"sonnet":[300,1500],"haiku":[25,125]}"#,
        )
        .unwrap();
        let to_map = |v: &serde_json::Value| -> HashMap<String, Vec<i64>> {
            v.as_object()
                .unwrap()
                .iter()
                .map(|(k, x)| {
                    (
                        k.clone(),
                        x.as_array()
                            .unwrap()
                            .iter()
                            .map(|n| n.as_i64().unwrap())
                            .collect(),
                    )
                })
                .collect()
        };
        let loaded_obj = load_pricing(&obj).unwrap();
        let loaded_arr = load_pricing(&arr).unwrap();
        for (fam, pair) in to_map(&g["load_obj"]) {
            assert_eq!(loaded_obj[&fam], (pair[0], pair[1]));
        }
        for (fam, pair) in to_map(&g["load_arr"]) {
            assert_eq!(loaded_arr[&fam], (pair[0], pair[1]));
        }
        let missing = dir.join("e.json");
        std::fs::write(&missing, r#"{"opus":[1,2],"sonnet":[1,2]}"#).unwrap();
        let err = load_pricing(&missing)
            .unwrap_err()
            .replace(missing.to_str().unwrap(), "<P>");
        assert_eq!(err, g["err_missing"].as_str().unwrap());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
