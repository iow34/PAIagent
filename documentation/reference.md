# Integration reference

For a fresh installation use [operations](operations.md); for the complete HTTP onboarding use [quickstart](quickstart.md). This reference preserves detailed lifecycle examples. Commands assume the repository root. Older validation/delivery examples use `PAIAGENT_API_KEY`; the new standalone client uses `PAIAGENT_SERVICE_KEY`. Both mean the inbound service key, never the provider key.

Contract version **0.7.0**, API prefix **/v1**. This release provides the tenant-scoped registry, persisted records/events, immutable version history, imports, reconciliation and privacy erasure. The current document, search, conversation and document-knowledge APIs remain available. General records and supported files have a shared normalization/context/search API; six AI capabilities execute through immutable profiles with synchronous runs, durable background jobs, change/interval triggers saved collected-data results, explicit managed memory and result feedback.

## Install and run

Use the existing project virtual environment and configured `DATABASE_URL` / `OPENAI_API_KEY`:

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.asgi:create_app --factory --host 127.0.0.1 --port 8000
```

Migration `f3a6c9d2e710` adds `integration_sources`, `integration_collections`, `integration_schemas` and API-key grants. It preserves existing documents and keys. Run migrations before starting this code. Existing keys receive `legacy:*` and `catalog:read`; they do not gain registry administration rights. Downgrade removes registry data and grants, so it is only appropriate for an intentional rollback.

Migration `a7d9e3f1b820` adds the seven `data_*` tables and a registry association constraint. It preserves existing records, keys and documents; there were no general data tables before this migration. Downgrading it destroys ingested data, history, outbox and erasure fences, so never use downgrade as a way to reconnect an erased identity.

The schema registry and data lifecycle need PostgreSQL but do not make model calls. The existing application lifespan still expects the OpenAI configuration used by the document features.

Live documentation: `/schema`. Machine-readable OpenAPI: `/schema/openapi.json`. The served OpenAPI describes implemented routes only. `GET /v1/capabilities` reports implemented capabilities, formats, limits and caller grants. `GET /v1/integration-contract` serves the bundled `app/integration/contract.json`, including implemented data/normalization/profile/result routes and durable jobs and change/interval triggers. Opt-in managed memory and feedback are described below. Planned routes are not executable and are absent from live OpenAPI.

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

The command prints the secret once; only its SHA-256 hash is stored. Fresh installs can provision a tenant using the administrative command in [operations](operations.md). Each `--scope` is an exact grant, with no implicit inheritance. To retain document functionality, explicitly add `--scope 'legacy:*'`. Omitting all `--scope` flags keeps the old CLI behavior (`legacy:*`, `catalog:read`). Unknown scopes are rejected; `profiles:read`, `profiles:write`, `ai:run`, `results:read` are also issuable; add these explicitly for profile execution. `data:write` does not imply `data:read` or `data:delete`; delete envelopes additionally require `data:delete`.

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

This independent HTTP client creates a uniquely named source, registers three collections/schemas, and validates three envelopes. It reads ordinary JSON examples from `examples/domains/` and uses the same endpoints for each domain:

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

jq '.schema' documentation/examples/domains/tasks.json | \
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

A field may have different roles, but duplicate path/role pairs are rejected. Units are labels of 1–64 characters; no automatic conversion is claimed. Mappings are saved and validated at registration; normalized retrieval and declared numeric analytics consume these roles. No model proposes/applies mappings automatically.

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

The registry uses [Litestar's rate-limit middleware](https://docs.litestar.dev/main/usage/middleware/builtin-middleware.html): **120 requests/minute per peer IP, per application process**, shared across registry and data routes. Failed requests count too. `X-Forwarded-For` does not change this identity. Behind a reverse proxy, its peer IP is shared; deployment-wide/key/tenant quotas require later infrastructure. `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` and `RateLimit-Policy` describe the current window. On 429, wait at least `RateLimit-Reset` seconds and add jitter. Multiple workers do not share this in-memory quota.

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

New required fields, removed fields or changed meanings require a new API major version; adding an optional response field can be compatible. Clients should ignore new response fields, while request/configuration fields are checked strictly. Schema versions are independent of API and profile versions.

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

Collection/source deletion applies to **the caller's user**, never other users of the shared registry. It leaves schema/source/collection definitions intact and fences further writes into that scope. User deletion additionally removes all legacy documents, chunks/vectors, saved document knowledge, conversation sessions and messages. Legacy content has no source/collection association, so only full-user erasure includes it. Dependent profile results, active runs, evidence and managed memory participate in the erasure contract described in the execution and memory sections below.

Minimal tombstones retain opaque external/event IDs, technical IDs, versions, ordering/time fields and erasure flags; they contain no deleted payload, metadata or content fingerprint. Use opaque identifiers, never private text as an ID. Replays into deleted identities return 410. Record/event reads hide erased items. Source queues cannot restore them, and a query already generating an answer cannot save its conversation after full-user erasure. Existing document indexing/knowledge saves recheck their parent document; it is removed atomically with dependent data.

Erasure is synchronous and atomic, subject to the request deadline. Large-history erasure has not been load-tested; deletion remains a synchronous atomic operation in this release. A timeout never acknowledges an incomplete deletion.

Content is retained until explicit erasure; there is no automatic TTL or retention scheduler. Tombstones/fences persist indefinitely. Reconnection must be intentional: a new external object ID for an erased object, a new collection/source identity outside an erased scope, or a newly provisioned user identity after full-user erasure. Changing an event ID alone does not resurrect an erased object. There is no API to reset an erasure fence. The integrator must not automatically regenerate identities after 410.

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

## Verification

Run the project regression suite with an isolated PostgreSQL/pgvector cluster:

```sh
.venv/bin/python tests/run_postgres_tests.py --all
```

Tests cover schema/envelope boundaries, real authenticated HTTP and PostgreSQL, two tenants, two users of one tenant, narrow grants, forged source/collection IDs, concurrent version registration, migration rollback/upgrade with a pre-existing key, pagination, body/rate limits and the three-domain quickstart over a real local TCP server. Same-tenant schema visibility is intentional. Step-7 tests also exercise concurrent delivery, conflicts and stale versions, sequence gaps, imports during changes, corrections, object/reference erasure, source/user fences, source outbox retries over real TCP and deletion during an active query.

Content lifecycle and migration checks use real PostgreSQL. Legacy SQLite fixtures alone do not verify erasure fencing. The suite makes no real OpenAI requests. Model quality is outside this step. The implementation was validated on a temporary database; applying the migration to the configured working database is a separate deployment action.


## Normalized formats and shared evidence

Migration `b8e4f2a6c930` adds `evidence_fragments` and `file_provenance`. Apply it before running this release. Existing ingested payloads remain intact; explicitly normalize old current data with `POST /v1/collections/{id}/normalize?limit=50`, following `next_cursor`. New JSON/CSV deliveries create fragments in the ingestion transaction, using only registered `search_text` mappings. Numeric/date/category values remain exact JSON values in the original schema-versioned delivery; an evidence item identifies that delivery for typed-value retrieval through `/v1/events/{delivery_id}`. No numeric value is reconstructed from embedding text.

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

The multipart envelope is the same record envelope used by JSON ingestion; its payload holds registered metadata, not a fabricated text-field schema for each file. The server stores extracted text, positions, byte fingerprint, format and extraction coverage, without retaining the original binary. Identical envelope/file replay is idempotent. Changed file bytes at the same version conflict; supply a higher object version. A previously accepted JSON-only version cannot later acquire a file: use a higher version. The source keeps the original binary for external retrieval; raw uploaded bytes and arbitrary URLs are not exposed through evidence.

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

Context pages and search results carry explicit sampling limitations. UUID pages are not a snapshot under concurrent changes; `selection_complete` only indicates that the current page exhausted the remaining traversal. Collection/history completeness stays unknown. The pipeline supports all stored fragments in batches rather than treating the first 12 chunks as an entire book. Profile summarization consumes bounded evidence sections through the shared context layer; inspect coverage and limitations on its result.

General evidence search is separate from legacy `/v1/search` and `/v1/query`, which keep their contracts and document pipeline. The common evidence endpoints are the shared context interface for profile capabilities. Search applies owner/collection restrictions before ranking, validates the source grant, combines pgvector cosine rank and PostgreSQL `simple` full-text rank, and returns source identifiers and exact versions. It does not use unselected JSON fields as embedding/search text. Unindexed evidence participates only in full-text ranking. New versions immediately hide old fragments from current context/search/evidence retrieval; erasure deletes fragments/vectors/file provenance with the original private content. An indexing result is discarded if its evidence changed or was erased while the provider ran. Stored historical source payloads retain their original schema versions.

The step-8 test suite uses deterministic embeddings and generated PDF/EPUB fixtures. It does not call paid models or establish real embedding quality. The existing legacy TXT/Markdown regression suite remains part of acceptance. New formats can be added as bounded adapters returning extraction units with honest positions/coverage, while integrators can convert unsupported formats to normalized JSON or text themselves.

## AI capabilities and declarative profiles

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

Page size is 1..100; cursors are exclusive UUIDs, not chronological offsets or snapshots. Authorization can leave a short/empty page with a next cursor. A successful HTTP request commits its writes. The synchronous profile route remains compatible; the durable run API below adds jobs, triggers and idempotent run replay. A timeout or transport failure should be handled by the client; an ambiguous HTTP response can occur after commit, so inspect saved result summaries before retrying an expensive request.

### Profile and run example

The following uses the existing task example schema; replace `$COLLECTION_ID` with your own registered collection ID. The same declarations work with other scalar field names and registered quantity/category roles. No entity-specific controller or graph node is needed. Six complete profile examples are bundled in `documentation/examples/profiles/`; their placeholder collection UUID must be replaced.

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
| `recommend` | Payload `{claims,limitations}`; claims describe evidence-grounded advisory proposals for the declared goal. No command execution. Managed memory/personalization is optional under the step-11 contract below. |

For long extract/analyze/recommend inputs, qualitative generation uses the first bounded text batch and reports partial; exact analyze metrics still use all matching selected rows or fail explicitly. Search/query operate on a bounded candidate sample, not a full-corpus search guarantee. Summarize covers its selected batches, not necessarily every original/source-side object. Coverage distinguishes matched/selected rows and fragments, processed text, pinned schema, and unknown collection/history completeness. Large inputs beyond the selection/source or generation budget must be narrowed or explicitly given a larger supported budget.

For an ephemeral run, create a profile with `selections:[]`, then submit `{"version":1,"inline":[{"label":"note","text":"..."}]}`. Up to 10 explicit text inputs and 500000 total characters are accepted. Do not mix inline inputs with collection selections. Inline periods and exact metrics are unsupported; use schema-validated collection data for them. Input text/history and resulting output are not written to the application DB, added to managed memory, or logged by this workflow; they remain in request memory until it finishes. The profile itself is persisted, so do not put ephemeral source text into its goal/constraints. Provider requests set `store=false`; provider operational retention follows its own account/policy terms.

### Exact numeric operations

Only `analyze` accepts `metrics`, at most 20. A metric names one selection index and `operation=count|sum|average|min|max`. `count` counts received selected rows and prohibits `field`; numeric operations require a selected, schema-registered `quantity` with an explicit unit. `group_by` requires a selected registered `category`. Optional `interval=day|week|month` uses the selection time field. At most 100 group/bucket combinations per metric/period. No formula inference or unit conversion is performed.

Missing/null quantity values are skipped. Empty or all-null numeric aggregates are `null`; an ungrouped empty count is `"0"`. Group keys preserve scalar types; missing/null categories share a null group. Metrics return `sample_count`, `value_count`, `null_count`, and the non-null `denominator` for average, with supporting delivery IDs. Numeric values are decimal strings to avoid client JSON float loss. Arithmetic uses decimal precision 650 and ROUND_HALF_EVEN, enough for supported finite input magnitudes 1e-324..1e308 and bounded sums; repeating averages are rounded. Larger numeric magnitudes fail explicitly. `count` unit is `1`; other units are copied from the registered quantity.

Two periods return primary/comparison aggregates plus primary-minus-comparison differences. Groups and calendar buckets align by their exact values; absent groups/buckets produce `null`, not assumed zero. Calendar buckets use absolute local dates rather than relative day numbers across periods. Per-period bucket series can therefore be compared by the client using its own declared alignment. Exact input exceeding `max_records` returns `422 exact_input_limit`; no sampled metric is labeled complete. Model-generated prose cannot overwrite the numeric metric payload.

### Budgets, failure and lifecycle

Defaults / hard maxima: records 1000/5000, file/inline fragments 1000/1000, source characters per call 24000/30000, model calls 16/32, input token reservation 64000/256000, total output reservation 16000/32000, per-call output 2000/4000, provider-phase seconds 50/50. Source JSON is limited to 4 MiB; result envelope to 8 MiB. Limits also have positive lower bounds in the schema. Input reservation conservatively charges UTF-8 bytes plus framing including system/schema; output reserves the complete per-call maximum, not just observed text. These are safe local reservations, not provider billing reports. Step-10 tenant admission may further clamp these per-profile bounds (including provider-phase seconds to 40); the effective budget appears in RunView. Capability generation/embedding SDK clients disable retries; retrieval embedding counts as a model call. Existing legacy clients keep their existing retry policy.

If any selected dataset/period fails to meet `minimum_samples` (default 1), no generation occurs and the data state is insufficient; exact metrics can still be returned. Inline minimum counts inputs, not fragments. `422 generation_budget` signals exhausted call/token budget; `422 metric_group_limit` means too many groups; `422 size_limit` means source/result JSON too large; `504 capability_timeout` is the deadline; `502 invalid_generation` covers refusal, incomplete/invalid output, provider errors or unverified/duplicate evidence IDs. Existing successful results remain saved. A successful insufficient-data result is a separate record; no prior result is overwritten.

The provider adapter uses strict Responses JSON Schema and validates output again locally, including every claim's source IDs. A source ID check proves membership/authorization, not that the statement is supported. Semantic accuracy, recommendation usefulness, causality and prompt-injection resistance still require real-model evaluation; no numeric confidence is invented. [Official structured-output contract](https://developers.openai.com/api/docs/guides/structured-outputs).

Collected runs capture the collection mutation signature under the same owner lock used by ingestion/erasure. Provider calls occur outside the transaction. The signature, profile and erasure fences are rechecked under that lock before saving; concurrent input changes return `409 input_changed`. Results are append-only successes; GET refreshes `freshness=current|stale` without model calls. Changes anywhere in a selected collection conservatively mark a result stale, even outside its filters. Profile version stays fixed during execution; creating a later version does not mutate older versions/results.

Erasing any dependent object/observation physically removes results for its affected collections, including results based on absence/counts and corrected observation chains. Scope erasure also deletes profiles referencing that scope; user erasure removes all private profiles/results, including inline-only profile settings. In-flight work cannot save behind an erasure fence or recreate an erased result. Profile deletion cascades its versions/results. Collected successes persist until explicit deletion; no automatic expiry is configured in this step. Backups/source queues/provider operational storage remain outside live application erasure.


## Durable execution, triggers and polling

Migration `d0a6b4c8e152` follows step 9 and adds `analysis_runs`, `analysis_triggers`, `analysis_tenant_usage` and a unique optional `analysis_results.run_id` association. It preserves existing profiles/results. Downgrade removes jobs, trigger configuration and quota history; it drops the run association while preserving result contents. Apply migrations before starting either process:

```sh
.venv/bin/alembic upgrade head
# Web process, as before:
.venv/bin/uvicorn app.asgi:create_app --factory --host 127.0.0.1 --port 8000
# Separate process, using the same configured PostgreSQL and model settings:
.venv/bin/python -m app.jobs.worker --concurrency 4 --poll-seconds 1
```

The worker fails startup if the job schema is unavailable. Keep it running under your process supervisor. `--concurrency` accepts 1..16 and `--poll-seconds` 0.1..30. `--once` drains work currently due, then exits; it leaves delayed retries and future scheduled work in PostgreSQL. No Redis, external broker, checkpointer or web-process task is required for durability. Multiple worker processes coordinate through the database; task objects in a worker only execute jobs already committed there.

New key grants: **`runs:write`** for durable submission/cancellation and **`runs:read`** for polling/listing. Submission also requires `ai:run` and `data:read`; trigger configuration requires these plus `profiles:write`. Result GET uses `results:read` + `data:read`; source grants always apply. Existing keys gain no permissions automatically. The compatible `POST /profiles/{id}/runs` continues to require `ai:run` (+ `data:read` for collections) and now shares tenant admission with background work.

### Submit and consume a result

```sh
curl -sS "$BASE_URL/v1/runs" \
  -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $USER_ID" \
  -H 'Content-Type: application/json' -d "{
    \"profile_id\":\"$PROFILE_ID\",
    \"input\":{\"version\":1},
    \"idempotency_key\":\"analysis-request-001\",\"mode\":\"async\"}"

# Read id from the accepted RunView:
curl -sS "$BASE_URL/v1/runs/$RUN_ID" \
  -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $USER_ID"
# When status=succeeded and result_id is non-null:
curl -sS "$BASE_URL/v1/results/$RESULT_ID" \
  -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $USER_ID"
```

For `search`/`query`, also pass `input.query`; optional explicit bounded query history is supported. The profile version is required and pinned at submission even if another version is registered before execution. `idempotency_key` is a printable non-blank string, at most 100 characters, scoped to tenant+user. The same key/profile/RunInput returns the same job and eventual result; different input conflicts with `409 run_idempotency_conflict`. `mode` is a transport preference and is not part of that computation identity. Erasure clears fingerprints and leaves terminal identities unusable for replay.

`mode=async` returns **202 RunView** after commit, without a provider call in the request. `mode=sync` may execute the admitted job immediately: it returns **200** for terminal state or **202** if work is queued, retrying, or lacks an available slot. Another worker may already own it, in which case poll its ID. The same run/status/result contract is used in either case. A succeeded run with `data_state=insufficient_data` is an execution success; inspect the separate result data state. GET routes never run AI.

The bundled independent client submits, polls and reads a structured result without importing service internals:

```sh
# Provide the inbound service key via this environment variable, not a CLI argument.
export PAIAGENT_SERVICE_KEY="$PAIAGENT_API_KEY"
.venv/bin/python -m app.integration.run_client \
  --base-url "$BASE_URL" --user-id "$USER_ID" \
  --profile-id "$PROFILE_ID" --version 1 --idempotency-key analysis-request-002
```

The client has a 120-second polling deadline; a timeout does not cancel the durable job. Poll again or explicitly cancel. For query/search add `--query '...'`. This client was exercised in a separate process through a real localhost HTTP socket and a worker with a fake provider; that verifies integration boundaries, not real AI quality.

| Route | Input / result |
| --- | --- |
| `POST /v1/runs` | `{profile_id,input:RunInput,idempotency_key,mode:async|sync}` → RunView, 202 or synchronous terminal 200. |
| `GET /v1/runs/{run_id}` | RunView with status, attempts/max_attempts, effective budget, lease expiry, availability/start/finish timestamps, input boundary/data-as-of, result ID and safe error code. Input text/history and lease token are never exposed. |
| `GET /v1/runs` | `cursor`, `limit=1..100`, optional `status` and `profile_id`; private authorized RunPage. |
| `POST /v1/runs/{run_id}/cancel` | 200 RunView. Pending/running becomes cancelled; terminal states remain unchanged. |
| `PUT /v1/profiles/{id}/triggers` | Full TriggerInput configuration with pinned version; cancels prior active trigger jobs and rotates its revision. |
| `GET /v1/profiles/{id}/triggers` | TriggerView; `enabled` is effective state, `config.enabled` is the submitted preference. `error_code` explains automatic disable. |
| `GET /v1/profiles/{id}/results` | Existing summary page additionally filters `version` and `data_state`; full payload remains a separate result GET. |

Run pages use exclusive UUID cursors, not chronological offsets or snapshot pagination. Lists can have short/empty pages with a next cursor after source filtering. Cross-owner/cross-tenant IDs are unavailable. Cancellation does not disable future trigger events or erase prior successful results; disable the trigger or use privacy erasure for those separate operations. Cancel-vs-save serializes under the owner lock: if save wins, cancellation returns succeeded; if cancellation wins, the attempt cannot persist output.

### Configure change and interval triggers

```sh
curl -sS -X PUT "$BASE_URL/v1/profiles/$PROFILE_ID/triggers" \
  -H "Authorization: Bearer $PAIAGENT_API_KEY" -H "X-User-ID: $USER_ID" \
  -H 'Content-Type: application/json' -d '{
    "version":1,"enabled":true,"on_change":true,
    "debounce_seconds":10,"max_wait_seconds":120,"interval_seconds":3600}'
