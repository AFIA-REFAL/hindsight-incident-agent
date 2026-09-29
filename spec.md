# IncidentMind: Project Specification

**Version:** 1.0
**Event:** Hindsight Memory Hackathon (Vectorize)
**Tagline:** An on-call AI copilot that learns from every outage.

---

## 1. Overview

### 1.1 Problem
When production systems fail, engineers lose hours searching old tickets, Slack threads, and post-mortems. Institutional knowledge lives in individuals' heads and is lost when they are unavailable or leave. Teams repeat fixes that already failed before.

### 1.2 Solution
IncidentMind is an AI incident-response copilot backed by **Hindsight** persistent memory. It:
1. **Retains** every incident, resolution, and outcome (including failed fixes).
2. **Recalls** the most relevant past incidents whenever a new error appears.
3. **Learns** from engineer feedback on its own suggestions.
4. **Reflects** on recurring patterns across services.

### 1.3 Differentiator
Most tools *search* past incidents. IncidentMind *learns from experience*: it records what failed, warns against repeating it, and improves measurably between Interaction 1 and Interaction N.

### 1.4 Target users
- On-call engineers and SREs
- DevOps and platform teams
- Engineering managers reviewing reliability patterns

### 1.5 Non-goals
- Not a general-purpose chatbot
- Not a student or education tool
- No automated remediation (the agent advises, humans act)
- No real production log ingestion in v1 (synthetic and pasted data only)

---

## 2. Goals and Success Criteria

| Goal | Success measure |
|---|---|
| Memory is the centerpiece | Demo clearly shows a different answer before vs. after memory exists |
| Learns from failure | Agent warns "do NOT try X" based on a stored failed fix |
| Real-world B2B framing | Realistic multi-service incident data and downtime-cost framing |
| Fast demo | Full demo loop under 60 seconds |
| Clean delivery | Documented repo, live demo, video, article and social posts |

### Judging criteria mapping
| Criterion | Weight | How the project addresses it |
|---|---|---|
| Innovation | 30% | Learns from failed fixes, feedback loop, cross-service pattern reflection |
| Use of Hindsight | 25% | `retain`, `recall`, and `reflect` all used; memory shown next to AI output |
| Technical Implementation | 20% | Layered architecture, error handling, cached clients, prompt constraints |
| User Experience | 15% | Two-panel view, sample alerts, confidence badge, dashboard, fresh-bank demo mode |
| Real-world Impact | 10% | Realistic incidents, downtime and time-to-fix framing |

---

## 3. Functional Requirements

### FR-1 Incident Solver
- **Input:** raw error log or alert text (required), service name (optional), or a preloaded sample alert.
- **Process:**
  1. Build query = `"{service} {error_log}"`.
  2. Call Hindsight `recall` on the active memory bank.
  3. Pass recalled memories and the error to the LLM with a constrained system prompt.
- **Output:**
  - Recalled memories panel (raw memory text from Hindsight)
  - Action plan panel in a fixed format: Likely root cause, Fix steps, Do NOT try, Why I think this
  - Confidence badge: 🔴 0 memories (new issue), 🟡 1 to 2 (partial match), 🟢 3 or more (strong match)
- **Rules:** the LLM must not invent history and must cite only recalled incidents.

### FR-2 Feedback Loop
- After a plan is shown, the engineer can click **👍 It worked** or **👎 It didn't work**, with an optional note (real cause or what was actually done).
- The outcome, error, service, and truncated plan are stored via `retain` with context `"agent suggestion feedback"`.
- Feedback can be submitted once per analysis.
- Effect: future recalls include whether earlier suggestions succeeded or failed.

### FR-3 Teach a Resolution
- Form fields: service (required), error or symptom (required), what fixed it and what did NOT help (required), outcome (SUCCESS or FAILED).
- On submit, store one memory via `retain` with context `"production incident post-mortem"`.
- The form clears on submit and shows a confirmation.

### FR-4 Insights (Reflect)
- Preset questions plus a custom question, with an optional service focus.
- Calls Hindsight `reflect` on the active bank and displays the returned insight.
- Example output: recurring root causes, fixes that repeatedly failed, least reliable service.

### FR-5 Learning Dashboard (sidebar)
Per-bank counters, persisted locally in `stats.json`:
- Memories stored (via this app)
- Incidents analyzed
- Solved with memory (analyses where at least one memory was recalled)
- Fix success rate (worked / (worked + failed))

