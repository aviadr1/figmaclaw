# Sync Observability (Checkpoint Pull Loop)

> **Canon cross-reference:** the refresh-trigger ladder that produces these `SYNC_OBS` events is canonized in [`figmaclaw-canon.md` §3](figmaclaw-canon.md#3-refresh-trigger-ladder). This document remains authoritative for the *event taxonomy and artifact format*.

This document defines the structured telemetry emitted by
`scripts/checkpoint_pull_loop.sh` in reusable sync workflow runs.
It also documents per-file pull telemetry emitted by `figmaclaw pull`.

## Goals

- Explain where sync time is spent (pull vs git checkpoint stages).
- Make progress visible during long runs.
- Preserve machine-readable artifacts for post-run profiling.

## Live log format

Every loop phase prints a single-line log prefix:

`SYNC_OBS event=<event> batch=<n> elapsed_s=<sec> max_pages=<n> pull_status=<code> committed=<bool|na> has_more=<bool> idle_has_more=<n> reason="<text>"`

This is intended for real-time log tailing while a run is active.

`figmaclaw pull` now emits per-run and per-file lines with prefix:

`SYNC_OBS_PULL event=<event> ...`

Key events:

- `run_start` / `run_end`
- `listing_prefilter` (when `--team-id` is used)
- `file_heartbeat` (every N seconds while a single file pull is still running; default 30s)
- `file_end` (one line per considered file with outcome + duration)

`file_end` outcomes include:

- `updated`: file processing produced page/component/schema writes
- `processed_no_writes`: file was processed but no page/component/schema writes occurred
- `pull_skipped`, `listing_prefilter_skip`, `manifest_skipped`, `no_access_pruned`, `error`

## Artifact files

Reusable workflow `sync.yml` uploads an artifact named:

`figmaclaw-sync-observability-<run_id>-<run_attempt>`

Containing:

- `checkpoint_events.csv` (per-event timeline)
- `checkpoint_summary.txt` (rollup counters and final reason)

## `checkpoint_events.csv` schema

Header:

`ts_utc,elapsed_s,batch,event,input_force,max_pages,pull_status,pull_duration_s,git_pull_s,git_add_s,git_diff_s,git_commit_s,git_push_s,committed,has_more,idle_has_more,reason`

Notes:

- `elapsed_s`: seconds from loop start.
- `pull_duration_s`: duration of `figmaclaw pull` batch call.
- `git_*_s`: stage durations for checkpoint operations.
- `event` values include:
  - `loop_start`, `batch_start`, `batch_end`
  - `batch_timeout_backoff`, `batch_timeout_stop`
  - `loop_break`, `loop_end`

## `checkpoint_summary.txt` keys

- `total_elapsed_s`
- `batches_started`
- `total_commits`
- `total_timeouts`
- `total_backoffs`
- `final_reason`
- `max_batches`
- `max_pages_per_batch`
- `batch_timeout_seconds`
- `input_force`

## Current limitation

GitHub artifact upload occurs after step completion (`if: always()`), so artifacts
are post-run. During execution, use `SYNC_OBS` lines in live logs.

## Correlating loop + pull telemetry

- `SYNC_OBS` gives checkpoint-batch level timing and git checkpoint stages.
- `SYNC_OBS_PULL` gives per-file timing/outcomes inside each `figmaclaw pull` call.

Together they isolate whether slowness is primarily:

- in pull internals (API/render/write),
- or in checkpoint git stages (pull/add/diff/commit/push),
- or in repeated timeout/backoff patterns.
