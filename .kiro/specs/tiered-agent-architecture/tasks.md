# Implementation Plan: Tiered Agent Architecture

## Overview

Introduces a three-tier agent architecture (Opus advisor → Haiku explorers → Sonnet coders) to the existing orchestration pipeline. Implementation proceeds bottom-up: config layer first, then prompts, then cost estimation and pipeline rendering — each layer building on the one below. Property-based tests validate correctness properties from the design; unit tests cover structural and integration scenarios.

## Tasks

- [x] 1. Extend TeamConfig with explorer fields
  - [x] 1.1 Add explorer fields to TeamConfig dataclass and default_team_config
    - Add `explorer_model: str = "claude-haiku-4-5"`, `max_explorers: int`, `explorer_output_char_ceiling: int = 40_000`, `explorer_file_read_ceiling: int = 40` fields to `TeamConfig`
    - Extend `default_team_config` with `explorer_model`, `max_explorers`, `explorer_output_char_ceiling`, `explorer_file_read_ceiling` parameters
    - Add env var fallbacks: `ADVISOR_EXPLORER_MODEL`, `ADVISOR_MAX_EXPLORERS`, `ADVISOR_EXPLORER_OUTPUT_CHAR_CEILING`, `ADVISOR_EXPLORER_FILE_READ_CEILING`
    - Default `max_explorers` to the post-clamp `max_runners` value when not explicitly provided
    - Clamp `max_explorers` to `[0, POOL_SIZE_CEILING]` with warning on out-of-range (note: 0 is valid for legacy mode)
    - Clamp budget fields (`explorer_output_char_ceiling`, `explorer_file_read_ceiling`) to minimum of 1
    - Extend `warn_unknown_model` check to cover `explorer_model`
    - Emit warning to stderr for non-integer env var values, falling back to defaults
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 7.1, 7.2, 7.6, 7.7, 7.8_

  - [x]* 1.2 Write property tests for TeamConfig explorer fields
    - **Property 1: Explorer environment variable propagation**
    - **Property 2: Explorer environment variable fallback on invalid input**
    - **Property 3: Explorer config field clamping invariant**
    - **Property 4: Default max_explorers mirrors max_runners**
    - **Property 5: Unknown explorer model warning**
    - **Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 7.6, 7.7, 7.8**

  - [x]* 1.3 Write unit tests for TeamConfig explorer defaults and edge cases
    - Test structural defaults (explorer_model, max_explorers, ceilings)
    - Test env var override for each new field
    - Test fallback when env var is non-integer
    - Test clamping at boundaries (0, POOL_SIZE_CEILING, negative, >ceiling)
    - Test warn_unknown_model with invalid explorer_model
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 7.1, 7.2, 7.6, 7.7, 7.8_

- [x] 2. Extend preset system with explorer_model
  - [x] 2.1 Add optional explorer_model field to RulePack and integrate with default_team_config
    - Add `explorer_model: str | None = None` to `RulePack` dataclass
    - In `default_team_config` preset-merge block: if preset provides `explorer_model`, apply it; otherwise use the TeamConfig default `"claude-haiku-4-5"`
    - _Requirements: 10.6_

  - [x]* 2.2 Write property test for preset explorer_model override
    - **Property 17: Preset explorer_model override**
    - **Validates: Requirements 10.6**

  - [x]* 2.3 Write unit tests for preset explorer_model handling
    - Test that RulePack with `explorer_model=None` leaves TeamConfig default intact
    - Test that RulePack with explicit explorer_model overrides the default
    - Test backward compatibility: existing presets work unchanged
    - _Requirements: 10.6_

- [x] 3. Checkpoint — config layer complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement explorer prompt builder
  - [x] 4.1 Create explorer prompt template and build_explorer_prompt function
    - Create `advisor/orchestrate/_prompts/explorer.txt` template with:
      - Read-only instructions (Read, Glob, Grep only)
      - Explicit prohibition of file writing/editing/deletion
      - SCOPE anchor protocol (`SCOPE: <file> · <stage>`)
      - SendMessage-to-team-lead reporting protocol
      - Structured Exploration_Report format (file paths, snippets with line ranges, structural observations)
      - Graceful handling of unreadable files (record path + reason, continue)
    - Create `advisor/orchestrate/explorer_prompts.py` with `build_explorer_prompt(config, target_files, guidance)` function
    - Function accepts TeamConfig, sequence of target file paths, and mapping of file paths to guidance strings
    - Embeds per-file guidance into the prompt
    - Returns complete prompt text as a string
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [x] 4.2 Implement build_explorer_pool_agents function
    - Add `build_explorer_pool_agents(config, pool_size=None)` to `explorer_prompts.py`
    - Produces list of agent spec dicts: `explorer_model`, `run_in_background=True`, `team_name`, `subagent_type="explorer"`, names `explorer-{i}` (i from 1)
    - Respects `max_explorers` ceiling from config
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x]* 4.3 Write property tests for explorer prompt builder
    - **Property 6: Explorer agent spec structure**
    - **Property 7: Explorer prompt read-only invariants**
    - **Property 8: Explorer prompt guidance embedding**
    - **Validates: Requirements 2.1, 2.2, 2.4, 4.1, 4.2, 4.4, 4.5, 4.6, 4.7**

  - [x]* 4.4 Write unit tests for explorer prompt content and agent specs
    - Test prompt contains Read/Glob/Grep instructions
    - Test prompt prohibits file writing
    - Test prompt includes SCOPE anchor protocol
    - Test prompt includes SendMessage-to-team-lead instruction
    - Test Exploration_Report format guidance is present
    - Test unreadable-file handling instructions
    - Test agent spec naming pattern (explorer-1, explorer-2, ...)
    - Test agent spec uses configured explorer_model
    - _Requirements: 2.1, 2.2, 2.3, 4.1, 4.2, 4.3, 4.4, 4.5, 4.8_

