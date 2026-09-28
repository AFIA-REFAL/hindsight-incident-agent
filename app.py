"""DevOps Incident Copilot — Streamlit + Gemini + Hindsight Memory."""

from __future__ import annotations

import asyncio
import atexit
import os
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import APIError
from hindsight_client import Hindsight

load_dotenv()

BANK_ID = "devops-incident-memory"
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_MAX_ATTEMPTS = 4
GEMINI_RETRYABLE_STATUS_CODES = (408, 429, 500, 502, 503, 504)
HINDSIGHT_BASE_URL = os.getenv(
    "HINDSIGHT_BASE_URL",
    "https://api.hindsight.vectorize.io",
).rstrip("/")

SOLVE_PROMPT = """You are a senior SRE writing an exact, executable incident fix.

Use recalled past runbooks when they match this error. If they do not match, say so
and still propose a concrete fix grounded in the log.

Return markdown with these sections:
1. Incident diagnosis (root cause in 2-4 sentences)
2. Exact fix (numbered commands / config changes, copy-paste ready)
3. Verification (how to confirm recovery)
4. What to watch next (alerts, follow-up)

Recalled runbook memory:
{memory}

Incoming error log:
{error_log}
"""


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name}. Copy .env.example to .env and fill it in.")
    return value


T = TypeVar("T")


