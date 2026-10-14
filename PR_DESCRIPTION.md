Update: add React + FastAPI WebUI (feature/webui-react)

This PR adds a minimal backend (FastAPI) and frontend (React via Vite) for a
web-based UI for the InvestmentAdvisoryAgent. It provides endpoints to read
state, run the agent, restart the portfolio (immediately perform buys), and
confirm/execute pending actions — no GitHub issues required.

How to run locally (quick):
- Backend: python -m venv .venv && source .venv/bin/activate && pip install -r backend/requirements.txt && uvicorn backend.app.main:app --reload
- Frontend: cd frontend && npm install && npm run dev

Or using docker-compose (requires Docker):
- docker-compose up --build

Security: if you set UI_API_TOKEN env var, the API requires Authorization: Bearer <token>.