```

There is one current trigger configuration per profile, pinned to an explicit version. Manual runs are always available with the corresponding grants. An enabled automatic configuration requires `on_change`, an interval, or both. Search/query profiles also need a non-blank trigger `query`; `limit` defaults to 5. Trigger-driven runs use the profile's collection selection and budgets; inline-only profiles cannot be scheduled. PUT with `{"version":1,"enabled":false}` disables automatic execution and cancels its active jobs. Reconfiguration cancels old trigger jobs, not independent manual runs.

On-change activation establishes a baseline of currently received data; it reacts to subsequent mutations. Submit a manual run if you also want immediate analysis of existing content. Ingestion already commits an outbox receipt in the same owner transaction as the data change. The scheduler reads the full mutation fingerprint and commits **job creation/coalescing + trigger cursor advancement atomically**. It does not rely on a callback from the web process. Queue pressure leaves the dirty cursor unadvanced, so the intent is retried later.

Multiple changes coalesce into at most one pending job per trigger, enforced by a PostgreSQL partial unique index. Debounce is 1..300 seconds (default 10); maximum wait is 1..600 seconds (default 120), at least debounce. Repeated changes defer a pending job only up to that first enqueue's maximum wait. A running attempt can have one pending successor. If data changed during that attempt, its stale save is rejected; retry yields to the pending successor when present. Saving a result does not create a source outbox receipt, preventing self-trigger loops.

`interval_seconds` is 60..2592000 (30 days). Optional `start_at` is an aware timestamp; otherwise the first tick is now+interval. This is an elapsed-time schedule aligned to that anchor, not a cron expression or a local wall-clock schedule. Missed ticks during downtime coalesce into one job; the next due time advances past now without drifting the anchor. If both time and change triggers are due, they share the same pending recalculation. Profile periods remain the explicit periods declared in that immutable version; scheduling does not invent a moving time window. Update the profile/trigger version if the selection must change.

A revoked key or removed required grant disables a trigger with `execution_forbidden`; explicitly reconfigure it using a valid authorized key after rotation. Effective enable state is returned separately from submitted config. Candidate polling rotates by last-check time in bounded batches; the default worker checks up to 50 configurations per scheduling pass.

### Recovery, budgets and fairness

Jobs transition `pending → running → succeeded|failed|cancelled`; retry and expired-lease recovery can return them to pending. Every claim gets a fresh random token and a **90-second lease**. Provider work executes outside database transactions. The whole attempt has an effective-profile deadline plus 10 seconds, at most 50 seconds under the default tenant policy; its provider phase is at most 40 seconds. A worker process killed without cleanup leaves the job recoverable after lease expiry. Recovery applies backoff and the attempt limit; no graph checkpoint is required.

Profile Budget now also accepts `max_attempts=1..3` (default 3, including the first attempt), `retry_seconds=1..60` (default 5), and `tool_calls=0`. No tools or commands can be enabled by configuration. Retries use exponential delay from retry_seconds, capped at 300 seconds. Provider/timeout/transient-storage/input-changed failures are retryable; invalid declarations, forbidden access and internal errors terminate. A crash consumes an attempt/reservation. Inline synchronous runs have one attempt and cannot be replayed by a worker because their input was never stored. Cancelled jobs cannot be resumed through retry.

At attempt start the worker records a data boundary and server data-as-of under the owner lock. Before save it checks source/fences, live key grants, lease/token, trigger revision and the mutation signature again. `409 input_changed` rejects output generated against a changing collection and requests a bounded retry. New on-change data can retain a successor independently of that active attempt, so an older success cannot clear a newer intent. A unique result association and one transaction for result insertion plus succeeded job prevent duplicate visible results when execution is repeated. Model requests themselves may be repeated after a crash; leases cannot revoke work already accepted by an upstream provider.

Tenant limits are server policy (`app/jobs/runtime.py`) and cannot be raised by a profile:

| Limit | Default server bound |
| --- | --- |
| Active jobs (pending + running) | 100 per tenant, shared across users/keys |
| Running attempts | 2 per tenant, across worker/API processes |
| Trigger configurations | 100 per tenant |
| UTC-day attempts / model-call reservations | 200 / 3200 per tenant |
| UTC-day input / output token reservations | 10000000 / 2000000 per tenant |
| Per-attempt rows / fragments | 5000 / 1000, further limited by profile |
| Per-attempt calls / input / output reservations | 16 / 128000 / 16000, further limited by profile |
| Provider phase / whole attempt / lease | 40s / at most 50s / 90s |

A short PostgreSQL admission lock serializes queue/quota accounting across processes. It is never held during generation. Claims use `FOR UPDATE SKIP LOCKED`, exclude saturated tenants and rotate eligible tenants by last-claim time. One tenant therefore cannot occupy every slot in the default four-slot worker. [PostgreSQL queue-locking semantics](https://www.postgresql.org/docs/current/sql-select.html). Actual throughput and multi-tenant latency still require load measurement at the intended deployment volume.

A profile budget is clamped to server maxima and persisted at admission; RunView exposes it. Reservations charge each entire admitted attempt's model/input/output allowance, even on failure or crash, rather than assuming unused provider budget can be refunded. Generation already uses conservative byte-based input reservations and complete per-call output reservations. Search calls can therefore reserve more than they consume. Tenant-day exhaustion postpones a queued job until the next UTC midnight (`daily_budget`); it does not call the provider. Active queue/concurrency overflow on immediate synchronous admission returns 429 with a safe code and retry header. Cancellation does not refund spent reservations. Different tenants have independent budgets and reserved concurrency.

### Authorization, retention and erasure

Each accepted job stores the authenticating key ID and its original scope/source upper bounds, never the raw secret. The worker intersects those bounds with the key's current live grants at claim, start and save; later grant expansion cannot broaden an accepted job. Profile version, owner, source restrictions and erasure fences remain authoritative. Scheduled work has the same checks. Worker database credentials are operator credentials and must remain private.

Durable jobs store their bounded collection query/history until explicit erasure/profile deletion; there is no automatic job-content expiry. Status/list endpoints never return that input. Explicit inline input remains supported only through the synchronous profile endpoint: its text, history, result and content fingerprint are not persisted. Content-free run metadata, grants, budget and timestamps are retained so it can participate in admission/accounting. The inline result ID still returns 404 on saved-result GET.

Object/observation erasure cancels affected collection jobs, clears their query/history/fingerprint, invalidates the lease and removes dependent results; this also covers results derived from absence, counts and corrected observations. Collection/source erasure cascades affected profiles, trigger configurations and jobs; user erasure removes all its private profiles/jobs, including inline run metadata. Tenant usage keeps only content-free reservation totals. An in-flight worker cannot save behind these changes. Historical request settings in a profile are separate private configuration; remove the profile when those settings themselves need erasure. Operational backups/provider retention remain outside live application cleanup.


## Managed memory, personalization and feedback

Migration `e1b7c5d9f263` follows `d0a6b4c8e152`. It adds `memory_settings`, `memory_messages`, `memory_items`, `memory_blocks`, `result_feedback` and `personalization_uses`; existing source data, profiles, jobs and results are preserved. Downgrade removes managed memory/feedback and its dependency metadata. Apply migrations before restarting web and worker processes. No additional dependency is required.

The following are separate objects with separate meanings:

| Object | Meaning |
| --- | --- |
| Collection records/events/documents | Source history/evidence, governed by the existing schema and ingestion lifecycle. A document is not a personal fact. |
| Query history / explicit user-message receipts | Conversation context or an explicitly submitted user statement. Query history and assistant answers are never automatically extracted into memory. |
| Preference | A versioned user assertion or a candidate confirmed by the caller. It guides presentation/advisory choices; it is not source evidence. |
| Observation | A confirmed projection from a declared event field, with explicit period, sample count 1, unit when mapped and source provenance. It is not an immutable personal trait or established causality. |
| Result feedback | An explicit assessment/decision about a result/proposal. Acceptance does not prove execution or benefit. Comments and AI outputs do not become preferences. |

Memory is off by default and needs **two explicit opt-ins**: owner memory settings and an immutable analysis profile. All memory belongs to tenant+X-User-ID and a registered collection, making source grants enforceable even for explicit preferences/messages. Data from matching user IDs in different tenants is never merged. New exact scopes are `memory:read`, `memory:write`, `feedback:read`, `feedback:write`; existing keys receive none automatically. Owner-wide settings/forget operations require an unrestricted source grant. Other operations enforce the addressed collection's source grant.

### Enable only declared collections, keys and fields

Read `GET /v1/memory/settings` first; its initial version is 0. Feedback or memory mutations can advance the owner-wide version, so always use the returned version when changing settings. An unrestricted administration key with `memory:write` can then submit:

```json
{
  "expected_version": 0,
  "enabled": true,
  "collections": ["MEMORY_COLLECTION_UUID"],
  "definitions": [{
    "key": "response_format",
    "value_schema": {"type":"string","maxLength":20,"enum":["brief","detailed"]},
    "message_prefixes": ["Response format: "]
  }],
  "field_rules": []
}
```

Send this to `PUT /v1/memory/settings`, using the standard Authorization and X-User-ID headers. Collection IDs must exist in the caller's tenant/source grants and not be erased. A dedicated preferences collection can be empty: explicit preferences/messages do not need a source record. Definitions use the existing bounded scalar schema subset (no objects, arrays, references or executable validators); string values are at most 500 characters. Null/missing values are not preferences; use DELETE to forget. Keys use the documented ASCII identifier pattern. There are at most 10 collections, 30 keys and 30 field rules.

For an actual schema-validated **user-authored record** such as `{"preferred_format":"brief"}`, an explicit field rule can be configured:

```json
{"collection_id":"MEMORY_COLLECTION_UUID","schema_version":1,
 "field":"/preferred_format","key":"response_format","kind":"preference","source_kind":"record"}
