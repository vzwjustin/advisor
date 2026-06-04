//! `advisor` CLI binary (Rust port — in progress).
//!
//! Only the subcommands whose behavior has been ported and parity-verified are
//! wired up here. The full argparse surface (see RUST_PORT_PLAN.md §2) is being
//! migrated incrementally; until then the Python CLI remains the reference
//! implementation and ships alongside this binary.

use std::process::ExitCode;

use clap::{Parser, Subcommand};

use advisor::presets;

#[derive(Parser)]
#[command(
    name = "advisor",
    version,
    about = "Opus-led code-review-and-fix pipeline for Claude Code (Rust port, in progress)"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// List available rule-pack presets.
    Presets {
        /// Emit the preset catalog as JSON (byte-compatible with the Python CLI).
        #[arg(long)]
        json: bool,
    },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match cli.command {
        Command::Presets { json } => cmd_presets(json),
    }
}

/// `advisor presets [--json]` — mirrors the Python CLI handler.
fn cmd_presets(json: bool) -> ExitCode {
    let packs = presets::list_presets();
    if json {
        println!("{}", presets::presets_json(&packs));
    } else {
        // `presets_pretty` already includes the trailing newline structure.
        print!("{}", presets::presets_pretty(&packs));
    }
    ExitCode::SUCCESS
}
