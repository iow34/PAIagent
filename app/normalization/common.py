import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import psutil

from app.ingestion.chunking import chunk_text
from app.integration.validation import ContractError
from app.normalization.adapters import MAX_BYTES, Extraction, Unit


def fragments(units):
    result = []
    for unit in units:
        offset = 0
        for value in chunk_text(unit.text, max_chars=1200, overlap=0):
            start = unit.text.find(value, offset)
            position = {**unit.position}
            if start >= 0:
                position.update(char_start=start, char_end=start + len(value))
                offset = start + len(value)
            result.append((value, position))
            if len(result) > 1000:
                raise ContractError("Fragment limit exceeded.")
    return result


def structured(payload, mappings):
    units = []
    for mapping in mappings:
        if mapping["role"] != "search_text":
            continue
        value = payload
        for key in mapping["path"].lstrip("/").split("/"):
            value = (
                value.get(key.replace("~1", "/").replace("~0", "~"))
                if isinstance(value, dict)
                else None
            )
        if isinstance(value, str) and value.strip():
            units.append(Unit(value, {"field": mapping["path"]}))
    return fragments(units)


async def parse_file(raw, extension):
    if not 0 < len(raw) <= MAX_BYTES:
        raise ContractError("File limit: 1 byte to 5 MiB.")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.normalization.worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(Path(__file__).resolve().parents[2]),
        env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"},
    )
    communication = asyncio.create_task(
        process.communicate(
            json.dumps(
                {"extension": extension, "data": base64.b64encode(raw).decode()}
            ).encode()
        )
    )
    try:
        async with asyncio.timeout(10):
            while not communication.done():
                await asyncio.sleep(0.05)
                try:
                    if (
                        psutil.Process(process.pid).memory_info().rss
                        > 768 * 1024 * 1024
                    ):
                        raise ValueError("Parser memory limit exceeded.")
                except psutil.NoSuchProcess:
                    pass
            output, _ = await communication
        response = json.loads(output)
        if "result" not in response:
            raise ValueError()
        result = response["result"]
        return Extraction(
            [Unit(**x) for x in result["units"]],
            result["coverage"],
            result["limitations"],
            result["total_parts"],
        )
    except (ValueError, TimeoutError) as error:
        raise ContractError(
            "Document parsing failed or exceeded resource limits."
        ) from error
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if not communication.done():
            communication.cancel()
        await asyncio.gather(communication, return_exceptions=True)
