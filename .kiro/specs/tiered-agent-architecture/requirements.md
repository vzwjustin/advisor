# Requirements Document

## Introduction

The Advisor CLI currently operates a two-tier Agent Teams pipeline: an Opus advisor (reasoning, planning, dispatch) and a pool of Sonnet runners (exploration + fixing). This feature splits the runner tier into two specialized tiers — Haiku explorers for read-only discovery and Sonnet coders for write operations — creating a three-tier architecture optimized for cost. Haiku exploration costs ~12x less than Sonnet per token, so delegating file reading, grep, glob, and structural discovery to Haiku agents dramatically reduces spend on the explore wave while preserving Sonnet quality for the fix wave.

Current flow: `Opus advisor → Sonnet runners (explore + fix)`
Target flow: `Opus advisor → Haiku explorers (read-only) → Sonnet coders (write-only)`

The Opus advisor retains its role as strategist — it reasons, plans, and dispatches — but stops reading files directly (except for initial scope sizing via Glob/Grep). Haiku explorers feed context back to the advisor, who synthesizes it and dispatches targeted fix assignments to Sonnet coders with pre-gathered context so coders spend tokens writing, not reading.

## Glossary

- **Advisor_Agent**: The Opus-tier agent responsible for high-level reasoning, planning, prioritization, and dispatch decisions. Already exists in the current pipeline.
- **Explorer_Agent**: A new Haiku-tier agent responsible for cheap file reading, structural scanning, grep/glob, and context gathering. Read-only.
- **Coder_Agent**: The existing Sonnet-tier runner, refined to focus exclusively on code modification, creation, and fix implementation. Formerly called "runner" in the two-tier architecture.
- **TeamConfig**: The frozen dataclass that holds all configuration for a review team run.
- **Exploration_Report**: A structured summary an Explorer_Agent returns after scanning files — contains file contents, structural observations, and relevance annotations.
- **Dispatch_Plan**: The structured output from the Advisor_Agent that assigns tasks to Explorer_Agents and Coder_Agents based on task category.
- **Task_Category**: A classification of work as either `explore` (read/scan/discover) or `fix` (write/modify/create).
- **SCOPE_Anchor**: The existing per-message `SCOPE: <file> · <stage>` line runners emit to signal position. Extended to Explorer_Agents.

## Requirements

### Requirement 1: Explorer Model Configuration

**User Story:** As a user, I want to configure a separate explorer model tier in TeamConfig, so that cheap exploration tasks use Haiku pricing instead of Sonnet pricing.

#### Acceptance Criteria

1. THE TeamConfig SHALL include an `explorer_model` field that defaults to `"claude-haiku-4-5"`.
2. WHERE the `ADVISOR_EXPLORER_MODEL` environment variable is set, THE `default_team_config` function SHALL use its value as the `explorer_model` when the caller passes the default sentinel value `"claude-haiku-4-5"`.
3. IF `warn_unknown_model` is True AND the `explorer_model` value fails `is_known_model` validation, THEN THE `default_team_config` function SHALL emit a warning to stderr indicating the model name may be rejected.
4. THE TeamConfig SHALL include a `max_explorers` field that defaults to the final (post-clamp) value of `max_runners`.
5. WHERE the `ADVISOR_MAX_EXPLORERS` environment variable is set, THE `default_team_config` function SHALL parse its value as an integer and use it as the `max_explorers` default.
6. IF the `ADVISOR_MAX_EXPLORERS` environment variable is set to a non-integer value, THEN THE `default_team_config` function SHALL emit a warning to stderr and fall back to the `max_runners`-derived default.
7. THE `default_team_config` function SHALL clamp `max_explorers` to the range [0, POOL_SIZE_CEILING] and emit a warning to stderr when the provided value falls outside that range.

### Requirement 2: Explorer Agent Spawning and Lifecycle

**User Story:** As the orchestration pipeline, I want to spawn Haiku-tier Explorer_Agents as part of the Agent Teams pool, so that cheap exploration does not consume Sonnet tokens.

#### Acceptance Criteria

