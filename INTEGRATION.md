# PAIagent integration contract — steps 6–9

Contract version **0.5.0**, API prefix **/v1**. This release provides the tenant-scoped registry, persisted records/events, immutable version history, imports, reconciliation and privacy erasure. The current document, search, conversation and document-knowledge APIs remain available. General records and supported files have a shared normalization/context/search API; six AI capabilities now execute through immutable declarative profiles with bounded synchronous runs and saved collected-data results.

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

Live documentation: `/schema`. Machine-readable OpenAPI: `/schema/openapi.json`. The served OpenAPI describes implemented routes only. `GET /v1/capabilities` reports implemented capabilities, formats, limits and caller grants. `GET /v1/integration-contract` serves the bundled `app/integration/contract.json`, including implemented data/normalization/profile/result routes and explicitly **planned** durable jobs/triggers. Planned routes are not executable and are absent from live OpenAPI.

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

The command prints the secret once; only its SHA-256 hash is stored. Existing tenant provisioning is unchanged. Each `--scope` is an exact grant, with no implicit inheritance. To retain document functionality, explicitly add `--scope 'legacy:*'`. Omitting all `--scope` flags keeps the old CLI behavior (`legacy:*`, `catalog:read`). Unknown scopes are rejected; `profiles:read`, `profiles:write`, `ai:run`, `results:read` are also issuable; add these explicitly for step 9. `data:write` does not imply `data:read` or `data:delete`; delete envelopes additionally require `data:delete`.

After creating a source, issue a narrower integration key by repeating `--source-id "$SOURCE_ID"`. Each ID must belong to the selected tenant. Omission means all tenant sources, including future ones. A source-restricted key cannot create sources or use legacy document routes, which have no source association. Listing sources returns only allowed sources. A matching scope is still required for each operation. Schemas and profiles cannot extend grants. Profiles/results are private to tenant + user; every read/run also rechecks collection source grants.

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

## Step 8 — normalized formats and shared evidence (contract 0.4.0)

Migration `b8e4f2a6c930` adds `evidence_fragments` and `file_provenance`. Apply it before running this release. Existing step-7 payloads remain intact; explicitly normalize old current data with `POST /v1/collections/{id}/normalize?limit=50`, following `next_cursor`. New JSON/CSV deliveries create fragments in the ingestion transaction, using only registered `search_text` mappings. Numeric/date/category values remain exact JSON values in the original schema-versioned delivery; an evidence item identifies that delivery for typed-value retrieval through `/v1/events/{delivery_id}`. No numeric value is reconstructed from embedding text.

New APIs use `data:read`/`data:write` and the existing owner/source grants:

| Route | Purpose |
| --- | --- |
| `POST /v1/collections/{id}/files` | Multipart `file` and `envelope` (JSON IngestEnvelope string). Supports `.txt`, `.md`, text-layer `.pdf`, DRM-free `.epub`; envelope is snapshot/upsert with a schema-valid metadata payload. |
| `POST /v1/collections/{id}/csv` | Multipart `file`, `envelope` (snapshot template), `mapping` (JSON configuration below). Returns line numbers and conversion/schema/ingestion errors per row. |
| `POST /v1/collections/{id}/normalize` | Explicit paginated normalization of current pre-existing JSON data. No embedding calls. |
| `GET /v1/collections/{id}/context` | Paginated evidence units; `cursor`, `limit=1..100`. Includes positions, record/event versions and extraction limitations. |
| `GET /v1/evidence/{fragment_id}` | Retrieve a current evidence unit with fresh access checks; superseded/erased units return 404. |
| `POST /v1/collections/{id}/index` | Explicit embedding batch, at most 64 units. Repeat until `indexed=0`. Requires configured OpenAI access, may incur provider costs. |
| `POST /v1/collections/{id}/search` | JSON `{"query":"...","limit":5}`; vector/full-text RRF over current authorized collection evidence, limit 1..20. |

The multipart envelope is the same record envelope used by step 7; its payload holds registered metadata, not a fabricated text-field schema for each file. The server stores extracted text, positions, byte fingerprint, format and extraction coverage, without retaining the original binary. Identical envelope/file replay is idempotent. Changed file bytes at the same version conflict; supply a higher object version. A previously accepted JSON-only version cannot later acquire a file: use a higher version. The source keeps the original binary for external retrieval; raw uploaded bytes and arbitrary URLs are not exposed through evidence.

Example with existing `source_id`, `collection_id`, metadata schema and `envelope.json`:

