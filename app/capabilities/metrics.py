"""Reproducible decimal aggregates. The model never supplies numeric metrics."""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from app.capabilities.validation import value_at
from app.integration.validation import ContractError


def sample_time(row, selection):
    if selection.time_field == "occurred_at":
        return row["occurred_at"]
    value = value_at(row["payload"], selection.time_field)
    return datetime.fromisoformat(value.upper()) if value is not None else None


def in_period(row, selection, period):
    if period is None:
        return True
    value = sample_time(row, selection)
    return value is not None and period.start <= value < period.end


def bucket(value, interval, timezone):
    if not interval:
        return None
    if value is None:
        return "missing_time"
    try:
        local = value.astimezone(ZoneInfo(timezone))
    except (ValueError, OverflowError) as error:
        raise ContractError(
            "Timestamp is outside the supported timezone range."
        ) from error
    day = local.date()
    if interval == "week":
        day -= timedelta(days=day.weekday())
    elif interval == "month":
        day = day.replace(day=1)
    return day.isoformat()


def decimal_text(value):
    return format(value, "f") if value is not None else None


def aggregate(config, samples, schemas):
    output = []
    with localcontext() as ctx:
        # Input JSON numbers are bounded to IEEE finite range; 5000 terms.
        ctx.prec = 650
        for metric in config.metrics:
            selection = config.selections[metric.selection]
            unit = "1"
            if metric.field:
                unit = next(
                    m["unit"]
                    for m in schemas[metric.selection].field_mappings
                    if m["path"] == metric.field and m["role"] == "quantity"
                )
            periods = [("primary", config.period)]
            if config.comparison_period:
                periods.append(("comparison", config.comparison_period))
            values_by_period = {}
            for period_name, period in periods:
                groups = defaultdict(list)
                for row in samples[metric.selection]:
                    if in_period(row, selection, period):
                        group = (
                            value_at(row["payload"], metric.group_by)
                            if metric.group_by
                            else None
                        )
                        # JSON scalar categories: preserve bool/int/string distinction.
                        key = (
                            json.dumps(group, sort_keys=True),
                            bucket(
                                sample_time(row, selection),
                                metric.interval,
                                config.timezone,
                            ),
                        )
                        groups[key].append(row)
                if not groups and not metric.group_by and not metric.interval:
                    groups[("null", None)] = []
                if len(groups) > 100:
                    raise ContractError(
                        "Metric exceeds 100 groups/buckets; narrow the selection.",
                        code="metric_group_limit",
                    )
                calculated = {}
                for (group_json, time_bucket), rows in sorted(
                    groups.items(), key=lambda item: str(item[0])
                ):
                    numbers = [
                        Decimal(str(value_at(r["payload"], metric.field)))
                        for r in rows
                        if metric.field
                        and value_at(r["payload"], metric.field) is not None
                    ]
                    if any(
                        not v.is_finite()
                        or v.adjusted() > 308
                        or (v and v.adjusted() < -324)
                        for v in numbers
                    ):
                        raise ContractError(
                            "Metric quantities must be finite in the supported 1e-324..1e308 magnitude range.",
                            code="metric_numeric_limit",
                        )
                    n = len(numbers)
                    operation = metric.operation
                    if operation == "count":
                        result = Decimal(len(rows))
                    elif not numbers:
                        result = None
                    elif operation == "sum":
                        result = sum(numbers, Decimal(0))
                    elif operation == "average":
                        result = sum(numbers, Decimal(0)) / n
                    elif operation == "min":
                        result = min(numbers)
                    else:
                        result = max(numbers)
                    key = (group_json, time_bucket)
                    calculated[key] = result
                    output.append(
                        {
                            "name": metric.name,
                            "operation": operation,
                            "selection": metric.selection,
                            "period": period_name,
                            "group": json.loads(group_json),
                            "bucket": time_bucket,
                            "timezone": config.timezone,
                            "unit": unit,
                            "value": decimal_text(result),
                            "sample_count": len(rows),
                            "value_count": len(rows) if operation == "count" else n,
                            "null_count": 0 if operation == "count" else len(rows) - n,
                            "decimal_precision": 650,
                            "rounding": "ROUND_HALF_EVEN",
                            "denominator": n if operation == "average" else None,
                            "source_ids": [r["id"] for r in rows],
                        }
                    )
                values_by_period[period_name] = calculated
            if "comparison" in values_by_period:
                for key in sorted(
                    set(values_by_period["primary"])
                    | set(values_by_period["comparison"]),
                    key=str,
                ):
                    a = values_by_period["primary"].get(key)
                    b = values_by_period["comparison"].get(key)
                    output.append(
                        {
                            "name": metric.name,
                            "operation": "difference",
                            "selection": metric.selection,
                            "group": json.loads(key[0]),
                            "bucket": key[1],
                            "unit": unit,
                            "primary": decimal_text(a),
                            "comparison": decimal_text(b),
                            "value": decimal_text(a - b)
                            if a is not None and b is not None
                            else None,
                            "meaning": "primary minus comparison; absent groups are null, not zero",
                        }
                    )
    return output
