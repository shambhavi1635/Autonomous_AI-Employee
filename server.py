"""HTTP API behind the web interface.

    venv\\python.exe server.py

Like both front ends before it, this file holds no agent logic. It
exposes the graph over HTTP and streams what happens; the decisions all
live in core/. That boundary is why a third interface could be added
without touching the agent -- the same reason the Streamlit UI cost no
changes to core/ either.

Built on Starlette rather than FastAPI because Starlette is already
present as a Streamlit dependency, and this needs routing, JSON and a
streaming response -- nothing FastAPI would add.

Execution is streamed as server-sent events. The alternative, a request
that blocks until the agent finishes, would leave the page silent for a
minute and then dump a result -- and watching the work happen is most of
the point of this interface.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterator

import uvicorn
from langgraph.types import Command
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import tools.browser  # noqa: F401  -- importing registers the tools
import tools.documents  # noqa: F401
import tools.files  # noqa: F401
import tools.ledger  # noqa: F401
import tools.mail  # noqa: F401
import tools.office  # noqa: F401
import tools.report  # noqa: F401
from core import config
from core.graph import build_graph, open_checkpointer
from core.schemas import Plan

WEB_DIR = Path(__file__).resolve().parent / "web"

_saver, _conn = open_checkpointer()
_graph = build_graph(_saver)


def _config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


def _pending_gate(snapshot) -> dict[str, Any] | None:
    """The interrupt this run is waiting on, read from the checkpoint.

    Read from storage rather than from whatever the last call returned,
    so a reloaded page or a different browser picks a paused run up
    exactly where it stopped.
    """
    for task in snapshot.tasks or ():
        for interrupt in task.interrupts or ():
            return interrupt.value
    return None


def _snapshot(run_id: str) -> dict[str, Any]:
    """Everything the page needs to render a run."""
    snapshot = _graph.get_state(_config(run_id))
    values = snapshot.values or {}
    gate = _pending_gate(snapshot)

    plan = values.get("plan")
    answer = None
    if plan:
        outputs = values.get("outputs") or {}
        for step in plan.get("steps", []):
            if step.get("tool") == "report":
                answer = outputs.get(str(step["id"]))

    return {
        "run_id": run_id,
        "task": values.get("task", ""),
        "status": values.get("status", "unknown"),
        "message": values.get("message", ""),
        "plan": plan,
        "results": values.get("results") or [],
        "outputs": values.get("outputs") or {},
        "answer": answer,
        "replans": values.get("replans", 0),
        "gate": gate,
        # A run with no gate but unfinished business was abandoned
        # mid-execution -- the page was closed while a step was running.
        "resumable": gate is None and bool(snapshot.next),
    }


# ----------------------------------------------------------------- routes
async def index(request: Request) -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


async def list_runs(request: Request) -> JSONResponse:
    seen: dict[str, str] = {}
    for checkpoint in _saver.list(None, limit=300):
        thread = checkpoint.config["configurable"]["thread_id"]
        seen.setdefault(thread, (checkpoint.checkpoint.get("ts") or "")[:19])

    runs = []
    for run_id, when in list(seen.items())[:40]:
        values = (_graph.get_state(_config(run_id)).values) or {}
        if not values.get("task"):
            continue
        runs.append(
            {
                "run_id": run_id,
                "task": values["task"],
                "status": values.get("status", "unknown"),
                "when": when,
            }
        )
    return JSONResponse({"runs": runs})


async def get_run(request: Request) -> JSONResponse:
    return JSONResponse(_snapshot(request.path_params["run_id"]))


async def ledger(request: Request) -> JSONResponse:
    from tools.ledger import list_invoices

    return JSONResponse({"invoices": list_invoices()})


async def memory(request: Request) -> JSONResponse:
    from memory import store

    return JSONResponse(
        {
            "stats": store.stats(),
            "lessons": [
                {
                    "id": row["id"],
                    "text": row["text"],
                    "kind": row["kind"],
                    "uses": row["uses"],
                    "from_failure": row["source_status"] == "failed",
                }
                for row in store.active_lessons_with_source(limit=30)
            ],
        }
    )


async def forget_lesson(request: Request) -> JSONResponse:
    from memory import store

    store.delete_lesson(int(request.path_params["lesson_id"]))
    return JSONResponse({"ok": True})


async def start(request: Request) -> JSONResponse:
    body = await request.json()
    task = (body.get("task") or "").strip()
    if not task:
        return JSONResponse({"error": "No task given."}, status_code=400)
    return JSONResponse({"run_id": uuid.uuid4().hex[:8], "task": task})


def _events(run_id: str, payload: Any) -> Iterator[str]:
    """Run the graph until it next pauses, yielding events as it goes.

    A sync generator on purpose: Starlette iterates it in a worker
    thread, which is what the graph needs anyway -- `graph.stream` is
    blocking, and the LLM calls inside it are slow.
    """

    def event(kind: str, **fields: Any) -> str:
        return f"data: {json.dumps({'type': kind, **fields})}\n\n"

    try:
        for chunk in _graph.stream(payload, _config(run_id), stream_mode="updates"):
            for node, update in (chunk or {}).items():
                if not isinstance(update, dict):
                    continue
                if node == "plan":
                    yield event("note", text="Planned the work.")
                elif node == "step":
                    for result in (update.get("results") or [])[-1:]:
                        yield event(
                            "step",
                            step_id=result["step_id"],
                            passed=bool(result.get("verified")),
                            detail=result.get("evidence") or result.get("error") or "",
                        )
                elif node == "repair":
                    yield event("note", text=update.get("message", "Repairing."))
                elif node == "replan":
                    yield event("note", text="That approach failed. Planning again.")
                elif node == "finalise":
                    yield event("note", text=update.get("message", ""))
    except Exception as exc:  # a crash must still reach the page
        yield event("error", text=f"{type(exc).__name__}: {exc}")

    yield event("state", **_snapshot(run_id))


async def stream(request: Request) -> StreamingResponse:
    body = await request.json()
    run_id = body["run_id"]
    resume = body.get("resume")

    if resume is None:
        payload: Any = {"task": body["task"]}
    elif resume == "continue":
        payload = None  # pick an abandoned run back up
    else:
        payload = Command(resume=resume)

    return StreamingResponse(
        _events(run_id, payload),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def validate_plan(request: Request) -> JSONResponse:
    """Check an edited plan before the human commits to it.

    A human edit can break a plan as easily as a model can -- a deleted
    step a later one referenced, a tool name mistyped -- so it gets the
    same validation, and the page can say so before anything runs.
    """
    from core.planner import validate_plan as check

    body = await request.json()
    try:
        plan = Plan.model_validate(body["plan"])
    except Exception as exc:
        return JSONResponse({"ok": False, "problems": [str(exc)[:400]]})

    problems = check(plan)
    return JSONResponse(
        {"ok": not problems, "problems": problems,
         "plan": plan.model_dump(mode="json")}
    )


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/runs", list_runs),
        Route("/api/runs", start, methods=["POST"]),
        Route("/api/run/{run_id}", get_run),
        Route("/api/stream", stream, methods=["POST"]),
        Route("/api/plan/validate", validate_plan, methods=["POST"]),
        Route("/api/ledger", ledger),
        Route("/api/memory", memory),
        Route("/api/memory/{lesson_id:int}", forget_lesson, methods=["DELETE"]),
        Mount("/static", StaticFiles(directory=WEB_DIR), name="static"),
    ]
)


if __name__ == "__main__":
    config.ensure_dirs()
    print("Your AI Colleague -> http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
