import time
import json
import os
from typing import Any, Dict

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from solver import solve_quiz_from_url

app = FastAPI()

EXPECTED_SECRET = os.getenv("QUIZ_SECRET")

class TaskPayload(BaseModel):
    email: str
    secret: str
    url: str

@app.post("/task")
async def task(payload: TaskPayload):
    if not payload.email or not payload.secret or not payload.url:
        raise HTTPException(status_code=400, detail="Missing fields")

    if EXPECTED_SECRET:
        if payload.secret != EXPECTED_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret")
    else:
        print("WARNING: EXPECTED_SECRET not set in environment. Accepting provided secret for debugging.")


    start = time.time()
    try:
        result = solve_quiz_from_url(payload.dict(), timeout_seconds=160)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Solver error: {e}")

    duration = time.time() - start
    return {"status": "ok", "duration": duration, "solver_result": result}