- [x] 5. Refactor coder prompt and fix assignment message
  - [x] 5.1 Create build_coder_prompt function and refactor build_runner_pool_prompt
    - Create `build_coder_prompt(runner_id, config)` in the appropriate prompts module
    - Remove exploration-related instructions (grep/glob discovery emphasis)
    - Emphasize code modification, creation, and fix implementation as sole responsibility
    - Add instruction to prefer embedded exploration context over re-reading files
    - Retain fix assignment handling, context pressure, SCOPE anchors, live-dialogue protocol
    - Preserve `build_runner_pool_prompt` as backward-compatible alias delegating to `build_coder_prompt`
    - _Requirements: 5.2, 5.4_

  - [x] 5.2 Extend build_fix_assignment_message with exploration_context parameter
    - Add optional `exploration_context: str | None = None` parameter
    - When provided and non-empty: embed exploration context in the message body BEFORE fix instructions
    - When None or empty: produce existing message format unchanged (legacy mode)
    - _Requirements: 5.5, 5.6, 5.7_

  - [x]* 5.3 Write property tests for coder prompt and fix assignment
    - **Property 9: Coder prompt focuses on code modification**
    - **Property 10: Fix assignment exploration context embedding**
    - **Validates: Requirements 5.2, 5.4, 5.6, 5.7**

  - [x]* 5.4 Write unit tests for coder prompt and fix assignment
    - Test build_coder_prompt contains code modification emphasis
    - Test build_coder_prompt references embedded exploration context preference
    - Test build_runner_pool_prompt still works as alias
    - Test fix assignment with exploration_context=None matches legacy format
    - Test fix assignment with exploration_context embeds it before fix instructions
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

- [x] 6. Checkpoint — prompt layer complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Update advisor prompt for three-tier dispatch
  - [x] 7.1 Extend advisor.txt template for three-tier architecture
    - Add three-tier description: Opus reasons, Haiku explores, Sonnet fixes
    - Update Step 2 to size both explorer and coder pools
    - Add Step 3: dispatch explore wave to Haiku explorers (instead of runners reading directly)
    - Add Step 3.5: synthesize Exploration_Reports before building fix assignments
    - Describe the explore → reason → fix loop
    - Add conditional section: when `max_explorers=0`, operate in legacy two-tier mode
    - Add placeholders: `{explorer_model}`, `{max_explorers}`, `{explorer_output_char_ceiling}`, `{explorer_file_read_ceiling}`
    - Retain Advisor's direct Glob/Grep for structural discovery (Step 1 unchanged)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x] 7.2 Update advisor prompt builder to pass new template placeholders
    - Extend `build_advisor_prompt` (or equivalent function) to pass `explorer_model`, `max_explorers`, `explorer_output_char_ceiling`, `explorer_file_read_ceiling` into the template
    - Conditionally render three-tier vs two-tier sections based on `max_explorers`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x]* 7.3 Write property test for legacy mode prompt
    - **Property 11: Legacy mode prompt and rendering (max_explorers=0)**
    - **Validates: Requirements 6.6, 9.5**

  - [x]* 7.4 Write unit tests for advisor prompt three-tier content
    - Test prompt with max_explorers > 0 contains three-tier description
    - Test prompt mentions explorer dispatch step
    - Test prompt mentions Exploration_Report synthesis
    - Test prompt retains direct Glob/Grep for structural discovery
    - Test prompt with max_explorers=0 describes legacy two-tier mode
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