1. WHEN the Advisor_Agent dispatches exploration tasks, THE Orchestrator SHALL spawn Explorer_Agents using the configured `explorer_model` via the Agent Teams `Agent()` tool, with `run_in_background=true`, the configured `team_name`, and names following the pattern `explorer-{i}` where `i` is a sequential integer starting at 1.
2. THE Orchestrator SHALL spawn Explorer_Agents with a `subagent_type` of `"explorer"`.
3. WHEN an Explorer_Agent is spawned, THE Orchestrator SHALL provide a prompt that scopes the agent to read-only operations (Read, Glob, Grep).
4. THE Orchestrator SHALL support spawning multiple Explorer_Agents concurrently, up to the configured `max_explorers` ceiling.
5. WHEN an Explorer_Agent has read all files in its assigned task batch or has reached a budget ceiling defined in TeamConfig, THE Explorer_Agent SHALL send its Exploration_Report to team-lead via `SendMessage`.
6. THE Explorer_Agent lifecycle SHALL follow the same `shutdown_request` / `shutdown_response` protocol as existing runners.
7. IF an Explorer_Agent fails to respond within 120 seconds or returns an error, THEN THE Orchestrator SHALL log the failure, mark the exploration task as incomplete, and allow the Advisor_Agent to re-dispatch the task to a fresh Explorer_Agent or fall back to a Coder_Agent.

### Requirement 3: Task Routing by Category

**User Story:** As the Advisor_Agent, I want to classify dispatch tasks as "explore" or "fix" in the Dispatch_Plan, so that each task routes to the cheapest capable tier.

#### Acceptance Criteria

1. WHEN the Advisor_Agent generates a Dispatch_Plan, THE Advisor_Agent SHALL assign each task exactly one Task_Category: either `explore` or `fix`.
2. WHEN a task is classified as `explore`, THE Advisor_Agent SHALL route the task to an Explorer_Agent.
3. WHEN a task is classified as `fix`, THE Advisor_Agent SHALL route the task to a Coder_Agent.
4. THE Advisor_Agent SHALL classify tasks that require no file writing, editing, or deletion — involving only file reading, grep, glob, structural discovery, or context gathering — as `explore`.
5. THE Advisor_Agent SHALL classify tasks that involve code modification, creation, deletion, or refactoring as `fix`.
6. WHEN the Advisor_Agent needs file contents to reason about a fix plan, THE Advisor_Agent SHALL dispatch a file-read exploration to an Explorer_Agent rather than using the Read tool directly; this does not restrict the Advisor_Agent's direct use of Glob or Grep for initial structural discovery.
7. WHEN a task requires both reading existing files and modifying code, THE Advisor_Agent SHALL split it into an `explore` subtask (to gather context) followed by a `fix` subtask (to apply changes), routing each to its respective agent tier.

### Requirement 4: Explorer Agent Prompt and Constraints

**User Story:** As a user, I want Explorer_Agents restricted to read-only operations with structured output, so that cheap Haiku exploration cannot accidentally modify the codebase.

#### Acceptance Criteria

1. THE Explorer_Agent prompt SHALL instruct the agent to use only Read, Glob, and Grep tools.
2. THE Explorer_Agent prompt SHALL explicitly prohibit file writing, editing, or deletion.
3. WHEN an Explorer_Agent completes its task, THE Explorer_Agent SHALL return an Exploration_Report containing: file paths read, code snippets with file path and line-number ranges matching the per-file guidance, and structural observations (function/class signatures, import relationships, and call-site locations).
4. THE Explorer_Agent prompt SHALL include the SCOPE_Anchor protocol (opening each message with `SCOPE: <file> · <stage>`).
5. THE Explorer_Agent prompt SHALL instruct the agent to report findings to team-lead via `SendMessage(to='team-lead')`.
6. THE `build_explorer_prompt` function SHALL accept a `TeamConfig`, a sequence of target file paths, and a mapping of file paths to guidance strings from the Advisor_Agent, and return the complete explorer prompt text as a string.
7. THE `build_explorer_prompt` function SHALL embed the per-file guidance string for each target file into the prompt, instructing the Explorer_Agent what to look for and what context to gather in that file.
8. IF a target file path cannot be read (missing, permission denied, or binary), THEN THE Explorer_Agent SHALL record the file path and the failure reason in the Exploration_Report and continue processing the remaining files in its batch.

