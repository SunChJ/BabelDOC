# Gloss service boundary

`gloss-babeldoc serve` exposes the downstream runtime as an authenticated,
loopback-only HTTP service. It is the private integration boundary for the
Gloss desktop application, not a general network API.

## Bootstrap and ownership

Gloss creates a new private workroot and bearer token for every service
instance. Before launch:

- the workroot must be an existing `0700` directory owned by the current user;
- `<workroot>/.executor-workroot-ready` must be a regular `0600` file owned by
  the current user;
- the token file must be a regular `0600` file owned by the current user and
  contain 32–256 URL-safe ASCII characters;
- the workroot is canonicalized before use; `/`, the user's home directory,
  marker/token symlinks, and group/world-accessible paths are rejected.

The production runner requires both `--work-dir` and `--token-file`:

```text
gloss-babeldoc serve \
  --work-dir /private/path/to/runtime \
  --token-file /private/path/to/runtime/service.token \
  --parent-pid 1234 \
  --parent-start-time 1784749200.125
```

`parent-start-time` is the process creation time in Unix epoch seconds, as
reported by `psutil.Process(pid).create_time()`. The service validates the
PID/start-time pair before announcing readiness and monitors it afterward to
prevent PID-reuse mistakes.

The service binds only to `127.0.0.1`; the default port is `0`, so the kernel
selects a free port. It writes exactly one ready record to stdout, prefixed by
`__GLOSS_BABELDOC_SERVICE_READY__`. Logs go to stderr. A ready payload contains
the schema and protocol versions, instance ID, PID and process start time,
parent identity, runner, and endpoint. Production ready records never contain
the bearer token. A generated token is available only when the explicitly
enabled fake test runner is used.

All HTTP requests, including health checks, use:

```text
Authorization: Bearer <per-instance-token>
```

Gloss must authenticate `GET /v1/runtime` and compare the returned
`instance_id`, PID, and process start time before reusing or terminating a
remembered process. A process must never be killed merely because it occupies
a remembered port.

## HTTP endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Liveness, identity, and workroot write probe. |
| `GET` | `/v1/runtime` | Runtime version, capabilities, and service identity. |
| `GET` | `/v1/executions/current` | The active execution, or `null`. |
| `GET` | `/v1/executions/latest` | The most recently created execution, including terminal executions. |
| `GET` | `/v1/executions/{id}` | One retained execution snapshot. |
| `GET` | `/v1/executions/{id}/events?after_sequence=N` | Replay and follow UTF-8 NDJSON events. |
| `POST` | `/v1/executions` | Idempotently start one PDF execution. |
| `POST` | `/v1/executions/{id}/cancel` | Idempotently cancel that execution only. |
| `POST` | `/v1/shutdown` | Stop admission, optionally cancel active work, and exit. |
| `POST` | `/v1/pdf/watermark1` | Apply the retained single-asset watermark transform. |
| `POST` | `/v1/pdf/watermark2` | Apply the retained two-asset watermark transform. |

The former global `/v1/abort` route is intentionally not part of this
protocol: a delayed client must never cancel a newer execution.

Requests are JSON objects no larger than 1 MiB. Chunked request bodies are not
accepted. A task ID is 1–128 URL-safe identifier characters. Reusing a task ID
with the same canonical request returns the existing execution; reusing it
with different data returns `409 idempotency_conflict`.

## Execution request

Version 1 accepts one input PDF per execution. Paths may be workroot-relative
or absolute paths already contained by the private workroot; they may not
escape it. Returned paths are always workroot-relative. The complete request
shape is:

```json
{
  "task_id": "pdf-018f0d8f",
  "paths": {
    "input_file": "inputs/paper.pdf",
    "output_dir": "outputs/pdf-018f0d8f",
    "working_dir": "working/pdf-018f0d8f"
  },
  "translation_config": {
    "debug": false,
    "lang_in": "en",
    "lang_out": "zh-CN",
    "pages": null,
    "no_dual": true,
    "no_mono": false,
    "skip_clean": false,
    "dual_translate_first": false,
    "disable_rich_text_translate": true,
    "use_side_by_side_dual": false,
    "use_alternating_pages_dual": false,
    "skip_scanned_detection": true,
    "ocr_workaround": false,
    "custom_system_prompt": null,
    "primary_font_family": null,
    "auto_extract_glossary": false,
    "auto_enable_ocr_workaround": false,
    "only_include_translated_page": false,
    "merge_alternating_line_numbers": true,
    "remove_non_formula_lines": false
  },
  "runtime_limits": {
    "qps": 4,
    "report_interval_seconds": 0.5,
    "max_pages_per_part": 50,
    "pool_max_workers": 4,
    "term_pool_max_workers": 4
  },
  "gateways": {
    "main_llm": {
      "model": "gloss-provider",
      "base_url": "http://127.0.0.1:49160/v1",
      "api_key": "per-task-bridge-token"
    },
    "ate_llm": {
      "model": "gloss-provider",
      "base_url": "http://127.0.0.1:49160/v1",
      "api_key": "per-task-bridge-token"
    },
    "layout": {
      "adapter": "rpc_doclayout8",
      "base_url": "http://127.0.0.1:49161",
      "requires_line_extraction": false
    }
  },
  "assets": {
    "glossaries": [],
    "layout_ir_cache": {
      "enabled": true
    }
  },
  "metadata": {
    "metadata_extra_data": null
  }
}
```

Gateway credentials are held only for the active worker and are cleared from
the retained execution record after the worker exits. They are never included
in snapshots or events. `qps`, `max_pages_per_part`, `pool_max_workers`, and
`term_pool_max_workers` must be positive integers (JSON booleans are not
integers). `report_interval_seconds` must be a positive number. `no_dual` and
`no_mono` cannot both be true.

