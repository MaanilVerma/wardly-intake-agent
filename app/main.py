"""FastAPI entrypoint.

Thin glue: wires routers, mounts static, and renders the Markdown brief as
HTML for the clinician view. All business logic lives in app/chat.py and
app/brief.py.
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
from app.storage import read_markdown

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


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> FileResponse:
    """Serve the chat UI."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/briefs/{call_id}", response_class=HTMLResponse)
def view_brief(call_id: str) -> HTMLResponse:
    """Render the saved Markdown brief as HTML — what a clinician would actually read."""
    md = read_markdown(call_id)
    if md is None:
        raise HTTPException(status_code=404, detail=f"brief {call_id} not found")
    body = md_renderer.markdown(md, extensions=["extra", "sane_lists"])
    html = _BRIEF_HTML_TEMPLATE.format(call_id=call_id, body=body)
    return HTMLResponse(html)


_BRIEF_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Brief {call_id} — Wardly</title>
<link rel="stylesheet" href="/static/style.css" />
</head>
<body class="brief-view">
<main class="brief">
{body}
<p class="brief-foot"><a href="/">← Back to intake</a></p>
</main>
</body>
</html>
"""
