"""IncidentMind: an on-call AI copilot that learns from past outages."""

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
    from groq import Groq
except Exception:  # pragma: no cover - optional dependency for offline demo mode
    Groq = None

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
DEFAULT_HINDSIGHT_URL = "https://api.hindsight.vectorize.io"


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
    return stats.get(bank_id, {
        "memories_stored": 0,
        "incidents_analyzed": 0,
        "solved_with_memory": 0,
        "worked": 0,
        "failed": 0,
    })


def update_bank_stats(bank_id: str, key: str, delta: int = 1) -> None:
    stats = read_stats()
    stats.setdefault(bank_id, {
        "memories_stored": 0,
        "incidents_analyzed": 0,
        "solved_with_memory": 0,
        "worked": 0,
        "failed": 0,
    })
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
    """Fallback in-memory store for demo and offline environments."""

    def ensure_bank(self, bank_id: str) -> None:
        records = get_bank_records(bank_id)
        if records is not None:
            return
        save_bank_records(bank_id, [])

    def retain(self, bank_id: str, content: str, context: str, timestamp: str | None = None) -> dict[str, Any]:
        record = {
            "id": f"local-{len(get_bank_records(bank_id)) + 1}-{int(datetime.now(timezone.utc).timestamp())}",
            "bank_id": bank_id,
            "content": content,
            "context": context,
            "timestamp": timestamp or utc_now_iso(),
        }
        records = get_bank_records(bank_id)
        records.append(record)
        save_bank_records(bank_id, records)
        update_bank_stats(bank_id, "memories_stored", 1)
        return record

    def recall(self, bank_id: str, query: str) -> dict[str, Any]:
        records = get_bank_records(bank_id)
        if not records:
            return {"results": []}
        query_terms = set(re.findall(r"[a-zA-Z0-9-]+", query.lower()))
        scored: list[tuple[int, dict[str, Any]]] = []
        for rec in records:
            text = f"{rec.get('content', '')} {rec.get('context', '')}".lower()
            overlap = len(query_terms.intersection(re.findall(r"[a-zA-Z0-9-]+", text)))
            if overlap or not query_terms:
                scored.append((overlap, rec))
        scored.sort(key=lambda item: item[0], reverse=True)
        results = [{"text": rec["content"], "type": rec.get("context", "incident")} for _, rec in scored[:5]]
        return {"results": results}

    def reflect(self, bank_id: str, query: str) -> str:
        records = get_bank_records(bank_id)
        if not records:
            return "No stored incidents yet. Teach a resolution to create the first pattern."
        service_map: dict[str, int] = {}
        failed_fixes: list[str] = []
        for rec in records:
            content = rec.get("content", "")
            service_match = re.search(r"service '([^']+)'|service ([A-Za-z0-9\-]+)", content, flags=re.I)
            if service_match:
                service = service_match.group(1) or service_match.group(2)
                service_map[service] = service_map.get(service, 0) + 1
            if "did NOT help" in content.lower() or "did not help" in content.lower():
                failed_fixes.append(content)
        service = max(service_map.items(), key=lambda item: item[1], default=("unknown", 0))[0]
        if failed_fixes:
            return (
                f"Recurring pattern: {service} has the most incident history and several entries highlight failed recovery steps. "
                "The strongest learning is to avoid repeat actions that were explicitly marked as 'did NOT help'."
            )
        return f"Recurring pattern: {service} shows the highest volume of saved incidents in this bank. The team should review the recent post-mortems for repeated corrective actions."


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
            base_url = os.getenv("HINDSIGHT_API_URL", DEFAULT_HINDSIGHT_URL).rstrip("/")
            self.real_client = Hindsight(base_url=base_url, api_key=api_key, timeout=60.0)
            self.use_real = True
        except Exception:
            self.real_client = None
            self.use_real = False

    def ensure_bank(self, bank_id: str) -> None:
        if self.use_real and self.real_client is not None:
            try:
                if hasattr(self.real_client, "create_bank"):
                    self.real_client.create_bank(bank_id=bank_id)
                elif hasattr(self.real_client, "acreate_bank"):
                    self.real_client.acreate_bank(bank_id=bank_id)
                else:
                    self.local.ensure_bank(bank_id)
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
                    return self.real_client.recall(bank_id=bank_id, query=query)
                if hasattr(self.real_client, "arecall"):
                    return self.real_client.arecall(bank_id=bank_id, query=query)
            except Exception:
                pass
        return self.local.recall(bank_id, query)

    def reflect(self, bank_id: str, query: str) -> str:
        if self.use_real and self.real_client is not None:
            try:
                if hasattr(self.real_client, "reflect"):
                    res = self.real_client.reflect(bank_id=bank_id, query=query)
                    if res is not None:
                        return getattr(res, "text", None) or str(res)
                if hasattr(self.real_client, "areflect"):
                    res = self.real_client.areflect(bank_id=bank_id, query=query)
                    if res is not None:
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
    banks = list(BANK_DIR.glob("*.json"))
    names = [path.stem for path in banks]
    if not names:
        names = [SEED_BANK_ID]
    return sorted(set([SEED_BANK_ID] + names))


