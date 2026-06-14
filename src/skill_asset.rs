use crate::version::resolve_version;

pub fn version_badge() -> String {
    format!("<!-- advisor:{} -->", resolve_version())
}

const SKILL_MD_TEMPLATE: &str = include_str!("assets/skill_asset_skill_md.txt");
const SKILL_MD_UPDATE_TEMPLATE: &str = include_str!("assets/skill_asset_skill_md_update.txt");

pub fn skill_md() -> String {
    SKILL_MD_TEMPLATE.replace("__VERSION_BADGE__", &version_badge())
}

pub fn skill_md_update() -> String {
    SKILL_MD_UPDATE_TEMPLATE.replace("__VERSION_BADGE__", &version_badge())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn golden() -> serde_json::Value {
        let s = include_str!("../tests/parity/skill_strings.json");
        serde_json::from_str(s).unwrap()
    }

    #[test]
    fn parity_skill_md() {
        let g = golden();
        assert_eq!(skill_md(), g["SKILL_MD"].as_str().unwrap());
    }

    #[test]
    fn parity_skill_md_update() {
        let g = golden();
        assert_eq!(skill_md_update(), g["SKILL_MD_UPDATE"].as_str().unwrap());
    }

    #[test]
    fn parity_version_badge() {
        let g = golden();
        assert_eq!(version_badge(), g["version_badge"].as_str().unwrap());
    }

    /// `ADVISOR_UPDATE_SNAPSHOTS=1 cargo test write_skill_strings -- --ignored`
    #[test]
    #[ignore]
    fn write_skill_strings() {
        if std::env::var("ADVISOR_UPDATE_SNAPSHOTS").ok().as_deref() != Some("1") {
            return;
        }
        let mut g = golden();
        g["SKILL_MD"] = serde_json::Value::String(skill_md());
        g["SKILL_MD_UPDATE"] = serde_json::Value::String(skill_md_update());
        g["version_badge"] = serde_json::Value::String(version_badge());
        g["SKILL_MD_CODEX_RENDERED"] =
            serde_json::Value::String(crate::codex_skill::render_codex_skill_md());
        std::fs::write(
            "tests/parity/skill_strings.json",
            serde_json::to_string(&g).unwrap(),
        )
        .unwrap();
    }
}
