"""The seam between `web/` and everything that feeds it.

The web layer owns no state. It reads frames from a `LiveSource` (in the running
service that is `state.live.LiveState`) and scrapes counters off any number of
`StatsSource`s (parser, DB writer, raw logger, UDP listener) for `/metrics`.

Both are structural protocols on purpose: implementors never import this module,
and nothing here may import from `parser/`, `state/` or `store/`. That keeps the
dependency arrow pointing one way — producers know nothing about HTTP.

Frame payloads are plain JSON-ready dicts shaped exactly as `docs/ws-protocol.md`
specifies; the web layer never reshapes them, it only serialises and fans out.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

# A single WS frame payload: the dict that gets json.dumps()'d onto the wire.
Frame = dict[str, Any]


class LiveSource(Protocol):
    """Read side of the live state machine, as consumed by the WS hub.

    All three getters are called from the event loop and MUST NOT block or
    await: they hand back the most recently built frame (or a cheap copy of it).
    Building frames is the state layer's job, on its own cadence.
    """

    #: Unbounded-ish fan-in of `event` frames. The hub is the only consumer;
    #: producers use `put_nowait`. Every item is a complete `event` payload.
    events: asyncio.Queue[Frame]

    def get_snapshot(self) -> Frame:
        """Full `snapshot` payload for a client that just connected."""
        ...

    def get_fast(self) -> Frame | None:
        """Latest `fast` payload, or None when no telemetry has arrived yet.

        The hub skips broadcasting when the payload's `t` is unchanged since the
        previous tick, so returning a stale frame is safe (it costs one dict
        lookup, not a broadcast).
        """
        ...

    def get_slow(self) -> Frame | None:
        """Latest `slow` payload, or None before the first session packet."""
        ...


class StatsSource(Protocol):
    """Anything that can describe itself to `/metrics` as a flat dict.

    Keys become `f126_{source_name}_{key}`; non-numeric values are rendered by
    nothing and silently skipped, so a source may mix in strings (e.g. a version
    or a file name) for its own logging without breaking the exposition.
    Counters should be named with a `_total` suffix — the renderer types them.

    Called from a request handler: must not block, must not raise (the renderer
    guards anyway, but a raising source loses all of its metrics).
    """

    def stats(self) -> dict[str, float | int | str]: ...


#: `create_app` takes these by name: {"parser": ..., "writer": ..., ...}.
#: The name "ws" is reserved for the hub's own counters.
StatsSources = Mapping[str, StatsSource | None]