- [x] 8. Update cost estimation for three tiers
  - [x] 8.1 Extend CostEstimate dataclass with explorer tier fields
    - Add `explorer_model`, `explorer_input_tokens_min`, `explorer_input_tokens_max`, `explorer_output_tokens_min`, `explorer_output_tokens_max`, `explorer_cost_usd_min`, `explorer_cost_usd_max` fields
    - Update `to_dict()` to include all new explorer fields
    - _Requirements: 8.1, 8.5_

  - [x] 8.2 Extend estimate_cost to compute explorer tier costs
    - Add `explorer_model` and `max_explorers` parameters to `estimate_cost`
    - When `max_explorers > 0`: estimate explorer input tokens (sum of content tokens for explore-wave file reads + per-explorer system prompt overhead + message framing), estimate explorer output tokens (per-explorer findings block), use `_family_of(explorer_model)` for haiku pricing
    - When `max_explorers=0`: set all explorer fields to 0
    - _Requirements: 8.2, 8.4_

  - [x] 8.3 Update format_estimate for per-tier breakdown
    - Display per-tier USD cost ranges: Advisor (Opus), Explorers (Haiku), Coders (Sonnet), Total
    - Omit explorer line when explorer fields are 0 (max_explorers=0)
    - _Requirements: 8.3, 8.4_

  - [x]* 8.4 Write property tests for cost estimation
    - **Property 12: Three-tier cost estimation**
    - **Property 13: Cost estimate format per-tier breakdown**
    - **Property 14: CostEstimate serialization includes explorer fields**
    - **Validates: Requirements 8.2, 8.3, 8.4, 8.5**

  - [x]* 8.5 Write unit tests for cost estimation edge cases
    - Test estimate with max_explorers > 0 produces positive explorer costs
    - Test estimate with max_explorers=0 produces zero explorer fields
    - Test format_estimate includes all three tier lines when explorers active
    - Test format_estimate omits explorer line when max_explorers=0
    - Test to_dict includes all explorer keys
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

- [x] 9. Update pipeline rendering for three tiers
  - [x] 9.1 Extend render_pipeline for three-tier output
    - Include `explorer_model` in the rendered output header
    - Add explorer spawn step between advisor spawn and coder spawn
    - Describe the loop as "Explorer discovers → Advisor reasons → Coder fixes"
    - Display `max_explorers` in configuration summary
    - When `max_explorers=0`: render legacy two-tier pipeline without explorer references
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

  - [x]* 9.2 Write property test for pipeline rendering
    - **Property 15: Pipeline rendering includes explorer tier**
    - **Validates: Requirements 9.1, 9.2, 9.4**

  - [x]* 9.3 Write unit tests for pipeline rendering
    - Test render output contains explorer_model when max_explorers > 0
    - Test render output contains explorer spawn step
    - Test render output describes three-tier loop
    - Test render output shows max_explorers count
    - Test render output omits explorer when max_explorers=0
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

- [x] 10. Checkpoint — all tiers wired
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Backward compatibility and model flag isolation
  - [x] 11.1 Verify backward compatibility and add model flag isolation test
    - Ensure existing `--advisor-model` and `--runner-model` CLI flags do not affect `explorer_model`
    - Ensure no new mandatory arguments or env vars are required for a successful run
    - Ensure default TeamConfig enables three-tier mode (max_explorers = max_runners, explorer_model = "claude-haiku-4-5")
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

  - [x]* 11.2 Write property test for model flag isolation
    - **Property 16: Model flag isolation**
    - **Validates: Requirements 10.4**

  - [x]* 11.3 Write unit tests for backward compatibility
    - Test default TeamConfig has three-tier mode enabled
    - Test max_explorers=0 triggers legacy two-tier mode
    - Test existing CLI flags don't affect explorer_model
    - Test no new mandatory arguments needed
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

- [x] 12. Final checkpoint — all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation after each architectural layer
- Property tests validate the 17 universal correctness properties from the design document
- Unit tests validate specific examples, edge cases, and LLM behavioral requirements
- The `build_runner_pool_prompt` alias preserves backward compatibility for any existing callers
- All tests run via `uv run pytest tests/ -v`; type checking via `uv run mypy advisor/`
- Zero new runtime dependencies — all changes use stdlib dataclasses, string templates, os.environ

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "4.1"] },
    { "id": 3, "tasks": ["4.2", "4.3", "4.4", "5.1"] },
    { "id": 4, "tasks": ["5.2", "5.3", "5.4"] },
    { "id": 5, "tasks": ["7.1", "8.1"] },
    { "id": 6, "tasks": ["7.2", "8.2", "9.1"] },
    { "id": 7, "tasks": ["7.3", "7.4", "8.3"] },
    { "id": 8, "tasks": ["8.4", "8.5", "9.2", "9.3"] },
    { "id": 9, "tasks": ["11.1"] },
    { "id": 10, "tasks": ["11.2", "11.3"] }
  ]
}
```
