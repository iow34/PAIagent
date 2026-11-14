# PAIagent developer documentation

PAIagent is a backend API for bringing your application's records, events and supported files into a shared evidence layer, then running six bounded AI capabilities through versioned declarative profiles. API `/v1`, contract **0.7.0**. Send requests from a trusted backend with a service key and your authenticated user ID.

Start here:

1. [Install, provision access and operate the service](operations.md).
2. [Complete the end-to-end quickstart](quickstart.md).
3. [Find an endpoint and its primary scope](routes.md), inspect [OpenAPI](openapi.json), then use the [integration reference](reference.md) for full semantics and request examples.
4. [Interpret results, errors, limits and lifecycle](client-behavior.md).
5. [Inspect verified success/error responses and memory walkthrough](response-examples.md).
6. [Add a domain or evolve an integration](extensions.md).

The [complete reference](reference.md) covers registry and schema validation, snapshots/events, imports and reconciliation, erasure, files and CSV, context/search, all six capabilities, profiles, durable runs, triggers, managed memory, personalization and feedback. [Machine-readable contract](contract.json) includes capability-specific payload schemas and examples; registered application payloads use the collection's immutable schema endpoint, because OpenAPI cannot enumerate schemas created by your backend at runtime.

Example assets:

- [Tasks](examples/domains/tasks.json), [books](examples/domains/books.json), and [learning sessions](examples/domains/learning_sessions.json): complete collection/schema/mapping/payload definitions using the same APIs.
- [Standalone HTTP client](examples/client.py) and [runnable onboarding](examples/quickstart.py).
- [Search](examples/profiles/search.json), [query](examples/profiles/query.json), [summarize](examples/profiles/summarize.json), [extract](examples/profiles/extract.json), [analyze](examples/profiles/analyze.json), [recommend](examples/profiles/recommend.json) profile templates. Replace collection UUIDs; templates target the tasks schema.
- [Memory settings](examples/memory/settings.json) and other files in `examples/memory/`: replace collection/result/item IDs before use. See the memory section of the reference for the required schemas, opt-in and grants.

The API serves the static HTML and downloadable assets in this directory at **`/developer/`**, interactive docs at **`/schema`**, and live OpenAPI at **`/schema/openapi.json`**. Markdown links work in GitHub. A source checkout remains the installation unit; no separate published Python package or external documentation host is assumed.

After changing handlers, contract, fixtures or Markdown, run:

```sh
.venv/bin/python -m app.integration.export_documentation
.venv/bin/python -m app.integration.export_documentation --check
```

These commands need neither a database nor provider credentials. Exported OpenAPI, route index, fixture copies and HTML must ship together with the corresponding code. They contain no live customer data or secrets.
