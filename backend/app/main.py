# FastAPI backend for InvestmentAdvisoryAgent
# Serves simple REST endpoints for the React frontend.

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import os
import subprocess
import json
import threading

from agent_runner import (
    read_state_locked,
    write_state_locked,
    run_agent_subprocess,
    restart_portfolio_wrapper,
    confirm_action_wrapper,
)

API_TOKEN = os.environ.get("UI_API_TOKEN")  # optional simple bearer token

app = FastAPI(title="Investment Advisory API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_token(request: Request):
    if not API_TOKEN:
        return True
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization")
    token = auth.split(" ", 1)[1]
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    return True


class RunResponse(BaseModel):
    ok: bool
    output: str


class RestartRequest(BaseModel):
    auto_buy: Optional[bool] = True


class ConfirmRequest(BaseModel):
    action_id: str
    type: str  # "sell" or "buy"


@app.get("/api/state")
def get_state(_=Depends(require_token)):
    state = read_state_locked()
    return {"state": state}


@app.post("/api/run-agent", response_model=RunResponse)
def run_agent(_=Depends(require_token)):
    out = run_agent_subprocess()
    return {"ok": True, "output": out}


@app.post("/api/restart")
def restart(req: RestartRequest, _=Depends(require_token)):
    state = restart_portfolio_wrapper(auto_buy=req.auto_buy)
    return {"ok": True, "state": state}


@app.post("/api/confirm-action")
def confirm_action(req: ConfirmRequest, _=Depends(require_token)):
    try:
        tx = confirm_action_wrapper(req.action_id, req.type)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "tx": tx}


@app.get("/api/health")
def health():
    return {"ok": True}
