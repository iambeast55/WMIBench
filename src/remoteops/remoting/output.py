from __future__ import annotations

import json
from datetime import date, datetime


def plain_value(value, depth=0):
    if depth > 8:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): plain_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_value(v, depth + 1) for v in value]
    properties = {**getattr(value, "adapted_properties", {}), **getattr(value, "extended_properties", {})}
    if properties:
        return plain_value(properties, depth + 1)
    return str(value)


def output_records(output):
    records = []
    for item in output:
        value = plain_value(item)
        records.append(value if isinstance(value, dict) else {"Value": value})
    return records


def display_value(value):
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)


def records_text(records):
    if not records:
        return ""
    if all(list(row) == ["Value"] for row in records):
        return "\n".join(display_value(row["Value"]) for row in records)
    columns = list(dict.fromkeys(key for row in records for key in row))
    # Keep complete cell values; the GUI table provides wrapping and column resizing.
    widths = {key: max(len(key), *(len(display_value(row.get(key)).split('\n')[0]) for row in records)) for key in columns}
    lines = ["  ".join(key.ljust(widths[key]) for key in columns),
             "  ".join("-" * widths[key] for key in columns)]
    lines += ["  ".join(display_value(row.get(key)).ljust(widths[key]) for key in columns) for row in records]
    return "\n".join(lines)
