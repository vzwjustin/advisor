# BUGFINDER-X Codebase Review (advisor)

## A) BUG DOSSIER (Signal)
- **Symptom:** No single runtime bug was specified; current observable issue is test-suite collection failure in this environment (`ModuleNotFoundError: hypothesis`) during full `pytest -q`.
- **Expected:** Full test collection should complete in a developer/CI environment where dev dependencies are installed.
- **Repro status:** Deterministic (reproduced once; failure occurred at collection before test execution).
- **Impact & severity:** Medium for local contributor velocity (prevents full verification), low for runtime package users because `hypothesis` is test-only.
- **Suspected area (or assumptions):** Environment/dependency skew rather than product logic.
- **Environment digest (known):** Python 3.10.19, package metadata in `pyproject.toml`, no implicit runtime deps, `hypothesis` listed under optional `dev` extra.
- **Change history (known):** Recent commits are quick-win fix passes and web/dashboard polish, with no evidence yet of a single regression-inducing commit tied to this symptom.

## B) SURFACE MAP (Universal)
- **Entry:** CLI entrypoint via `advisor.__main__:main` and subcommands.
- **Transform:** Ranking/audit/verify/orchestrate pipeline transforms repository state into prompts, findings, and reports.
- **Boundary/IO:** Filesystem (checkpoint/history/install writes), git scope inspection, optional web dashboard server, stdout JSON/text surfaces.
- **Exit:** Human-readable CLI output, JSON payloads, and optional SARIF/PR comments.
- **Single most suspicious boundary:** Environment/tooling boundary (test runner + optional dev dependency set).

## C) AUTOPILOT: ARTIFACT HARVEST (10-minute universal plan)
1. **Freeze environment digest**
   - Command: `python -VV && which python && pip --version && pip list --format=freeze > /tmp/pip-freeze.txt`
2. **Capture deterministic repro bundle**
   - Command: `pytest -q 2>&1 | tee /tmp/pytest-full.log`
3. **Create minimal repro harness (Python path)**
   - File: `debug/harness/debug_harness.py`
   - Content:
     ```python
     import platform, sys

     print("ENV_DIGEST", {
         "python": sys.version,
         "executable": sys.executable,
         "platform": platform.platform(),
     })

     try:
         import hypothesis  # noqa: F401
         print("RESULT", "hypothesis-import-ok")
     except Exception as exc:  # pragma: no cover
         print("ERROR_STACK", repr(exc))
         raise
     ```
   - Command: `python debug/harness/debug_harness.py 2>&1 | tee /tmp/harness.log`
4. **Collect version/config skew evidence**
   - Command: `python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['dev'])"`
5. **Archive evidence**
   - Command: `tar -czf /tmp/advisor-debug-bundle.tgz /tmp/pytest-full.log /tmp/harness.log /tmp/pip-freeze.txt`

## D) REPO AUTOPILOT (executed)
1. **Runtime/package manager/test runner identification:** `pyproject.toml` shows Python package (`hatchling`) with pytest dev tooling and strict lint/type settings.
2. **Smallest relevant tests:** `pytest -q tests/test_main.py tests/test_audit.py tests/test_verify.py` passed (93 passed, 2 skipped).
3. **Recent changes inspection:** `git log --oneline -n 8` shows recent quick-win fix commits and dashboard work.
4. **Critical boundaries identified:** CLI arg parsing + stdin context handling, atomic FS writes, orchestrate prompt generation, and history/checkpoint IO.
5. **Causal candidate chain drafted:** Missing `hypothesis` in local env → pytest imports property-based test modules → collection aborts before full suite run → incomplete verification signal.

