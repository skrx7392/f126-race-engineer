"""HTTP + WebSocket surface for the live dashboard.

Read-only by construction: nothing in this package mutates state, because the
dashboard is exposed without authentication. See `app.py` for the invariant and
`docs/ws-protocol.md` for the frame contract.
"""

from __future__ import annotations

from f126.web.app import build_server, create_app, run
from f126.web.broadcast import PROTOCOL_VERSION, WsHub
from f126.web.interfaces import LiveSource, StatsSource

__all__ = [
    "PROTOCOL_VERSION",
    "LiveSource",
    "StatsSource",
    "WsHub",
    "build_server",
    "create_app",
    "run",
]
