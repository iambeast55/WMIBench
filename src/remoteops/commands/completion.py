"""Completion for command aliases and declared parameters."""
from dataclasses import dataclass
import shlex

from remoteops.commands.templates import argument_schema


@dataclass(frozen=True)
class Completion:
    start: int
    end: int
    text: str


def tokens(text):
    result = []
    start = None
    quote = None
    value = ""
    for index, char in enumerate(text):
        if start is None:
            if char.isspace():
                continue
            start = index
        if quote:
            if char == quote:
                quote = None
            else:
                value += char
        elif char in "\"'":
            quote = char
        elif char.isspace():
            result.append((start, index, value))
            start, value = None, ""
        else:
            value += char
    if start is not None:
        result.append((start, len(text), value))
    return result


def complete(text, cursor, templates):
    cursor = max(0, min(len(text), cursor))
    spans = tokens(text)
    current = next((token for token in spans if token[0] <= cursor <= token[1]), (cursor, cursor, ""))
    start, end, _ = current
    before = [token[2] for token in spans if token[1] < start]
    prefix_tokens = tokens(text[start:cursor])
    prefix = prefix_tokens[0][2] if prefix_tokens else ""
    if not before:
        choices = [template.alias for template in templates]
        return [Completion(start, end, value) for value in sorted(set(choices))
                if value.casefold().startswith(prefix.casefold())]
    template = next((t for t in templates if t.alias.casefold() == before[0].casefold()), None)
    if template is None:
        return []
    schema = argument_schema(template)
    used = set()
    pending = None
    positional = list(template.parameters.get("_positional", []))
    for token in before[1:]:
        if pending:
            pending = None
            continue
        name = token.removeprefix("--").split("=", 1)[0].replace("-", "_")
        if token.startswith("--") or ("=" in token and name in schema):
            used.add(name)
            if "=" not in token and name in schema:
                pending = name
        else:
            slot = next((name for name in positional if name not in used), None)
            if slot:
                used.add(slot)
    value_name = pending
    leader = ""
    value_prefix = prefix
    if "=" in prefix:
        raw_name, value_prefix = prefix.split("=", 1)
        value_name = raw_name.removeprefix("--").replace("-", "_")
        leader = raw_name + "="
    elif not pending and not prefix.startswith("-"):
        value_name = next((name for name in positional if name not in used), None)
    choices = []
    if value_name in schema:
        spec = schema[value_name]
        values = spec.get("choices", [True, False] if spec.get("type") == "bool" else [])
        for value in values:
            value = str(value).lower() if isinstance(value, bool) else str(value)
            if value.casefold().startswith(value_prefix.casefold()):
                choices.append(shlex.quote(leader + value))
    if not pending and not leader:
        flags = ["--" + name.replace("_", "-") for name in schema if name not in used] + ["--help"]
        choices.extend(flag for flag in flags if flag.casefold().startswith(prefix.casefold()))
    return [Completion(start, end, value) for value in sorted(set(choices))]
