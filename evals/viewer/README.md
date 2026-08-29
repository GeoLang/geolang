# Viewer eval fixtures

`snapshot.json` and `catalogue.json` are a copy of the two halves of the AG-UI
`state` the viewer sends with every chat message. Refresh them from viewtopia
rather than editing them here:

```
UPDATE_VIEWER_SNAPSHOT=1 npx vitest run tests/unit/viewer-snapshot.test.ts
UPDATE_ACTION_CATALOGUE=1 npx vitest run tests/unit/actions-catalogue.test.ts
cp tests/unit/fixtures/viewer-snapshot.json ../geolang/evals/viewer/snapshot.json
cp tests/unit/fixtures/action-catalogue.json ../geolang/evals/viewer/catalogue.json
```

Run the eval with `python -m evals.viewer_runner --repeat 3`. One run is a poor
estimate of a score.

`--record recordings/name.json` writes the tool calls each task drew, along with
the pass or fail each one earned. `--replay recordings/name.json` scores those
calls against the current tasks and snapshot without asking a model, so it needs
neither sibyl nor the network and prints the same report a live run prints.
`tests/test_viewer_replay.py` replays `recordings/grok-2026-08-29.json` and fails
when any recorded score moves, which is what makes the eval a CI gate.