`assets.layout_ir_cache.enabled` is optional and defaults to `false` for
protocol compatibility. When enabled, the runtime chooses a private cache
directory and derives the cache key itself. It is eligible only for
non-debug, unsplit, full-document executions that skip scanned-document
detection. No client-controlled cache path or key is accepted.

## State, events, and output validation

Version 1 deliberately permits one active PDF execution per service instance.
Gloss owns the visible multi-file queue and submits the next file only after
the current worker has finished cleanup. Each PDF runs in an isolated process.

```text
running -> succeeded
        -> failed
        -> cancelling -> cancelled
```

Snapshots contain `execution_id`, `task_id`, `status`, `initial_sequence`,
`first_available_sequence`, `last_sequence`, `worker_finished`, `created_at`,
and optional `finished_at`. Times are Unix epoch seconds.

Normal event lines have this envelope:

```json
{
  "schema_version": 1,
  "service_id": "gloss-babeldoc",
  "instance_id": "6a4e...",
  "type": "progress",
  "execution_id": "7c8a...",
  "sequence": 812345,
  "emitted_at": 1784749212.5,
  "payload": {
    "type": "progress_update",
    "stage": "Translate Paragraphs",
    "stage_current": 8,
    "stage_total": 20,
    "overall_progress": 42,
    "part_index": 1,
    "total_parts": 1,
    "performance": {
      "schema_version": 1,
      "phase": "translating",
      "elapsed_milliseconds": 12401,
      "phase_timings_milliseconds": {
        "launching": 320,
        "parsing": 3100,
        "translating": 8981,
        "typesetting": 0,
        "saving": 0,
        "finalizing": 0
      },
      "layout_ir_cache_status": "hit"
    }
  }
}
```

Terminal types are exactly one of `result`, `error`, or `cancelled`. A result
contains workroot-relative PDF paths plus metrics. Before success, every
reported PDF is checked against that execution's `paths.output_dir` for
containment, regular-file type, extension, PDF header, at least one page, and
successful first-page loading with MuPDF. The requested mono and/or dual
outputs must be present. Auto-extracted glossary output is likewise confined
to the execution output directory.

The terminal result contains a final top-level `performance` object with
`phase` set to `completed`. Phase durations use a monotonic clock and
accumulate when split parts revisit a phase. Cache status is one of
`disabled`, `ineligible`, `miss`, `hit`, `stored`, `invalidated`,
`input_changed`, `unsafe_directory`, `read_error`, or `write_error`.

A cancelled payload is stable across cooperative and forced cancellation:

```json
{
  "reason": "client_request",
  "code": "cancelled",
  "message": "execution cancelled",
  "message_for_user": null,
  "details": {}
}
```

`reason` may also be `service_shutdown` or `parent_exit`.

## Reconnection

Gloss stores `instance_id`, `execution_id`, and the last consumed sequence.
Disconnecting an event stream does not cancel its execution. For the same
instance, reconnect with `after_sequence=<last-consumed-sequence>`.

While an execution is active and no normal events arrive, the stream emits a
`heartbeat` control record at least once every five seconds. It carries the
schema, service, instance, and execution identity, with `sequence: null` and an
empty payload. Heartbeats are not retained or replayed and do not advance the
client's sequence cursor.

The service retains the 16 most recent executions and up to 1,000 events per
execution. An already-trimmed cursor returns `410 replay_gap` with a snapshot;
resume from `first_available_sequence - 1`. A cursor ahead of
`last_sequence` returns `409 cursor_ahead`, which normally indicates a stale
instance/cursor pairing.

If the event window changes after a `200` stream has started, the final NDJSON
line is a `stream_error` control record. Control records use `sequence: null`
and must be handled before normal sequence de-duplication. Its payload contains
the last consumed sequence and, when the execution is still retained, its
authoritative snapshot. The snapshot is `null` if retention evicted the
execution while the stream was attached.

When the instance ID changes, do not send an old cancellation request to the
new service. Mark the old running task interrupted and let an explicit user
retry create a new task ID.

## Cancellation and shutdown

Task cancellation first asks BabelDOC to stop cooperatively, then escalates to
process-group `SIGTERM` and `SIGKILL` after bounded grace periods. A cancelling
task holds the single-worker slot until the worker and descendants have exited.

`POST /v1/shutdown` accepts `{"cancel_active": true}` by default. Admission is
closed atomically before the response is sent. `false` drains the current task;
a later shutdown request with `true` escalates that drain to cancellation.
SIGINT, SIGTERM, and parent disappearance use the same controlled path.

## Compatibility boundary

The protocol is advertised by `gloss-babeldoc runtime-info` as
`executor.http.v1` and `executor.events.ndjson.v1`. Performance support is
advertised independently as `font-assets.memory-cache.v1`,
`layout-ir-cache.v1`, and `performance.telemetry.v1`.

The same executable provides the persistent layout gateway used by
`rpc_doclayout8`:

```text
gloss-babeldoc layout-serve \
  --host 127.0.0.1 \
  --port 0 \
  --parent-pid 1234
```

It reports `__GLOSS_BABELDOC_LAYOUT_READY__<port>` only after the ONNX model is
ready, exposes `GET /healthz` and `POST /inference`, and exits when its owning
Gloss process disappears. This is advertised as
`layout.rpc-doclayout8.v1`, allowing the self-contained runtime to operate
without a system Python installation. The ordinary `babeldoc` CLI remains
available for debugging and upstream compatibility.
