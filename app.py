"""IncidentMind: an on-call AI copilot that learns from every outage."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import load_dotenv

try:
    from google import genai
except Exception:  # pragma: no cover - optional dependency for offline demo mode
    genai = None

try:
    from hindsight_client import Hindsight
except Exception:  # pragma: no cover - optional dependency for offline demo mode
    Hindsight = None

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_DIR = PROJECT_ROOT / ".incidentmind"
BANK_DIR = STATE_DIR / "banks"
STATS_PATH = STATE_DIR / "stats.json"
SAMPLE_ALERTS = [
    "payments-api: DB unreachable",
    "search-service: OOM after deploy",
    "auth-service: 502 after deploy",
    "checkout-web: disk full on web-02",
    "notification-service: NEW issue, no history",
]

SEED_BANK_ID = "devops-incidents"
DEFAULT_HINDSIGHT_URL = os.getenv("HINDSIGHT_API_URL") or os.getenv("HINDSIGHT_BASE_URL") or "https://api.hindsight.vectorize.io"


def local_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --bg: #07111f;
            --panel: #0d1b2a;
            --panel-soft: rgba(19, 35, 55, 0.8);
            --card: rgba(12, 22, 36, 0.9);
            --primary: #67e8f9;
            --secondary: #8b5cf6;
            --success: #34d399;
            --warning: #fbbf24;
            --danger: #f87171;
            --text: #e2e8f0;
            --muted: #a5b4c7;
            --border: rgba(148, 163, 184, 0.2);
        }
        html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {
            background: radial-gradient(circle at top left, rgba(103,232,249,0.12), transparent 28%),
                        radial-gradient(circle at bottom right, rgba(139,92,246,0.18), transparent 32%),
                        var(--bg);
            color: var(--text);
        }
        [data-testid="stSidebar"] {
            background: rgba(8, 15, 25, 0.94);
            border-right: 1px solid var(--border);
        }
        .stTabs [role="tablist"] {
            gap: 10px;
            margin-bottom: 1rem;
        }
        .stTabs [role="tab"] {
            border-radius: 12px;
            background: rgba(15, 23, 42, 0.8);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 0.5rem 1rem;
        }
        .stTabs [role="tab"][aria-selected="true"] {
            background: linear-gradient(135deg, rgba(103,232,249,0.2), rgba(139,92,246,0.25));
            border-color: rgba(103,232,249,0.55);
        }
        div[data-testid="stMetricContainer"] {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 0.6rem 0.8rem;
        }
        .block-container {
            padding-top: 2rem;
            padding-bottom: 3rem;
        }
        .top-hero {
            background: linear-gradient(135deg, rgba(103,232,249,0.12), rgba(139,92,246,0.18));
            border: 1px solid rgba(103,232,249,0.3);
            border-radius: 20px;
            padding: 1.2rem 1.3rem;
            margin-bottom: 1.2rem;
        }
        .pill {
            display: inline-block;
            background: rgba(52, 211, 153, 0.12);
            color: var(--success);
            border: 1px solid rgba(52, 211, 153, 0.35);
            border-radius: 999px;
            padding: 0.25rem 0.7rem;
            font-size: 0.74rem;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
        }
        .card {
            background: rgba(15, 23, 42, 0.75);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 1rem;
        }
        .css-1d391kg, .css-12oz5g7 {
            color: var(--text);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_state_files() -> None:
    STATE_DIR.mkdir(exist_ok=True)
    BANK_DIR.mkdir(exist_ok=True)
    if not STATS_PATH.exists():
        STATS_PATH.write_text(json.dumps({}, indent=2), encoding="utf-8")


def read_stats() -> dict[str, Any]:
    ensure_state_files()
    try:
        return json.loads(STATS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_stats(stats: dict[str, Any]) -> None:
    ensure_state_files()
    STATS_PATH.write_text(json.dumps(stats, indent=2), encoding="utf-8")


def get_bank_stats(bank_id: str) -> dict[str, int]:
    stats = read_stats()
    return stats.get(
        bank_id,
        {
            "memories_stored": 0,
            "incidents_analyzed": 0,
            "solved_with_memory": 0,
            "worked": 0,
            "failed": 0,
        },
    )


def update_bank_stats(bank_id: str, key: str, delta: int = 1) -> None:
    stats = read_stats()
    stats.setdefault(
        bank_id,
        {
            "memories_stored": 0,
            "incidents_analyzed": 0,
            "solved_with_memory": 0,
            "worked": 0,
            "failed": 0,
        },
    )
    stats[bank_id][key] = stats[bank_id].get(key, 0) + delta
    write_stats(stats)


def get_bank_file(bank_id: str) -> Path:
    ensure_state_files()
    return BANK_DIR / f"{bank_id}.json"


def get_bank_records(bank_id: str) -> list[dict[str, Any]]:
    path = get_bank_file(bank_id)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except json.JSONDecodeError:
        return []


def save_bank_records(bank_id: str, records: list[dict[str, Any]]) -> None:
    path = get_bank_file(bank_id)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


class LocalMemory:
    def ensure_bank(self, bank_id: str) -> None:
        save_bank_records(bank_id, get_bank_records(bank_id))

    def retain(self, bank_id: str, content: str, context: str, timestamp: str | None = None) -> dict[str, Any]:
        records = get_bank_records(bank_id)
        record = {
            "id": f"local-{len(records) + 1}-{int(datetime.now(timezone.utc).timestamp())}",
            "bank_id": bank_id,
            "content": content,
            "context": context,
            "timestamp": timestamp or utc_now_iso(),
        }
        records.append(record)
        save_bank_records(bank_id, records)
        update_bank_stats(bank_id, "memories_stored", 1)
        return record

    def recall(self, bank_id: str, query: str) -> dict[str, Any]:
        records = get_bank_records(bank_id)
        if not records:
            return {"results": []}
        terms = {term for term in re.findall(r"[a-zA-Z0-9-]+", query.lower())}
        scored: list[tuple[int, dict[str, Any]]] = []
        for rec in records:
            text = f"{rec.get('content', '')} {rec.get('context', '')}".lower()
            overlap = len(terms.intersection(set(re.findall(r"[a-zA-Z0-9-]+", text))))
            if overlap or not terms:
                scored.append((overlap, rec))
        scored.sort(key=lambda item: item[0], reverse=True)
        return {
            "results": [{"text": rec["content"], "type": rec.get("context", "incident")} for _, rec in scored[:5]],
        }

    def reflect(self, bank_id: str, query: str) -> str:
        records = get_bank_records(bank_id)
        if not records:
            return "No stored incidents yet. Teach a resolution to create the first pattern."
        service_map: dict[str, int] = {}
        failed_fixes = 0
        for rec in records:
            content = rec.get("content", "")
            match = re.search(r"service '([^']+)'|service ([A-Za-z0-9\-]+)", content, flags=re.I)
            if match:
                service = match.group(1) or match.group(2)
                service_map[service] = service_map.get(service, 0) + 1
            if "did not help" in content.lower() or "did NOT help" in content.lower():
                failed_fixes += 1
        top_service = max(service_map.items(), key=lambda item: item[1], default=("unknown", 0))[0]
        if failed_fixes:
            return (
                f"Recurring pattern: {top_service} has the most incident history and many entries explicitly record failed recovery steps. "
                "The strongest learning is to avoid repeated actions that were labeled as 'did NOT help'."
            )
        return (
            f"Recurring pattern: {top_service} shows the highest volume of saved incidents in this bank. "
            "The team should review those records for repeated root causes and recovery changes."
        )


class MemoryClient:
    def __init__(self) -> None:
        self.local = LocalMemory()
        self.real_client = None
        self.use_real = False
        self._init_real_client()

    def _init_real_client(self) -> None:
        if Hindsight is None:
            return
        api_key = os.getenv("HINDSIGHT_API_KEY", "").strip()
        if not api_key:
            return
        try:
            base_url = (os.getenv("HINDSIGHT_API_URL") or os.getenv("HINDSIGHT_BASE_URL") or DEFAULT_HINDSIGHT_URL).rstrip("/")
            self.real_client = Hindsight(base_url=base_url, api_key=api_key, timeout=60.0)
            self.use_real = True
        except Exception:
            self.real_client = None
            self.use_real = False

    def ensure_bank(self, bank_id: str) -> None:
        if self.use_real and self.real_client is not None:
            try:
                if hasattr(self.real_client, "create_bank"):
                    self.real_client.create_bank(bank_id=bank_id, name=bank_id, mission="Store incident history and failed fixes for the team.")
                elif hasattr(self.real_client, "acreate_bank"):
                    self.real_client.acreate_bank(bank_id=bank_id, name=bank_id, mission="Store incident history and failed fixes for the team.")
            except Exception:
                pass
            return
        self.local.ensure_bank(bank_id)

    def retain(self, bank_id: str, content: str, context: str, timestamp: str | None = None) -> Any:
        if self.use_real and self.real_client is not None:
            try:
                if hasattr(self.real_client, "retain"):
                    return self.real_client.retain(bank_id=bank_id, content=content, context=context, timestamp=timestamp or utc_now_iso())
                if hasattr(self.real_client, "aretain"):
                    return self.real_client.aretain(bank_id=bank_id, content=content, context=context, timestamp=timestamp or utc_now_iso())
            except Exception:
                pass
        return self.local.retain(bank_id, content, context, timestamp)

    def recall(self, bank_id: str, query: str) -> dict[str, Any]:
        if self.use_real and self.real_client is not None:
            try:
                if hasattr(self.real_client, "recall"):
                    return self.real_client.recall(bank_id=bank_id, query=query, max_tokens=1200, budget="high")
                if hasattr(self.real_client, "arecall"):
                    return self.real_client.arecall(bank_id=bank_id, query=query, max_tokens=1200, budget="high")
            except Exception:
                pass
        return self.local.recall(bank_id, query)

    def reflect(self, bank_id: str, query: str) -> str:
        if self.use_real and self.real_client is not None:
            try:
                if hasattr(self.real_client, "reflect"):
                    res = self.real_client.reflect(bank_id=bank_id, query=query, budget="low")
                    return getattr(res, "text", None) or str(res)
                if hasattr(self.real_client, "areflect"):
                    res = self.real_client.areflect(bank_id=bank_id, query=query, budget="low")
                    return getattr(res, "text", None) or str(res)
            except Exception:
                pass
        return self.local.reflect(bank_id, query)


def get_memory_client() -> MemoryClient:
    return MemoryClient()


def strip_think_blocks(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def read_bank_names() -> list[str]:
    ensure_state_files()
    bank_names = [path.stem for path in BANK_DIR.glob("*.json")]
    if not bank_names:
        bank_names = [SEED_BANK_ID]
    return sorted(set([SEED_BANK_ID] + bank_names))


def seed_demo_data() -> None:
    ensure_state_files()
    bank_id = SEED_BANK_ID
    if get_bank_records(bank_id):
        return
    seed_memories = [
        "Incident on service 'payments-api'. Error: DB unreachable after a failover; after restarting the DB listener, the issue persisted. Resolution: remove stale firewall rule and restore the primary DB route; this fix did NOT help: restarting the db listener. Outcome: SUCCESS.",
        "Incident on service 'payments-api'. Error: timeout from checkout API to payments. Resolution: flush stale Redis connection pool and add health checks; what did NOT help: increasing the DB max connections. Outcome: SUCCESS.",
        "Incident on service 'search-service'. Error: OOM warning after deploy; heap usage grew until selectors timed out. Resolution: reduce query cache size and enforce index refresh; what did NOT help: heap bump alone. Outcome: SUCCESS.",
        "Incident on service 'search-service'. Error: 503s during indexing spike. Resolution: temporarily disable deep search on hot paths; what did NOT help: adding more workers without cache tuning. Outcome: FAILED.",
        "Incident on service 'auth-service'. Error: 502 after deploy, login latency spiked. Resolution: rollback the auth worker image and restore env config; what did NOT help: rolling forward to a hotfix without config sync. Outcome: SUCCESS.",
        "Incident on service 'auth-service'. Error: JWT validation timeout during peak load. Resolution: add a circuit break on downstream IDP; what did NOT help: scaling only the API pods. Outcome: SUCCESS.",
        "Incident on service 'checkout-web'. Error: disk full on web-02. Resolution: rotate logs and clear debug output; what did NOT help: rebooting the app server without cleaning the partition. Outcome: SUCCESS.",
        "Incident on service 'checkout-web'. Error: rendering error on mobile checkout. Resolution: disable debug flag and rehydrate the static asset cache; what did NOT help: purging CDN cache without disabling the noisy logs. Outcome: FAILED.",
        "Incident on service 'payments-api'. Error: Elasticsearch 429s causing invoice jobs to stall. Resolution: increase queue timeout and throttle indexing; what did NOT help: forcing a full reconnect loop. Outcome: SUCCESS.",
        "Incident on service 'search-service'. Error: Redis connection storm after cache invalidation. Resolution: reduce TTL and add backoff; what did NOT help: clearing all cache entries at once. Outcome: SUCCESS.",
        "Incident on service 'auth-service'. Error: intermittent 401s after token rotation. Resolution: sync secret version and rotate the public key; what did NOT help: short TTL reissue without reloading the gateway. Outcome: SUCCESS.",
        "Incident on service 'checkout-web'. Error: browser hydration stuck after release. Resolution: rollback frontend JS bundle and restore stable asset fingerprint; what did NOT help: toggling feature flags without clearing stale builds. Outcome: SUCCESS.",
    ]
    save_bank_records(
        bank_id,
        [
            {
                "id": f"seed-{idx}",
                "bank_id": bank_id,
                "context": "production incident post-mortem",
                "timestamp": utc_now_iso(),
                "content": memory,
            }
            for idx, memory in enumerate(seed_memories, start=1)
        ],
    )


def get_gemini_client() -> Any:
    if genai is None:
        raise RuntimeError("The Google GenAI SDK is not installed. Run pip install -r requirements.txt.")
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY in your environment. Add it to .env.")
    return genai.Client(api_key=api_key)


def confidence_from_memory_count(count: int) -> str:
    if count == 0:
        return "🔴 0 memories"
    if count <= 2:
        return "🟡 1-2 memories"
    return "🟢 3+ memories"


def build_solver_prompt(memory_text: str, error_log: str, service: str | None) -> str:
    service_note = f"Service: {service}\n" if service else ""
    return (
        "You are IncidentMind, an on-call AI incident-response copilot for a production team.\n\n"
        "Use only the recalled incidents below. Do not invent history or cite anything not in memory.\n"
        "If history is empty or irrelevant, say 'No relevant history' in the 'Why I think this' section and keep the recommendation conservative.\n\n"
        f"{service_note}"
        "Recalled incident memory:\n"
        f"{memory_text}\n\n"
        "Incoming issue:\n"
        f"{error_log}\n\n"
        "Return exactly this format:\n"
        "**Likely root cause:** ...\n"
        "**Fix steps:**\n"
        "1. ...\n"
        "**Do NOT try:** ...\n"
        "**Why I think this:** ...\n"
    )


def generate_incident_plan(memory_text: str, error_log: str, service: str | None) -> str:
    client = get_gemini_client()
    response = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        contents=build_solver_prompt(memory_text, error_log, service),
        config={"temperature": 0.2},
    )
    text = getattr(response, "text", None) or ""
    return strip_think_blocks(text) or "No solution generated."


def normalize_recall_response(response: Any) -> list[dict[str, str]]:
    if isinstance(response, dict):
        return list(response.get("results", []) or [])
    values = getattr(response, "results", None)
    if values is None and hasattr(response, "model_dump"):
        try:
            values = response.model_dump().get("results")
        except Exception:
            values = None
    if values is None:
        return []
    return list(values)


def render_solver() -> None:
    st.subheader("Incident Solver")
    sample_alert = st.selectbox("Sample alert", SAMPLE_ALERTS, index=0)
    service = st.text_input("Service name (optional)", value="")
    error_log = st.text_area(
        "Error log or alert text",
        value=sample_alert,
        height=220,
        placeholder="Paste the alert, traceback, or error snippet here…",
    )

    run_button = st.button("Analyze incident", type="primary", use_container_width=True)
    if run_button:
        if not error_log.strip():
            st.warning("Paste an error log before analyzing.")
            return

        bank_id = st.session_state.get("active_bank", SEED_BANK_ID)
        memory_client = get_memory_client()
        memory_client.ensure_bank(bank_id)

        with st.spinner("Recalling past incidents from Hindsight…"):
            recall_response = memory_client.recall(bank_id, f"{service} {error_log}".strip())

        memory_records = normalize_recall_response(recall_response)
        memory_text = "\n\n".join(item.get("text", "") for item in memory_records) if memory_records else "No relevant history"
        memory_count = len(memory_records)
        st.session_state["last_memory_text"] = memory_text
        st.session_state["last_plan_input"] = {"service": service, "error_log": error_log}
        update_bank_stats(bank_id, "incidents_analyzed")
        if memory_count > 0:
            update_bank_stats(bank_id, "solved_with_memory")

        st.markdown(f"<div class='pill'>Confidence: {confidence_from_memory_count(memory_count)}</div>", unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)

        left, right = st.columns(2)
        with left:
            st.markdown("### Recalled memory")
            if memory_records:
                for idx, item in enumerate(memory_records, start=1):
                    st.markdown(f"**Memory {idx}**")
                    st.write(item.get("text", ""))
            else:
                st.info("No relevant history")

        with right:
            st.markdown("### Suggested action plan")
            try:
                with st.spinner("Generating the plan with Gemini…"):
                    plan = generate_incident_plan(memory_text, error_log, service or None)
                st.markdown(plan)
                st.session_state["last_plan_text"] = plan
            except Exception as exc:
                st.error(f"Unable to generate the plan: {exc}")
                st.session_state["last_plan_text"] = "Unable to generate a plan."

        st.markdown("---")
        feedback_note = st.text_input("Optional note", placeholder="What actually happened or the real cause?")
        fb_cols = st.columns(2)
        with fb_cols[0]:
            if st.button("👍 It worked", use_container_width=True):
                submit_feedback(bank_id, error_log, service, st.session_state.get("last_plan_text", ""), "WORKED", feedback_note)
                st.success("Thanks — that outcome was saved into memory.")
        with fb_cols[1]:
            if st.button("👎 It didn't work", use_container_width=True):
                submit_feedback(bank_id, error_log, service, st.session_state.get("last_plan_text", ""), "FAILED", feedback_note)
                st.warning("The failed suggestion was retained so it is not repeated.")


def submit_feedback(bank_id: str, error_log: str, service: str, plan: str, outcome: str, note: str) -> None:
    memory_client = get_memory_client()
    memory_client.ensure_bank(bank_id)
    summary = (
        f"Incident on service '{service or 'unknown'}'. Error: {error_log[:400]}. "
        f"Agent suggested: {plan[:600]}. Engineer feedback: suggested fix {outcome}. "
        f"Note: {note.strip() if note.strip() else 'No note provided.'}"
    )
    memory_client.retain(bank_id, summary, "agent suggestion feedback", utc_now_iso())
    if outcome == "WORKED":
        update_bank_stats(bank_id, "worked")
    else:
        update_bank_stats(bank_id, "failed")


def render_teach_resolution() -> None:
    st.subheader("Teach a Resolution")
    with st.form("teach_resolution"):
        service = st.text_input("Service", placeholder="payments-api")
        error = st.text_area("Error or symptom", height=120, placeholder="DB unreachable after failover")
        fix = st.text_area("What fixed it", height=150, placeholder="Rollback the worker and restore the DB route")
        did_not_help = st.text_area("What did NOT help", height=120, placeholder="Restarting the DB listener, increasing heap, etc.")
        outcome = st.selectbox("Outcome", ["SUCCESS", "FAILED"])
        submitted = st.form_submit_button("Save memory", use_container_width=True)

    if submitted:
        if not service.strip() or not error.strip() or not fix.strip() or not did_not_help.strip():
            st.warning("Service, error, fix, and what did not help are all required.")
            return
        bank_id = st.session_state.get("active_bank", SEED_BANK_ID)
        memory_client = get_memory_client()
        memory_client.ensure_bank(bank_id)
        content = (
            f"Incident on service '{service.strip()}'. Error: {error.strip()}. Resolution: {fix.strip()}. "
            f"What did NOT help: {did_not_help.strip()}. Outcome: {outcome}."
        )
        memory_client.retain(bank_id, content, "production incident post-mortem", utc_now_iso())
        st.success("Resolution stored. Future analyses can use this memory.")


def render_insights() -> None:
    st.subheader("Insights")
    question_options = [
        "What recurring root causes appear across incidents?",
        "Which fixes repeatedly failed?",
        "Which service is least reliable?",
    ]
    question = st.selectbox("Sample question", question_options)
    custom_question = st.text_input("Custom question", placeholder="What patterns do you want to review?")
    service_focus = st.text_input("Service focus (optional)", placeholder="payments-api")
    if st.button("Reflect", type="primary", use_container_width=True):
        bank_id = st.session_state.get("active_bank", SEED_BANK_ID)
        memory_client = get_memory_client()
        memory_client.ensure_bank(bank_id)
        query = (custom_question.strip() or question).strip()
        insight = memory_client.reflect(bank_id, f"{service_focus.strip()} {query}".strip())
        st.markdown("### Insight")
        st.write(insight)


def render_sidebar() -> None:
    st.sidebar.title("IncidentMind")
    seed_demo_data()
    bank_names = read_bank_names()
    st.sidebar.caption("Persistent memory for resilient incident response")
    active_bank = st.sidebar.selectbox("Memory bank", bank_names, index=bank_names.index(SEED_BANK_ID) if SEED_BANK_ID in bank_names else 0)
    st.session_state["active_bank"] = active_bank

    if st.sidebar.button("Create fresh bank", use_container_width=True):
        fresh_bank = f"demo-fresh-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        get_memory_client().ensure_bank(fresh_bank)
        st.session_state["active_bank"] = fresh_bank
        st.sidebar.success(f"Created fresh bank: {fresh_bank}")
        st.rerun()

    stats = get_bank_stats(active_bank)
    st.sidebar.markdown("### Learning dashboard")
    metrics = [
        ("Memories stored", stats.get("memories_stored", 0)),
        ("Incidents analyzed", stats.get("incidents_analyzed", 0)),
        ("Solved with memory", stats.get("solved_with_memory", 0)),
    ]
    for label, value in metrics:
        st.sidebar.metric(label, value)

    worked = stats.get("worked", 0)
    failed = stats.get("failed", 0)
    total_attempts = worked + failed
    success_rate = round((worked / total_attempts) * 100, 1) if total_attempts else 0.0
    st.sidebar.metric("Fix success rate", f"{success_rate}%")


def main() -> None:
    st.set_page_config(page_title="IncidentMind", page_icon="🧠", layout="wide")
    local_css()
    render_sidebar()

    st.markdown(
        """
        <div class='top-hero'>
            <span class='pill'>Hindsight memory + Gemini</span>
            <h2 style='margin: 0.6rem 0 0.2rem 0;'>IncidentMind</h2>
            <p style='margin: 0; color: #cbd5e1;'>An on-call AI copilot that remembers every outage, warns against failed fixes, and improves over time.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    solver_tab, teach_tab, insights_tab = st.tabs(["Incident Solver", "Teach a Resolution", "Insights"])
    with solver_tab:
        render_solver()
    with teach_tab:
        render_teach_resolution()
    with insights_tab:
        render_insights()


if __name__ == "__main__":
    main()