def seed_demo_data() -> None:
    ensure_state_files()
    bank_id = SEED_BANK_ID
    records = get_bank_records(bank_id)
    if records:
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
    save_bank_records(bank_id, [{"id": f"seed-{idx}", "bank_id": bank_id, "context": "production incident post-mortem", "timestamp": utc_now_iso(), "content": memory} for idx, memory in enumerate(seed_memories, start=1)])
    stats = read_stats()
    stats.setdefault(bank_id, {
        "memories_stored": 0,
        "incidents_analyzed": 0,
        "solved_with_memory": 0,
        "worked": 0,
        "failed": 0,
    })
    write_stats(stats)


def get_groq_client() -> Any:
    if Groq is None:
        raise RuntimeError("The Groq Python SDK is not installed. Run pip install -r requirements.txt.")
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing GROQ_API_KEY in your environment. Add it to .env or use the demo mode.")
    return Groq(api_key=api_key)


def confidence_from_memory_count(count: int) -> str:
    if count == 0:
        return "🔴 0 memories"
    if count <= 2:
        return "🟡 1-2 memories"
    return "🟢 3+ memories"


def build_solver_prompt(memory_text: str, error_log: str, service: str | None) -> str:
    service_clause = f"Service: {service}\n" if service else ""
    return (
        "You are IncidentMind, an on-call AI incident-response copilot for production teams.\n\n"
        "Use only the recalled incidents below. Do not invent historical context or cite anything not in the memory.\n"
        "If there is no relevant history, say 'No relevant history' in the 'Why I think this' section and keep the recommendation cautious and generic.\n\n"
        f"{service_clause}"
        "Recalled incident memory:\n"
        f"{memory_text}\n\n"
        "Incoming issue:\n"
        f"{error_log}\n\n"
        "Return exactly this format, with concise but actionable content:\n"
        "**Likely root cause:** ...\n"
        "**Fix steps:**\n"
        "1. ...\n"
        "**Do NOT try:** ...\n"
        "**Why I think this:** ...\n"
    )


def generate_incident_plan(memory_text: str, error_log: str, service: str | None) -> str:
    model = os.getenv("GROQ_MODEL", "qwen/qwen3-32b")
    client = get_groq_client()
    prompt = build_solver_prompt(memory_text, error_log, service)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a senior production incident responder. Prefer fixes that worked, explicitly warn about failed fixes, and stay within the recalled incident history."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    text = response.choices[0].message.content or ""
    return strip_think_blocks(text) or "No solution generated."


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

    if st.button("Analyze", type="primary", use_container_width=True):
        if not error_log.strip():
            st.warning("Paste an error log before analyzing.")
            return
        bank_id = st.session_state.get("active_bank", SEED_BANK_ID)
        memory_client = get_memory_client()
        memory_client.ensure_bank(bank_id)
        recall_response = memory_client.recall(bank_id, f"{service} {error_log}".strip())
        memory_records = recall_response.get("results", []) if isinstance(recall_response, dict) else []
        memory_text = "\n\n".join(item.get("text", "") for item in memory_records) if memory_records else "No relevant history"
        memory_count = len(memory_records)
        st.session_state["last_memory_text"] = memory_text
        st.session_state["last_memory_count"] = memory_count
        st.session_state["last_plan_input"] = {"service": service, "error_log": error_log}
        update_bank_stats(bank_id, "incidents_analyzed")
        if memory_count > 0:
            update_bank_stats(bank_id, "solved_with_memory")

        st.markdown(f"### Confidence: {confidence_from_memory_count(memory_count)}")
        left, right = st.columns(2)
        with left:
            st.markdown("### Recalled memories")
            if memory_records:
                for idx, item in enumerate(memory_records, start=1):
                    st.markdown(f"**Memory {idx}**")
                    st.write(item.get("text", ""))
            else:
                st.info("No relevant history")
        with right:
            st.markdown("### Action plan")
            try:
                plan = generate_incident_plan(memory_text, error_log, service or None)
                st.markdown(plan)
                st.session_state["last_plan_text"] = plan
            except Exception as exc:
                st.error(f"Unable to generate the plan: {exc}")
                st.session_state["last_plan_text"] = "Unable to generate a plan."

        st.markdown("---")
        feedback_col, note_col = st.columns([1, 3])
        with feedback_col:
            st.markdown("### Was this helpful?")
        with note_col:
            note = st.text_input("Optional note", placeholder="What actually happened or the real cause?")
        if st.button("👍 It worked"):
            submit_feedback(bank_id, error_log, service, st.session_state.get("last_plan_text", ""), "WORKED", note)
            st.success("Thanks — this outcome was retained in memory.")
        if st.button("👎 It didn't work"):
            submit_feedback(bank_id, error_log, service, st.session_state.get("last_plan_text", ""), "FAILED", note)
            st.warning("The failed suggestion was retained so it is not repeated.")