```

The field must be declared and scalar in that registered schema. The trusted backend is responsible for declaring that it represents the user's own statement; the service never guesses authorship from text. Do not configure book text or third-party statements as user-preference fields. Attached files, including their metadata deliveries, are rejected by memory extraction. To record user notes about a document, submit a separate user-authored record/message.

Event fields require `kind=observation, source_kind=event`. Extraction then needs an aware `[start,end)` period covering the event. The response retains that period, source/schema/object versions, field, unit if declared, `sample_count=1` and applicability limits. It does not infer a pattern or personality trait from one event. Observations are excluded from personalization unless the profile separately enables them; with a requested analysis/comparison period, their period must overlap one of those windows.

### Explicit preferences and confirmable candidates

`PUT /v1/memory/preferences` accepts:

```json
{"collection_id":"MEMORY_COLLECTION_UUID","key":"response_format",
 "value":"brief","expected_version":0,"restore":false}
```

`expected_version=0` means no current confirmed preference for that collection/key. For a correction, fetch the current confirmed preference and use its actual version. Stale writes return `409 memory_version_conflict`. One confirmed preference per owner/collection/key is enforced in PostgreSQL. Explicit corrections take priority over inferred candidates and increment the memory version; old personalized results become stale rather than silently changing their historical payload.

`POST /v1/memory/messages` explicitly records a bounded user-authored message and extracts only registered literal-prefix assignments:

```json
{"collection_id":"MEMORY_COLLECTION_UUID","external_id":"message-001",
 "author":"user","text":"Response format: brief"}
