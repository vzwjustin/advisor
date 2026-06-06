//! Port of `advisor/presets.py` — curated rule-pack presets (pure data).
//!
//! Presets cannot execute code; they only contribute extra ranking keywords and
//! adjust `TeamConfig` default fields. Seven ship out of the box. This module
//! mirrors the Python `RulePack` dataclass, `PRESETS` table, `get_preset`, and
//! `list_presets`, plus the CLI rendering used by `advisor presets`.

use crate::jsonutil::ensure_ascii;

/// A curated set of tuning knobs for a common stack. Mirrors the `RulePack`
/// dataclass. `extra_keywords_by_tier` preserves insertion order (as a `Vec` of
/// `(tier, keywords)`) to match the Python dict's emitted JSON ordering.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RulePack {
    pub name: &'static str,
    pub description: &'static str,
    pub file_types: &'static str,
    pub min_priority: i64,
    pub extra_keywords_by_tier: Vec<(i64, Vec<&'static str>)>,
    pub test_command: Option<&'static str>,
    pub notes: Vec<&'static str>,
    pub explorer_model: Option<&'static str>,
}

/// Return all presets, sorted by name. Mirrors Python `list_presets`.
pub fn list_presets() -> Vec<RulePack> {
    let mut packs = all_presets();
    packs.sort_by(|a, b| a.name.cmp(b.name));
    packs
}

/// Return the preset named `name` (whitespace-trimmed). Returns an error string
/// listing available presets when unknown — mirrors Python `get_preset`'s
/// `ValueError` message.
pub fn get_preset(name: &str) -> Result<RulePack, String> {
    let name = name.trim();
    if let Some(p) = all_presets().into_iter().find(|p| p.name == name) {
        return Ok(p);
    }
    let mut names: Vec<&str> = all_presets().iter().map(|p| p.name).collect();
    names.sort();
    Err(format!(
        "unknown preset {name:?}. available: {}",
        names.join(", ")
    ))
}

/// JSON-serialize a preset list to match the Python CLI's `presets --json`
/// payload byte-for-byte (key order, 2-space indent, `ensure_ascii=True`).
pub fn presets_json(presets: &[RulePack]) -> String {
    // Build the payload by hand to guarantee key ordering identical to the
    // Python CLI handler (name, description, file_types, min_priority,
    // test_command, notes, extra_keywords_by_tier).
    let mut out = String::new();
    out.push_str("{\n");
    out.push_str("  \"schema_version\": \"1.0\",\n");
    out.push_str(&format!("  \"count\": {},\n", presets.len()));
    out.push_str("  \"presets\": [\n");
    for (i, p) in presets.iter().enumerate() {
        out.push_str("    {\n");
        out.push_str(&format!("      \"name\": {},\n", json_str(p.name)));
        out.push_str(&format!(
            "      \"description\": {},\n",
            json_str(p.description)
        ));
        out.push_str(&format!(
            "      \"file_types\": {},\n",
            json_str(p.file_types)
        ));
        out.push_str(&format!("      \"min_priority\": {},\n", p.min_priority));
        out.push_str(&format!(
            "      \"test_command\": {},\n",
            match p.test_command {
                Some(s) => json_str(s),
                None => "null".to_string(),
            }
        ));
        // notes array
        if p.notes.is_empty() {
            out.push_str("      \"notes\": [],\n");
        } else {
            out.push_str("      \"notes\": [\n");
            for (j, note) in p.notes.iter().enumerate() {
                let comma = if j + 1 < p.notes.len() { "," } else { "" };
                out.push_str(&format!("        {}{}\n", json_str(note), comma));
            }
            out.push_str("      ],\n");
        }
        // extra_keywords_by_tier object
        if p.extra_keywords_by_tier.is_empty() {
            out.push_str("      \"extra_keywords_by_tier\": {}\n");
        } else {
            out.push_str("      \"extra_keywords_by_tier\": {\n");
            for (ti, (tier, kws)) in p.extra_keywords_by_tier.iter().enumerate() {
                let tier_comma = if ti + 1 < p.extra_keywords_by_tier.len() {
                    ","
                } else {
                    ""
                };
                out.push_str(&format!("        \"{tier}\": [\n"));
                for (ki, kw) in kws.iter().enumerate() {
                    let kw_comma = if ki + 1 < kws.len() { "," } else { "" };
                    out.push_str(&format!("          {}{}\n", json_str(kw), kw_comma));
                }
                out.push_str(&format!("        ]{tier_comma}\n"));
            }
            out.push_str("      }\n");
        }
        let item_comma = if i + 1 < presets.len() { "," } else { "" };
        out.push_str(&format!("    }}{item_comma}\n"));
    }
    out.push_str("  ]\n");
    out.push('}');
    out
}

