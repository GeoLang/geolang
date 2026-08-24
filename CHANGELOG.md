# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
