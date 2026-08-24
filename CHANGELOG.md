# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- 2026-08-24: the QGIS tools work more than once per process. Each of
  `check_qgis_status`, `list_qgis_algorithms`, `run_qgis_algorithm` and
  `pyqgis_api` built its own `QgsApplication` and, for two of them, called
  `exitQgis()` after; a second `QgsApplication` in the same process dies with
  SIGSEGV, so whichever QGIS tool ran second killed the executor and every tool
  called after that reported it unreachable. `src/core/qgis_session.py` now owns
  one session per process: it bridges the system QGIS python paths onto
  `sys.path`, builds the application on first use, adds `QgsNativeAlgorithms`,
  initialises the processing plugin, and never calls `exitQgis`. A failed start
  is remembered and re-raised rather than retried, because the retry is the
  crash. `pyqgis_api` also gained the paths, so it reaches QGIS in the platform
  image at all, and returns every failure behind the ❌ marker instead of a
  plain string the sweep read as a pass. The four tools lost their
  `crashes_executor` marks in `tool_sweep/arguments.py` and are now `offline`,
  so the per-push suite runs them wherever the QGIS bindings are importable and
  skips them where they are not.

### Added
- 2026-08-24: every advertised tool is run through the real HTTP path by a
  nightly sweep. `python -m tool_sweep.runner` reads `GET /tools`, uploads the
  two sample layers the path-taking tools name, then posts `POST /tools/{name}`
  once per manifest entry with the arguments in `tool_sweep/arguments.py`. Each
  tool's name, outcome, duration and truncated message is appended to a JSONL
  file as that tool finishes, so a killed run still says where it died, and the
  run exits nonzero if anything failed. A manifest tool with no entry in the
  table is one of those failures, so a new tool cannot ship unswept. Tools that
  can reach a third party are marked and listed apart from broken ones in the
  summary. `--only` pulls in whatever an entry names in `after`, which is how
  `run_workflow` gets its `plan_workflow`. `--skip-external` and
  `--skip-crashing` narrow a run to what is deterministic.
  `tests/test_tool_sweep.py` runs the 16 offline entries of the same table
  through the in-process app on every push, and checks the tool count and
  catalogue in `README.md` and `docs/api_reference.md` against the loaded
  manifest. viewtopia's `platform-sweep.yml` runs the full sweep nightly.
- 2026-08-24: `run_workflow` refuses a manifest `plan_workflow` never validated,
  where before the plan-then-run order was only asked for in the persona. A
  successful plan records the sha256 of the confined manifest text under the
  caller's own directory name, and a run whose text has no record answers with
  an instruction to call `plan_workflow` first instead of reaching geodukt. An
  edited manifest, another caller's plan, and a plan from before a restart are
  all refused. A record is kept for an hour, 32 per caller, so retrying the
  approved pipeline does not need a re-plan. `src/core/planned_manifests.py`
  holds the store, in core because the tool loader imports and reloads tool
  modules under a second package name.