```

This is a deterministic, configurable assignment grammar, **not arbitrary natural-language inference**. Prefixes are exact, non-overlapping, single-line literals; text after the prefix must satisfy the declared scalar schema. Numeric/boolean assignments use JSON scalar syntax. Prefixes can be localized. Quoted/unmatched prose produces no candidate. Multiple conflicting assignments for one key in a message are returned in `ambiguous_keys` with no candidate for that key. `author=assistant` and unknown fields are rejected.

Message external IDs are owner+collection scoped. Exact replay returns the same candidate identity; different text under the same ID conflicts. Use versioned `PUT /v1/memory/messages/{id}` to edit the source, which invalidates its prior dependent memory, then creates new candidates. GET returns the explicitly retained user text. DELETE clears text/fingerprint and invalidates dependent memory; replay of that erased message identity is refused.

`POST /v1/memory/extractions` accepts `{"delivery_id":"UUID","period":null}` for allowed current record fields, or an explicit aware period for event observations. It reads only matching collection/schema/kind/field rules. Old record versions, corrected/erased observations and uploaded files cannot create personal-memory candidates. Reprocessing deduplicates by stable source/key identity. There are no extra model calls or hidden extraction on ingestion, file upload, query or generation.

All extracted items start as `state=candidate`. Inspect `GET /v1/memory/items?collection_id=...&status=candidate&limit=50`, then confirm intentionally:

```http
POST /v1/memory/items/{item_id}/confirm
Content-Type: application/json

