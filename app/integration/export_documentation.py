"""Generate public assets without database access or provider calls.

Run after API/guide changes; --check fails when committed assets are stale.
"""

import argparse
import json
import re
from html import escape
from pathlib import Path

from litestar.serialization import encode_json
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "documentation"


def artifacts() -> dict[Path, str]:
    from app.asgi import create_app

    app = create_app()
    result = {}
    result[PUBLIC / "openapi.json"] = (
        json.dumps(
            json.loads(encode_json(app.openapi_schema.to_schema())),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    result[PUBLIC / "contract.json"] = (
        ROOT / "app/integration/contract.json"
    ).read_text()
    for source, dest in [
        ("examples", "domains"),
        ("profile_examples", "profiles"),
        ("memory_examples", "memory"),
    ]:
        for path in sorted((ROOT / "app/integration" / source).glob("*.json")):
            result[PUBLIC / "examples" / dest / path.name] = path.read_text()
    rows = [
        "# Route index",
        "",
        "Generated from the running application's handlers. Use [OpenAPI](openapi.json) for request/response types, and [the reference](reference.md) for dynamic payload validation, ownership, and additional grants.",
        "",
        "The scope below is the handler's primary grant; composite operations can also require data, AI, memory or feedback grants. Source restrictions always apply. Legacy endpoints use `legacy:*`.",
        "",
        "| Method | Path | Primary scope |",
        "| --- | --- | --- |",
    ]
    for route in app.routes:
        if not route.path.startswith(("/v1/", "/health")):
            continue
        for method, value in sorted(getattr(route, "route_handler_map", {}).items()):
            if method in {"OPTIONS", "HEAD"}:
                continue
            scope = value[0].opt.get(
                "required_scope",
                "legacy:*" if route.path.startswith("/v1/") else "public",
            )
            rows.append(f"| {method} | `{route.path}` | `{scope}` |")
    result[PUBLIC / "routes.md"] = "\n".join(rows) + "\n"
    guides = {p: p.read_text() for p in PUBLIC.glob("*.md")}
    guides.update({p: s for p, s in result.items() if p.suffix == ".md"})
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
    for path, content in guides.items():
        body = markdown.render(content)
        body = re.sub(r'href="([^"#:]+)\.md(?=[#"])', r'href="\1.html', body)
        title = escape(content.splitlines()[0].lstrip("# "))
        html = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        html += f"<title>{title}</title><style>body{{font:17px/1.6 system-ui;max-width:1050px;margin:2rem auto;padding:0 1rem}}pre{{overflow:auto;background:#f3f4f6;padding:1rem}}table{{border-collapse:collapse;display:block;overflow:auto}}td,th{{border:1px solid #bbb;padding:.5rem}}a{{color:#125ac5}}code{{font-size:.9em}}</style>"
        html += (
            '<nav><a href="index.html">Developer guide</a> · <a href="/schema">Interactive API</a> · <a href="openapi.json">OpenAPI download</a></nav><main>'
            + body
            + "</main></html>\n"
        )
        result[
            PUBLIC / ("index.html" if path.name == "README.md" else path.stem + ".html")
        ] = html.replace('href="README.html"', 'href="index.html"')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for path, content in artifacts().items():
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if stale:
        parser.exit(1, "Stale documentation assets: " + ", ".join(stale) + "\n")


if __name__ == "__main__":
    main()
