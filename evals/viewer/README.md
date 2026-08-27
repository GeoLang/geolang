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
