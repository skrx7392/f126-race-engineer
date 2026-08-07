"""The one place an LLM is allowed to run, and the narrowest job it could be given.

**No model is anywhere near the live loop.** Nothing here is called while a session is
running; the pit wall, the WebSocket frames and the capture path never import this module.
A debrief is written once, after a session has closed, from a fact sheet that was already
complete before the request went out. If this endpoint is down, slow, or not configured at
all, the only thing that does not happen is a paragraph of prose.

The model is given `f126.analysis.factsheet`'s dict and told, in the system prompt, that it
may not compute or introduce a number that is not in it. That is the grounding contract: the
arithmetic is done in Python where it is tested, and the model does sentence construction.
Both halves are stored on the `debriefs` row, so any claim in the text can be checked against
the numbers it was handed.

Transport is an OpenAI-compatible `POST {base_url}/chat/completions` over `httpx`. Any
endpoint speaking that dialect works; the deployment points at a local proxy. Configuration
is `F126_LLM_BASE_URL` (empty disables the feature), `F126_LLM_MODEL` and the optional
`F126_LLM_API_KEY`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping

    from f126.config import Config

log = logging.getLogger(__name__)

#: Bumped whenever SYSTEM_PROMPT changes in a way that would change the output. Stored on
#: every `debriefs` row: a debrief written under an older prompt stays attributable instead
#: of being silently reinterpreted as the current one.
PROMPT_VERSION = 1

#: One retry, on failures that are plausibly transient. A debrief is not worth hammering a
#: shared endpoint over, and the CLI can always be re-run.
MAX_ATTEMPTS = 2
RETRY_BACKOFF_S = 2.0

#: Statuses worth a second attempt: rate limiting and the 5xx family. A 400 or a 401 will
#: fail identically the second time and retrying only obscures the real error in the log.
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: Generous but finite. The prompt is small and the reply is ~300 words, so anything past
#: this is a wedged endpoint rather than a slow one.
MAX_TOKENS = 900

#: Low but not zero: the numbers are fixed by the fact sheet, so the only thing temperature
#: varies is phrasing, and zero produces noticeably stilted prose.
TEMPERATURE = 0.4

SYSTEM_PROMPT = """\
You are a Formula 1 race engineer writing the post-session debrief for your own driver, \
who has just climbed out of the car.

You will be given a JSON fact sheet. It was computed from the session's recorded telemetry \
before you were called. It is the ONLY source of information you have.

Rules, in order of importance:

1. Use ONLY numbers that appear in the fact sheet. Do not calculate, average, convert, \
extrapolate or estimate any figure that is not already there. If you want to state a \
difference, a percentage or a total that the sheet does not contain, do not state it.
2. If something is not in the sheet, it did not happen or was not measured. The "omitted" \
key lists what could not be computed and why - treat those as unknown, never as zero. Do \
not speculate about causes the data cannot show (setup, tyre pressures, driver mood, what \
rivals were doing).
3. Metric units throughout, exactly as the "units" key defines them, and mind the sign \
conventions given there. Quote lap times as they are written in the sheet (1:32.345).
3a. Corner numbers in this sheet are detected braking zones, NOT the circuit's official turn \
numbers. Never write "Turn 8". Identify a corner by its distance round the lap (the apex_m \
value), for example "the slow corner at 5589 m".
3b. A degradation slope whose "degradation_confidence" is "weak" has not been measured. Do \
not quote it, and do not describe the tyres as degrading on the strength of it.
4. 200-300 words. Plain prose in short paragraphs. No headings, no bullet lists, no \
markdown, no tables.
5. Address the driver directly as "you". Be direct and specific. No flattery, no cheerleading, \
no restating the fact sheet as a list. Say what the numbers mean.
6. Lead with the outcome that matters most in this session - the result for a race, the lap \
time for a qualifying run, the programme for a practice session.
7. End with exactly one concrete, actionable focus item for the next session, drawn from \
the largest measured weakness in the sheet. One thing, named specifically.

