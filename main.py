# main.py
"""
Task orchestration endpoint for LLM Analysis Quiz
- Accepts POST /task with JSON { email, secret, url, ... }.
- Validates JSON and secret (compares to env QUIZ_SECRET if set).
- Calls async solver.solve_quiz_from_url to visit the quiz page and attempt solving/submitting.
- Follows returned URLs (prefer the URL returned by the submit response).
- Enforces a total 3-minute window (180s) and per-quiz timeout (configurable).
- Protects against cycles (visited set) and avoids re-visiting the same URL.
- Respects 'delay' in submit response by awaiting it (capped).
"""
import os
import time
import asyncio
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import httpx

from solver import solve_quiz_from_url  # your async solver module (must be present)

app = FastAPI(title="LLM Analysis Quiz Orchestrator")

# Configure via environment
EXPECTED_SECRET = os.getenv("QUIZ_SECRET")  # if set, enforce; if not set, we accept provided secret (useful locally)
MAX_TOTAL_SECONDS = int(os.getenv("MAX_TOTAL_SECONDS", "180"))  # 3 minutes default
PER_QUIZ_TIMEOUT = int(os.getenv("PER_QUIZ_TIMEOUT", "60"))  # per-quiz timeout (seconds)

class TaskPayload(BaseModel):
    email: str
    secret: str
    url: str
    class Config:
        extra = "allow"

def _validate_payload_json(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    for key in ("email", "secret", "url"):
        if key not in data or not isinstance(data[key], str) or data[key].strip() == "":
            raise HTTPException(status_code=400, detail=f"Missing or invalid field: {key}")

@app.post("/task")
async def task(request: Request):
    """
    Main entrypoint. Accepts JSON payload, validates, then orchestrates solver runs until
    no next URL or timeout. Returns a summary JSON and HTTP 200 on success.
    """
    # Parse JSON
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    _validate_payload_json(body)
    email = body["email"]
    secret = body["secret"]
    start_url = body["url"]

    # Secret check
    if EXPECTED_SECRET:
        if secret != EXPECTED_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret")
    else:
        # For local testing convenience we allow any secret when QUIZ_SECRET not set,
        # but still log a warning.
        print("WARNING: QUIZ_SECRET not set in environment; accepting provided secret for debugging.")

    # Orchestration timing
    start_time = time.monotonic()
    deadline = start_time + MAX_TOTAL_SECONDS

    run_history = []
    current_url = start_url
    last_submission = None

    # visited set prevents cycles / infinite re-visits
    visited = set()
    visited.add(current_url)

    # loop: visit current_url, let solver run, record results, decide next_url
    while current_url and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        # leave small slack; ensure at least 10s for a quiz run
        per_quiz = min(PER_QUIZ_TIMEOUT, max(10, int(remaining - 5)))
        if per_quiz <= 0:
            break

        step = {
            "quiz_url": current_url,
            "start_at": time.time(),
            "solve_result": None,
            "submit_result": None,
        }

        # Run solver with a bounded timeout
        try:
            solver_result = await asyncio.wait_for(
                solve_quiz_from_url({"email": email, "secret": secret, "url": current_url}, timeout_seconds=per_quiz),
                timeout=per_quiz + 5,
            )
            step["solve_result"] = solver_result
        except asyncio.TimeoutError:
            solver_result = {"error": "solver_timeout", "timeout_seconds": per_quiz}
            step["solve_result"] = solver_result
        except Exception as e:
            solver_result = {"error": "solver_exception", "exception": str(e)}
            step["solve_result"] = solver_result

        submit_resp = None
        next_url = None

        # Prefer submit response's 'url' as canonical next URL (if provided)
        if isinstance(solver_result, dict):
            # if solver returned a 'submitted' field (submit endpoint response)
            candidate_submitted = solver_result.get("submitted")
            if isinstance(candidate_submitted, dict):
                submit_resp = candidate_submitted
                step["submit_result"] = submit_resp
                # prefer the submit response's url if present
                resp_url = submit_resp.get("url")
                if isinstance(resp_url, str) and resp_url.strip():
                    next_url = resp_url

            # If submit response didn't supply next_url, consider solver-level 'url' but only if different
            if not next_url:
                candidate = solver_result.get("url")
                if isinstance(candidate, str) and candidate.strip() and candidate != current_url:
                    next_url = candidate

            # fallback: check other key names
            if not next_url:
                for k in ("next_url", "next", "new_url"):
                    cand = solver_result.get(k)
                    if isinstance(cand, str) and cand.strip() and cand != current_url:
                        next_url = cand
                        break

        # Normalize next_url if it's relative
        if next_url:
            try:
                if next_url.startswith("/"):
                    # join relative to current_url
                    next_url = httpx.URL(current_url).join(next_url).human_repr()
            except Exception:
                pass

        # If submit_resp indicates correctness and no next url, stop immediately
        if submit_resp:
            if submit_resp.get("correct") is True and not submit_resp.get("url"):
                step["note"] = "answer accepted; stopping (submit returned correct and no next URL)"
                step["end_at"] = time.time()
                run_history.append(step)
                last_submission = submit_resp
                break

            # Respect server-provided delay if present
            try:
                d = submit_resp.get("delay")
                if isinstance(d, (int, float)) and d > 0:
                    # small sleep to respect server instruction (cap to 10s)
                    await asyncio.sleep(min(d, 10))
            except Exception:
                pass

        # record step
        step["end_at"] = time.time()
        run_history.append(step)
        last_submission = submit_resp

        # Decide to continue or stop
        if not next_url:
            break

        # Avoid revisiting same URL (cycle detection)
        if next_url in visited:
            run_history.append({
                "quiz_url": next_url,
                "start_at": time.time(),
                "solve_result": {"error": "cycle_detected", "url": next_url},
                "end_at": time.time(),
            })
            break

        # push and continue
        visited.add(next_url)
        current_url = next_url
        continue

    total_duration = time.monotonic() - start_time
    response_body = {
        "status": "completed" if time.monotonic() <= deadline else "timed_out",
        "email": email,
        "start_url": start_url,
        "duration_seconds": total_duration,
        "history": run_history,
        "last_submission": last_submission,
    }

    return response_body