Seeded memories are not counted in "stored" because they are inserted by `seed_data.py`.

### FR-6 Memory Bank Modes
- **Seeded team memory:** bank `devops-incidents`, preloaded by `seed_data.py`.
- **Fresh empty memory:** auto-generated bank `demo-fresh-<timestamp>` for the "Interaction 1" demo. The user can create a new fresh bank on demand.

### FR-7 Seed Data
`seed_data.py` inserts 12 synthetic incidents across five services (payments-api, search-service, auth-service, checkout-web, plus Elasticsearch/Redis-related events). It includes deliberate "this fix did NOT help" cases and repeat errors with different root causes. It accepts an optional bank id argument.

---

## 4. System Architecture

```
                     ┌──────────────────────────────┐
                     │         Flask UI            │
                     │ Solver | Teach | Insights    │
                     │ Navigation + dashboard       │
                     └──────┬──────────────┬────────┘
                            │              │
              recall / retain / reflect    │ chat completion
                            │              │
                 ┌──────────▼───────┐  ┌───▼──────────┐
                 │    Hindsight     │  │  Gemini LLM  │
                 │ (memory banks)   │  │ google-genai │
                 └──────────────────┘  └──────────────┘
                            │
                      stats.json (local counters)
```

### 4.1 Components
| Component | Responsibility |
|---|---|
| `app.py` | Flask routes, orchestration, prompts, stats |
| `templates/` and `static/` | Responsive web interface and styles |
| `seed_data.py` | One-time synthetic data loader |
| Hindsight (`hindsight-client`) | Persistent memory: retain, recall, reflect |
| Gemini (`google-genai`) | Incident plan generation |
| `stats.json` | Local per-bank dashboard counters |

### 4.2 Tech stack
- Python 3.10+
- Flask (UI)
- `hindsight-client` (memory SDK)
- `google-genai` (LLM SDK)
- `python-dotenv` (config)

### 4.3 Configuration (`.env`)
| Variable | Purpose | Default |
|---|---|---|
| `GEMINI_API_KEY` | Gemini authentication | none (required) |
| `HINDSIGHT_API_KEY` | Hindsight authentication | none (required) |
| `HINDSIGHT_API_URL` | Hindsight endpoint | `https://api.hindsight.vectorize.io` |
| `GEMINI_MODEL` | LLM model override | `gemini-3.8-flash` |

---

## 5. Memory Design

### 5.1 Banks
- One bank per team or environment. Shared memory means every engineer benefits from every incident.
- `devops-incidents`: seeded demo memory.
- `demo-fresh-<timestamp>`: empty bank for before/after demos.

### 5.2 Memory content format
Every memory is a self-contained natural-language record so Hindsight's extraction works well.

**Incident post-mortem** (`context="production incident post-mortem"`)
```
Incident on service '<service>'. Error: <error>. Resolution: <resolution incl. what did NOT help> Outcome: <SUCCESS|FAILED>.
```

**Agent feedback** (`context="agent suggestion feedback"`)
```
Incident on service '<service>'. Error: <error, first 400 chars>. Agent suggested: <plan, first 600 chars>. Engineer feedback: suggested fix <WORKED|FAILED>. Note: <optional>
```

Each `retain` also passes a `timestamp` (UTC ISO 8601) so Hindsight can reason about time.

### 5.3 Memory operations
| Operation | When | Call |
|---|---|---|
| Retain | Teach form submit, feedback click, seeding | `retain(bank_id, content, context, timestamp)` |
| Recall | Every Solver analysis | `recall(bank_id, query)` reads `.results[].text` |
| Reflect | Insights tab | `reflect(bank_id, query)` reads `.text` (falls back to `str(res)`) |

### 5.4 Design principles
1. **Store failures explicitly.** "Did NOT help" is the most valuable signal.
2. **Recall before every LLM call.** Memory is injected into the prompt, never skipped.
3. **Close the loop.** Outcomes of the agent's own advice become new memory.
4. **Keep memories self-contained**, with service, error, action, and outcome in one record.

---

## 6. LLM Prompt Contract

**System prompt rules**
- Prefer fixes that worked; explicitly warn about fixes that failed.
- Never invent history; reference only recalled incidents.
- If memory is empty or irrelevant, say "No relevant history" and give a cautious generic plan.
- Be concise and actionable.

