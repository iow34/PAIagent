"""Explicit CSV mapping, no locale or type inference."""

import csv
import io
import math
from datetime import date, datetime
from typing import Annotated, Literal

import msgspec

from app.integration.validation import ContractError


class Column(msgspec.Struct, forbid_unknown_fields=True):
    column: str
    field: str
    type: Literal["string", "integer", "number", "boolean", "date", "date-time"]
    empty: Literal["reject", "null", "omit"] = "reject"
    unit: str | None = None


class CSVConfig(msgspec.Struct, forbid_unknown_fields=True):
    columns: Annotated[list[Column], msgspec.Meta(min_length=1, max_length=100)]
    id_column: str
    version_column: str
    occurred_at: datetime
    encoding: Literal["utf-8", "utf-8-sig"] = "utf-8-sig"
    delimiter: Literal[",", ";", "\t"] = ","


def rows(raw, config):
    if len(raw) > 5 * 1024 * 1024:
        raise ContractError("CSV size limit exceeded.")
    try:
        reader = csv.DictReader(
            io.StringIO(raw.decode(config.encoding), newline=""),
            delimiter=config.delimiter,
            strict=True,
        )
        names = reader.fieldnames
        if not names or len(names) != len(set(names)):
            raise ContractError("CSV headers must be unique.")
        columns = [x.column for x in config.columns]
        fields = [x.field for x in config.columns]
        if len(fields) != len(set(fields)) or any(
            not x or "/" in x or "\x00" in x for x in fields
        ):
            raise ContractError("CSV field mapping must use unique root field names.")
        if set(columns + [config.id_column, config.version_column]) - set(names):
            raise ContractError("CSV mapped column missing.")
        result = []
        for line, row in enumerate(reader, 2):
            if len(result) >= 100:
                raise ContractError("CSV row limit: 100.")
            try:
                if None in row or any(x is None for x in row.values()):
                    raise ValueError()
                payload = {}
                for mapping in config.columns:
                    value = row[mapping.column]
                    if value == "":
                        if mapping.empty == "omit":
                            continue
                        if mapping.empty == "null":
                            payload[mapping.field] = None
                            continue
                        raise ValueError()
                    if mapping.type == "integer":
                        if value.strip() != value or not value.lstrip("-").isdigit():
                            raise ValueError()
                        value = int(value)
                    elif mapping.type == "number":
                        value = float(value)
                        if not math.isfinite(value):
                            raise ValueError()
                    elif mapping.type == "boolean":
                        if value not in {"true", "false"}:
                            raise ValueError()
                        value = value == "true"
                    elif mapping.type == "date":
                        if date.fromisoformat(value).isoformat() != value:
                            raise ValueError()
                    elif mapping.type == "date-time":
                        from app.integration.validation import timestamp

                        if not timestamp(value):
                            raise ValueError()
                    # Dates remain ISO strings; units are labels, never conversions.
                    payload[mapping.field] = value
                version = int(row[config.version_column])
                if not 1 <= version <= 2147483647:
                    raise ValueError()
                result.append(
                    {
                        "line": line,
                        "external_id": row[config.id_column],
                        "object_version": version,
                        "payload": payload,
                    }
                )
            except (ValueError, KeyError):
                result.append({"line": line, "error": "CSV conversion failed."})
        return result
    except (UnicodeError, csv.Error) as error:
        raise ContractError("Invalid CSV encoding or structure.") from error
