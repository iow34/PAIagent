# Verified response examples

[Download the JSON examples](examples/responses.json). These are responses captured from the documented standalone clients over a real local HTTP server and temporary PostgreSQL/pgvector database. The database and providers were isolated test fixtures; generation used a fake provider. IDs/timestamps identify disposable demo resources and must be replaced in your integration. Generated wording illustrates the wire format; it is not a model-quality result.

The first six entries contain a profile, first-delivery/replay acknowledgements, the typed result and trigger for search, query, summarize, extract, analyze and recommend. Each client submitted the same run body twice and verified one run ID. The worker also completed a subsequent `reason=change` run and its result was fetched through HTTP.

The next entry contains real settings, explicit preference, feedback, forgetting and disabled-settings responses. The following error/erasure entry shows actual HTTP 422 invalid schema, 422 memory disabled, 409 changed feedback replay, and a successful record erasure followed by GET 404. The final two entries demonstrate a books search profile and a learning-session event analysis profile using the same endpoints. These complement the request bodies in the [reference](reference.md) and [quickstart](quickstart.md).

Validate an application result against `capability_contract.result_json_schema`, then validate `payload` against `capability_contract.payload_json_schemas[capability]` from the [contract](contract.json). For `extract`, also apply the particular profile's `output_schema` to `payload.data`. Registered ingestion payloads use the collection's `GET /v1/collections/{collection_id}/schemas/{kind}/{version}` response; profile/request schemas do not replace that application schema.

## Memory and feedback walkthrough

Use a fresh demo user and the tasks collection/result printed by the quickstart. This script refuses to overwrite pre-existing owner settings, enables a declared `response_format` preference, writes it explicitly, posts and replays helpful feedback, forgets the preference and disables memory again. It reads the current settings version before disabling, because preference/feedback/forget operations can advance the owner revision. It does not enable a profile's separate personalization opt-in.

```sh
# Same PAIAGENT_URL / PAIAGENT_SERVICE_KEY / PAIAGENT_USER_ID as quickstart.
export PAIAGENT_COLLECTION_ID=your-tasks-collection-uuid
export PAIAGENT_RESULT_ID=your-result-uuid
.venv/bin/python documentation/examples/memory_feedback.py
```

The [script](examples/memory_feedback.py) uses the same standalone HTTP helper and stores no credentials. It intentionally leaves a forgotten-preference tombstone and the explicit helpful-feedback receipt. For actual personalized execution, configure a new profile version with `personalization.enabled=true`, its allowed collections/keys and the relevant grants, as shown in the [full reference](reference.md). Feedback never executes an external task.

When updating public response examples, record only disposable fixture data. Never copy a production response containing private payloads, message text, secrets or ownership details into this directory.
