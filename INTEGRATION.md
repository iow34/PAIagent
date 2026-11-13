# PAIagent integration contract — steps 6–7

Contract version **0.3.0**, API prefix **/v1**. This release provides the tenant-scoped registry, persisted records/events, immutable version history, imports, reconciliation and privacy erasure. The current document, search, conversation and document-knowledge APIs remain available. General records are not yet indexed or passed to AI; execution of analysis profiles comes later.

## Install and run

Use the existing project virtual environment and configured `DATABASE_URL` / `OPENAI_API_KEY`:

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.asgi:create_app --factory --host 127.0.0.1 --port 8000
```

Migration `f3a6c9d2e710` adds `integration_sources`, `integration_collections`, `integration_schemas` and API-key grants. It preserves existing documents and keys. Run migrations before starting this code. Existing keys receive `legacy:*` and `catalog:read`; they do not gain registry administration rights. Downgrade removes registry data and grants, so it is only appropriate for an intentional rollback.

Migration `a7d9e3f1b820` adds the seven `data_*` tables and a registry association constraint. It preserves existing records, keys and documents; there were no general data tables before this migration. Downgrading it destroys step-7 data, history, outbox and erasure fences, so never use downgrade as a way to reconnect an erased identity.

The schema registry and data lifecycle need PostgreSQL but do not make model calls. The existing application lifespan still expects the OpenAI configuration used by the document features.

Live documentation: `/schema`. Machine-readable OpenAPI: `/schema/openapi.json`. The served OpenAPI describes implemented routes only. `GET /v1/capabilities` reports implemented capabilities, formats, limits and caller grants. `GET /v1/integration-contract` serves the bundled `app/integration/contract.json`, including implemented record/event/import routes and explicitly **planned** files, profiles, runs and results. Planned routes are not executable and are absent from live OpenAPI.

## Authentication and ownership

Every integration request includes:

```http
Authorization: Bearer <service-api-key>
X-User-ID: <user-authenticated-by-your-backend>
Content-Type: application/json
```

The key fixes the tenant; the trusted backend authenticates its own user and supplies `X-User-ID` (1–255 characters, no surrounding whitespace or control characters). Client bodies cannot supply tenant/user ownership or `received_at`. No browser-facing secret key is intended.

Sources, collections and schema definitions are **tenant-shared configuration**. They must not contain private user content in names, descriptions or enum values. A user ID is required consistently, but does not make a shared schema private. Registry access never grants access to stored user content. Existing document/search/query/knowledge operations still use `tenant_id + user_id`. General records/events use the same ownership boundary. Object identity is tenant + user + source + collection + external ID.

Administrative issuance for an existing tenant:

```sh
.venv/bin/python -m app.cli.create_api_key \
  --tenant-id "$TENANT_ID" --name integration-admin \
  --scope catalog:read \
  --scope sources:read --scope sources:write \
  --scope collections:read --scope collections:write \
  --scope schemas:read --scope schemas:write \
  --scope data:validate --scope data:read --scope data:write --scope data:delete
