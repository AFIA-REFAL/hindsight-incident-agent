# IncidentMind

IncidentMind is an on-call AI incident-response copilot that learns from every outage. It stores post-mortems and failed fixes in a memory bank, recalls the relevant past incidents for new alerts, and reflects on recurring patterns across services.

## What it does

- Incident solver: analyze an alert or error log and retrieve similar historical incidents
- Memory learning loop: accept feedback on whether a suggested fix worked
- Teach a resolution: save a new incident resolution and failed steps into memory
- Insights: ask pattern questions across the active memory bank
- Dashboard: track memory usage, incident counts, and fix success rate
- Durable local storage: every memory is stored in SQLite and synced to Hindsight when available

## Tech stack

- Python 3.10+
- Flask
- Gemini (`google-genai`)
- Hindsight memory client
- SQLite local memory database for restart-safe storage and offline usage

## Quick start

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Create a `.env` file based on `.env.example` and fill in the keys:

   ```bash
   copy .env.example .env
   ```

3. Run the app:

   ```bash
   python app.py
   ```

4. Optional: seed a bank manually:

   ```bash
   python seed_data.py --bank-id devops-incidents
   ```

Memories and bank names are stored in `.incidentmind/memory.sqlite3`. Existing JSON memories in `.incidentmind/banks/` are imported automatically once. When Hindsight is configured, locally saved memories are synced during the next bank operation; if Hindsight is unavailable, they remain in SQLite and the app displays the sync status. Keep the SQLite database on the same machine to preserve local data. Runtime database and counter files are excluded from Git; the checked-in JSON bank remains as a one-time import source.

## Environment variables

- `GEMINI_API_KEY`: Gemini authentication key
- `GEMINI_MODEL`: LLM model override, default `gemini-3.8-flash`
- `HINDSIGHT_API_KEY`: Hindsight API key
- `HINDSIGHT_API_URL`: Hindsight endpoint, default `https://api.hindsight.vectorize.io`

## Demo flow

1. Start with a fresh empty bank
2. Analyze a sample alert to show no-history guidance
3. Teach the resolution or submit feedback
4. Analyze a similar alert again and confirm the memory is recalled
5. Open the Insights tab and the dashboard to review trends

## Repository structure

- `app.py`: Flask UI routes and incident orchestration
- `templates/`: responsive incident solver, resolution, and insights screens
- `static/styles.css`: application styling
- `seed_data.py`: synthetic incident dataset loader for the seeded bank
- `requirements.txt`: Python dependencies
- `.env.example`: environment variable template
