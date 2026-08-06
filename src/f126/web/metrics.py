"""Prometheus text exposition, hand-rolled (format version 0.0.4).

No client library. The entire metric surface is a handful of counters that
already exist as plain dicts on the components that own them, so pulling in
`prometheus_client` would buy string formatting and a global registry we would
then have to keep in sync with those dicts. `render()` is the whole thing.

Naming: `f126_{source}_{key}`, e.g. source "parser" key "packets_total" becomes
`f126_parser_packets_total`. Keys ending in `_total` are typed as counters,
everything else as a gauge. Non-numeric values are skipped — sources are free to
put strings in their stats dict for logging purposes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

#: Exactly what a Prometheus scraper expects to see on /metrics.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_INVALID_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_]")


def _sanitize(part: str) -> str:
    """Make one name fragment safe for `[a-zA-Z_:][a-zA-Z0-9_:]*`.

    The full name is always prefixed with `f126_`, so a fragment starting with a
    digit is fine and only the character set needs fixing.
    """
    return _INVALID_NAME_CHARS.sub("_", part)


def _format(value: Any) -> str | None:
    """Render one sample value, or None if it is not a number.

    bools are numbers in Python but read as flags in a dashboard, so they are
    rendered as 1/0. Non-finite floats use Prometheus' own spellings.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "+Inf" if value > 0 else "-Inf"
        return repr(value)
    return None


def render(sources: Mapping[str, Mapping[str, Any] | None]) -> str:
    """Render every numeric value in every source as one exposition document.

    `f126_up 1` is always present: it is the "the process answered a scrape"
    signal, deliberately independent of whether telemetry or the DB are healthy
    (that is what the other gauges are for).

    Sources are rendered in sorted order, keys too, so the output is stable
    across scrapes and diffable in tests. A source whose value is None or is not
    a mapping is skipped; a name collision after sanitising keeps the first.
    """
    lines = [
        "# HELP f126_up 1 when the f126 process is serving requests.",
        "# TYPE f126_up gauge",
        "f126_up 1",
    ]
    emitted = {"f126_up"}

    for source_name in sorted(sources):
        values = sources[source_name]
        if not isinstance(values, Mapping):
            continue
        prefix = f"f126_{_sanitize(source_name)}"
        for key in sorted(values):
            rendered = _format(values[key])
            if rendered is None:
                continue
            name = f"{prefix}_{_sanitize(str(key))}"
            if name in emitted:
                continue
            emitted.add(name)
            kind = "counter" if name.endswith("_total") else "gauge"
            lines.append(f"# TYPE {name} {kind}")
            lines.append(f"{name} {rendered}")

    lines.append("")  # exposition documents end with a newline
    return "\n".join(lines)
