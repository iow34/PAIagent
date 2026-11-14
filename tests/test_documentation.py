"""Published guide assets, downloads and independent HTTP client retry behavior."""

import importlib.util
import json
import unittest
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import httpx
from litestar.testing import TestClient

from app.asgi import create_app
from app.integration.export_documentation import PUBLIC, artifacts

spec = importlib.util.spec_from_file_location(
    "example_client", PUBLIC / "examples/client.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Client = module.Client


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(v for k, v in attrs if k == "href")


class DocumentationTests(unittest.TestCase):
    def test_assets_are_current_and_links_resolve(self):
        for path, content in artifacts().items():
            self.assertEqual(path.read_text(), content, str(path))
        for path in PUBLIC.glob("*.html"):
            parser = Links()
            parser.feed(path.read_text())
            for link in parser.links:
                url = urlsplit(link)
                if url.scheme or not url.path or url.path.startswith("/"):
                    continue
                self.assertTrue(
                    (path.parent / unquote(url.path)).exists(), (path, link)
                )

    def test_public_docs_and_live_schema_match_without_private_files(self):
        # Preserve actual route handlers, but remove configured production lifespans.
        from litestar import Litestar
        from litestar.static_files import create_static_files_router

        app = Litestar(
            route_handlers=[
                create_static_files_router(
                    "/developer",
                    directories=[str(PUBLIC)],
                    html_mode=True,
                    resolve_symlinks=False,
                )
            ],
            openapi_config=create_app().openapi_config,
        )
        with TestClient(app) as client:
            for path in (
                "/developer/",
                "/developer/quickstart.html",
                "/developer/openapi.json",
                "/developer/examples/quickstart.py",
            ):
                self.assertEqual(client.get(path).status_code, 200, path)
            for path in (
                "/developer/%2e%2e/.env",
                "/developer/%2e%2e/docs/PLAN.md",
                "/developer/.env",
            ):
                self.assertIn(client.get(path).status_code, (400, 404))
        # Export is exactly the live OpenAPI encoding, including dynamic schema links.
        from litestar.serialization import encode_json

        self.assertEqual(
            json.loads((PUBLIC / "openapi.json").read_text()),
            json.loads(encode_json(create_app().openapi_schema.to_schema())),
        )

    def test_recorded_responses_match_public_schemas(self):
        from jsonschema import Draft202012Validator

        contract = json.loads((PUBLIC / "contract.json").read_text())[
            "capability_contract"
        ]
        samples = json.loads((PUBLIC / "examples/responses.json").read_text())
        names = set()
        for sample in samples:
            if "capability" not in sample:
                continue
            names.add(sample["capability"])
            result = sample["result"]
            Draft202012Validator(contract["result_json_schema"]).validate(result)
            Draft202012Validator(
                contract["payload_json_schemas"][sample["capability"]]
            ).validate(result["payload"])
        self.assertEqual(
            names, {"search", "query", "summarize", "extract", "analyze", "recommend"}
        )

    def test_retry_is_bounded_and_only_for_replay_safe_requests(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(503 if len(calls) == 1 else 200, json={"ok": True})

        client = Client("http://test", "secret", "alice")
        client.http.close()
        client.http = httpx.Client(
            transport=httpx.MockTransport(handle), base_url="http://test"
        )
        with patch.object(module.time, "sleep"):
            self.assertEqual(
                client.request(
                    "POST",
                    "/v1/runs",
                    replay_safe=True,
                    json={"idempotency_key": "same"},
                ),
                {"ok": True},
            )
        self.assertEqual(calls[0].content, calls[1].content)
        calls.clear()
        with self.assertRaises(httpx.HTTPStatusError):
            client.request("POST", "/v1/sources", json={"name": "fresh"})
        self.assertEqual(len(calls), 1)
        client.close()

    def test_empty_page_with_cursor_is_followed(self):
        values = iter(
            [
                {"items": [], "next_cursor": "next"},
                {"items": [{"id": "one"}], "next_cursor": None},
            ]
        )
        client = Client("http://test", "secret", "alice")
        with patch.object(client, "request", side_effect=lambda *a, **kw: next(values)):
            self.assertEqual(list(client.pages("/v1/profiles")), [{"id": "one"}])
        client.close()