```sh
curl --fail-with-body "$PAIAGENT_URL/v1/collections/$COLLECTION_ID/files" \
  -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $PAIAGENT_USER_ID" \
  -F 'file=@/path/to/document.pdf' -F 'envelope=</path/to/envelope.json'
```

CSV mapping example (root fields only, no type inference):

```json
{
  "columns": [
    {"column":"title","field":"caption","type":"string","empty":"reject"},
    {"column":"hours","field":"estimate","type":"number","empty":"null","unit":"hour"},
    {"column":"status","field":"phase","type":"string","empty":"reject"}
  ],
  "id_column":"id", "version_column":"version",
  "occurred_at":"2026-11-13T08:00:00Z", "encoding":"utf-8-sig", "delimiter":","
}
```

Types: `string`, `integer`, finite `number`, lowercase `true|false` boolean, ISO `date`, offset-bearing `date-time`. Dates remain ISO strings for JSON Schema validation; there is no timezone/locale inference. Encodings: UTF-8 with optional BOM. Delimiters: comma, semicolon or tab. Empty behavior is `reject`, `null` or `omit`. A quantity mapping's CSV unit must exactly match the registered schema unit; units are not converted. Each row must contain an external ID and positive source version. Unmapped columns are ignored, duplicate/missing headers rejected. Valid rows persist independently; invalid rows have a visible line error. Retry the same unchanged CSV/envelope; changed successful rows require higher versions. Missing rows never imply deletion. CSV is a bounded ingestion batch, not a historical export.

Limits: file 5 MiB, multipart request 6 MiB, extracted text 500,000 characters, 500 PDF pages/EPUB spine parts, 1,000 fragments/input, CSV 100 rows, EPUB 2,000 entries/20 MiB total uncompressed declared bytes. Over-limit files are rejected rather than silently truncated. Fragment length is at most 1,200 characters; indexing prepares batches using the existing embedding token limits. File parsing runs in a disposable process with an 8-second CPU limit and a 10-second wall deadline. Linux applies a 768 MiB address-space limit; macOS uses a parent RSS watchdog sampled every 50 ms (brief overshoot is possible). The worker receives only input/format, with a minimal environment and no service API credentials. It never executes document scripts, extracts archive files to disk, fetches URLs or performs OCR/DRM removal.

PDF provenance uses actual 1-based page numbers; image-only PDFs are rejected, pages without extractable text cause partial coverage. PDF text-layer extraction does not reproduce layout or recognize image text. EPUB follows the OPF spine, with actual internal paths/section ordinals; encrypted EPUBs (including encryption manifests) are rejected. Active scripts/styles are omitted; images and non-spine material are outside extraction. TXT/Markdown retain extracted text, without executing markup. Character offsets refer to the extracted unit text, not binary file offsets. Full extraction coverage means all supported text units were visited; it is not proof that every visual element was interpreted.

Context pages and search results carry explicit sampling limitations. UUID pages are not a snapshot under concurrent changes; `selection_complete` only indicates that the current page exhausted the remaining traversal. Collection/history completeness stays unknown. The pipeline supports all stored fragments in batches rather than treating the first 12 chunks as an entire book. Hierarchical summarization can later consume section/page units and their ordinals; no new summarization model or profile executor is added here.

General evidence search is separate from legacy `/v1/search` and `/v1/query`, which keep their contracts and document pipeline. The common evidence endpoints are the shared context interface for later capabilities. Search applies owner/collection restrictions before ranking, validates the source grant, combines pgvector cosine rank and PostgreSQL `simple` full-text rank, and returns source identifiers and exact versions. It does not use unselected JSON fields as embedding/search text. Unindexed evidence participates only in full-text ranking. New versions immediately hide old fragments from current context/search/evidence retrieval; erasure deletes fragments/vectors/file provenance with the original private content. An indexing result is discarded if its evidence changed or was erased while the provider ran. Stored historical source payloads retain their original schema versions.

The step-8 test suite uses deterministic embeddings and generated PDF/EPUB fixtures. It does not call paid models or establish real embedding quality. The existing legacy TXT/Markdown regression suite remains part of acceptance. New formats can be added as bounded adapters returning extraction units with honest positions/coverage, while integrators can convert unsupported formats to normalized JSON or text themselves.

## Step 9 — AI capabilities and declarative profiles (contract 0.5.0)

