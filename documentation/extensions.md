# Extending and upgrading an integration

Most applications need configuration and a source-side adapter, without editing PAIagent server files. The three shipped domain examples deliberately use different field names and an event-only collection; all use the same registry, ingestion, profile and run routes.

| Change | Where it belongs |
| --- | --- |
| New task/book/sensor/study domain; rename fields; select evidence; set goals, periods, metrics or budgets | Register collection schemas/mappings, then declare immutable profiles through HTTP. |
| Your source timestamps, identifiers, CSV columns, units, outbox and user authentication | Convert in your integrating backend. Preserve stable external/event IDs and explicit source versions. |
| New binary format, parser, unsupported computation, new provider behavior or tool | Implement and test a server adapter, then update catalogue/contract/docs. A profile is not executable Python or arbitrary graph code. |

## Add your own collection

Choose record snapshots for mutable state and independent events for observations. Declare a bounded `paiagent-1` schema with `additionalProperties:false`, string/array bounds, explicit nullability and version. Register semantic field roles using JSON Pointer scalar paths. `quantity` fields need explicit unit labels; convert units before delivery. `search_text` decides what text enters retrieval. Map `display_name`, category and event time where meaningful.

Validate with `/collections/{id}/validate` before persisting. Send typed payloads and exact version in every envelope; maintain source-side versions and a durable delivery outbox. Declare reconciliation/import completeness honestly. An event's elapsed seconds and a record's current status express different facts; do not turn one into the other without a documented source transformation.

Create a profile with the relevant collection ID, schema version, selected fields, kind/current/history mode, filters and evidence bounds. For learning sessions select event fields `/subject`, `/started`, `/elapsed`, `/reflection` with `kind:event` and `mode:history`; for books choose `/heading`, `/pages`, `/isbn` (arrays are not scalar analysis selections). Numeric metrics operate on registered quantity mappings; history represents versions/events, not automatically a state-duration calculation. Search needs a mapped search-text field; include supported indexed file text explicitly when desired.

The tasks, books and learning-session fixtures are examples, never special server keywords. Do not add `if collection.name == "books"` to a controller to support a new domain. New configuration should be discoverable/readable through the registry and profile APIs and callable by an independent client.

## Versioning and upgrades

`/v1` is the API family; `contract_version` tracks this implemented release's capability contract. Application record/event schemas and profiles each have their own explicit integer versions. Unknown body fields, unsupported schema keywords, and unknown/repeated query parameters are rejected. Do not depend on silent ignoring, coercion, default injection or an implicit latest version.

For a schema change: register a new immutable schema version, transform and send new object versions under it, then create a new profile version pinned to that schema. Existing deliveries/results retain their versions; changing configuration does not rewrite them. Read current memory-setting/preference versions before updates. Reconfigure a trigger explicitly when moving to another profile version; inspect the trigger revision and accepted run version. Old run idempotency keys must not be repurposed for a changed body.

Before a server release, back up data, rehearse migrations and client tests against the new OpenAPI/contract, compare catalogue availability/limits, regenerate public assets and ship them with that same code. Use the live `/schema/openapi.json` and `/v1/integration-contract` to detect a mismatched installed release. An API path/version alone does not promise identical future capabilities or budgets. For an incompatible request/response change, introduce an explicit compatible migration path or a new API family; never silently reinterpret stored payloads.

## Adding a server adapter

Keep parsing, SQL, authorization, storage, exact arithmetic and source isolation in ordinary services. The capability workflow is a fixed bounded graph, selected by the registered capability; it cannot execute user-authored code or unbounded tools. A new parser must preserve provenance/positions, bounds, timeout isolation, replay fingerprints and deletion behavior. A new computation needs typed inputs/outputs, units/null/period semantics and exactness tests where numeric correctness matters. A provider adapter must respect generation budgets, validate outputs and source references, and handle lifecycle revocation before saving results.

Update the catalogue, semantic contract, fixture examples and exported documentation together. Verify isolation, limits, replay, migrations and erasure in a temporary database. Real model quality, semantic support, injection resistance and usefulness require separate evaluation; fake-provider integration checks establish protocol behavior only.