Write the debrief now. Output the prose only - no preamble, no sign-off, no notes about \
these instructions.\
"""


class LlmError(RuntimeError):
    """A debrief could not be generated. Always caught by the caller; never fatal."""


@dataclass(slots=True, frozen=True)
class Debrief:
    """A generated debrief and the provenance needed to store it."""

    text: str
    model: str
    prompt_version: int


def user_prompt(fact_sheet: Mapping[str, Any]) -> str:
    """The fact sheet as the user turn.

    `sort_keys=False` on purpose: `build_fact_sheet` already emits its sections in the order
    a debrief should read them (result, then pace, then stints, then corners), and sorting
    them alphabetically would put "events" first and bury the outcome.
    """
    return json.dumps(fact_sheet, indent=2, ensure_ascii=False, sort_keys=False, default=str)


def build_messages(fact_sheet: Mapping[str, Any]) -> list[dict[str, str]]:
    """The exact two-turn conversation sent to the endpoint."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt(fact_sheet)},
    ]


def _chat_url(base_url: str) -> str:
    """`.../v1` -> `.../v1/chat/completions`, tolerating a trailing slash.

    The base URL is written by a human into an env file, and `/v1/` versus `/v1` should not
    be the difference between a working feature and a 404.
    """
    return f"{base_url.strip().rstrip('/')}/chat/completions"


def _extract_text(payload: Any) -> str:
    """Pull the assistant message out of an OpenAI-compatible response body."""
    if not isinstance(payload, dict):
        raise LlmError("response was not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmError("response carried no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise LlmError("response carried no message content")
    return content.strip()


class LlmClient:
    """Minimal OpenAI-compatible chat client: one method, one retry, no session state."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        timeout_s: float = 60.0,
    ) -> None:
        if not base_url.strip():
            raise LlmError("no LLM base URL configured")
        if not model.strip():
            raise LlmError("no LLM model configured")
        self.base_url = base_url.strip()
        self.model = model.strip()
        self._api_key = api_key.strip()
        self._timeout_s = float(timeout_s)

    @classmethod
    def from_config(cls, cfg: Config) -> LlmClient:
        return cls(
            cfg.llm_base_url,
            cfg.llm_model,
            api_key=cfg.llm_api_key,
            timeout_s=cfg.llm_timeout_s,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    async def chat(
        self, messages: list[dict[str, str]], *, transport: httpx.AsyncBaseTransport | None = None
    ) -> str:
        """POST one chat completion and return the assistant's text.

        `transport` is the seam the tests drive: a `httpx.MockTransport` here exercises the
        full request-building and response-parsing path with no network involved.

        Raises:
            LlmError: on any transport failure, non-2xx status, or unusable response body.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "stream": False,
        }
        url = _chat_url(self.base_url)
        last: Exception | None = None

        async with httpx.AsyncClient(timeout=self._timeout_s, transport=transport) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    response = await client.post(url, json=body, headers=self._headers())
                except httpx.HTTPError as exc:
                    last = LlmError(f"request to {url} failed: {exc}")
                else:
                    if response.status_code < 400:
                        return _extract_text(response.json())
                    # The body of an error is the endpoint's own diagnostic and is worth
                    # keeping, but it is bounded: a proxy returning an HTML error page must
                    # not put a kilobyte of markup into the log line.
                    detail = response.text[:400].replace("\n", " ").strip()
                    last = LlmError(f"endpoint returned {response.status_code}: {detail}")
                    if response.status_code not in RETRY_STATUSES:
                        break
                if attempt < MAX_ATTEMPTS:
                    log.warning(
                        "LLM attempt %d/%d failed (%s); retrying", attempt, MAX_ATTEMPTS, last
                    )
                    await asyncio.sleep(RETRY_BACKOFF_S)

        raise last if last is not None else LlmError("no attempt was made")


async def write_debrief(
    cfg: Config,
    fact_sheet: Mapping[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Debrief:
    """Turn a fact sheet into a debrief.

    Raises:
        LlmError: if the feature is disabled or the endpoint could not produce text. Callers
            on the serve path log and swallow this — a missing debrief is a missing
            paragraph, not an incident.
    """
    if not cfg.llm_enabled:
        raise LlmError("LLM is not configured (set F126_LLM_BASE_URL and F126_LLM_MODEL)")
    client = LlmClient.from_config(cfg)
    text = await client.chat(build_messages(fact_sheet), transport=transport)
    return Debrief(text=text, model=client.model, prompt_version=PROMPT_VERSION)
