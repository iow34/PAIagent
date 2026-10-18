from app.ingestion.parsers.base import Parser
from app.ingestion.parsers.text import PlainTextParser

_PARSERS: dict[str, Parser] = {
    "txt": PlainTextParser(),
    "md": PlainTextParser(),
}

def get_parser(filename: str) -> Parser:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        return _PARSERS[extension]
    except KeyError:
        raise ValueError(f"Unsupported file extension: .{extension}")