# Client behavior and result semantics

## Authentication and authorization

Every integration request has `Authorization: Bearer <service-key>` and `X-User-ID: <your-backend-authenticated-user>`. The key determines the tenant; your trusted backend supplies the user. Never accept an arbitrary browser-provided user ID without authentication. Sources, collections and schemas are tenant-shared configuration; records, events, profiles, results, memory and feedback are tenant/user scoped. Use neutral configuration names. The [route index](routes.md) shows primary scopes; runs additionally require `ai:run`, access to the selected data, and any enabled personalization grants. Reads of personalized results require the memory/feedback grants actually used. Source grants remain enforced on every access.

## Delivery and pagination

Snapshot identity is tenant + user + source + collection + external ID. Object versions are monotonically increasing positive integers. Replay the exact accepted envelope to receive a duplicate acknowledgement. Different content at the same identity/version conflicts; a lower version is stale. Independent events need a stable event ID or the supported source sequence identity. Do not generate a new event ID on retry. Registry/profile creation is not replay-safe: after an ambiguous response, list/get and compare before attempting another create. An HTTP `Idempotency-Key` header is not a general registry feature.

Run admission takes **body** `idempotency_key`, profile ID and explicit `input.version`. Preserve the complete body on retry; a changed body under the same key conflicts. `mode=async` returns 202 with a RunView; `mode=sync` can return 200 terminal or 202 still pending/running. Follow status rather than assuming completion from the HTTP code. Successful durable results are persisted and read by `result_id`. Inline data is only supported by the bounded synchronous profile-run route, with ephemeral output; it cannot become a background job.

Pages use `items` and `next_cursor`, limit 1–100. Follow even an empty page if `next_cursor` is non-null; stop only at null. Cursors are exclusive UUIDs, not time offsets or a consistent snapshot under inserts. Keep the same filters and ownership on continuation; restart traversal to reconcile concurrent additions. See the [client implementation](examples/client.py) for cursor loop detection.

Single delivery success can look like this (IDs and timestamps vary):

```json
{"status":"accepted","delivery_id":"00000000-0000-4000-8000-000000000001","object_id":"00000000-0000-4000-8000-000000000002","received_at":"2026-11-13T12:00:00Z","error":null,"index":0}
```

A replay returns `status=duplicate`; batched ingestion reports each item's status independently, so inspect every `items[]` acknowledgement even when HTTP succeeds. See the reference for transactional import/reconciliation boundaries and correction semantics.

## Errors and bounded retries

Litestar errors use `status_code`, `detail` and usually `extra`; check `extra.code` when present rather than matching human-readable text. Schema violations include bounded issues. Representative examples:

```json
{"status_code":403,"detail":"API key does not grant this operation.","extra":{"code":"insufficient_scope"}}
```

```json
{"status_code":422,"detail":"Invalid payload.","extra":{"code":"invalid_payload","issues":[]}}
```

The 422 example illustrates the envelope shape; actual validation detail/code/issues depend on the violated constraint. The [semantic contract](contract.json) and live response are authoritative. Framework-level parse/body-limit errors need not carry a PAIagent code.

| HTTP | Client action |
| --- | --- |
| 400 / 422 | Correct parsing, schema, bounds or configuration; do not blindly replay unchanged input. |
| 401 | Check the service key and its active status; missing/invalid X-User-ID returns 400. |
| 403 | Check exact scope and source grants; retrying cannot add authorization. |
| 404 | Check identity and ownership; erased/foreign resources are intentionally hidden. |
| 409 | Compare immutable version/content or idempotency body before reconciliation. |
| 410 | Stop replaying an erased identity. |
| 413 | Split or reduce the request/file; preserve per-item identities. |
| 429 | Honor Retry-After when supplied and use bounded backoff; resume only replay-safe work. |
| 502 / 503 / 504 / transport timeout | Outcome may be ambiguous. Replay only reads or requests with documented stable identities; otherwise inspect state. |

