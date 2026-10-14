# Backend for InvestmentAdvisoryAgent

This folder contains a small FastAPI wrapper around the existing
investment_advisory_agent.py script. It provides endpoints the React frontend
can call: /api/state, /api/run-agent, /api/restart, /api/confirm-action.

Run locally:

1. Create a virtualenv and install requirements:
   python -m venv .venv && source .venv/bin/activate
   pip install -r backend/requirements.txt

2. Run the API:
   uvicorn backend.app.main:app --reload --port 8000

Environment:
- UI_API_TOKEN (optional): if set, the API requires an Authorization: Bearer <token>

Note: the agent performs network calls (yfinance, anthropic) and may send email
via SMTP if SMTP credentials are present in env. Be careful when running in
production.