{"expected_version":1}
```

Confirmation rechecks the current source, consent, version and deletion marker. A candidate that contradicts a confirmed preference returns `409 memory_conflict`; it cannot replace that preference. Use an explicit versioned preference correction to resolve the conflict. Same-value candidates merge into the existing confirmed preference. Ordinary source updates/corrections invalidate and clear dependent values; re-extraction of the new source produces candidates requiring confirmation again. Source/message history remains separate from the current memory record.

### Use a bounded context in a profile

Create a new immutable profile version with:

```json
"personalization": {
  "enabled": true,
  "collections": ["MEMORY_COLLECTION_UUID"],
  "keys": ["response_format"],
  "observations": false,
  "use_feedback": true,
  "max_items": 10,
  "max_chars": 4000
}
```

Personalization is supported for `query`, `analyze` and `recommend`. The profile must name non-empty unique memory collections and keys; these can differ from its data selections. Every referenced collection still needs current source grants. Owner settings must allow those collections/keys and be enabled. The profile's key order is its deterministic relevance priority; preferences and explicit assertions take precedence over observations. Only confirmed, current-source items enter the bounded context, at most 20 items/6000 serialized characters. The model is told that context is untrusted preference guidance and observations are period-bound; memory IDs are never allowed as factual source citations. Exact metrics continue to come exclusively from server computations over source data.

`memory:read` is required to supply values and `feedback:read` to apply feedback. Optional read rights are checked against the current live key and its captured source bounds. Missing optional rights do not grant access; a generation can proceed without that context. An initial memory read has a one-second deadline and a savepoint: errors/timeouts fall back to the base response with `personalization.status=unavailable`. No memory write is attempted after an answer. If context was actually used, its revision, versions and permissions must still match before commit; unsafe changes return `409 personalization_changed` (or a cancelled lease) for bounded retry. Such an uncommitted attempt is not a delivered answer.

The result envelope now includes `personalization` status, revision, memory IDs/versions, feedback coverage and suppressed recommendation count. It does not echo private memory values as source evidence. Saved results that used memory additionally require `memory:read` to retrieve; those that used feedback additionally require `feedback:read`. Unauthorized personalized summaries are omitted from lists. Normal corrections/policy changes mark previous personalized results stale. Explicit erasure physically removes results that depended on the erased memory, including their feedback.

Changes to opted-in managed context also participate in an on-change trigger's durable signature. Saving results creates no memory update, so it does not loop. Turning memory off may request one unpersonalized refresh; subsequent memory/feedback revisions while it remains off do not create personalization recalculations. Source-data triggers still operate normally. There is no automatic profile replacement, model training or guarantee that more retained data improves the user experience.

### Feedback with a concrete, verifiable effect

Submit feedback about an accessible stored result:

```http
POST /v1/results/{result_id}/feedback
Content-Type: application/json

