"""FastAPI entrypoint.

Thin glue: wires routers, mounts static, serves the chat UI, and returns
the chart-style brief view. The HTML rendering itself lives in app/render.py
and the business logic lives in app/chat.py and app/brief.py.
"""

from __future__ import annotations
import logging
import os
from pathlib import Path

import markdown as md_renderer
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# .env must load before any module that reads GOOGLE_API_KEY.
load_dotenv()

from app.chat import router as chat_router
from app.render import render_brief, render_brief_index, render_markdown_fallback
from app.storage import list_briefs, read_json, read_markdown
from app.webhooks import router as webhooks_router

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STATIC_DIR = _REPO_ROOT / "app" / "static"


app = FastAPI(
    title="Wardly Pre-Visit Intake Agent",
    description="Web chat (and Vapi voice, bonus) clinical intake with a structured brief.",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
app.include_router(chat_router)
app.include_router(webhooks_router)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> FileResponse:
    """Serve the chat UI."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/briefs/", response_class=HTMLResponse)
@app.get("/briefs", response_class=HTMLResponse, include_in_schema=False)
def list_all_briefs() -> HTMLResponse:
    """List every saved brief, newest first.

    Reads `briefs/*.json` directly — single source of truth, always
    current — and renders a clickable list. Each row links to the chart
    view at `/briefs/{call_id}`.

    Note: this route MUST be defined before `/briefs/{call_id}` or FastAPI
    will route `/briefs/` to the detail view with call_id="" (which then
    fails the safe-call_id validator).
    """
    items = list_briefs()
    return HTMLResponse(render_brief_index(items))


@app.get("/briefs/{call_id}", response_class=HTMLResponse)
def view_brief(call_id: str) -> HTMLResponse:
    """Render the saved brief as a chart-style HTML document.

    Prefers the structured JSON for proper field-by-field layout. Falls
    back to rendering the Markdown if the JSON file is missing — the
    clinician still gets a usable view in degraded cases.
    """
    data = read_json(call_id)
    if data is not None:
        return HTMLResponse(render_brief(data, call_id))

    md = read_markdown(call_id)
    if md is None:
        raise HTTPException(status_code=404, detail=f"brief {call_id} not found")
    # python-markdown renders raw HTML in the source by default. Our own
    # to_markdown() never emits HTML tags, but escape '<' before render anyway
    # so a hand-edited or maliciously-written .md file can't ship JS through.
    md_safe = md.replace("<", "&lt;")
    md_html = md_renderer.markdown(md_safe, extensions=["extra", "sane_lists"])
    return HTMLResponse(render_markdown_fallback(md_html, call_id))