Migration `c9f5a3b7d041` follows `b8e4f2a6c930` and adds `analysis_profiles`, immutable `analysis_profile_versions`, and `analysis_results`. Existing data is preserved. Apply with `.venv/bin/alembic upgrade head` before serving this version. Downgrading removes profiles/results. Keys receive no implicit new permissions. Add `profiles:read`, `profiles:write`, `ai:run`, `results:read` explicitly; collection execution and result reads additionally require `data:read`. The service still needs `OPENAI_API_KEY` for generation/embedding operations; this is separate from its inbound service API key.

Profiles belong to **tenant + X-User-ID**, including their goals and settings. A profile can never select another owner's data or extend the key's source grant. Registered schemas remain tenant-shared metadata. The static `/v1/integration-contract` includes JSON Schemas for ProfileInput, RunInput, profile pages and result envelopes; OpenAPI exposes these types too.

| Method and route | Behavior |
| --- | --- |
| `POST /v1/profiles` | Create version 1 from ProfileInput; 201. |
| `GET /v1/profiles?limit=50&cursor=...` | Page of latest authorized versions, ordered by profile UUID. |
| `GET /v1/profiles/{id}` | Latest profile metadata; does not run it. |
| `GET /v1/profiles/{id}/versions/{version}` | Specific immutable version. |
| `POST /v1/profiles/{id}/versions/{version}` | New full ProfileInput, version must be previous maximum + 1; concurrent duplicates conflict. |
| `DELETE /v1/profiles/{id}` | 200; delete all versions and saved results. |
| `POST /v1/profiles/{id}/runs` | RunInput with **explicit version**; 200 CapabilityResult. Finite synchronous execution. |
| `GET /v1/profiles/{id}/results?limit=50&cursor=...` | Page of small result summaries (IDs, version, state, generated_at, freshness); fetch payload by ID. |
| `GET /v1/results/{id}` | Saved envelope with freshly checked current/stale status. Never generates. Inline IDs are not persisted and return 404. |

Page size is 1..100; cursors are exclusive UUIDs, not chronological offsets or snapshots. Authorization can leave a short/empty page with a next cursor. A successful HTTP request commits its writes. This step does not provide asynchronous jobs, schedules, triggers, idempotent run replay or a durable `GET /runs/{id}`. A timeout or transport failure should be handled by the client; an ambiguous HTTP response can occur after commit, so inspect saved result summaries before retrying an expensive request.

### Profile and run example

The following uses the existing task example schema; replace `$COLLECTION_ID` with your own registered collection ID. The same declarations work with other scalar field names and registered quantity/category roles. No entity-specific controller or graph node is needed. Six complete profile examples are bundled in `app/integration/profile_examples/`; their placeholder collection UUID must be replaced.

```sh
curl -sS "$BASE_URL/v1/profiles" \
  -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $USER_ID" \
  -H 'Content-Type: application/json' -d "{
    \"name\":\"workload\",\"capability\":\"analyze\",
    \"goal\":\"Describe the supplied workload and its limitations\",
    \"language\":\"uk\",\"timezone\":\"Europe/Warsaw\",
    \"selections\":[{\"collection_id\":\"$COLLECTION_ID\",\"schema_version\":1,
      \"fields\":[\"/caption\",\"/phase\",\"/estimate\"],\"kind\":\"record\",\"mode\":\"current\"}],
    \"metrics\":[{\"name\":\"records\",\"selection\":0,\"operation\":\"count\"},
      {\"name\":\"estimate_by_phase\",\"selection\":0,\"operation\":\"average\",
       \"field\":\"/estimate\",\"group_by\":\"/phase\"}],
    \"minimum_samples\":1}"

# Use profile_id from the response. Run version 1, not an implicit latest.
curl -sS "$BASE_URL/v1/profiles/$PROFILE_ID/runs" \
  -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $USER_ID" \
  -H 'Content-Type: application/json' -d '{"version":1}'
```

ProfileInput supports `name`, `capability`, `goal`, `selections`, `period`, `comparison_period`, `timezone`, `constraints`, `language`, `output_schema`, `metrics`, `minimum_samples`, `budget`. Unknown keys, tools, SQL, formulas and executable graphs are rejected. Goals/constraints/schema annotations are data under system rules; user text is never allowed to change access or limits.

Selections pin `collection_id`, `kind=record|event`, `schema_version`, and 1..30 scalar JSON Pointer `fields` through objects (arrays/whole objects are not selected). `mode=current` chooses the current record version; `history` chooses non-erased snapshots/upserts, or independent observations when `kind=event`. Events require `mode=history`; corrected observations are excluded. Snapshots describe state and must not be counted as user actions. Schema versions are never coerced or combined implicitly. Future payload versions require a new profile version or explicit compatible input. `include_text=true` explicitly adds attached file contents; otherwise only selected scalar values are visible. It does not expose unselected JSON search-text fields. Registering a schema alone does not ingest or normalize its data.

