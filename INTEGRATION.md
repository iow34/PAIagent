# PAIagent integration contract — step 6

Contract version **0.2.0**, API prefix **/v1**. This release adds a tenant-scoped registry and validation. It does not yet store general records/events or execute analysis profiles. The current document, search, conversation and document-knowledge APIs remain available.

## Install and run

Use the existing project virtual environment and configured `DATABASE_URL` / `OPENAI_API_KEY`:

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.asgi:create_app --factory --host 127.0.0.1 --port 8000
```

Migration `f3a6c9d2e710` adds `integration_sources`, `integration_collections`, `integration_schemas` and API-key grants. It preserves existing documents and keys. Run migrations before starting this code. Existing keys receive `legacy:*` and `catalog:read`; they do not gain registry administration rights. Downgrade removes registry data and grants, so it is only appropriate for an intentional rollback.

The schema registry needs PostgreSQL but does not make model calls. The existing application lifespan still expects the OpenAI configuration used by the document features.

Live documentation: `/schema`. Machine-readable OpenAPI: `/schema/openapi.json`. The served OpenAPI describes implemented routes only. `GET /v1/capabilities` reports implemented capabilities, formats, limits and caller grants. `GET /v1/integration-contract` serves the bundled `app/integration/contract.json`, including explicitly **planned** routes for imports, records, events, files, profiles, runs and results. Planned routes are not executable and are absent from live OpenAPI.

## Authentication and ownership

Every integration request includes:

```http
Authorization: Bearer <service-api-key>
X-User-ID: <user-authenticated-by-your-backend>
Content-Type: application/json
```

The key fixes the tenant; the trusted backend authenticates its own user and supplies `X-User-ID` (1–255 characters, no surrounding whitespace or control characters). Client bodies cannot supply tenant/user ownership or `received_at`. No browser-facing secret key is intended.

Sources, collections and schema definitions are **tenant-shared configuration**. They must not contain private user content in names, descriptions or enum values. A user ID is required consistently, but does not make a shared schema private. Registry access never grants access to stored user content. Existing document/search/query/knowledge operations still use `tenant_id + user_id`. General records/events will use the same ownership boundary in step 7.

Administrative issuance for an existing tenant:

```sh
.venv/bin/python -m app.cli.create_api_key \
  --tenant-id "$TENANT_ID" --name integration-admin \
  --scope catalog:read \
  --scope sources:read --scope sources:write \
  --scope collections:read --scope collections:write \
  --scope schemas:read --scope schemas:write \
  --scope data:validate
```

The command prints the secret once; only its SHA-256 hash is stored. Existing tenant provisioning is unchanged. Each `--scope` is an exact grant, with no implicit inheritance. To retain document functionality, explicitly add `--scope 'legacy:*'`. Omitting all `--scope` flags keeps the old CLI behavior (`legacy:*`, `catalog:read`). Unknown scopes are rejected; future profile/run/data-write scopes are not issuable yet.

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

A duplicate registry name/version returns 409, not a silent update. After a lost POST response, GET/list the resource and compare its definition before proceeding. Registry POST does not implement `Idempotency-Key`. Reliable batch retries are part of step 7.

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

IDs have 1–255 characters without surrounding whitespace/control characters. Timestamps require an offset. `occurred_at` describes the external occurrence; future ingestion will assign `received_at` in UTC. A semantic event-time field describes a time within the payload and need not equal the envelope timestamp. Future ordering uses object versions, not arrival order. Observations do not implicitly modify any state record.

`/validate` checks current grants, schema existence, operation semantics and payload. It returns `{"valid":true,"schema_kind":"record","schema_version":1,"persisted":false}`. It does not assign `received_at`, reserve event IDs, guarantee later ingestion, index data, or delete anything. The same operation may become unauthorized before a later write. A delete envelope still requires an existing record schema version.

## Limits and errors

Registry HTTP bodies: **96 KiB**. Compact UTF-8 schema JSON: **32 KiB**. Compact UTF-8 payload JSON: **64 KiB**. HTTP limits include whitespace; inner limits use compact serialization. At most 100 field mappings. Existing document uploads remain UTF-8 TXT/Markdown, at most 1 MiB.

The registry uses [Litestar's rate-limit middleware](https://docs.litestar.dev/main/usage/middleware/builtin-middleware.html): **120 requests/minute per peer IP, per application process**, shared across registry routes. Failed requests count too. `X-Forwarded-For` does not change this identity. Behind a reverse proxy, its peer IP is shared; deployment-wide/key/tenant quotas require later infrastructure. `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` and `RateLimit-Policy` describe the current window. On 429, wait at least `RateLimit-Reset` seconds and add jitter. Multiple workers do not share this in-memory quota.

Errors preserve Litestar's `status_code`, `detail`, `extra` structure. Domain errors add `extra.code` and optionally `extra.issues`; framework errors can have `extra: null` or framework validation details, so clients must always handle the HTTP status. Payload issues include paths and failed keywords, without echoing private values. Up to 20 payload issues are returned.

- 400: malformed JSON/DTO, invalid parameter, unknown/repeated query parameter, unknown DTO field.
- 401: missing/invalid/inactive key; missing/invalid `X-User-ID` is a 400 input error.
- 403: insufficient operation/source grant.
- 404: missing or foreign-tenant registry resource.
- 409: duplicate name or immutable schema version (also enforced during concurrent POSTs).
- 413: HTTP body too large; inner schema/payload limits return 422 with `size_limit`.
- 422: unsupported/invalid schema, incompatible mapping, invalid envelope or payload.
- 429: registry request frequency exceeded.
- 500: storage/unexpected failure; private database exception text is not returned by registry services.

New required fields, removed fields or changed meanings require a new API major version; adding an optional response field can be compatible. Clients should ignore new response fields, while request/configuration fields are checked strictly. Schema versions are independent of API and future profile versions.

## Verification and continuation

Run the project regression suite with an isolated PostgreSQL/pgvector cluster:

```sh
.venv/bin/python tests/run_postgres_tests.py --all
```

Tests cover schema/envelope boundaries, real authenticated HTTP and PostgreSQL, two tenants, two users of one tenant, narrow grants, forged source/collection IDs, concurrent version registration, migration rollback/upgrade with a pre-existing key, pagination, body/rate limits and the three-domain quickstart over a real local TCP server. Same-tenant schema visibility is intentional. Existing document tests verify user-content isolation; general record storage is not implemented yet.

The suite makes no real OpenAI requests. Model quality is outside this step. The implementation was validated on a temporary database; applying the migration to the configured working database is a separate deployment action.

`docs/STEP6_PROGRESS.md` records implementation checkpoints, exact verification results and resumption instructions. Existing `.gitignore` rules still exclude `docs/` and `tests/`; this root guide, the JSON contract, examples and client are regular deliverable files. No commit, push or deployment is implied by saving them.