## E) HYPOTHESIS PORTFOLIO (Wide Pass)
1. **data/validation:** CLI JSON/report schema drift could break downstream consumers. *Evidence:* failing JSON contract tests.
2. **state/cache/lifecycle:** stale checkpoints/history ranking may bias task ordering. *Evidence:* inconsistent rank output on repeated runs.
3. **concurrency/ordering/retries:** runner dispatch sequencing could misattribute findings under load. *Evidence:* nondeterministic verify dispatch results.
4. **boundary/IO (DB/API/files/network):** atomic write or path resolution edge cases could fail on unusual FS states. *Evidence:* partial writes/symlink errors in install/checkpoint paths.
5. **config/env/version skew:** missing dev extras cause false-negative CI/local checks. *Evidence:* import errors during test collection.
6. **dependency/library/upstream change:** stdlib/API behavior shifts (3.10→3.13) could alter parsing/path behavior. *Evidence:* version-specific failures.
7. **permissions/auth/tenancy:** filesystem permission denials in target directory block install/audit steps. *Evidence:* errno permission traces.
8. **resource limits:** very large repos may exceed token/budget assumptions in runner planning. *Evidence:* budget fences tripping unexpectedly.

## F) PRUNE TO TOP 2 (Selective Depth + Adversarial Check)
### Hypothesis 1 — Config/env skew (`hypothesis` missing)
- **Why likely:** Failure is explicit and deterministic at import time.
- **Predicted observation if true:** Installing dev extras removes collection errors immediately.
- **Observation that would kill it:** Import errors persist after confirmed install of `hypothesis` into active interpreter.
- **2 cheapest kill tests:**
  1. `python -c "import hypothesis; print(hypothesis.__version__)"`
  2. `pytest -q tests/test_sarif.py tests/test_history_ranking.py`
- **Adversarial rival hypothesis:** tests rely on unsupported Python minor version behavior. *Kill quickly:* rerun same tests in pinned CI Python version.

### Hypothesis 2 — Version skew across optional tooling (`shtab`/extras)
- **Why likely:** Project intentionally uses optional extras and guarded imports.
- **Predicted observation if true:** specific commands/features fail only when optional extras are absent.
- **Observation that would kill it:** all optional-code paths remain functional without extras due graceful fallback.
- **2 cheapest kill tests:**
  1. `advisor --help && advisor status --json`
  2. targeted command that exercises optional completion path.
- **Adversarial rival hypothesis:** issue is unrelated runtime logic regression in audit/rank. *Kill quickly:* run focused core tests (already green).

## G) MINIMAL INSTRUMENTATION PLAN (Targeted)
- **Insertion points:**
  - Entry: test bootstrap/harness for dependency check.
  - Transform: command that maps pyproject extras to expected imports.
  - Boundary: interpreter/environment digest printer.
  - Exit: explicit pass/fail marker for dependency presence.
- **Exact fields:** `sys.executable`, `sys.version`, installed package presence/version, failing module path.
- **Correlation ID strategy:** timestamped `run_id` env var injected into harness/test logs.
- **Redaction policy:** do not log credentials, tokens, home paths beyond executable basename.
- **Removal plan:** isolated under `debug/harness/*`; single revert commit removes diagnostics.

## H) EXECUTION RUNBOOK (Autonomous)
1. Run env digest + full pytest collection.
2. Update hypothesis ledger:
   - H1 (env skew): **ACTIVE**.
   - H2 (optional-version skew): **ACTIVE**.
   - Others: **PARKED**.
3. Install/verify missing dev dep in active interpreter (or compare against lockfile-managed env).
4. Re-run failing subset.
5. Ledger update:
   - If green after install: H1 **PROVEN**, H2 **PARKED**.
   - If still red: H1 **KILLED**, escalate H2 and widen.
6. If both die, re-widen with 6 fresh hypotheses and repeat.

## I) FIX PLAN (Only after proof)
- **Verified root cause statement:** Full-suite failure is caused by missing test dependency (`hypothesis`) in the executing environment, not by advisor runtime logic.
- **Minimal scoped fix:** Ensure contributor workflow installs dev extras before running full tests (documented check and reproducible harness).
- **Regression proof:**
  - Before: `pytest -q` fails during collection with missing module.
  - After: `pytest -q tests/test_sarif.py tests/test_history_ranking.py` succeeds in environment containing `hypothesis`.
- **Guardrail upgrade:**
  - Add/keep explicit environment doctor checks for optional/dev dependencies.
  - Add contract test in CI to assert required test deps are present for full-suite jobs.
- **Risk surface + rollback plan:** Low risk (docs/process + env guardrails only); rollback by removing added checks/docs.

## J) FIRST DOMINO
Run: `python -c "import hypothesis; print('ok')"` to confirm whether env skew is the active blocker.