{"request_id":"feedback-001","rating":"unhelpful",
 "decision":"rejected","claim_index":0,"comment":"This does not fit my current goal."}
```

`request_id` is an immutable owner-scoped idempotency key. A changed body under the same ID conflicts. Allowed ratings are `helpful|unhelpful`; decisions are `accepted|rejected|outdated`. `claim_index` addresses an existing payload claim; omission applies to all claims. At least one assessment, comment or explicit preference is required. `feedback:write` and data/source access are required. GET on that result's feedback is paginated and requires `feedback:read`; DELETE removes all feedback records for that result.

For recommendations, the **latest explicit decision per normalized proposal text within the same profile** controls repeat suppression. Normalization is Unicode NFKC, case folding and collapsed whitespace. `rejected` and `outdated` suppress that exact normalized text; a later `accepted` decision allows it again. The server filters generated recommendation claims even if the model repeats them. If every claim is withheld, the run succeeds with `data_state=insufficient_data` and a feedback explanation. This is exact-repeat control, not a claim of semantic/paraphrase detection. Other capabilities retain feedback but do not suppress factual outputs. The effect requires both personalization opt-ins and feedback permission.

An accepted proposal is **not** recorded as completed, executed, effective or a personal fact. A comment is not parsed into memory. If the caller explicitly provides a nested `preference` matching PreferenceWrite, it is validated as an independent user assertion and additionally needs `memory:write`, enabled owner settings, an allowed result/profile memory collection and the current preference version. Deleting feedback alone does not delete that separately asserted preference.

`GET /v1/feedback/profiles` reports a transparent relevance heuristic over the latest record per result/claim target: helpful+accepted minus unhelpful+rejected+outdated. It inspects at most 1000 records and reports `partial`. This is an aid for the integrating backend, not confidence or verified usefulness. Run APIs still require the caller's explicit profile ID/version; the score never silently switches profiles.

### Correct, disable and forget

| Operation | Effect |
| --- | --- |
| Versioned preference PUT | Explicit correction; does not let a candidate override it. |
| `PUT /memory/settings` with `enabled=false` | Stops future context use; keeps current private values for inspection. |
| New profile version with personalization disabled | Stops that profile's context/feedback use; explicit owner settings remain separate. |
| `DELETE /memory/items/{id}` | Clears value/provenance/period, leaves a tombstone and collection/key suppression marker, removes dependent results and cancels active personalized work. |
| `DELETE /memory/messages/{id}` | Clears retained text/fingerprint and invalidates dependent values; erased receipt cannot replay. |
| `DELETE /memory` | Owner-wide memory-only forget: clears values/messages, blocks all declared keys from automatic replay and disables memory. Requires an unrestricted source grant. Primary sources/dialogue and separate result feedback remain separately managed. |
| Existing source/collection/user data erasure | Clears/cascades dependent memory, messages, feedback/results/jobs; primary erasure fences prevent restoration. Memory-source collections are part of profile/job privacy boundaries. |

Deleting a candidate does not delete an independent explicit preference with the same key. Suppression markers prevent automatic re-extraction under that collection/key. A new explicit preference after deletion requires `restore=true` and the appropriate current confirmed version (0 if absent); that flag never authorizes background restoration of old values. Normal source/policy changes invalidate memory and mark affected results stale; erasure removes private dependent copies physically. The current value is versioned in place; old private values are not kept as a hidden memory history.

Retention is explicit: current memory, explicitly submitted user-message text and feedback persist until deletion; there is no automatic expiry. Deleted items retain opaque identity and collection/key suppression, not value/provenance. Erased messages retain their immutable receipt identity, not text/fingerprint. User/source scope erasure removes private memory configuration for that scope. Feedback cascades with its result. Backups/provider operational retention remain outside application live-data erasure.

Limits per owner: 2000 memory entries including tombstones, 1000 message receipts and 5000 feedback records; messages 8000 characters, memory strings 500, comments 1000. Hitting a storage bound returns `422 memory_limit`. Settings and memory API requests are bounded to 96 KiB with a 60-second request deadline. The generation context uses the existing profile/tenant budget; deterministic extraction adds no model calls. Examples with replaceable collection UUIDs are bundled in `documentation/examples/memory/`.