class HindsightSession:
    """Own one asyncio loop + one Hindsight client for the Streamlit process.

    The official client is aiohttp-based. Sync ``recall()``/``retain()`` call
    ``run_until_complete`` without a running Task (which aiohttp timeouts need),
    and ``@st.cache_resource`` plus a new ``asyncio.run()`` per rerun would bind
    the same ClientSession to different loops. Keep the client on this loop and
    drive it with ``arecall`` / ``aretain`` / ``acreate_bank``.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="hindsight-asyncio",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("Hindsight event loop failed to start.")
        # Construct the client on the loop thread so aiohttp binds here.
        self._client = self.run(self._build_client)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    async def _build_client(self) -> Hindsight:
        return Hindsight(
            base_url=self._base_url,
            api_key=_require_env("HINDSIGHT_API_KEY"),
            timeout=120.0,
        )

    def run(self, factory: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        if self._loop.is_closed():
            raise RuntimeError("Hindsight event loop is closed.")
        coro = factory(*args, **kwargs)
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    @property
    def client(self) -> Hindsight:
        return self._client

    def close(self) -> None:
        if not self._loop.is_closed():
            try:
                self.run(self._client.aclose)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)


@st.cache_resource
def get_hindsight(base_url: str) -> HindsightSession:
    session = HindsightSession(base_url)
    atexit.register(session.close)
    return session


@st.cache_resource
def get_gemini() -> genai.Client:
    return genai.Client(
        api_key=_require_env("GEMINI_API_KEY"),
        http_options=types.HttpOptions(
            timeout=30_000,
            retry_options=types.HttpRetryOptions(
                attempts=GEMINI_MAX_ATTEMPTS,
                initial_delay=1.0,
                max_delay=8.0,
                exp_base=2.0,
                jitter=0.1,
                http_status_codes=list(GEMINI_RETRYABLE_STATUS_CODES),
            )
        ),
    )


def ensure_memory_bank(session: HindsightSession) -> None:
    try:
        session.run(
            session.client.acreate_bank,
            bank_id=BANK_ID,
            name="DevOps Incident Memory",
            mission=(
                "Store production incident summaries and step-by-step runbook fixes "
                "so future alerts can be resolved from past resolutions."
            ),
        )
    except Exception:
        # Bank already exists or the server created it on first retain.
        pass


def format_recalled_memory(recall_response) -> str:
    results = getattr(recall_response, "results", None) or []
    if not results:
        return "(No matching runbook memories were found in the bank.)"

    parts = []
    for i, item in enumerate(results, start=1):
        text = getattr(item, "text", None) or getattr(item, "content", None) or str(item)
        fact_type = getattr(item, "type", None)
        header = f"Memory {i}" + (f" [{fact_type}]" if fact_type else "")
        parts.append(f"{header}:\n{text}")
    return "\n\n".join(parts)


def recall_runbooks(session: HindsightSession, error_log: str):
    return session.run(
        session.client.arecall,
        bank_id=BANK_ID,
        query=error_log,
        budget="high",
        max_tokens=4096,
    )


def retain_resolution(session: HindsightSession, summary: str, runbook: str):
    content = (
        f"Incident summary:\n{summary.strip()}\n\n"
        f"Step-by-step runbook fix:\n{runbook.strip()}"
    )
    return session.run(
        session.client.aretain,
        bank_id=BANK_ID,
        content=content,
        context="devops incident resolution runbook",
        metadata={"source": "incident-copilot", "bank": BANK_ID},
    )


def _is_retryable_gemini_error(error: APIError) -> bool:
    return error.code in GEMINI_RETRYABLE_STATUS_CODES


def generate_fix(gemini: genai.Client, memory: str, error_log: str) -> str:
    prompt = SOLVE_PROMPT.format(memory=memory, error_log=error_log)
    response = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    return (response.text or "").strip() or "Gemini returned an empty response."


def render_live_solver(hindsight: Hindsight, gemini: genai.Client) -> None:
    st.subheader("Live Incident Solver")
    st.caption(
        f"Past runbooks are recalled from Hindsight bank `{BANK_ID}`, "
        f"then {GEMINI_MODEL} writes an exact fix."
    )

    error_log = st.text_area(
        "Incoming server / error log",
        height=260,
        placeholder="Paste kubectl, systemd, nginx, traceback, or cloud logs here…",
    )

    if st.button("Generate exact fix", type="primary", use_container_width=True):
        if not error_log.strip():
            st.warning("Paste an error log first.")
            return

        with st.spinner("Recalling past runbooks from Hindsight…"):
            ensure_memory_bank(hindsight)
            recall_response = recall_runbooks(hindsight, error_log.strip())

        memory_text = format_recalled_memory(recall_response)
        with st.expander("Recalled runbook memory", expanded=True):
            st.markdown(memory_text)

        try:
            with st.spinner(f"Asking {GEMINI_MODEL} for an exact fix…"):
                fix = generate_fix(gemini, memory_text, error_log.strip())
        except APIError as exc:
            if _is_retryable_gemini_error(exc):
                st.error(
                    f"Gemini is temporarily unavailable after {GEMINI_MAX_ATTEMPTS} "
                    f"attempts (HTTP {exc.code}). Please try again shortly."
                )
            else:
                st.error(
                    f"Gemini request failed (HTTP {exc.code}). Check the configured "
                    "Gemini API key and model."
                )
            return
        except Exception:
            st.error("Gemini could not complete the request. Please try again shortly.")
            return

        st.markdown("### Exact fix")
        st.markdown(fix)


def render_retain_tab(hindsight: Hindsight) -> None:
    st.subheader("Retain New Resolution")
    st.caption(
        f"Save this incident and its runbook into `{BANK_ID}` with "
        "`hindsight.retain()` so the copilot can reuse it next time."
    )

    summary = st.text_area(
        "Incident summary",
        height=140,
        placeholder="What failed, where, and what users / SLOs were impacted?",
    )
    runbook = st.text_area(
        "Step-by-step runbook fix",
        height=220,
        placeholder="1. Diagnose…\n2. Apply the change…\n3. Verify…",
    )

    if st.button("Retain in Hindsight", type="primary", use_container_width=True):
        if not summary.strip() or not runbook.strip():
            st.warning("Fill in both the summary and the runbook before retaining.")
            return

        with st.spinner("Writing memory to Hindsight…"):
            ensure_memory_bank(hindsight)
            result = retain_resolution(hindsight, summary, runbook)

        st.success("Resolution retained. Future recalls can use this runbook.")
        if result is not None:
            st.json(
                result.model_dump()
                if hasattr(result, "model_dump")
                else {"result": str(result)}
            )


def main() -> None:
    st.set_page_config(
        page_title="DevOps Incident Copilot",
        page_icon="🛠️",
        layout="wide",
    )
    st.title("DevOps Incident Copilot")
    st.write(
        "Recall past incident runbooks from Hindsight Memory, then generate an "
        "exact Gemini fix — or retain a new resolution so the agent learns."
    )

    try:
        hindsight = get_hindsight(HINDSIGHT_BASE_URL)
        gemini = get_gemini()
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    live_tab, retain_tab = st.tabs(["Live Incident Solver", "Retain New Resolution"])
    with live_tab:
        render_live_solver(hindsight, gemini)
    with retain_tab:
        render_retain_tab(hindsight)


if __name__ == "__main__":
    main()