### Requirement 5: Coder Agent Prompt Refinement

**User Story:** As a user, I want Coder_Agents to receive pre-gathered context from Explorer_Agents, so that Sonnet tokens are spent writing fixes rather than reading files.

#### Acceptance Criteria

1. WHEN the Advisor_Agent dispatches a fix task to a Coder_Agent, THE Advisor_Agent SHALL include the Exploration_Report content for files referenced in the fix assignment as inline context in the fix assignment message.
2. THE Coder_Agent prompt SHALL instruct the agent to use the provided exploration context rather than re-reading files whose content is already present in the exploration context.
3. IF the exploration context does not include a file needed for the fix, or does not cover the specific line range required, THEN THE Coder_Agent SHALL read the file directly using Read tools.
4. THE existing `build_runner_pool_prompt` function SHALL be refactored into `build_coder_prompt` that removes exploration-related instructions (grep/glob discovery) and emphasizes code modification, creation, and fix implementation as the agent's sole responsibility.
5. THE `build_fix_assignment_message` function SHALL accept an optional `exploration_context` parameter of type `str` containing Exploration_Report snippets for files referenced in the fix assignment.
6. IF the `exploration_context` parameter is None or empty, THEN THE `build_fix_assignment_message` function SHALL produce a fix assignment message without an exploration context section (preserving existing behavior for legacy two-tier mode).
7. WHEN exploration context is provided, THE `build_fix_assignment_message` function SHALL embed the exploration context in the message body before the fix instructions so the Coder_Agent sees file contents without needing to re-read them.

### Requirement 6: Advisor Prompt Update for Three-Tier Dispatch

**User Story:** As the orchestration pipeline, I want the Advisor_Agent prompt to describe the three-tier dispatch model, so that Opus knows to delegate exploration to Haiku before dispatching fixes to Sonnet.

#### Acceptance Criteria

1. THE `advisor.txt` prompt template SHALL describe the three-tier architecture: Opus reasons, Haiku explores, Sonnet fixes.
2. THE advisor prompt SHALL instruct the Advisor_Agent to dispatch Explorer_Agents for the explore wave (Step 3) instead of having runners read files directly.
3. THE advisor prompt SHALL instruct the Advisor_Agent to synthesize Exploration_Reports before building fix assignments for Coder_Agents.
4. THE advisor prompt SHALL retain the Advisor_Agent's ability to perform direct Glob and Grep for initial structural discovery (Step 1) — only file Read operations are delegated to explorers.
5. THE advisor prompt SHALL describe the explore → reason → fix loop as: Explorer discovers → Advisor synthesizes → Coder fixes.
6. IF `max_explorers` is 0, THEN THE advisor prompt SHALL instruct the Advisor_Agent to operate in legacy two-tier mode where Coder_Agents perform both exploration and fixing.

### Requirement 7: Explorer Budget and Rotation

**User Story:** As the orchestration pipeline, I want Explorer_Agents to have budget limits, so that cheap exploration sessions do not accumulate unbounded context in Haiku agents.

#### Acceptance Criteria

1. THE TeamConfig SHALL include an `explorer_output_char_ceiling` field that defaults to 40000 characters.
2. THE TeamConfig SHALL include an `explorer_file_read_ceiling` field that defaults to 40 files.
3. WHEN an Explorer_Agent's cumulative reply character count (sum of `len(text)` across all messages sent by that explorer) crosses 80% of `explorer_output_char_ceiling`, THE Advisor_Agent SHALL rotate to a fresh Explorer_Agent and transfer any remaining unfinished exploration tasks from the rotated agent's assignment to the new agent.
4. WHEN an Explorer_Agent has read `explorer_file_read_ceiling` distinct files (deduplicated by normalized path), THE Advisor_Agent SHALL rotate to a fresh Explorer_Agent and transfer any remaining unfinished exploration tasks from the rotated agent's assignment to the new agent.
5. WHEN an Explorer_Agent is rotated due to a budget limit, THE rotated Explorer_Agent SHALL send its partial Exploration_Report to the Advisor_Agent before shutdown.
6. WHERE `ADVISOR_EXPLORER_OUTPUT_CHAR_CEILING` environment variable is set, THE `default_team_config` function SHALL use its integer value, falling back to the default when the value is not a valid integer.
7. WHERE `ADVISOR_EXPLORER_FILE_READ_CEILING` environment variable is set, THE `default_team_config` function SHALL use its integer value, falling back to the default when the value is not a valid integer.
8. THE `default_team_config` function SHALL clamp both explorer budget fields to a minimum of 1.