`filters` is an AND of `{field,op,value}` declarations. Every filter/time/metric path must also be in `fields`. `eq`, `ne`, `in` take typed non-null values satisfying the pinned schema; `in` has 1..100 entries. `gt/gte/lt/lte` support numeric fields only. `is_null` matches both missing and JSON null; `not_null` excludes both; these operators take no value. All other operators exclude null/missing. There is no implicit text/number/date conversion.

`period` and optional non-overlapping `comparison_period` are `{start,end}` with explicit offsets, interpreted as **[start, end)**. Comparison requires metrics and a primary period. A selection's `time_field` defaults to delivery metadata `occurred_at`; alternatively select a schema field with `format=date-time`. Missing/null time is excluded by a period. Without a period it remains available and uses `missing_time` for interval grouping. The IANA `timezone` changes day/week/month calendar buckets, including daylight-saving transitions; it never assigns an offset to naive timestamps. Week starts Monday; bucket values are local start dates.

### Inputs and typed payloads

Every run returns the shared CapabilityResult envelope. `execution_status=succeeded` describes execution, separately from `data_state=sufficient|partial|insufficient_data`. It includes `run_id`, `result_id`, capability, immutable profile version, typed payload, authorized `sources` with delivery/object/schema versions and evidence positions, periods/timezone, coverage, limitations, generated/data-as-of timestamps, freshness and budget reservations. `sufficient` means enough selected evidence for this operation, not complete external history or verified truth.

| Capability | RunInput and payload |
| --- | --- |
| `search` | Non-blank `query`, `limit=1..20` (default 5). Payload `{items: Source[]}`; no generation. Ranks only the bounded authorized selection: lexical matching on projected scalar fields; explicitly included indexed file fragments also use cosine similarity, fused with RRF k=60. No automatic indexing. Always partial or insufficient. |
| `query` | Non-blank `query`; optional explicit `history=[{role:user|assistant,content}]`, at most 10 messages × 2000 chars. Payload `{claims:[{text,source_ids}],limitations}`. History is context only, never evidence; it is not saved as a conversation or memory. Uses the same retrieval/partial coverage as search. |
| `summarize` | Payload `{claims:[{text,source_ids}],limitations}`. Processes all selected source batches, summarizes each and recursively reduces them within the budget. All processed source IDs/positions remain in the envelope; final claim references must trace to supplied evidence. Multiple selected documents retain distinct source identities. |
| `extract` | Requires `output_schema` using the bounded registered-schema subset; objects require all properties in `required` and `additionalProperties:false`. Use nullable fields for unknown values; enum fields implement classification. Payload `{data,source_ids,claims,limitations}`; `data=null` on insufficient evidence. No arbitrary code or schema references. |
| `analyze` | Payload `{metrics,claims,limitations}`; metrics come exclusively from server operations below. Claims provide qualitative comparisons/observations. |
| `recommend` | Payload `{claims,limitations}`; claims describe evidence-grounded advisory proposals for the declared goal. No command execution. Managed memory/personalization is reserved for step 11. |

For long extract/analyze/recommend inputs, qualitative generation uses the first bounded text batch and reports partial; exact analyze metrics still use all matching selected rows or fail explicitly. Search/query operate on a bounded candidate sample, not a full-corpus search guarantee. Summarize covers its selected batches, not necessarily every original/source-side object. Coverage distinguishes matched/selected rows and fragments, processed text, pinned schema, and unknown collection/history completeness. Large inputs beyond the selection/source or generation budget must be narrowed or explicitly given a larger supported budget.

For an ephemeral run, create a profile with `selections:[]`, then submit `{"version":1,"inline":[{"label":"note","text":"..."}]}`. Up to 10 explicit text inputs and 500000 total characters are accepted. Do not mix inline inputs with collection selections. Inline periods and exact metrics are unsupported; use schema-validated collection data for them. Input text/history and resulting output are not written to the application DB, added to managed memory, or logged by this workflow; they remain in request memory until it finishes. The profile itself is persisted, so do not put ephemeral source text into its goal/constraints. Provider requests set `store=false`; provider operational retention follows its own account/policy terms.

### Exact numeric operations