**Required output format**
```
**Likely root cause:** ...
**Fix steps:**
1. ...
**Do NOT try:** ...
**Why I think this:** ...
```

**Parameters:** temperature 0.2. `<think>...</think>` blocks are stripped from the response (needed for Qwen3).

---

## 7. UX Specification

### 7.1 Layout
- **Navigation:** memory bank selector, active bank, learning dashboard.
- **Incident Solver:** sample alert dropdown, service field, error text area, Analyze button, confidence badge, recalled history and action plan, feedback buttons.
- **Teach a Resolution:** post-mortem form, save button, confirmation.
- **Insights:** question selector, service focus, Reflect button, insight output.

### 7.2 Sample alerts (for demos)
| Sample | Expected behavior in seeded bank |
|---|---|
| payments-api: DB unreachable | Recalls firewall incident; warns restart did not help |
| search-service: OOM | Recalls that a heap bump alone failed; points to query cache |
| auth-service: 502 after deploy | Recalls rollback fix |
| checkout-web: disk full | Recalls logrotate and the debug-flag incident |
| NEW: notification-service | 🔴 No history; generic cautious plan |

### 7.3 State handling
Analysis results are held by the Flask process for feedback submission and are recorded once per analysis.

---

## 8. Non-Functional Requirements

| Area | Requirement |
|---|---|
| Performance | Recall and LLM response within a few seconds; demo loop under 60s |
| Reliability | Hindsight and Gemini calls are wrapped in try/except with a visible UI error |
| Security | Keys in `.env`, excluded via `.gitignore`; no secrets in the repo |
| Portability | Runs locally with `python app.py`; deployable to a Python web host |
| Maintainability | Flask routes and templates keep UI separate from shared incident and memory logic |

---

## 9. Demo Script (60 seconds)

| Time | Action | What judges see |
|---|---|---|
| 0:00 to 0:10 | State the problem: downtime costs thousands per hour and knowledge is lost | Framing |
| 0:10 to 0:25 | **Fresh empty memory**, analyze an error | 🔴 No history, generic plan |
| 0:25 to 0:35 | Teach the resolution or click 👍/👎 | Memory saved |
| 0:35 to 0:50 | Analyze a similar error | Agent recalls the exact past fix and warns about the failed one |
| 0:50 to 1:00 | Insights tab, then dashboard | Pattern summary; counters rising |

Closing line: "Every outage teaches something. IncidentMind makes sure it is never forgotten."

---

## 10. Deliverables Checklist

- [ ] GitHub repo with clean code and README (problem, architecture, Hindsight usage, setup)
- [ ] Live demo (Python web host or similar)
- [ ] Demo video (60 to 90 seconds)
- [ ] Explanation of Hindsight integration (retain, recall, reflect)
- [ ] Article from each team member
- [ ] Social media post from each team member
- [ ] Synthetic dataset documented in the repo

---

## 11. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Hindsight retain processing delay | Seed data early; wait 30 to 60s before recording |
| SDK or auth argument differences | Client init falls back to no `api_key`; verify against the Hindsight docs |
| `reflect` response shape differs | Uses `.text` with `str(res)` fallback; inspect the object if output looks odd |
| Gemini model unavailable or renamed | Override via `GEMINI_MODEL` |
| LLM hallucinating history | Strict system prompt plus recalled memory shown beside the answer |
| Weak recall on vague queries | Include service name in the query; keep seed data specific |
| Scope creep | Ship the MVP (FR-1 to FR-3) first, then add FR-4 and FR-5 |

---

## 12. Assumptions and Items to Verify

These were not confirmed against live services at build time and should be checked on first run:
1. Hindsight cloud base URL and the exact client authentication argument.
2. `retain` accepts `timestamp` as an ISO string in the installed client version.
3. `reflect` returns an object with a `.text` attribute.
4. The configured Gemini model is available to the project's API key.

---

## 13. Future Work (post-hackathon)

- Slack or PagerDuty alert ingestion that auto-triggers the Solver
- Per-service memory tags or mental models for finer recall
- Runbook linking and automated post-mortem drafting
- Incident severity and cost-of-downtime tracking
- Team-level memory analytics (MTTR trend before vs. after adoption)
- Role-based memory banks per team or environment

---

## 14. Repository Layout

```
incidentmind/
├── app.py
├── seed_data.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── spec.md
```