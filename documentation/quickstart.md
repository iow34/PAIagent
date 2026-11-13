# End-to-end quickstart

First follow [installation and key issuance](operations.md). Run the API and worker. Install `httpx` in your client environment (already included in server requirements). Keep the service key in an environment variable, never in CLI arguments:

```sh
export PAIAGENT_URL=http://127.0.0.1:8000
export PAIAGENT_USER_ID=demo-user
# Set PAIAGENT_SERVICE_KEY securely to the issued PAIagent service key.
.venv/bin/python documentation/examples/quickstart.py
```

This independent Python process imports no server services. It uses HTTP to:

1. Create a fresh source and the tasks, books and learning-session collections.
2. Register immutable record/event schemas and semantic mappings.
3. Validate and persist snapshots and an independent event; replay each identical envelope to demonstrate deduplication.
4. Attach a Markdown file to a book object using multipart `file` and JSON `envelope` parts.
5. Create a search profile pinned to schema version 1; submit `/v1/runs` and replay the same body/idempotency key.
6. Poll pending/running until terminal and read the typed result using `result_id`.
7. Configure both an on-change trigger and an hourly trigger, then deliver a new selected record version or an independent event. Page profiles with `limit=1`.

The default search uses JSON lexical evidence and does not index the uploaded file or call a model. It verifies protocol integration without generation cost. Each invocation creates another demo source, and the enabled trigger can create later runs. Keep the printed IDs to inspect or clean up this data. Output includes demo payloads; do not use this stdout pattern with private production content.

To call another capability with the same schema/profile API:

```sh
.venv/bin/python documentation/examples/quickstart.py --capability analyze --mode sync
.venv/bin/python documentation/examples/quickstart.py --capability query
.venv/bin/python documentation/examples/quickstart.py --capability summarize
.venv/bin/python documentation/examples/quickstart.py --capability extract
.venv/bin/python documentation/examples/quickstart.py --capability recommend
```

Generation modes use your configured provider and can consume tokens. The same HTTP script supports all six registered templates. `analyze` computes exact declared metrics server-side and grounds generated commentary in them. Profile minimum samples and evidence bounds can produce `partial` or `insufficient_data`; do not equate HTTP success with sufficient evidence. Read the result's `payload`, `sources`, `coverage`, `limitations` and `freshness`. See [result semantics](client-behavior.md) and [full reference](reference.md).

The same client can bind a profile to another domain using ordinary declarations from [domain-bindings.json](examples/domain-bindings.json):

```sh
.venv/bin/python documentation/examples/quickstart.py --domain books --capability search
.venv/bin/python documentation/examples/quickstart.py --domain learning_sessions --capability analyze
```

Books select record fields and page quantities; learning sessions select event fields in history mode and elapsed seconds. These bindings change only the client-supplied profile and delivery envelope. No server files are edited.

## Inspect and consume with curl

The commands below use the IDs printed by the script. `jq` is only a local JSON selection tool.

```sh
curl --fail-with-body -sS "$PAIAGENT_URL/v1/capabilities" \
  -H "Authorization: Bearer $PAIAGENT_SERVICE_KEY" -H "X-User-ID: $PAIAGENT_USER_ID"
curl --fail-with-body -sS "$PAIAGENT_URL/v1/collections/$COLLECTION_ID/schemas/record/1" \
  -H "Authorization: Bearer $PAIAGENT_SERVICE_KEY" -H "X-User-ID: $PAIAGENT_USER_ID"

# Generate KEY once; retain it and the exact request body across retries.
KEY=demo-manual-run-1
jq -n --arg profile "$PROFILE_ID" --arg key "$KEY" \
  '{profile_id:$profile,input:{version:1,query:"звіт"},idempotency_key:$key,mode:"async"}' | \
  curl --fail-with-body -sS "$PAIAGENT_URL/v1/runs" \
    -H "Authorization: Bearer $PAIAGENT_SERVICE_KEY" -H "X-User-ID: $PAIAGENT_USER_ID" \
    -H 'Content-Type: application/json' --data-binary @-

curl --fail-with-body -sS "$PAIAGENT_URL/v1/runs/$RUN_ID" \
  -H "Authorization: Bearer $PAIAGENT_SERVICE_KEY" -H "X-User-ID: $PAIAGENT_USER_ID"
# When status=succeeded, read result_id from the response:
curl --fail-with-body -sS "$PAIAGENT_URL/v1/results/$RESULT_ID" \
  -H "Authorization: Bearer $PAIAGENT_SERVICE_KEY" -H "X-User-ID: $PAIAGENT_USER_ID"
```

For manual registry, snapshot, event, import, file/CSV, profile, trigger, memory, feedback and erasure requests, use the copyable curl/JSON examples in the [reference](reference.md). Dynamic payloads must pass the registered collection schema; no tenant/user ownership fields are accepted in the envelope. A complete request/response contract is downloadable as [OpenAPI](openapi.json) and [semantic contract](contract.json).

## Automatic result and cleanup

After the post-trigger selected-data mutation, poll `GET /v1/runs?profile_id=PROFILE_ID`. A run with `reason=change` should appear after the debounce and worker processing; hourly runs have `reason=schedule`. Read each successful `result_id` through the same result endpoint. Overlapping changes coalesce. The manual result becomes stale after the mutation; an automatic run reads the new boundary. Polling is the implemented delivery mechanism; no webhook or streaming result transport is promised.

Disable the demo trigger before leaving it unattended:

```sh
curl --fail-with-body -sS -X PUT "$PAIAGENT_URL/v1/profiles/$PROFILE_ID/triggers" \
  -H "Authorization: Bearer $PAIAGENT_SERVICE_KEY" -H "X-User-ID: $PAIAGENT_USER_ID" \
  -H 'Content-Type: application/json' -d '{"version":1,"enabled":false,"query":"звіт"}'
```

Use the source/user erasure endpoint documented in the reference to remove demo content, or delete the demo profile to remove its trigger and dependent results. Registry metadata is tenant-shared and has no delete endpoint; use neutral demo names. Erasure fences intentionally prevent silent replay into a forgotten identity.