Only `analyze` accepts `metrics`, at most 20. A metric names one selection index and `operation=count|sum|average|min|max`. `count` counts received selected rows and prohibits `field`; numeric operations require a selected, schema-registered `quantity` with an explicit unit. `group_by` requires a selected registered `category`. Optional `interval=day|week|month` uses the selection time field. At most 100 group/bucket combinations per metric/period. No formula inference or unit conversion is performed.

Missing/null quantity values are skipped. Empty or all-null numeric aggregates are `null`; an ungrouped empty count is `"0"`. Group keys preserve scalar types; missing/null categories share a null group. Metrics return `sample_count`, `value_count`, `null_count`, and the non-null `denominator` for average, with supporting delivery IDs. Numeric values are decimal strings to avoid client JSON float loss. Arithmetic uses decimal precision 650 and ROUND_HALF_EVEN, enough for supported finite input magnitudes 1e-324..1e308 and bounded sums; repeating averages are rounded. Larger numeric magnitudes fail explicitly. `count` unit is `1`; other units are copied from the registered quantity.

Two periods return primary/comparison aggregates plus primary-minus-comparison differences. Groups and calendar buckets align by their exact values; absent groups/buckets produce `null`, not assumed zero. Calendar buckets use absolute local dates rather than relative day numbers across periods. Per-period bucket series can therefore be compared by the client using its own declared alignment. Exact input exceeding `max_records` returns `422 exact_input_limit`; no sampled metric is labeled complete. Model-generated prose cannot overwrite the numeric metric payload.

### Budgets, failure and lifecycle

Defaults / hard maxima: records 1000/5000, file/inline fragments 1000/1000, source characters per call 24000/30000, model calls 16/32, input token reservation 64000/256000, total output reservation 16000/32000, per-call output 2000/4000, provider-phase seconds 50/50. Source JSON is limited to 4 MiB; result envelope to 8 MiB. Limits also have positive lower bounds in the schema. Input reservation conservatively charges UTF-8 bytes plus framing including system/schema; output reserves the complete per-call maximum, not just observed text. These are safe local reservations, not provider billing reports. Capability generation/embedding SDK clients disable retries; retrieval embedding counts as a model call. Existing legacy clients keep their existing retry policy.

If any selected dataset/period fails to meet `minimum_samples` (default 1), no generation occurs and the data state is insufficient; exact metrics can still be returned. Inline minimum counts inputs, not fragments. `422 generation_budget` signals exhausted call/token budget; `422 metric_group_limit` means too many groups; `422 size_limit` means source/result JSON too large; `504 capability_timeout` is the deadline; `502 invalid_generation` covers refusal, incomplete/invalid output, provider errors or unverified/duplicate evidence IDs. Existing successful results remain saved. A successful insufficient-data result is a separate record; no prior result is overwritten.

The provider adapter uses strict Responses JSON Schema and validates output again locally, including every claim's source IDs. A source ID check proves membership/authorization, not that the statement is supported. Semantic accuracy, recommendation usefulness, causality and prompt-injection resistance still require real-model evaluation; no numeric confidence is invented. [Official structured-output contract](https://developers.openai.com/api/docs/guides/structured-outputs).

Collected runs capture the collection mutation signature under the same owner lock used by ingestion/erasure. Provider calls occur outside the transaction. The signature, profile and erasure fences are rechecked under that lock before saving; concurrent input changes return `409 input_changed`. Results are append-only successes; GET refreshes `freshness=current|stale` without model calls. Changes anywhere in a selected collection conservatively mark a result stale, even outside its filters. Profile version stays fixed during execution; creating a later version does not mutate older versions/results.

Erasing any dependent object/observation physically removes results for its affected collections, including results based on absence/counts and corrected observation chains. Scope erasure also deletes profiles referencing that scope; user erasure removes all private profiles/results, including inline-only profile settings. In-flight work cannot save behind an erasure fence or recreate an erased result. Profile deletion cascades its versions/results. Collected successes persist until explicit deletion; no automatic expiry is configured in this step. Backups/source queues/provider operational storage remain outside live application erasure.

Local resume/checkpoints: `docs/STEP9_PROGRESS.md`. Verification command: `PYTHONWARNINGS=ignore .venv/bin/python tests/run_postgres_tests.py --all`. Tests use temporary PostgreSQL/pgvector and fake providers or local MockTransport, never paid model calls. Those checks establish API/computation/lifecycle behavior, not real model quality. `docs/` and `tests/` remain ignored under the existing repository policy.
