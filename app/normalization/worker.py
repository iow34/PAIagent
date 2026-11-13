"""Isolated parser process: JSON protocol, no URLs or application credentials."""

import base64
import json
import resource
import sys

from app.normalization.adapters import extract


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    try:
        request = json.load(sys.stdin)
        value = extract(
            base64.b64decode(request["data"], validate=True), request["extension"]
        )
        from dataclasses import asdict

        print(json.dumps({"result": asdict(value)}))
    except Exception:  # noqa: BLE001 - isolated parser returns a safe protocol error
        print(
            json.dumps(
                {
                    "error": "Unsupported, malformed, protected, empty or over-limit document."
                }
            )
        )


if __name__ == "__main__":
    main()