The sample client retries only explicit replay-safe operations, at most four attempts and within a deadline. It supports numeric Retry-After; HTTP-date values are returned as errors for the caller to schedule. It never silently cancels a run on local polling timeout. Do not apply generic POST retries to registry creation or immutable preference updates. Use your source-side durable outbox for ingestion, as demonstrated in the reference.

## Limits

Use `GET /v1/capabilities` for the current limits, formats, actual availability and your grants; availability does not authorize a call. Registry requests are limited to 96 KiB. Registry and data delivery routes share a process-local 120 requests/minute bucket per peer address; this is not a universal gateway limit. Honor RateLimit-Reset as well as Retry-After when present. Repeated full tutorials in a short window can exhaust this shared bucket. Supported file bytes are bounded to 5 MiB inside a 6 MiB multipart request. Extraction stops at configured character/fragment/timeout bounds. Do not infer full coverage from a partial extraction.

Default per-tenant admission allows 2 running, 100 active runs, and 100 enabled triggers. Daily reservation ceilings: 200 attempts, 3200 model calls, 10 million input and 2 million output tokens. These are reservations rather than invoices. Profile budgets may be clamped by worker limits; inspect the RunView's effective `budget`. Attempts are at most 3, each at most 50 seconds overall and 40 seconds for provider work. Failed/retried work can consume budget. Never synthesize monetary cost from token counts without your own pricing/accounting source.

## Interpret a result

The result envelope contains `execution_status`, `data_state`, a capability-specific `payload`, sources, selected periods/timezone, coverage, limitations, data boundary, generation timestamp, freshness, usage and optional personalization metadata. The six typed payload schemas are under `capability_contract.payload_json_schemas` in the contract (inspect the downloaded contract's structure). `extract` additionally validates against the profile's bounded output schema. Search is lexical JSON plus compatible indexed-file hybrid retrieval; it is not a free-text SQL or arbitrary tool executor.

`data_state` is `sufficient`, `partial` or `insufficient_data`. `freshness` is `current` or `stale`. `succeeded` describes execution, not factual confidence or completeness. Source changes can make an old result stale; erasure may remove it completely. A successful source citation check establishes source membership, not semantic entailment. Exact metrics use server-side decimal operations on registered numeric fields; null is missing, not zero. Units are explicit labels and are never automatically converted.

Timezone controls calendar grouping/period interpretation. Use aware timestamps and IANA timezone names; periods are start-inclusive/end-exclusive. Server `received_at` differs from source `occurred_at`. Completeness is declared through import boundaries/coverage, not guessed from an empty selection. A missing optional field differs from explicit null, which requires a nullable schema. Schema/profile versions are immutable and explicit; registering a new version does not migrate old objects or change a pinned trigger.

## Memory, feedback and erasure

Memory is disabled by default. Enable owner settings and profile personalization separately, for explicitly declared collections/keys/fields. Literal message-prefix extraction and current-field projections can produce candidates; arbitrary natural-language preference extraction is not provided. Candidates need confirmation. Versioned explicit preferences override automatic candidates. Temporal observations describe an event/period/unit/sample, not a permanent personality trait. Memory context is bounded to 20 items/6000 characters and is not factual evidence citation material.

Result feedback is immutable and idempotent by request ID. The latest normalized exact recommendation text marked rejected/outdated suppresses repetition within that profile. Accepted feedback does not execute external actions. Profile feedback scores are heuristics, not a calibrated confidence measure or automatic router. See the runnable JSON/curl examples and exact scopes in the [reference](reference.md).

Disabling memory stops its use; forgetting creates tombstones that prevent automatic replay. Only a new explicit assertion restores a forgotten preference. Source/user erasure also removes dependent evidence/memory/results and cancels affected runs. It is privacy deletion with replay fences, not an archive you can restore by re-uploading old deliveries. Profile deletion also removes its triggers/results. Verify your intended boundary before calling deletion endpoints.
