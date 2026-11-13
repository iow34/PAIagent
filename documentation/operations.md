# Installation and operations

Run commands from the repository root. The verified development environment is Python 3.14.7, PostgreSQL 18.4 with pgvector 0.8.6. These are tested versions, not a claim that other versions are incompatible. Install Python, PostgreSQL and the pgvector server extension before proceeding. This repository does not provision a PostgreSQL server, TLS ingress or a production deployment.

Create a database and an owner role using your PostgreSQL administrator connection (choose your own strong password):

```sql
CREATE ROLE paiagent LOGIN PASSWORD 'replace-password';
CREATE DATABASE paiagent OWNER paiagent;
```

Install the `vector` extension into the new database using an administrator connection:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Then configure the application:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env locally: DATABASE_URL and OPENAI_API_KEY are required.
.venv/bin/alembic upgrade head
.venv/bin/alembic current
```

Never overwrite an existing `.env`. Its DATABASE_URL must address the intended database, with credentials URL-encoded. The database role needs migration privileges on its application schema. The expected migration head for this release is `e1b7c5d9f263`. Back up existing data and rehearse migration on a restored copy before upgrading. Downgrade removes feature data; do not use it as an erasure recovery mechanism. `.env.example` lists optional timeout/retry overrides. The embedding model/dimension are fixed to `text-embedding-3-small`/1536; configure a generation model accessible to your provider account if the configured default is unavailable.

Provision a fresh tenant and a development integration key:

```sh
TENANT_ID=$(.venv/bin/python -m app.cli.create_tenant --name local-integration)
.venv/bin/python -m app.cli.create_api_key --tenant-id "$TENANT_ID" --name local-backend \
  --scope catalog:read --scope sources:read --scope sources:write \
  --scope collections:read --scope collections:write --scope schemas:read --scope schemas:write \
  --scope data:validate --scope data:read --scope data:write --scope data:delete \
  --scope profiles:read --scope profiles:write --scope ai:run \
  --scope runs:read --scope runs:write --scope results:read \
  --scope memory:read --scope memory:write --scope feedback:read --scope feedback:write
```

Each tenant command creates a new UUID; names are labels, not idempotency keys. Keep the UUID. The key command prints a secret once, storing only a hash. Transfer it to your backend secret store; do not paste it into source control or frontend code. This broad development grant supports the whole tutorial. For deployed integrations issue narrower keys, optionally `--source-id UUID` for each permitted existing source. All scopes are exact: `data:write` does not grant reads or deletes. Omitted scopes default to legacy access only. To use old document/conversation routes, explicitly grant `--scope 'legacy:*'`; it cannot be combined with source restrictions.

Start the API and, in another terminal, the worker with the same configuration:

```sh
.venv/bin/uvicorn app.asgi:create_app --factory --host 127.0.0.1 --port 8000
```

```sh
.venv/bin/python -m app.jobs.worker --concurrency 4 --poll-seconds 1
```

The worker includes the scheduler; no separate scheduler process, Redis or broker is required. `--once` drains work currently due and exits, so it is not a persistent scheduler. The API alone accepts background work but does not continuously process it. Serve `/developer/` and `/schema` from the API host; do not publish `.env`, ignored internal `docs/`, database dumps or private logs with documentation.

## Recovery and diagnostics

`GET /health` is a process liveness check. It is not a database, worker or provider readiness guarantee. Inspect migration head, worker process health, and authenticated run status separately. A stopped worker leaves runs pending. Restart workers against the same database; expired 90-second leases can be reclaimed, with bounded attempts and saved result deduplication. Do not manually duplicate runs to recover them: retain the original idempotency key and poll the returned run ID. A timeout in your client does not cancel a run; cancellation is an explicit API request.

Use source/collection IDs, external/event IDs, envelope `metadata.correlation_id`, run ID, result ID and trigger revision as correlation handles. Run status exposes attempts, lease, timestamps and `error_code`; results expose `usage`, data coverage, limitations and freshness. Token counts and reserved budgets are available; monetary cost accounting is not implemented. See [client behavior](client-behavior.md). Inspect status through authorized requests, rather than logging request bodies. Configure your hosting logs to exclude Authorization, payloads, messages, file text, signed credentials and response content. No central tracing dashboard is bundled.

A 403 indicates missing scope/source grants. A 404 may reflect isolation or erasure, not just a typo. A failed run with a provider error requires checking the configured model/access/limits; repeated submissions are not a fix. For invalid input inspect the 422 `extra.code` and issues. Preserve safe IDs when escalating, without private text or keys. TLS, process supervision, backups and production monitoring remain deployment responsibilities.

## Verification

```sh
.venv/bin/python -m app.integration.export_documentation --check
.venv/bin/python -m unittest discover -s tests -p test_documentation.py
.venv/bin/python tests/run_postgres_tests.py --all
```

The PostgreSQL runner creates and removes a temporary `_test` database cluster; it requires local PostgreSQL binaries and pgvector. It does not migrate the configured application database. Onboarding tests use real TCP HTTP and fake providers. They verify the integration protocol, not real model quality, throughput or production recovery guarantees. Test sources live in the local ignored `tests/` directory; include that suite separately if you want CI on another checkout. Documentation and executable client examples are tracked.
