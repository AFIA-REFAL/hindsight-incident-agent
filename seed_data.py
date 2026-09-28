"""Seed the devops-incidents Hindsight bank with synthetic incidents."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_bank_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except json.JSONDecodeError:
        return []


def save_bank_records(path: Path, records: list[dict]) -> None:
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


def build_seed_memories() -> list[str]:
    return [
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the IncidentMind memory bank with synthetic incidents.")
    parser.add_argument("--bank-id", default="devops-incidents", help="Target memory bank ID.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    bank_dir = root / ".incidentmind" / "banks"
    bank_dir.mkdir(parents=True, exist_ok=True)
    bank_file = bank_dir / f"{args.bank_id}.json"

    records = []
    for idx, memory in enumerate(build_seed_memories(), start=1):
        records.append(
            {
                "id": f"seed-{idx}",
                "bank_id": args.bank_id,
                "context": "production incident post-mortem",
                "timestamp": utc_now_iso(),
                "content": memory,
            }
        )

    save_bank_records(bank_file, records)
    print(f"Seeded {len(records)} incidents into {args.bank_id} at {bank_file}")


if __name__ == "__main__":
    main()
