"""Pure bounded adapters. Binary extraction runs in a disposable subprocess."""

import io
import posixpath
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any

from defusedxml import ElementTree
from pypdf import PdfReader

MAX_BYTES = 5 * 1024 * 1024
MAX_TEXT = 500_000
MAX_UNPACKED = 20 * 1024 * 1024
MAX_PARTS = 500


@dataclass
class Unit:
    text: str
    position: dict[str, Any]


@dataclass
class Extraction:
    units: list[Unit]
    coverage: str
    limitations: list[str]
    total_parts: int


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if tag in {"p", "div", "br", "h1", "h2", "li"} and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1
        if not self.skip:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def xml(raw):
    return ElementTree.fromstring(raw)


def text(raw):
    return Extraction([Unit(raw.decode("utf-8-sig"), {"kind": "text"})], "full", [], 1)


def pdf(raw):
    reader = PdfReader(io.BytesIO(raw), strict=True)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF is unsupported.")
    if len(reader.pages) > MAX_PARTS:
        raise ValueError("PDF page limit exceeded.")
    units = []
    missing = []
    for i, page in enumerate(reader.pages, 1):
        value = page.extract_text() or ""
        if value.strip():
            units.append(Unit(value, {"page": i}))
        else:
            missing.append(i)
        if sum(len(x.text) for x in units) > MAX_TEXT:
            raise ValueError("Extracted text limit exceeded.")
    if not units:
        raise ValueError("PDF has no extractable text; OCR is unsupported.")
    return Extraction(
        units,
        "partial" if missing else "full",
        (["Pages without text: " + ",".join(map(str, missing))] if missing else [])
        + ["Text layer only; images and visual layout are not extracted."],
        len(reader.pages),
    )


def epub(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        names = [x.filename for x in infos]
        if len(infos) > 2000 or len(names) != len(set(names)):
            raise ValueError("EPUB entry limit or duplicate paths.")
        if any(x.flag_bits & 1 for x in infos) or "META-INF/encryption.xml" in names:
            raise ValueError("Encrypted EPUB is unsupported.")
        if sum(x.file_size for x in infos) > MAX_UNPACKED:
            raise ValueError("EPUB unpacked size limit exceeded.")
        if any(
            x.filename.startswith("/")
            or ".." in PurePosixPath(x.filename).parts
            or "\\" in x.filename
            for x in infos
        ):
            raise ValueError("Unsafe EPUB paths.")
        if archive.read("mimetype") != b"application/epub+zip":
            raise ValueError("Invalid EPUB mimetype.")
        root = xml(archive.read("META-INF/container.xml"))
        paths = [
            x.attrib["full-path"] for x in root.iter() if x.tag.endswith("}rootfile")
        ]
        if len(paths) != 1:
            raise ValueError("EPUB requires one package.")
        package = paths[0]
        manifest = xml(archive.read(package))
        items = {
            x.attrib["id"]: x.attrib for x in manifest.iter() if x.tag.endswith("}item")
        }
        spine = [
            x.attrib["idref"] for x in manifest.iter() if x.tag.endswith("}itemref")
        ]
        if not spine or len(spine) > MAX_PARTS:
            raise ValueError("EPUB spine limit exceeded or empty.")
        units = []
        for i, identifier in enumerate(spine, 1):
            item = items[identifier]
            if item["media-type"] != "application/xhtml+xml":
                raise ValueError("Unsupported EPUB spine media.")
            path = posixpath.normpath(
                posixpath.join(posixpath.dirname(package), item["href"])
            )
            if path.startswith("../") or ":" in path or "#" in path:
                raise ValueError("Unsupported EPUB path.")
            document = archive.read(path)
            xml(
                document
            )  # Reject DTD/entities and malformed XML before HTML extraction.
            parser = TextHTML()
            parser.feed(document.decode("utf-8-sig"))
            units.append(Unit("".join(parser.parts), {"section": i, "path": path}))
            if sum(len(x.text) for x in units) > MAX_TEXT:
                raise ValueError("Extracted text limit exceeded.")
        return Extraction(
            units,
            "full",
            [
                "EPUB spine text only; images, notes outside spine and layout are not extracted."
            ],
            len(spine),
        )


ADAPTERS = {"txt": text, "md": text, "pdf": pdf, "epub": epub}


def extract(raw: bytes, extension: str) -> Extraction:
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("File must contain 1 byte to 5 MiB.")
    if extension not in ADAPTERS:
        raise ValueError("Unsupported format.")
    result = ADAPTERS[extension](raw)
    if sum(len(x.text) for x in result.units) > MAX_TEXT:
        raise ValueError("Extracted text limit exceeded.")
    if not any(x.text.strip() for x in result.units):
        raise ValueError("Empty extracted text.")
    if any("\x00" in x.text for x in result.units):
        raise ValueError("NUL is unsupported.")
    return result