```

The command prints the secret once; only its SHA-256 hash is stored. Existing tenant provisioning is unchanged. Each `--scope` is an exact grant, with no implicit inheritance. To retain document functionality, explicitly add `--scope 'legacy:*'`. Omitting all `--scope` flags keeps the old CLI behavior (`legacy:*`, `catalog:read`). Unknown scopes are rejected; future profile/run/result scopes are not issuable yet. `data:write` does not imply `data:read` or `data:delete`; delete envelopes additionally require `data:delete`.

After creating a source, issue a narrower integration key by repeating `--source-id "$SOURCE_ID"`. Each ID must belong to the selected tenant. Omission means all tenant sources, including future ones. A source-restricted key cannot create sources or use legacy document routes, which have no source association. Listing sources returns only allowed sources. A matching scope is still required for each operation. Schemas and profiles cannot extend grants; profile execution is a later step.

## Implemented routes

| Method | Route | Required scope | Success |
| --- | --- | --- | --- |
| GET | `/v1/capabilities` | `catalog:read` | 200 catalogue |
| GET | `/v1/integration-contract` | `catalog:read` | 200 versioned contract |
| POST / GET | `/v1/sources` | `sources:write` / `sources:read` | 201 source / 200 page |
| GET | `/v1/sources/{source_id}` | `sources:read` | 200 source |
| POST / GET | `/v1/sources/{source_id}/collections` | `collections:write` / `collections:read` | 201 collection / 200 page |
| GET | `/v1/collections/{collection_id}` | `collections:read` | 200 collection |
| POST / GET | `/v1/collections/{collection_id}/schemas` | `schemas:write` / `schemas:read` | 201 schema / 200 page |
| GET | `/v1/collections/{collection_id}/schemas/{kind}/{version}` | `schemas:read` | 200 immutable version |
| POST | `/v1/collections/{collection_id}/validate` | `data:validate` | 200 dry-run result |

Source and collection creation use `{"name":"your-name","description":"optional"}`. Names are 1–100 characters, start with a lowercase ASCII letter and contain lowercase letters, digits, underscores or hyphens. Descriptions have at most 1000 characters. Source names are unique per tenant, collection names per source. IDs are server-generated UUIDs. No update or delete registry route is exposed in this step.

Lists accept `limit=1..100` (default 50) and `cursor=<next_cursor>`. The schema list also accepts `kind=record|event`. Unknown/repeated query parameters are rejected. Results are `{"items":[...],"next_cursor":null}`. A non-null cursor continues ascending UUID order; this is not creation-time ordering or a snapshot under concurrent inserts. Start a fresh traversal when reconciling concurrent additions.

## Three-domain quickstart

Set environment variables without committing or sharing their secret values:

```sh
export PAIAGENT_URL=http://127.0.0.1:8000
export PAIAGENT_USER_ID=demo-user
# Set PAIAGENT_API_KEY to the newly issued registry key.
.venv/bin/python -m app.integration.quickstart
```

This independent HTTP client creates a uniquely named source, registers three collections/schemas, and validates three envelopes. It reads ordinary JSON examples from `app/integration/examples/` and uses the same endpoints for each domain:

- `tasks.json`: state records with caption, phase, nullable estimate and nested details.
- `books.json`: state records with heading, ISBN, page count and bounded labels array.
- `learning_sessions.json`: independent observations with subject, start time, duration and reflection; no mutable task-like state is required.

The script prints `valid: true` and **`persisted: false`**. It persists registry configuration only. Re-running creates another demo source. No core branch recognizes any of these domain names.

The equivalent first requests with curl (`jq` is used only to extract IDs and select the example schema):

```sh
SOURCE_ID=$(curl --fail-with-body -sS "$PAIAGENT_URL/v1/sources" \
  -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $PAIAGENT_USER_ID" \
  -H 'Content-Type: application/json' -d '{"name":"my-backend"}' | jq -r .id)

COLLECTION_ID=$(curl --fail-with-body -sS "$PAIAGENT_URL/v1/sources/$SOURCE_ID/collections" \
  -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $PAIAGENT_USER_ID" \
  -H 'Content-Type: application/json' -d '{"name":"tasks"}' | jq -r .id)

jq '.schema' app/integration/examples/tasks.json | \
  curl --fail-with-body -sS "$PAIAGENT_URL/v1/collections/$COLLECTION_ID/schemas" \
    -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $PAIAGENT_USER_ID" \
    -H 'Content-Type: application/json' --data-binary @-

jq -n --arg source "$SOURCE_ID" --arg collection "$COLLECTION_ID" \
  '{api_version:"1",source_id:$source,collection_id:$collection,schema_version:1,
    operation:"snapshot",external_id:"item-1",object_version:1,
    occurred_at:"2026-11-12T10:00:00Z",payload:{caption:"Write report",phase:"todo"}}' | \
  curl --fail-with-body -sS "$PAIAGENT_URL/v1/collections/$COLLECTION_ID/validate" \
    -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $PAIAGENT_USER_ID" \
    -H 'Content-Type: application/json' --data-binary @-
```

A duplicate registry name/version returns 409, not a silent update. After a lost POST response, GET/list the resource and compare its definition before proceeding. Registry POST does not implement `Idempotency-Key`. Data delivery uses the separate idempotency rules below.

## Schema subset `paiagent-1`

Registration body: `kind` (`record` or `event`), `version` (integer 1–2147483647), `json_schema`, and optional `field_mappings` (default empty). A collection has independent record/event version sequences. Version numbers need not be contiguous. Versions and mappings are immutable through this API even before any data exists. Register a new version for any change. Existing data will retain its explicit version; registration does not migrate or revalidate it. There is no implicit “latest”.

Schemas use a bounded subset of [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12/json-schema-validation):

| Type / feature | Supported contract |
| --- | --- |
| Root | Non-nullable `object` |
| Every object | Explicit `properties`, `additionalProperties: false`; optional `required` naming declared properties |
| Field types | `object`, `array`, `string`, `number`, `integer`, `boolean`; or `[type,"null"]` in either order |
| Arrays | One `items` schema, mandatory `maxItems` 1–100, optional `minItems`; nested structures share the depth limit |
| Strings | Mandatory `maxLength` 1–16384, optional `minLength` and `format` |
| Formats | `date`, `uuid`, or offset-bearing `date-time` (calendar-valid, no leap seconds); format validation is enabled |
| Numeric constraints | Finite bounds with `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`; absolute bounds at most 1e308 |
| Enum | 1–100 scalar values consistent with the field type/constraints; explicitly include null if it should be an enum option |
| Annotations | Optional `title` / `description`, at most 1000 characters each |
| Dialect | Optional root `$schema` equal to `https://json-schema.org/draft/2020-12/schema` |
| Structural limits | At most 100 property declarations total; root depth 0, children through depth 5; property names match `[A-Za-z_][A-Za-z0-9_-]{0,63}` |

Everything else is rejected, including `$ref` (local or remote), `$defs`, `allOf`, `anyOf`, `oneOf`, conditionals, regex patterns, `default`, `examples`, `additionalProperties: true` and boolean schemas. No remote resource is loaded. NUL characters and raw JSON nesting beyond 16 levels are rejected before database use. No implicit coercion or default injection occurs. Missing optional fields differ from explicit `null`; null is valid only when declared nullable.

Validation uses [`jsonschema`'s Draft202012Validator and explicit format checking](https://python-jsonschema.readthedocs.io/en/stable/validate/), preceded by PAIagent's own subset/size/semantic checks. JSON Schema validation alone does not assign business meaning to a field.

## Semantic mappings

Each mapping is `{"path":"/details/note","role":"search_text"}`; quantity adds `"unit":"hour"`. Paths are JSON Pointers through declared object properties. Array traversal/wildcards are not supported. Nullable ancestors and fields are allowed; a missing/null value is unavailable, never an implicit zero or empty string.

| Role | Accepted field type | Additional rules |
| --- | --- | --- |
| `search_text` | string | Multiple fields allowed |
| `display_name` | string | At most one |
| `event_time` | string | Requires `format: date-time`; at most one |
| `quantity` | number / integer | Explicit unit required; `"1"` means dimensionless |
| `category` | string / integer / boolean | Multiple fields allowed |
| `external_reference` | string | At most one; an external identifier, not authorization to fetch another object |

A field may have different roles, but duplicate path/role pairs are rejected. Units are labels of 1–64 characters; no automatic conversion is claimed. Mappings are saved and checked in step 6; indexing and numeric analytics over these fields come later. No model proposes/applies mappings automatically.

## Record/event envelope

Required common fields: `api_version: "1"`, `source_id`, `collection_id`, `schema_version`, `operation`, `occurred_at`, `payload`. The validation URL and body source/collection must agree. Optional `metadata` accepts only `trace_id`, `origin`, `correlation_id` with 1–255 printable characters per value.

| Operation | Schema kind | IDs and version | Payload |
| --- | --- | --- | --- |
| `snapshot` | record | `external_id`, positive `object_version`; no `event_id` | Full state object |
| `upsert` | record | `external_id`, positive `object_version`, `event_id` | Full replacement state, not a patch |
| `delete` | record | Same as upsert | `null` |
| `observe` | event | `event_id`; optional `external_id`; no `object_version` | Independent observation object |

IDs have 1–255 characters without surrounding whitespace/control characters. Timestamps require an offset. `occurred_at` describes the external occurrence; ingestion assigns `received_at` in UTC. A semantic event-time field describes a time within the payload and need not equal the envelope timestamp. Ordering uses object versions, not arrival order. Observations do not implicitly modify any state record.

`/validate` checks current grants, schema existence, operation semantics and payload. It returns `{"valid":true,"schema_kind":"record","schema_version":1,"persisted":false}`. It does not assign `received_at`, reserve event IDs, guarantee later ingestion, index data, or delete anything. The same operation may become unauthorized before a later write. A delete envelope still requires an existing record schema version.

## Limits and errors

Registry HTTP bodies: **96 KiB**. Content HTTP bodies: **1 MiB**, with at most 100 batch items. Compact UTF-8 schema JSON: **32 KiB**. Compact UTF-8 payload JSON: **64 KiB**. HTTP limits include whitespace; inner limits use compact serialization. At most 100 field mappings. Existing document uploads remain UTF-8 TXT/Markdown, at most 1 MiB.

The registry uses [Litestar's rate-limit middleware](https://docs.litestar.dev/main/usage/middleware/builtin-middleware.html): **120 requests/minute per peer IP, per application process**, shared across registry routes. Failed requests count too. `X-Forwarded-For` does not change this identity. Behind a reverse proxy, its peer IP is shared; deployment-wide/key/tenant quotas require later infrastructure. `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` and `RateLimit-Policy` describe the current window. On 429, wait at least `RateLimit-Reset` seconds and add jitter. Multiple workers do not share this in-memory quota.

Errors preserve Litestar's `status_code`, `detail`, `extra` structure. Domain errors add `extra.code` and optionally `extra.issues`; framework errors can have `extra: null` or framework validation details, so clients must always handle the HTTP status. Payload issues include paths and failed keywords, without echoing private values. Up to 20 payload issues are returned.

- 400: malformed JSON/DTO, invalid parameter, unknown/repeated query parameter, unknown DTO field.
- 401: missing/invalid/inactive key; missing/invalid `X-User-ID` is a 400 input error.
- 403: insufficient operation/source grant.
- 404: missing or foreign-owner resource.
- 409: duplicate registry name/schema version or conflicting immutable delivery/version/import.
- 410: erased identity; stop replaying it.
- 413: HTTP body too large; inner schema/payload limits return 422 with `size_limit`.
- 422: unsupported/invalid schema, incompatible mapping, invalid envelope or payload.
- 429: registry request frequency exceeded.
- 500: storage/unexpected failure; private database exception text is not returned.
- 503: temporary content storage failure; retry the identical delivery.
- 504: content deadline exceeded; completed batch items may have committed, so retry unchanged.

New required fields, removed fields or changed meanings require a new API major version; adding an optional response field can be compatible. Clients should ignore new response fields, while request/configuration fields are checked strictly. Schema versions are independent of API and future profile versions.

## Persisted content API

All content routes apply the caller's `tenant_id + X-User-ID`, even when a UUID is known. Registry read permission is neither required nor sufficient for data access. A source grant must authorize the collection for every read/write.

| Method | Route | Scope | Result |
| --- | --- | --- | --- |
| POST | `/v1/collections/{collection_id}/records/single` | `data:write` | One snapshot/upsert/delete acknowledgement |
| POST | `/v1/collections/{collection_id}/events/single` | `data:write` | One upsert/delete/observe acknowledgement |
| POST / GET | `/v1/collections/{collection_id}/records` | `data:write` / `data:read` | Batch acknowledgement / current records |
| POST / GET | `/v1/collections/{collection_id}/events` | `data:write` / `data:read` | Batch acknowledgement / version and event history |
| GET / PUT | `/v1/records/{object_id}` | `data:read` / `data:write` | Current record / full upsert with a higher version |
| GET | `/v1/records/{object_id}/versions` | `data:read` | Paginated version history |
| GET | `/v1/events/{delivery_id}` | `data:read` | One history item |
| POST | `/v1/events/{delivery_id}/corrections` | `data:write` | New observation linked to its predecessor |
| POST | `/v1/collections/{collection_id}/imports` | `data:write` | Atomic snapshot part |
| GET | `/v1/imports/{import_id}` | `data:read` | Parts received, boundary, completeness |
| GET | `/v1/imports/{import_id}/reconciliation` | `data:read` | Current state versus snapshot manifest |
| GET | `/v1/collections/{collection_id}/coverage` | `data:read` | Sequence gaps and history limitations |
| DELETE | `/v1/records/{object_id}` | `data:delete` | Erase object and dependent private content |
| DELETE | `/v1/events/{delivery_id}` | `data:delete` | Erase observation chain; state events erase the whole object |
| DELETE | `/v1/collections/{collection_id}/data` | `data:delete` | Erase this user's collection data |
| DELETE | `/v1/sources/{source_id}/data` | `data:delete` | Erase this user's source data |
| DELETE | `/v1/users/me/data` | `data:delete` | Erase all this user's data; unrestricted source grant required |

Successful content operations return HTTP 200. A `delete` envelope needs both `data:write` and `data:delete`. DELETE routes are explicit privacy erasure commands and do not require an incoming object version. They are permanent for that identity. Events are immutable: correct an observation using a **new** `event_id` and the `observe` operation at `/corrections`; the previous item gets `corrected_by_id`. Correct a record change with the next `object_version`. History includes both original and corrected observations; consumers must follow correction links before aggregating them.

Use the base envelope above. Ingestion additionally accepts `source_sequence` and `references`. The older `/validate` endpoint validates only the base envelope; omit these two delivery fields there. Ingestion checks them itself. `source_sequence` is optional, integer 1–2147483647, unique in the **owner + collection** change stream; it is forbidden for snapshots. It is not a global source sequence or an object version. Use it only if the source guarantees a contiguous stream starting at 1 for that owner/collection.

`references` contains at most 20 `{ "collection_id": "UUID", "external_id": "opaque-id" }` entries. Every target must be active, owned by the same user/tenant and belong to an allowed source. An observation's optional `external_id` is also a link to an active record in its own collection. Forward references fail with 404; deliver the target first. The schema's `external_reference` semantic role is an external label such as ISBN, not an instruction to resolve a local record.

A state-change request can look like:

```json
{
  "api_version": "1",
  "source_id": "<registered-source-uuid>",
  "collection_id": "<registered-collection-uuid>",
  "schema_version": 1,
  "operation": "upsert",
  "external_id": "item-1",
  "object_version": 2,
  "event_id": "change-2",
  "source_sequence": 2,
  "occurred_at": "2026-11-13T08:00:00Z",
  "payload": {"caption": "Write report", "phase": "done"}
}
```

Records expose their current payload, schema/object version, source/collection, metadata, resolved reference UUIDs and timestamps. History also exposes `event_id`, `source_sequence`, operation and `applied` (whether this delivery changed current state at acceptance). Events include snapshots as provenance. List records with optional `external_id`; list events with optional `external_id`, `occurred_from` inclusive and `occurred_before` exclusive. Both time bounds need offsets. All lists use ascending UUID pagination, **not chronological order or a stable multi-page snapshot**. Read immutable history by its version/time fields when building temporal analyses.

### Acknowledgements, retries and transactions

Batch bodies are `{"items":[envelope,...]}` with 1–100 envelopes and at most 1 MiB total. DTO errors (unknown fields, wrong types, overlong batches) reject the request before any write. Once the DTO parses, each item has its own transaction; semantic/payload/storage failures do not roll back accepted siblings. Check each acknowledgement, even on HTTP 200:

```json
{
  "batch_id": "<response-uuid>",
  "items": [
    {"index": 0, "status": "accepted", "delivery_id": "<history-uuid>", "object_id": "<record-uuid>", "received_at": "2026-11-13T08:00:01Z", "error": null},
    {"index": 1, "status": "rejected", "delivery_id": null, "object_id": null, "received_at": null, "error": {"status_code": 422, "code": "invalid_contract", "issues": []}}
  ]
}
```

`accepted` and `stale` mean committed, including the transactional outbox entry; `duplicate` returns the original receipt time. `stale` means an older object version was added to history without replacing current state. Observation arrival order never implies an object mutation. A repeated event ID with different content or a conflicting version returns 409. Normalization makes JSON key order and equivalent time offsets irrelevant; other envelope changes are conflicts.

A snapshot and an upsert at the same version share one receipt only when schema, payload, metadata, `occurred_at` and references agree. If the snapshot arrived first, its receipt is enriched with the event ID and sequence when the matching upsert arrives. This adds history provenance without a second state mutation. Source snapshots must preserve the version's occurrence time; using the export time as `occurred_at` would describe a different version body.

Use identical identifiers and content after a timeout or lost response. The batch ID identifies a response; it is not a deduplication key. 409 needs source-side reconciliation; 410 means the identity was erased and retries must stop. Content requests have a 60-second deadline; a 504 can follow partial batch commits. A 503, 504 or transport failure has an unknown outcome and is safe to retry unchanged. Respect the rate-limit reset on 429 and use bounded backoff.

The database serializes short mutations by tenant/user and also enforces unique identities, versions and sequences. The record/history change and invalidation outbox are committed together. There are no provider calls inside these transactions. The outbox carries identifiers and reasons (`state_changed`, `history_changed`, `erasure`), not copies of private text. **No worker, index over general records, automatic analysis, job API or result cache is introduced in this step.**

### Initial synchronization and reconciliation

At the source, capture a consistent snapshot plus its change-log cursor/boundary. Continue durable change delivery after that boundary, including changes occurring while snapshot parts are sent. Send each full-state snapshot with the source object's version. PAIagent keeps the highest version as current even if old snapshot data arrives later.

An import request is:

```json
{
  "mode": "snapshot",
  "snapshot_id": "export-2026-11-13",
  "boundary": "source-log-cursor-123",
  "part": 1,
  "final": true,
  "completeness": "complete",
  "items": []
}
```

A part contains 0–100 snapshot envelopes, at most 1 MiB. Unlike record/event batches, **each import part is atomic**; any item failure rolls back the part and its manifest. Empty parts allow an explicitly empty snapshot or final marker. Parts are numbered 1–10000 and can arrive out of order. `final:true` fixes the last part number, not proof that previous parts arrived. Snapshot ID, boundary, declared completeness and a part's content are immutable. Repeating a part returns its saved acknowledgements; reusing the part number with changed content returns 409. One snapshot cannot contain different versions of the same object. Different import runs may overlap; source versions still determine current state.

`GET /imports/{id}` returns received/missing parts, the opaque boundary and `snapshot_complete`, which is true only when the source declared completeness, the final part is known and every part is present. PAIagent cannot verify the source's claim or read its external log. The boundary is a handoff marker, not a receiver-generated cursor that independently guarantees no lost source changes.

Reconciliation lists current objects with `snapshot_version`, `current_version` and `matched`, `newer_than_snapshot` or `absent_from_snapshot`. It never deletes anything. Even a complete import must not erase a newly created object merely because it was absent at the earlier boundary. Send an explicit versioned delete from the source or use the privacy-erasure API. To finish reconciliation while writes continue, use the source log and repeat enumeration after reaching a known boundary.

Coverage reports `source_sequence_high_watermark`, missing inclusive ranges (up to 100 plus `gaps_truncated`), unsequenced events, snapshot receipts and erased deliveries. Missing ranges shrink when delayed events arrive. The maximum received sequence is **not** a safe resume cursor if gaps remain. A snapshot proves no past user actions; a contiguous received prefix does not prove the entire source history arrived. Consequently `history_complete` remains false, including after a complete snapshot. A new schema version also does not transform earlier payloads: explicitly normalize at the source and send a higher object version, retaining old history under its original schema.

### Erasure, retention and reconnection

Erasure removes current payloads, history payloads, metadata, fingerprints, resolved references and affected import manifests. It removes pending invalidation entries containing erased evidence, then records content-free invalidation when appropriate. Linked observations, their entire correction chains, and records whose current **or historical** versions refer to the erased object are erased transitively. This may affect dependent records in other collections/sources owned by that same user. A privacy delete of a state-event therefore erases the whole object's private history; a late versioned delete cannot erase a newer current version.

Collection/source deletion applies to **the caller's user**, never other users of the shared registry. It leaves schema/source/collection definitions intact and fences further writes into that scope. User deletion additionally removes all legacy documents, chunks/vectors, saved document knowledge, conversation sessions and messages. Legacy content has no source/collection association, so only full-user erasure includes it. There are no separate memory/profile/result tables yet; future implementations must join this erasure contract.

Minimal tombstones retain opaque external/event IDs, technical IDs, versions, ordering/time fields and erasure flags; they contain no deleted payload, metadata or content fingerprint. Use opaque identifiers, never private text as an ID. Replays into deleted identities return 410. Record/event reads hide erased items. Source queues cannot restore them, and a query already generating an answer cannot save its conversation after full-user erasure. Existing document indexing/knowledge saves recheck their parent document; it is removed atomically with dependent data.

Erasure is synchronous and atomic, subject to the request deadline. This step has not been load-tested for large histories; large-volume deletion may require the background execution introduced later. A timeout never acknowledges an incomplete deletion.

Content is retained until explicit erasure; this MVP has no automatic TTL or retention scheduler. Tombstones/fences persist indefinitely. Reconnection must be intentional: a new external object ID for an erased object, a new collection/source identity outside an erased scope, or a newly provisioned user identity after full-user erasure. Changing an event ID alone does not resurrect an erased object. There is no API to reset an erasure fence. The integrator must not automatically regenerate identities after 410.

Future workers must acquire the same owner lock before committing, check owner/source/collection fences, ensure all referenced evidence is active and compare the versions they processed. A queued event is not permission to send all user data to a model. Deletion covers live application storage; source-side outboxes, database backups and external provider retention remain the deployment/integrator's responsibility.

### Source-side outbox example

`app/integration/delivery_example.py` is an independent source-client example using SQLite and HTTP. It commits the source object and exact outgoing envelope in one local transaction, then sends **after** commit. A crash after the server commit but before removing the local outbox entry results in a duplicate delivery, not a second effect. It uses one sender; production sources should use their own DB and an appropriate claim/lease mechanism.

After registering a collection/schema, save an upsert envelope to a private JSON file and run:

```sh
.venv/bin/python -m app.integration.delivery_example \
  --database /path/to/private/source.sqlite --enqueue /path/to/private/upsert.json
# On retry, reuse the database and omit --enqueue:
.venv/bin/python -m app.integration.delivery_example --database /path/to/private/source.sqlite
```

Set `PAIAGENT_URL`, `PAIAGENT_USER_ID`, and `PAIAGENT_API_KEY` as in the quickstart. Use a separate source DB/outbox for each destination tenant/user; the example stores no credentials. Transport/503/429 failures leave the exact envelope pending; successful accepted/duplicate/stale acknowledgements remove it. Other errors quarantine it for operator reconciliation. The example does not implement a background scheduler or alter another repository.

## Verification and continuation

Run the project regression suite with an isolated PostgreSQL/pgvector cluster:

```sh
.venv/bin/python tests/run_postgres_tests.py --all
```

Tests cover schema/envelope boundaries, real authenticated HTTP and PostgreSQL, two tenants, two users of one tenant, narrow grants, forged source/collection IDs, concurrent version registration, migration rollback/upgrade with a pre-existing key, pagination, body/rate limits and the three-domain quickstart over a real local TCP server. Same-tenant schema visibility is intentional. Step-7 tests also exercise concurrent delivery, conflicts and stale versions, sequence gaps, imports during changes, corrections, object/reference erasure, source/user fences, source outbox retries over real TCP and deletion during an active query.

Content lifecycle and migration checks use real PostgreSQL. Legacy SQLite fixtures alone do not verify erasure fencing. The suite makes no real OpenAI requests. Model quality is outside this step. The implementation was validated on a temporary database; applying the migration to the configured working database is a separate deployment action.

`docs/STEP7_PROGRESS.md` records the current implementation checkpoints, verification results and resumption instructions; `docs/STEP6_PROGRESS.md` preserves the prior step. Existing `.gitignore` rules still exclude `docs/` and `tests/`; this root guide, the JSON contract, examples and client are regular deliverable files. No commit, push or deployment is implied by saving them.