### Requirement 8: Cost Estimation for Three Tiers

**User Story:** As a user, I want `advisor plan` cost estimates to reflect the three-tier architecture, so that I can see projected spend broken down by Opus/Sonnet/Haiku before running.

#### Acceptance Criteria

1. THE `CostEstimate` dataclass SHALL include per-tier token fields (`explorer_input_tokens_min`, `explorer_input_tokens_max`, `explorer_output_tokens_min`, `explorer_output_tokens_max`) and a per-tier cost field (`explorer_cost_usd_min`, `explorer_cost_usd_max`) for the explorer tier, in addition to existing aggregate fields and the `explorer_model` name.
2. WHEN `estimate_cost` is called with a TeamConfig that has `max_explorers > 0`, THE function SHALL estimate explorer input tokens as the sum of content tokens for all task files (the explore-wave read pass) plus per-explorer system prompt overhead and message framing, and estimate explorer output tokens as a per-explorer findings block, using the `explorer_model` pricing family resolved via `_family_of`.
3. THE `format_estimate` function SHALL display per-tier USD cost ranges for advisor (Opus), explorer (Haiku), and coder (Sonnet), plus a total projected USD range combining all three tiers.
4. WHEN `max_explorers` is 0, THE `estimate_cost` function SHALL set all explorer token and cost fields to 0, and THE `format_estimate` function SHALL omit the explorer line from rendered output.
5. THE `CostEstimate.to_dict` method SHALL include explorer tier fields (`explorer_model`, `explorer_input_tokens_min`, `explorer_input_tokens_max`, `explorer_output_tokens_min`, `explorer_output_tokens_max`, `explorer_cost_usd_min`, `explorer_cost_usd_max`) so that JSON output contains the full three-tier breakdown.

### Requirement 9: Pipeline Rendering for Three Tiers

**User Story:** As a user, I want `render_pipeline` to display the three-tier architecture, so that the reference output reflects the actual agent topology.

#### Acceptance Criteria

1. THE `render_pipeline` function SHALL include the `explorer_model` in the rendered output header alongside `advisor_model` and `runner_model`.
2. THE `render_pipeline` function SHALL include a step for spawning Explorer_Agents between the advisor spawn and the coder spawn.
3. THE `render_pipeline` function SHALL describe the loop as: Explorer discovers → Advisor reasons → Coder fixes.
4. THE `render_pipeline` function SHALL display `max_explorers` in the configuration summary.
5. WHEN `max_explorers` is 0, THE `render_pipeline` function SHALL render the legacy two-tier pipeline without Explorer_Agent references.

### Requirement 10: Backward Compatibility

**User Story:** As an existing user, I want the three-tier architecture to be the new default without breaking existing configurations or CLI invocations.

#### Acceptance Criteria

1. THE default TeamConfig SHALL enable three-tier mode with `max_explorers` equal to `max_runners` and `explorer_model` set to `"claude-haiku-4-5"`.
2. IF `max_explorers` is set to 0, THEN THE pipeline SHALL operate in legacy two-tier mode where Coder_Agents receive both exploration and fix tasks and no Explorer_Agents are spawned.
3. WHEN upgrading from a previous version, THE CLI SHALL not require any new mandatory arguments or environment variables to produce a successful run.
4. THE existing `--advisor-model` and `--runner-model` CLI flags SHALL continue to set `advisor_model` and `runner_model` on TeamConfig without affecting explorer-tier configuration.
5. IF the `explorer_model` is not explicitly provided via CLI or the `ADVISOR_EXPLORER_MODEL` environment variable, THEN THE TeamConfig SHALL use the default `"claude-haiku-4-5"` without user intervention.
6. THE existing preset system SHALL support an optional `explorer_model` field in RulePack, falling back to the TeamConfig default `"claude-haiku-4-5"` when the preset does not specify one.