/// Render the human-readable `advisor presets` markdown body + CTA, matching the
/// Python CLI with color disabled (`NO_COLOR`).
pub fn presets_pretty(presets: &[RulePack]) -> String {
    let mut lines: Vec<String> = vec![format!("## Presets ({})", presets.len()), String::new()];
    for p in presets {
        lines.push(format!("- **`{}`** \u{2014} {}", p.name, p.description));
        lines.push(format!(
            "  - defaults: `file-types={}`, `min-priority={}`, `test-cmd={}`",
            p.file_types,
            p.min_priority,
            p.test_command.unwrap_or("(none)")
        ));
        if !p.extra_keywords_by_tier.is_empty() {
            let mut tiers: Vec<(i64, usize)> = p
                .extra_keywords_by_tier
                .iter()
                .map(|(k, v)| (*k, v.len()))
                .collect();
            tiers.sort_by_key(|b| std::cmp::Reverse(b.0)); // reverse by tier
            let rendered: Vec<String> = tiers.iter().map(|(k, n)| format!("P{k}:{n}")).collect();
            lines.push(format!("  - extra keywords: {}", rendered.join(", ")));
        }
        for note in &p.notes {
            lines.push(format!("  - _{note}_"));
        }
        lines.push(String::new());
    }
    // Python: print(colorize_markdown(joined.rstrip() + "\n")) -> body + "\n\n"
    // (rstrip drops trailing blanks, +"\n" and print's "\n" give one blank line)
    // then print(cta("use", "advisor plan . --preset <name>")).
    let body = lines.join("\n");
    let body = body.trim_end_matches([' ', '\n']).to_string();
    let cta = "  > use  advisor plan . --preset <name>";
    format!("{body}\n\n{cta}\n")
}

/// Minimal JSON string encoder matching CPython `json.dumps` with
/// `ensure_ascii=True` for the simple strings in preset data.
fn json_str(s: &str) -> String {
    let mut escaped = String::with_capacity(s.len() + 2);
    escaped.push('"');
    for ch in s.chars() {
        match ch {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            c if (c as u32) < 0x20 => escaped.push_str(&format!("\\u{:04x}", c as u32)),
            c => escaped.push(c),
        }
    }
    escaped.push('"');
    ensure_ascii(&escaped)
}

