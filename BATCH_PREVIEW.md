# Library-wide batch previews

Turns per-click previewing (14–35s each, serialized) into a background job that
scales to 20k+ files. Built around four levers, highest impact first:

1. **Dedup + incremental cache** — one render per unique `sha256`; duplicates
   inherit it; a rescan only touches new/changed files. Turns a ~110-hour job
   into a one-time cost, then near-free.
2. **One long-lived MetaTrader session per shard** — a controller script loops
   the whole shard, so the terminal starts once instead of per file (~20s → a
   few seconds per item).
3. **Parallel shards** — N isolated portable runtimes work the queue at once.
4. **Bulk compile** — MetaEditor compiles a whole folder in one invocation.

## What's in this change

| File | Status | What it does |
|------|--------|--------------|
| `engine.py` | done, tested | adds `preview_status/preview_path/preview_hash/preview_error/preview_updated_at` columns + index |
| `preview_batch.py` | done; core tested on Linux, render path needs Windows validation | the batch engine (plan/run), dedup/cache, sharding, DB writeback, MT5/MT4 single-session renderers, embedded MQL controllers |
| `tauri.conf.json` | done | declares `binaries/mql-preview-batch` |
| `default.json` | done | shell execute/spawn permission for the sidecar |
| `build_windows.ps1` | done | PyInstaller build line for the sidecar |
| `lib.rs` | done; needs `cargo build` to compile-verify | `start_preview_batch` streaming command (mirrors `start_scan_engine`); `db_query` now returns preview columns |
| **frontend hooks** | **you apply** — see below | trigger after scan; show thumbnails; prioritize on-screen |

Tested here (Linux, no MetaTrader): migration is idempotent; the planner dedups
by content hash, splits by platform, orders favorites/recent first, skips cached
and known-bad, and re-renders on `--force`; writeback propagates a render to all
duplicate rows sharing the sha; sharding is balanced. The MetaTrader-facing render
(`render_shard`) reuses the proven `preview_bridge` helpers but can only be
validated on a Windows box with MT4/MT5 installed.

## CLI (for testing before wiring the UI)

```
mql-preview-batch plan --db <library.sqlite3> --out <appDataDir>
mql-preview-batch run  --db <library.sqlite3> --out <appDataDir> --shards 4
# optional: --limit N (cap this run), --force (ignore cache), --platform MT4|MT5
```

`<appDataDir>` is the same folder the app uses (`appDataDir()` →
`…\Roaming\com.mqlindicatorlibrary.app`). Images land in `…\previews\<sha>.png|.gif`,
which the asset-protocol scope already added to `tauri.conf.json` covers.

`plan` is read-only and instant — run it first to see `todo / cached /
skipped_known_bad` before committing to a `run`.

## Frontend hooks (apply in `main.js` / `preview_ui.js`)

**1. Kick the batch off after a scan finishes.** In the `scan-engine-done`
handler (main.js), once the scan committed successfully:

```js
import { appDataDir, join } from '@tauri-apps/api/path';
import { invoke } from '@tauri-apps/api/core';

async function startPreviewBatch() {
  const out = await appDataDir();
  await invoke('start_preview_batch', {
    args: ['run', '--db', await join(out, 'library.sqlite3'), '--out', out, '--shards', '4']
  });
}
```

Listen for progress (main.js, next to the other `listen(...)` calls):

```js
import { listen } from '@tauri-apps/api/event';
listen('preview-batch-line', e => {
  const line = e.payload?.line; if (!line) return;
  try {
    const m = JSON.parse(line);
    if (m.phase === 'shard_committed') refreshVisibleThumbnails(); // rows just got images
  } catch {}
});
```

**2. Show a thumbnail in each library row.** Rows from `db_query` now include
`preview_status` and `preview_path`. Where each row is rendered, add:

```js
import { convertFileSrc } from '@tauri-apps/api/core';
function thumbFor(row) {
  if (row.preview_status === 'ok' && row.preview_path) {
    return `<img class="thumb" src="${convertFileSrc(row.preview_path)}" loading="lazy" alt=""/>`;
  }
  if (row.preview_status === 'incompatible') return `<span class="thumb muted">no preview</span>`;
  if (row.preview_status === 'pending')      return `<span class="thumb muted">…</span>`;
  return `<span class="thumb muted">—</span>`;
}
```

**3. (v2, optional) Prioritize what's on screen.** When the user opens the
library or scrolls, call `start_preview_batch` with `--limit` and a filtered set
so visible rows jump the queue, then let the full run backfill. The engine already
orders favorites and most-recent first, so even without this, useful items render
early.

## Build

`build_windows.ps1` now builds `mql-preview-batch` via PyInstaller. Because
`preview_batch.py` imports `preview_bridge.py`, both must sit in the folder passed
to PyInstaller (`--paths` is set for that). Adjust the source path to match your
build layout. **Note:** the pre-existing `mql-preview` and `mql-preview-control`
sidecars are declared in `tauri.conf.json` but were never built by this script —
they need their own build lines too, following the same pattern.

## Performance envelope (20k files)

- **First full pass:** with dedup + one-session-per-shard + ~6 shards, low
  single-digit hours (vs 110+ serialized today). Tune `--shards` to cores/RAM —
  each shard is a full MetaTrader process, so 4–8 is typical; too many will thrash.
- **Every pass after:** minutes — only new/changed uniques are rendered.
- Set `--limit` for a quick first slice (e.g. favorites + newest 500) so the UI
  fills fast, then run unlimited in the background.

## Known limits / validation checklist

- Many indicators won't produce an image — won't compile, or draw nothing on a
  generic EURUSD/H1 chart. The engine records this per item (`incompatible` /
  `blank` / `failed`) and never retries a known-bad hash. Expect a real
  "no preview" bucket in the UI; that's correct behavior, not a failure.
- MT4 rendering opens a **child** chart per indicator and templates *that* chart
  (never the controller's own, which would unload it). Validate on Windows that
  `ChartApplyTemplate` reliably loads each indicator before the screenshot; if
  some come up blank, raise the settle loop / `Sleep` in `MT4_BATCH_SRC`.
- MT5 uses `ChartIndicatorAdd`/`ChartScreenShot` in one session.
- `render_shard` uses the same `clone_runtime`, history-copy and `prime` helpers
  as the single-preview path, so terminal discovery/pairing behave identically.
- Parallel shards are RAM/CPU heavy and MetaTrader automation is flaky — the
  batch controller writes results incrementally and the Python side has a
  per-item timeout and full process-tree teardown, so one stuck item can't stall
  the shard.
- Consider migrating the single-click preview to the same `sha256`-based filename
  so click and batch share one cache (today single previews are named from the
  file path; harmless, just not shared).
