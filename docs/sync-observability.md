# Sync Observability (Checkpoint Pull Loop)

This document defines the structured telemetry emitted by
`scripts/checkpoint_pull_loop.sh` in reusable sync workflow runs.

## Goals

- Explain where sync time is spent (pull vs git checkpoint stages).
- Make progress visible during long runs.
- Preserve machine-readable artifacts for post-run profiling.

## Live log format

Every loop phase prints a single-line log prefix:

`SYNC_OBS event=<event> batch=<n> elapsed_s=<sec> max_pages=<n> pull_status=<code> committed=<bool|na> has_more=<bool> idle_has_more=<n> reason="<text>"`

This is intended for real-time log tailing while a run is active.

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