fn all_presets() -> Vec<RulePack> {
    vec![
        RulePack {
            name: "general-python",
            description: "Generic Python codebase \u{2014} no stack-specific keyword boosting",
            file_types: "*.py",
            min_priority: 3,
            extra_keywords_by_tier: vec![],
            test_command: Some("pytest -q"),
            notes: vec![
                "use when no python-web / python-cli preset fits \u{2014} the ranker still uses its language-aware baseline keywords",
            ],
            explorer_model: None,
        },
        RulePack {
            name: "python-web",
            description: "Flask / Django / FastAPI \u{2014} auth + request handling focus",
            file_types: "*.py",
            min_priority: 3,
            extra_keywords_by_tier: vec![
                (5, vec!["csrf", "session", "login_required", "jwt", "oauth"]),
                (4, vec!["request.form", "request.json", "deserialize", "pickle.loads"]),
            ],
            test_command: Some("pytest -q"),
            notes: vec!["pairs well with `--since origin/main` for PR reviews"],
            explorer_model: None,
        },
        RulePack {
            name: "python-cli",
            description: "argparse / click CLIs \u{2014} subprocess + shell focus",
            file_types: "*.py",
            min_priority: 3,
            extra_keywords_by_tier: vec![(3, vec!["subprocess", "shell=True", "os.system"])],
            test_command: Some("pytest -q"),
            notes: vec![],
            explorer_model: None,
        },
        RulePack {
            name: "node-api",
            description: "Express / Fastify / Koa \u{2014} JWT, body parsing, eval surfaces",
            file_types: "*.js,*.ts",
            min_priority: 3,
            extra_keywords_by_tier: vec![
                (5, vec!["jsonwebtoken", "bcrypt", "session", "cookie-parser"]),
                (4, vec!["body-parser", "req.body", "eval", "new Function"]),
            ],
            test_command: Some("npm test"),
            notes: vec![],
            explorer_model: None,
        },
        RulePack {
            name: "typescript-react",
            description: "React + TypeScript \u{2014} DOM sinks and storage",
            file_types: "*.ts,*.tsx",
            min_priority: 3,
            extra_keywords_by_tier: vec![(
                4,
                vec!["dangerouslySetInnerHTML", "innerHTML", "href={", "localStorage"],
            )],
            test_command: Some("npm test"),
            notes: vec![],
            explorer_model: None,
        },
        RulePack {
            name: "go-service",
            description: "Go services \u{2014} net/http, database/sql, exec",
            file_types: "*.go",
            min_priority: 3,
            extra_keywords_by_tier: vec![
                (3, vec!["net/http", "sql.Query", "exec.Command", "unsafe."]),
                (4, vec!["ParseForm", "Unmarshal"]),
            ],
            test_command: Some("go test ./..."),
            notes: vec![],
            explorer_model: None,
        },
        RulePack {
            name: "rust-crate",
            description: "Rust libraries / crates \u{2014} unsafe and unwinding",
            file_types: "*.rs",
            min_priority: 3,
            extra_keywords_by_tier: vec![
                (3, vec!["unsafe", "transmute", "from_raw", "catch_unwind"]),
                (4, vec!["unwrap()"]),
            ],
            test_command: Some("cargo test"),
            notes: vec!["`unwrap()` is flagged P4 \u{2014} expected in tests, suspicious in prod"],
            explorer_model: None,
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parity_presets_json() {
        use std::collections::HashMap;
        let raw = std::fs::read_to_string("tests/parity/presets.json").unwrap();
        let v: HashMap<String, serde_json::Value> = serde_json::from_str(&raw).unwrap();
        let presets = list_presets();
        assert_eq!(presets.len() as u64, v["preset_count"].as_u64().unwrap());
        let names: Vec<&str> = presets.iter().map(|p| p.name).collect();
        let expected: Vec<&str> = v["preset_names"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s.as_str().unwrap())
            .collect();
        // Python's PRESETS dict is insertion-ordered; Rust may sort differently — check set equality
        let mut got = names.clone();
        got.sort_unstable();
        let mut exp = expected.clone();
        exp.sort_unstable();
        assert_eq!(got, exp);
        assert_eq!(
            get_preset("python-web").unwrap().file_types,
            v["python_web_file_types"].as_str().unwrap()
        );
        assert_eq!(
            get_preset("python-web").unwrap().min_priority as u64,
            v["python_web_min_priority"].as_u64().unwrap()
        );
        assert_eq!(
            !get_preset("python-web")
                .unwrap()
                .extra_keywords_by_tier
                .is_empty(),
            v["python_web_has_extra_keywords"].as_bool().unwrap()
        );
        assert_eq!(
            get_preset("rust-crate").unwrap().file_types,
            v["rust_crate_file_types"].as_str().unwrap()
        );
        assert!(
            get_preset("nonexistent-preset").is_err() == v["get_unknown_raises"].as_bool().unwrap()
        );
    }

    #[test]
    fn list_is_sorted_by_name() {
        let names: Vec<&str> = list_presets().iter().map(|p| p.name).collect();
        assert_eq!(
            names,
            vec![
                "general-python",
                "go-service",
                "node-api",
                "python-cli",
                "python-web",
                "rust-crate",
                "typescript-react",
            ]
        );
    }

    #[test]
    fn get_preset_trims_and_errors() {
        assert_eq!(get_preset("python-web ").unwrap().name, "python-web");
        let err = get_preset("nope").unwrap_err();
        assert!(err.contains("unknown preset \"nope\""));
        assert!(err.contains("available: general-python"));
    }

    #[test]
    fn json_matches_python_golden() {
        let golden = include_str!("../tests/parity/presets_json.txt");
        // The golden file has a trailing newline from `print`; our renderer
        // produces the payload without it.
        let expected = golden.strip_suffix('\n').unwrap_or(golden);
        assert_eq!(presets_json(&list_presets()), expected);
    }

    #[test]
    fn pretty_matches_python_golden() {
        let golden = include_str!("../tests/parity/presets_plain.txt");
        assert_eq!(presets_pretty(&list_presets()), golden);
    }
}
