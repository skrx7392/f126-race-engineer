"""Environment-driven configuration. Every deploy-varying value lives here."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(slots=True)
class Config:
    # UDP capture
    udp_host: str = field(default_factory=lambda: os.environ.get("F126_UDP_HOST", "0.0.0.0"))
    udp_port: int = field(default_factory=lambda: _int("F126_UDP_PORT", 20777))
    rcvbuf_bytes: int = field(default_factory=lambda: _int("F126_RCVBUF", 4 * 1024 * 1024))

    # Storage
    data_dir: str = field(default_factory=lambda: os.environ.get("F126_DATA_DIR", "./data"))
    database_url: str = field(
        default_factory=lambda: os.environ.get("F126_DATABASE_URL", "")
    )  # empty = DB writes disabled (raw log still on)
    db_batch_ms: int = field(default_factory=lambda: _int("F126_DB_BATCH_MS", 250))

    # Sampling
    telemetry_hz: int = field(default_factory=lambda: _int("F126_TELEMETRY_HZ", 20))
    wear_hz: int = field(default_factory=lambda: _int("F126_WEAR_HZ", 1))

    # Session lifecycle
    stall_after_s: float = field(default_factory=lambda: _float("F126_STALL_AFTER_S", 5.0))
    session_timeout_min: float = field(
        default_factory=lambda: _float("F126_SESSION_TIMEOUT_MIN", 30.0)
    )
    segment_rewind_s: float = field(default_factory=lambda: _float("F126_SEGMENT_REWIND_S", 30.0))

    # Web
    http_host: str = field(default_factory=lambda: os.environ.get("F126_HTTP_HOST", "0.0.0.0"))
    http_port: int = field(default_factory=lambda: _int("F126_HTTP_PORT", 8000))
    ws_fast_hz: float = field(default_factory=lambda: _float("F126_WS_FAST_HZ", 10.0))
    ws_slow_hz: float = field(default_factory=lambda: _float("F126_WS_SLOW_HZ", 1.0))
    ws_max_clients: int = field(default_factory=lambda: _int("F126_WS_MAX_CLIENTS", 20))
    static_dir: str = field(
        default_factory=lambda: os.environ.get("F126_STATIC_DIR", "frontend/dist")
    )

    # Post-session debrief (LLM). The endpoint is OpenAI-compatible and reached over plain
    # HTTP; an empty base URL disables the whole feature, which is the default everywhere.
    # No LLM is ever in the live loop — see the module docstring of f126.llm.
    llm_base_url: str = field(default_factory=lambda: os.environ.get("F126_LLM_BASE_URL", ""))
    llm_model: str = field(default_factory=lambda: os.environ.get("F126_LLM_MODEL", ""))
    llm_api_key: str = field(default_factory=lambda: os.environ.get("F126_LLM_API_KEY", ""))
    llm_timeout_s: float = field(default_factory=lambda: _float("F126_LLM_TIMEOUT_S", 60.0))
    #: Grace period between a session closing and the debrief reading the database, so the
    #: batching writer has flushed the final classification and the last laps. The serve
    #: path also flushes the writer explicitly; this covers the case where there is none.
    debrief_delay_s: float = field(default_factory=lambda: _float("F126_DEBRIEF_DELAY_S", 5.0))

    @property
    def llm_enabled(self) -> bool:
        """Whether a debrief can be generated at all. Both halves are required.

        A base URL without a model would fail one request at a time inside a fire-and-forget
        task, which is exactly the kind of failure that goes unnoticed; refusing up front
        makes it a startup-visible configuration error instead.
        """
        return bool(self.llm_base_url.strip() and self.llm_model.strip())


def load() -> Config:
    return Config()
