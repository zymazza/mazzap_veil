# New-twin setup shell

The `npm run init` setup page.

```
public/init.html      setup shell (stepper IA, live scan feed, build manifest)
public/init.css        its design system
public/init-shell.js   presentation-only enhancement (stepper/pill/manifest/feed)
public/init.js         shared engine: map drawing + scan/build orchestration
```

`scripts/init.js` serves `/init.html`.

The shell **reuses `/init.js`** — every element id the engine binds to (`map`,
the `address-search-*` group, `twin-name`, `point-count`, `area-label`,
`undo-point`, `clear-aoi`, `set-aoi`, `status-label`, `viewer-link`, `log`, and
the whole `layer-dialog` group) is preserved. `init-shell.js` adds nothing to
the build logic; it only reflects the state `init.js` already publishes (status
text, the log stream, the point count, the dialog's open state, and the
per-layer `veil-scan` events) into a 3-step progress rail, a status pill, an
animated build manifest, a live scan feed, and a self-dismissing on-map hint.

What it does, visually:
- Linear **Outline → Layers → Build** stepper instead of one flat column.
- An address search box geocodes U.S. street addresses through
  `GET /api/init-address-search?q=...` and jumps the Leaflet map to the chosen
  match before the user outlines the AOI.
- A **live scan feed**: each national layer streams in as the probe resolves it,
  with a progress bar, an `N/total` counter, and a per-layer badge (intersects +
  feature count / no features / manual source / error). See the streaming note.
- The "what gets fetched" list is a **build manifest** whose rows tick off as
  their data appears in the log (spinner on the row in flight), plus a "current
  step" caption and an elapsed timer so a long build always feels alive.
- A status **pill** (idle / building / done / failed) replaces the bare label.
- An on-map hint tells you to start drawing, and disappears after the first point.
- Restyled optional-layers dialog, badged as "Step 2".

## Streaming scan

The optional-layer scan used to be one blocking request that returned every
layer at once. It now streams per layer end-to-end:

- `scripts/fetch_national_layers.py probe --progress` emits NDJSON
  (`start` → one `layer` event each as its future resolves → `done`/`error`).
- `server.js` adds `POST /api/init-layer-scan-stream`, which spawns that probe
  and pipes its NDJSON straight to the client (chunked). The old buffered
  `/api/init-layer-scan` stays as the fallback.
- `public/init.js`'s `scanOptionalLayers` reads the stream, dispatches a `window`
  `veil-scan` CustomEvent per layer, and resolves with the same `{ ok, layers }`
  payload as before (so `renderLayerDialog` is unchanged). On any stream failure
  it falls back to the buffered endpoint.

`init-shell.js` is the consumer of the `veil-scan` events (it renders the live
feed).
