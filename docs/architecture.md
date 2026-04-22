# advisor — architecture

## Module dependency graph

```
┌─────────────────┐
│   __main__.py   │  ← CLI entry point (argparse, subcommands, stdin piping)
│  (advisor CLI)  │
└────────┬────────┘
         │
         │ imports
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Top-level modules                       │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐         │
│  │ install  │  │   rank   │  │  focus   │  │  verify  │         │
│  │          │  │          │  │          │  │          │         │
│  │ nudge +  │  │ scoring  │  │ batching │  │ findings │         │
│  │ skill IO │  │ priority │  │ plan gen │  │ parsing  │         │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘         │
│       │             │             │             │               │
│       └─────────────┴─────────────┴─────────────┘               │
│                           │                                     │
│                           ▼                                     │
│                  ┌──────────────────┐                           │
│                  │   _style.py      │                           │
│                  │ ANSI, markdown   │                           │
│                  │ colorizer        │                           │
│                  └──────────────────┘                           │
└─────────────────────────────────────────────────────────────────┘
         │
         │ also imports
         ▼
┌──────────────────────────────────────────────────────────────────┐
│                     advisor.orchestrate package                  │
│                                                                  │
│  ┌────────────┐  ┌──────────────────┐  ┌──────────────────┐      │
│  │ config.py  │  │ advisor_prompt.py│  │ runner_prompts.py│      │
│  │            │  │                  │  │                  │      │
│  │ TeamConfig │  │ Opus system      │  │ Sonnet runner    │      │
│  │ dataclass  │  │ prompt builder   │  │ pool + dispatch  │      │
│  └─────┬──────┘  └─────────┬────────┘  └────────┬─────────┘      │
│        │                   │                    │                │
│        │         uses      │         uses       │                │
│        │                   ▼                    │                │
│        │       ┌─────────────────────┐          │                │
│        │       │ _prompts/advisor.txt│          │                │
│        │       │ (prompt body res.)  │          │                │
│        │       └─────────────────────┘          │                │
│        │                                        │                │
│        └──────────────┬─────────────────────────┘                │
│                       ▼                                          │
│              ┌──────────────────────┐                            │
│              │ verify_dispatch.py   │                            │
│              │ pipeline.py          │                            │
│              └──────────────────────┘                            │
└──────────────────────────────────────────────────────────────────┘

         ┌──────────────────┐
         │ skill_asset.py   │  ← bundled SKILL.md constant
         └──────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                 v0.5 feature-pack modules                        │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ sarif.py │  │presets.py│  │ baseline.py  │  │suppressions  │  │
│  │          │  │          │  │              │  │     .py      │  │
│  │ SARIF    │  │ RulePack │  │ snapshot /   │  │ per-rule     │  │
│  │ 2.1.0    │  │ catalog  │  │ diff         │  │ drops + TTL  │  │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘  └──────┬───────┘  │
│       │             │               │                 │          │
│       ▼             ▼               ▼                 ▼          │
│              ┌──────────────────────────────┐                    │
│              │       pr_comment.py          │                    │
│              │  GFM summary for PR body     │                    │
│              └──────────────────────────────┘                    │
└──────────────────────────────────────────────────────────────────┘
```

## Runtime flow (what happens when a user invokes `/advisor`)

```
Claude Code (Opus)
     │
     ├── 1. TeamDelete           ← clears stale team
     │
     ├── 2. TeamCreate            ← fresh team with advisor + runner pool
     │
     ├── 3. Bash: advisor plan <dir>    ← silent; output redirected to /tmp
     │        │
     │        └─→ advisor.__main__.cmd_plan
     │               │
     │               ├─→ rank._score_file × N        (priority scores)
     │               ├─→ focus.create_focus_batches  (batches of files)
     │               └─→ focus.format_batch_plan     (human-readable)
     │
     ├── 4. Agent(advisor)         ← spawn Opus advisor
     │        │
     │        └─→ orchestrate.build_advisor_prompt(config)
     │               │
     │               └─→ loads _prompts/advisor.txt and fills TeamConfig vars
     │
     ├── 5. Advisor does its own Glob/Grep/Read exploration
     │
     ├── 6. Advisor dispatches runners (SendMessage):
     │        orchestrate.build_runner_dispatch_messages
     │
     ├── 7. Runners work, ping progress, send findings
     │
     ├── 8. Advisor verifies findings (orchestrate.verify_dispatch)
     │
     └── 9. TeamDelete on shutdown
```

## Data contract at a glance

| Object         | Defined in                       | Purpose                              |
|----------------|----------------------------------|--------------------------------------|
| `TeamConfig`   | `orchestrate/config.py`          | immutable config for a single run    |
| `FocusTask`    | `focus.py`                       | one file + priority + reasons        |
| `FocusBatch`   | `focus.py`                       | group of FocusTasks for one runner   |
| `Finding`      | `verify.py`                      | parsed runner output row             |
| `InstallResult`| `install.py`                     | nudge/skill write outcome + error    |
| `InstallAction`| `install.py` (StrEnum)           | installed / updated / unchanged / …  |
| `Status`       | `install.py`                     | advisor install health snapshot      |

All are `@dataclass(frozen=True, slots=True)` for immutability and memory
footprint. Instances are deep-hashable, safe to cache, and safe to pass
across subprocess boundaries (JSON-serializable via `dataclasses.asdict`).

## Design invariants

1. **No external API calls.** Everything runs inside Claude Code's own
   agent infrastructure. The `advisor` Python package is a prompt builder
   and plan formatter — it never hits the network.
2. **Idempotent install.** `advisor install` can run repeatedly and always
   converges to the same `CLAUDE.md` and `SKILL.md` state. Sentinel
   markers in the nudge block make updates trivial.
3. **Atomic writes.** Every file write goes through `_atomic_write_text`
   which uses `tempfile.mkstemp` + `os.replace`. Partial writes are
   impossible. Symlink targets are refused.
4. **Pure string prompt builders.** `build_*_prompt` functions take
   dataclasses in and return strings out. No I/O. Easy to unit-test.
5. **CLI subcommands return exit codes, not sys.exit.** All paths funnel
   through `main()` which applies the return code after the `ensure_nudge`
   lifecycle hook has run.
