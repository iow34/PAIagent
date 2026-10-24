from app.ingestion.parsers.base import Parser


class PlainTextParser(Parser):
    def parse(self, raw_bytes: bytes) -> str:
        return raw_bytes.decode("utf-8-sig")
