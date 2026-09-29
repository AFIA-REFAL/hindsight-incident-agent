"""IncidentMind: an on-call AI copilot that learns from every outage."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import local
from typing import Any

from flask import Flask, flash, redirect, render_template, request, session, url_for
from markdown_it import MarkdownIt
from markupsafe import Markup
from dotenv import load_dotenv

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency for offline demo mode
    OpenAI = None

try:
    from hindsight_client import Hindsight
except Exception:  # pragma: no cover - optional dependency for offline demo mode
    Hindsight = None

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_DIR = PROJECT_ROOT / ".incidentmind"
BANK_DIR = STATE_DIR / "banks"
STATS_PATH = STATE_DIR / "stats.json"
DATABASE_PATH = STATE_DIR / "memory.sqlite3"
SAMPLE_ALERTS = [
    "payments-api: DB unreachable",
    "search-service: OOM after deploy",
    "auth-service: 502 after deploy",
    "checkout-web: disk full on web-02",
    "notification-service: NEW issue, no history",
]

SEED_BANK_ID = "devops-incidents"
DEFAULT_HINDSIGHT_URL = os.getenv("HINDSIGHT_API_URL") or os.getenv("HINDSIGHT_BASE_URL") or "https://api.hindsight.vectorize.io"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_state_files() -> None:
    STATE_DIR.mkdir(exist_ok=True)
    BANK_DIR.mkdir(exist_ok=True)
    if not STATS_PATH.exists():
        STATS_PATH.write_text(json.dumps({}, indent=2), encoding="utf-8")
    with closing(sqlite3.connect(DATABASE_PATH, timeout=10)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                bank_id TEXT NOT NULL,
                id TEXT NOT NULL,
                content TEXT NOT NULL,
                context TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                remote_synced INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (bank_id, id)
            )
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memories)")}
        if "remote_synced" not in columns:
            connection.execute(
                "ALTER TABLE memories ADD COLUMN remote_synced INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute("CREATE TABLE IF NOT EXISTS memory_banks (bank_id TEXT PRIMARY KEY)")
        connection.execute("CREATE INDEX IF NOT EXISTS memories_by_bank ON memories (bank_id)")
        connection.execute("CREATE TABLE IF NOT EXISTS storage_migrations (name TEXT PRIMARY KEY)")
        migrated = connection.execute(
            "SELECT 1 FROM storage_migrations WHERE name = 'legacy-json-v1'"
        ).fetchone()
        if not migrated:
            for bank_file in BANK_DIR.glob("*.json"):
                try:
                    records = json.loads(bank_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO memory_banks (bank_id) VALUES (?)",
                    (bank_file.stem,),
                )
                if not isinstance(records, list):
                    continue
                for record in records:
                    if not isinstance(record, dict) or not record.get("content"):
                        continue
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO memories
                            (bank_id, id, content, context, timestamp, remote_synced)
                        VALUES (?, ?, ?, ?, ?, 0)
                        """,
                        (
                            str(record.get("bank_id") or bank_file.stem),
                            str(record.get("id") or uuid.uuid4().hex),
                            str(record["content"]),
                            str(record.get("context") or "incident"),
                            str(record.get("timestamp") or utc_now_iso()),
                        ),
                    )
            connection.execute(
                "INSERT INTO storage_migrations (name) VALUES ('legacy-json-v1')"
            )


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
    ensure_state_files()
    with closing(sqlite3.connect(DATABASE_PATH, timeout=10)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, bank_id, content, context, timestamp FROM memories WHERE bank_id = ? ORDER BY rowid",
            (bank_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_bank_records(bank_id: str, records: list[dict[str, Any]]) -> None:
    ensure_state_files()
    with closing(sqlite3.connect(DATABASE_PATH, timeout=10)) as connection, connection:
        connection.execute(
            "INSERT OR IGNORE INTO memory_banks (bank_id) VALUES (?)", (bank_id,)
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO memories
                (bank_id, id, content, context, timestamp, remote_synced)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            [
                (
                    bank_id,
                    str(record.get("id") or uuid.uuid4().hex),
                    str(record.get("content", "")),
                    str(record.get("context") or "incident"),
                    str(record.get("timestamp") or utc_now_iso()),
                )
                for record in records
                if record.get("content")
            ],
        )


def get_pending_memories(bank_id: str) -> list[dict[str, Any]]:
    ensure_state_files()
    with closing(sqlite3.connect(DATABASE_PATH, timeout=10)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, bank_id, content, context, timestamp FROM memories WHERE bank_id = ? AND remote_synced = 0 ORDER BY rowid",
            (bank_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_memory_synced(bank_id: str, memory_id: str) -> None:
    with closing(sqlite3.connect(DATABASE_PATH, timeout=10)) as connection, connection:
        connection.execute(
            "UPDATE memories SET remote_synced = 1 WHERE bank_id = ? AND id = ?",
            (bank_id, memory_id),
        )


def get_memory_bank_names() -> list[str]:
    ensure_state_files()
    with closing(sqlite3.connect(DATABASE_PATH, timeout=10)) as connection, connection:
        bank_names = {
            str(row[0])
            for row in connection.execute("SELECT bank_id FROM memory_banks").fetchall()
        }
    bank_names.update(path.stem for path in BANK_DIR.glob("*.json"))
    return sorted(bank_names or {SEED_BANK_ID})


class LocalMemory:
    def ensure_bank(self, bank_id: str) -> None:
        ensure_state_files()
        with closing(sqlite3.connect(DATABASE_PATH, timeout=10)) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO memory_banks (bank_id) VALUES (?)", (bank_id,)
            )

    def retain(self, bank_id: str, content: str, context: str, timestamp: str | None = None) -> dict[str, Any]:
        records = get_bank_records(bank_id)
        record = {
            "id": uuid.uuid4().hex,
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
        service_stats: dict[str, dict[str, int]] = {}
        failed_fixes: dict[str, int] = {}
        root_cause_terms = {
            "network and connectivity": ("firewall", "route", "unreachable", "connection", "connectivity"),
            "cache and resource pressure": ("cache", "redis", "oom", "heap", "memory", "timeout", "queue", "elasticsearch"),
            "deployment and configuration": ("deploy", "config", "secret", "token", "rollback", "version"),
            "disk and log management": ("disk", "log", "partition", "debug output"),
        }
        root_cause_counts = {category: 0 for category in root_cause_terms}
        for rec in records:
            content = str(rec.get("content", ""))
            lowered_content = content.lower()
            match = re.search(r"service '([^']+)'|service ([A-Za-z0-9\-]+)", content, flags=re.I)
            if match:
                service = match.group(1) or match.group(2)
                stats = service_stats.setdefault(service, {"incidents": 0, "failed": 0})
                stats["incidents"] += 1
                outcome = re.search(r"outcome:\s*(SUCCESS|FAILED)", content, flags=re.I)
                if outcome and outcome.group(1).upper() == "FAILED":
                    stats["failed"] += 1
            for failed_fix in re.findall(
                r"(?:what did not help|this fix did not help|did not help)\s*:?\s*([^.;]+)",
                content,
                flags=re.I,
            ):
                normalized_fix = failed_fix.strip().rstrip(",")
                if normalized_fix:
                    failed_fixes[normalized_fix] = failed_fixes.get(normalized_fix, 0) + 1
            for category, terms in root_cause_terms.items():
                if any(term in lowered_content for term in terms):
                    root_cause_counts[category] += 1

        lowered_query = query.lower()
        if any(term in lowered_query for term in ("failed", "did not help", "didn't help")):
            repeated_fixes = sorted(failed_fixes.items(), key=lambda item: item[1], reverse=True)[:3]
            if not repeated_fixes:
                return "No explicitly failed recovery steps are recorded in this memory bank."
            details = "; ".join(
                f"{fix} ({count} record{'s' if count != 1 else ''})"
                for fix, count in repeated_fixes
            )
            return f"Recorded unsuccessful recovery steps: {details}. Avoid repeating these without new evidence."

        if any(term in lowered_query for term in ("least reliable", "reliability", "most unreliable")):
            ranked_services = sorted(
                service_stats.items(),
                key=lambda item: (
                    item[1]["failed"] / item[1]["incidents"] if item[1]["incidents"] else 0,
                    item[1]["failed"],
                    item[1]["incidents"],
                ),
                reverse=True,
            )
            if not ranked_services:
                return "No service-level incident data is available in this memory bank."
            service, stats = ranked_services[0]
            failure_rate = round(stats["failed"] / stats["incidents"] * 100)
            return (
                f"By recorded outcomes, {service} has the highest failure rate: "
                f"{stats['failed']} failed of {stats['incidents']} incidents ({failure_rate}%)."
            )

        recurring_categories = sorted(
            ((category, count) for category, count in root_cause_counts.items() if count),
            key=lambda item: item[1],
            reverse=True,
        )[:3]
        if recurring_categories:
            summary = "; ".join(f"{category} ({count} records)" for category, count in recurring_categories)
            return (
                f"Recurring incident themes in this memory bank include {summary}. "
                "These are text-based patterns; review the matching incidents to confirm root causes."
            )

        top_service = max(
            service_stats.items(), key=lambda item: item[1]["incidents"], default=("unknown", {"incidents": 0})
        )[0]
        if failed_fixes:
            return (
                f"Recurring pattern: {top_service} has the most incident history and entries record failed recovery steps. "
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
        self.remote_error: str | None = None
        self._init_real_client()

    def _set_remote_error(self, error: Exception) -> None:
        self.remote_error = f"{type(error).__name__}: {error}"[:300]

    def close(self) -> None:
        if self.real_client is not None:
            close = getattr(self.real_client, "close", None)
            if callable(close):
                close()

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
        except Exception as exc:
            self._set_remote_error(exc)

    def _retain_remotely(self, record: dict[str, Any]) -> None:
        if self.real_client is None:
            return
        timestamp = datetime.fromisoformat(
            str(record["timestamp"]).replace("Z", "+00:00")
        )
        self.real_client.retain(
            bank_id=str(record["bank_id"]),
            content=str(record["content"]),
            context=str(record["context"]),
            timestamp=timestamp,
            document_id=str(record["id"]),
        )
        mark_memory_synced(str(record["bank_id"]), str(record["id"]))

    def _sync_pending(self, bank_id: str) -> None:
        if not self.use_real or self.real_client is None:
            return
        try:
            for record in get_pending_memories(bank_id):
                self._retain_remotely(record)
            self.remote_error = None
        except Exception as exc:
            self._set_remote_error(exc)

    def ensure_bank(self, bank_id: str) -> None:
        self.local.ensure_bank(bank_id)
        if not self.use_real or self.real_client is None:
            return
        try:
            self.real_client.create_bank(
                bank_id=bank_id,
                name=bank_id,
                mission="Store incident history and failed fixes for the team.",
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            if status != 409 and "already exists" not in str(exc).lower():
                self._set_remote_error(exc)
                return
        self._sync_pending(bank_id)

    def retain(self, bank_id: str, content: str, context: str, timestamp: str | None = None) -> Any:
        record = self.local.retain(bank_id, content, context, timestamp)
        self._sync_pending(bank_id)
        return record

    def recall(self, bank_id: str, query: str) -> dict[str, Any]:
        local_results = self.local.recall(bank_id, query).get("results", [])
        remote_results: list[dict[str, Any]] = []
        if self.use_real and self.real_client is not None:
            try:
                response = self.real_client.recall(
                    bank_id=bank_id, query=query, max_tokens=1200, budget="high"
                )
                remote_results = normalize_recall_response(response)
                if not get_pending_memories(bank_id):
                    self.remote_error = None
            except Exception as exc:
                self._set_remote_error(exc)
        combined: list[dict[str, Any]] = []
        seen_text: set[str] = set()
        for result in [*remote_results, *local_results]:
            text = str(result.get("text", "")).strip()
            key = text.casefold()
            if text and key not in seen_text:
                combined.append({**result, "text": text})
                seen_text.add(key)
        return {"results": combined[:8]}

    def reflect(self, bank_id: str, query: str) -> str:
        local_insight = self.local.reflect(bank_id, query)
        if self.use_real and self.real_client is not None:
            try:
                response = self.real_client.reflect(bank_id=bank_id, query=query, budget="low")
                if not get_pending_memories(bank_id):
                    self.remote_error = None
                return getattr(response, "text", None) or str(response)
            except Exception as exc:
                self._set_remote_error(exc)
        return local_insight


def get_memory_client() -> MemoryClient:
    memory_client = getattr(_memory_clients, "client", None)
    if memory_client is None:
        memory_client = MemoryClient()
        _memory_clients.client = memory_client
    return memory_client


def strip_think_blocks(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def read_bank_names() -> list[str]:
    return sorted(set([SEED_BANK_ID] + get_memory_bank_names()))


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


def get_openrouter_client() -> Any:
    if OpenAI is None:
        raise RuntimeError("The OpenAI SDK is not installed. Run pip install -r requirements.txt.")
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY in your environment. Add it to .env.")
    return OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")


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
    client = get_openrouter_client()
    response = client.chat.completions.create(
        model=os.getenv("OPENROUTER_MODEL", "openrouter/free"),
        messages=[{"role": "user", "content": build_solver_prompt(memory_text, error_log, service)}],
        temperature=0.2,
    )
    text = response.choices[0].message.content or ""
    return strip_think_blocks(text) or "No solution generated."


def format_plan_error(error: Exception) -> tuple[str, str]:
    error_text = str(error).lower()
    status_code = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status_code == 401 or "authenticationerror" in error_text or "user not found" in error_text:
        return (
            "OpenRouter rejected the API key (401). Create or copy a valid key from your OpenRouter account, "
            "set OPENROUTER_API_KEY in .env, and restart the app.",
            "Plan unavailable because OpenRouter authentication failed. Update the API key and retry.",
        )
    if status_code == 429 or "rate_limit" in error_text or "quota" in error_text:
        return (
            "OpenRouter's free-model rate limit was reached. Incident history was recalled "
            "successfully, but plan generation is temporarily unavailable. Retry later or "
            "choose another available model in OPENROUTER_MODEL.",
            "Plan unavailable. Review the recalled history above and retry later.",
        )
    return (
        "OpenRouter could not generate a plan. Check your API key, configured model, and model availability.",
        "Plan unavailable. Review the recalled history above and try again later.",
    )


def normalize_recall_response(response: Any) -> list[dict[str, str]]:
    if isinstance(response, dict):
        payload = response
    elif hasattr(response, "model_dump"):
        try:
            payload = response.model_dump()
        except Exception:
            return []
    else:
        payload = {"results": getattr(response, "results", None)}
    values = payload.get("results")
    if not values:
        return []
    normalized: list[dict[str, str]] = []
    for value in values:
        if isinstance(value, dict):
            item = value
        elif hasattr(value, "model_dump"):
            item = value.model_dump()
        else:
            item = {"text": getattr(value, "text", ""), "type": getattr(value, "type", "")}
        text = str(item.get("text", "")).strip()
        if text:
            normalized.append({"text": text, "type": str(item.get("type") or item.get("context") or "incident")})
    return normalized


def submit_feedback(bank_id: str, error_log: str, service: str, plan: str, outcome: str, note: str) -> MemoryClient:
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
    return memory_client


QUESTION_OPTIONS = [
    "What recurring root causes appear across incidents?",
    "Which fixes repeatedly failed?",
    "Which service is least reliable?",
]
_memory_clients = local()
_analysis_results: dict[str, dict[str, Any]] = {}
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(32)
markdown_renderer = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")


@app.teardown_appcontext
def close_request_memory_client(_error: BaseException | None) -> None:
    memory_client = getattr(_memory_clients, "client", None)
    if memory_client is not None:
        del _memory_clients.client
        memory_client.close()


def active_bank() -> str:
    bank_id = session.get("active_bank", SEED_BANK_ID)
    return bank_id if bank_id in read_bank_names() else SEED_BANK_ID


def page_context() -> dict[str, Any]:
    bank_id = active_bank()
    memory_client = get_memory_client()
    stats = get_bank_stats(bank_id)
    worked = stats.get("worked", 0)
    failed = stats.get("failed", 0)
    attempts = worked + failed
    return {
        "active_bank": bank_id,
        "bank_names": read_bank_names(),
        "stats": stats,
        "success_rate": round(worked / attempts * 100, 1) if attempts else 0,
        "storage_label": "SQLite + Hindsight" if memory_client.use_real else "Local SQLite",
        "storage_error": memory_client.remote_error,
    }


@app.get("/")
def home() -> str:
    seed_demo_data()
    return render_template(
        "solver.html",
        **page_context(),
        sample_alerts=SAMPLE_ALERTS,
        analysis=None,
    )


@app.post("/analyze")
def analyze() -> str:
    error_log = request.form.get("error_log", "").strip()
    service = request.form.get("service", "").strip()
    if not error_log:
        flash("Add an alert or error log before starting the analysis.", "error")
        return redirect(url_for("home"))

    bank_id = active_bank()
    memory_client = get_memory_client()
    memory_client.ensure_bank(bank_id)
    memory_records = normalize_recall_response(
        memory_client.recall(bank_id, f"{service} {error_log}".strip())
    )
    memory_text = "\n\n".join(item["text"] for item in memory_records) or "No relevant history"
    update_bank_stats(bank_id, "incidents_analyzed")
    if memory_records:
        update_bank_stats(bank_id, "solved_with_memory")

    try:
        plan = generate_incident_plan(memory_text, error_log, service or None)
        plan_error = None
    except Exception as exc:
        plan_error, plan = format_plan_error(exc)

    analysis_id = uuid.uuid4().hex
    analysis = {
        "id": analysis_id,
        "bank_id": bank_id,
        "service": service,
        "error_log": error_log,
        "memories": memory_records,
        "memory_count": len(memory_records),
        "plan": plan,
        "plan_error": plan_error,
        "feedback_submitted": False,
    }
    _analysis_results[analysis_id] = analysis
    return render_template("solver.html", **page_context(), sample_alerts=SAMPLE_ALERTS, analysis=analysis)


@app.post("/feedback")
def feedback() -> Any:
    analysis = _analysis_results.get(request.form.get("analysis_id", ""))
    outcome = request.form.get("outcome", "")
    if not analysis or analysis["feedback_submitted"] or outcome not in {"WORKED", "FAILED"}:
        flash("This analysis is no longer available for feedback.", "error")
        return redirect(url_for("home"))
    submit_feedback(
        analysis["bank_id"], analysis["error_log"], analysis["service"],
        analysis["plan"], outcome, request.form.get("note", ""),
    )
    analysis["feedback_submitted"] = True
    flash("Outcome saved to incident memory.", "success")
    return redirect(url_for("home"))


@app.route("/teach", methods=["GET", "POST"])
def teach() -> Any:
    if request.method == "POST":
        fields = {key: request.form.get(key, "").strip() for key in ("service", "error", "fix", "did_not_help")}
        if not all(fields.values()):
            flash("Complete each field before saving the resolution.", "error")
        else:
            bank_id = active_bank()
            memory_client = get_memory_client()
            memory_client.ensure_bank(bank_id)
            content = (
                f"Incident on service '{fields['service']}'. Error: {fields['error']}. "
                f"Resolution: {fields['fix']}. What did NOT help: {fields['did_not_help']}. "
                f"Outcome: {request.form.get('outcome', 'SUCCESS')}."
            )
            memory_client.retain(bank_id, content, "production incident post-mortem", utc_now_iso())
            flash("Resolution added to the active incident memory.", "success")
            return redirect(url_for("teach"))
    return render_template("teach.html", **page_context())


@app.route("/insights", methods=["GET", "POST"])
def insights() -> str:
    insight = None
    insight_html = None
    if request.method == "POST":
        question = (request.form.get("custom_question", "").strip()
                    or request.form.get("question", QUESTION_OPTIONS[0])).strip()
        service_focus = request.form.get("service_focus", "").strip()
        memory_client = get_memory_client()
        bank_id = active_bank()
        memory_client.ensure_bank(bank_id)
        insight = memory_client.reflect(bank_id, f"{service_focus} {question}".strip())
        insight_html = Markup(markdown_renderer.render(insight))
    return render_template(
        "insights.html", **page_context(), questions=QUESTION_OPTIONS,
        insight=insight, insight_html=insight_html,
    )


@app.post("/banks")
def select_bank() -> Any:
    action = request.form.get("action")
    if action == "create":
        bank_id = f"demo-fresh-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        get_memory_client().ensure_bank(bank_id)
        session["active_bank"] = bank_id
        flash(f"Created fresh memory bank: {bank_id}", "success")
    else:
        requested_bank = request.form.get("bank_id", "")
        if requested_bank in read_bank_names():
            session["active_bank"] = requested_bank
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=int(os.getenv("PORT", "5000")))