def submit_feedback(bank_id: str, error_log: str, service: str, plan: str, outcome: str, note: str) -> None:
    memory_client = get_memory_client()
    memory_client.ensure_bank(bank_id)
    truncated_plan = plan[:600]
    summary = (
        f"Incident on service '{service or 'unknown'}'. Error: {error_log[:400]}. "
        f"Agent suggested: {truncated_plan}. Engineer feedback: suggested fix {outcome}. "
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
        fix = st.text_area("What fixed it", height=140, placeholder="Rollback the worker and restore the DB route")
        did_not_help = st.text_area("What did NOT help", height=120, placeholder="Restarting the DB listener, increasing heap, etc.")
        outcome = st.selectbox("Outcome", ["SUCCESS", "FAILED"])
        submitted = st.form_submit_button("Save memory")

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
        query = custom_question.strip() or question
        bank_id = st.session_state.get("active_bank", SEED_BANK_ID)
        memory_client = get_memory_client()
        memory_client.ensure_bank(bank_id)
        insight = memory_client.reflect(bank_id, f"{service_focus.strip()} {query}".strip())
        st.markdown(insight)


def render_sidebar() -> None:
    st.sidebar.title("IncidentMind")
    seed_demo_data()
    bank_names = read_bank_names()
    active_bank = st.sidebar.selectbox("Memory bank", bank_names, index=bank_names.index(SEED_BANK_ID) if SEED_BANK_ID in bank_names else 0)
    st.session_state["active_bank"] = active_bank
    if st.sidebar.button("Create fresh bank"):
        fresh_bank = f"demo-fresh-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        get_memory_client().ensure_bank(fresh_bank)
        st.sidebar.success(f"Created fresh bank: {fresh_bank}")
        st.session_state["active_bank"] = fresh_bank
        st.rerun()

    stats = get_bank_stats(active_bank)
    st.sidebar.markdown("### Learning dashboard")
    st.sidebar.metric("Memories stored", stats.get("memories_stored", 0))
    st.sidebar.metric("Incidents analyzed", stats.get("incidents_analyzed", 0))
    st.sidebar.metric("Solved with memory", stats.get("solved_with_memory", 0))
    worked = stats.get("worked", 0)
    failed = stats.get("failed", 0)
    total_attempts = worked + failed
    success_rate = round((worked / total_attempts) * 100, 1) if total_attempts else 0.0
    st.sidebar.metric("Fix success rate", f"{success_rate}%")


def main() -> None:
    st.set_page_config(page_title="IncidentMind", page_icon="🧠", layout="wide")
    render_sidebar()
    st.title("IncidentMind")
    st.caption("An on-call AI copilot that learns from every outage.")

    solver_tab, teach_tab, insights_tab = st.tabs(["Incident Solver", "Teach a Resolution", "Insights"])
    with solver_tab:
        render_solver()
    with teach_tab:
        render_teach_resolution()
    with insights_tab:
        render_insights()


if __name__ == "__main__":
    main()
