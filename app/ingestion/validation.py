from dataclasses import dataclass

from app.ingestion.parsers.registry import get_parser

MAX_FILE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = MAX_FILE_BYTES + 64 * 1024


class InvalidDocumentError(ValueError):
    pass


class UnsupportedFileTypeError(InvalidDocumentError):
    pass


class FileToolLargeError(InvalidDocumentError):
    pass


@dataclass(frozen=True)
class ParserDocument:
    title: str
    content_type: str
    text: str


def parse_uploaded_document(
    *,
    filename: str | None,
    raw_bytes: bytes,
) -> ParserDocument:
    if (
        not filename
        or not filename.strip()
        or len(filename) > 500
        or "\x00" in filename
    ):
        raise InvalidDocumentError(
            "Filename must contain 1 to 500 characters and no NUL character."
        )

    if len(raw_bytes) > MAX_FILE_BYTES:
        raise FileToolLargeError("File must not exceed 1 MiB.")

    try:
        parser = get_parser(filename)
    except ValueError as error:
        raise UnsupportedFileTypeError(
            "Only .txt and .md files are supported."
        ) from error

    try:
        text = parser.parse(raw_bytes)
    except UnicodeDecodeError as error:
        raise InvalidDocumentError("File must use UTF-8 encoding.") from error

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    if "\x00" in text:
        raise InvalidDocumentError("File must not contain NUL characters.")

    if not text.strip():
        raise InvalidDocumentError("File must contain non-whitespace text.")

    return ParserDocument(
        title=filename,
        content_type=filename.rsplit(".", 1)[-1].lower(),
        text=text,
    )
