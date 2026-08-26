# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- 2026-08-25: the viewer tells the model what it is looking at and what it can
  be told to do. `/chat/agui` reads the AG-UI `state` field, whose `viewer` half
  is the viewer's own snapshot and whose `actions` half is the catalogue of
  named actions it can run, and `src/api/viewer_state.py` renders both into the
  run's system prompt. `viewer_control` gains `run`, which carries a catalogue
  name and its arguments back as
  `{"action": "run", "params": {"name": ..., "args": {...}}}`, and accepts
  `args` written as JSON text because small local models send it that way. A
  request with no catalogue sends the persona unchanged. `evals/viewer/` holds
  40 golden tasks over a fixture snapshot and catalogue, scored by
  `python -m evals.viewer_runner`.
- 2026-08-25: a chat question reaches the map it was asked from. `/chat/agui`
  binds the `X-Agora-Document` header for the length of the stream and the run
  request sends the bound document to sibyl, which puts it back on every tool
  call of that run. Without the header the body carries no `document` and the
  run reads no map.
- 2026-08-25: `asset_readings`, a tool that answers what the sensors on a live
  map are reporting. It reads agora's `GET /documents/{id}/assets` as the caller,
  or `/assets/at?t=` for a moment in the past, and filters by reading kind, one
  asset, a value above or below a threshold, or the assets that have gone
  offline. The answer carries each asset's latest value per kind, how many assets
  the map has, how many are offline, and a summary line, capped at 200 assets.
  Agora refusing the read comes back as the tool's error text rather than an
  exception. The tool exchanges for an `agora:read` token, which agora requires
  on every GET a tool token makes.
- 2026-08-25: `X-Agora-Document` also names the map a tool call reads. The
  document id travels in a context variable beside the caller's bearer, set by
  `POST /tools/{name}`, the MCP tool call and the isolated executor, so a tool
  can ask which map it was called from. Only a document id binds: agora answers
  its asset routes to members, and a share link guest is not one.
- 2026-08-25: the caller's bearer reaches sibyl on every session call. sibyl now
  decides session ownership from that token, so `sibyl_request` sends the
  `Authorization` header it is given and the five `/sessions` routes pass the
  caller's own along. `notify_agent` takes the token too and its three callers,
  the `notify` branch of `POST /tools/{name}`, `/upload` and `/draw`, hand theirs
  over, so a `thread_id` naming someone else's session appends nothing to it.
  Before this, any platform-authenticated caller could list, rename, delete or
  write text into another user's session, and appended text landed in that
  session's model context.
- 2026-08-24: output files are deleted by age. `src/api/outputs_retention.py`
  walks every caller directory under the outputs root once at API startup and
  once a day after that, deletes the regular files last written more than
  `GEOLANG_OUTPUTS_RETENTION_DAYS` ago, default 30, and removes a caller
  directory the pass emptied. A symlink is neither followed nor deleted, and a
  caller directory that is itself a symlink is skipped. `0` keeps everything.
  Each pass logs the files removed and the bytes freed, and a delete that fails
  is logged and skipped. The task starts in the API server's lifespan and
  nowhere else: the executor mounts the same volume, and one deleter is enough.
  `DELETE /outputs/{filename}` only ever reached the caller's own directory, and
  a file no tool announced was deleted by nothing.

### Fixed
- 2026-08-25: a token carrying agora's `agora_use` claim is refused wherever a
  platform token is required. `platform_token_error` rejected only `token_use`,
  so agora's long lived sensor feed token, signed with the same
  `PLATFORM_JWT_SECRET` and carrying no `role`, passed `require_platform_token`
  and opened every route behind `platform_auth` as the feed's uuid.
  `source_token_role` and `exchange_tool_token` reject it too, so a tool that
  needs no downstream scope cannot mint a tool token for a feed.
- 2026-08-25: `emit_ui_spec` called with no `layers` and nothing written in the
  last 15 minutes returns a map spec with an empty layer list, prefixed by a
  note saying the map has no data and what to do about it, instead of an error.
  grok omits the argument, and the error made it repeat the same call until
  sibyl aborted the run after three identical failures, so the user got
  RUN_ERROR and no map at all. A `layers` string that parses to no usable entry
  is still an error.
- 2026-08-24: a note about an upload, a drawn shape or a viewer-run tool goes
  only to the sibyl session the caller names (`thread_id` on `POST /upload`,
  `POST /draw` and `POST /tools/{name}`), and to nowhere without one. It used
  to go to sibyl's process-wide active session, so the tool sweep's fixture
  uploads under their own token put 351 "Filename for tools:
  sweep_polygons.geojson" notes into a person's chat session, whose model then
  named a file it could not reach five times in one run. The viewer sends its
  chat session's id on `run_workflow`. Sessions still carry no owner, recorded
  in viewtopia's DESIGN_TODO.
- 2026-08-24: `buffer_clip_dissolve` buffers in an azimuthal equidistant
  projection centred on the point instead of EPSG:3857, so `buffer_km` is the
  distance on the ground. At Athens a 3 km buffer came out 2.4 km wide.
- 2026-08-24: `geopandas_api` no longer advertises `buffer` and `to_file`.
  Both resolved through `getattr(geopandas, name)`, which has neither, so
  every call failed with "GeoPandas has no 'buffer'". The `distance` and
  `driver` arguments that only they read are gone with them, and a test keeps
  every advertised name backed by a branch or a real geopandas function.
- 2026-08-24: `geopandas_api` names the file it wrote the way every other tool
  does, `Saved to outputs/<name>` instead of an absolute path. The sweep's
  cleanup and any client reading announcements missed its files.

### Changed
- 2026-08-24: `buffer_clip_dissolve` takes no `input_path` to mean "save the
  buffer polygon itself": one EPSG:4326 polygon with `center_lon`,
  `center_lat` and `buffer_km` as properties. Before this no tool could answer
  "show me Athens with a 3 km buffer": the model clipped its own geocoded
  point to the circle and presented that point as the buffer. The success text
  now says `Saved to outputs/<name>` rather than the absolute container path,
  which the model had failed to hand on to `emit_ui_spec`.

### Added
- 2026-08-24: `run_workflow` refuses a plan the user never approved. The viewer's
  approve button posts the plan's own manifest to `POST /workflow/approve`
  before it posts the run, that marks the plan record in
  `src/core/planned_manifests.py` approved, and a manifest without one is
  refused with an error telling the model to ask the user rather than retry.
  Approving text nobody planned records nothing, planning again drops the
  earlier approval, and an approval expires with its plan. The route dispatches
  `approve_workflow`, a tool module kept off `GET /tools`, off
  `POST /tools/{name}` and off `/mcp` by `TOOL_APPROVAL_ROUTE_ONLY`, so the
  record of a person pressing a button is not something a model can ask for; it
  is a tool module at all because the record has to land in the process that
  runs `plan_workflow` and `run_workflow`, which is the executor when one is
  configured. `run_workflow` declares `TOOL_NEEDS_USER_APPROVAL`, which drops it
  from `/mcp` the way `TOOL_RUNS_CALLER_CODE` drops `sql_query`: an agent
  reaching that endpoint has no viewer to approve in, so it could never get
  past the gate. The nightly sweep presses approve for its `run_workflow` entry
  through the same route the button posts to.

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
