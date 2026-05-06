"""
main.py — FastAPI backend for the coverage interpretation tool.

Endpoints:
  POST /api/analyze            — upload one XML, get full metrics tree
  POST /api/compare            — upload two XMLs, get side-by-side metrics
  POST /api/explain            — LLM interpretation of a single target
  POST /api/explain/compare    — LLM comparison of two targets
  POST /api/ask                — user-supplied question about a target
  GET  /                       — serve the SPA
  GET  /static/*               — serve assets

Run with:
    uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

Set ANTHROPIC_API_KEY in your environment for the LLM endpoints to work.
The /api/analyze and /api/compare endpoints work without it.
"""

from __future__ import annotations
import os
import json
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # reads .env in the working directory (or parent dirs)

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.metrics import analyze_bytes, sensitivity_analysis
from app.report import generate_pdf
from app.prompts import (
    single_target_interpretation,
    comparison_interpretation,
    focused_question,
)

# ---------------------------------------------------------------------------
# Google Gemini client — optional. If the API key isn't set, LLM endpoints
# return a clear error instead of crashing on startup.
# ---------------------------------------------------------------------------
try:
    from groq import Groq
    _groq_available = True
except ImportError:
    _groq_available = False


def _make_groq_client():
    """Return a configured Groq client, or raise an HTTPException if not ready."""
    if not _groq_available:
        raise HTTPException(
            status_code=503,
            detail="The 'groq' package is not installed. Run: pip install groq",
        )
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="GROQ_API_KEY environment variable is not set. "
                   "Set it in your .env file and restart the server.",
        )
    return Groq(api_key=api_key)


LLM_MODEL = "llama-3.3-70b-versatile"

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="JaCoCo Coverage Interpretation Model")

# Serve static files (the SPA bundle)
import pathlib
ROOT = pathlib.Path(__file__).parent.parent
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-page app."""
    html_path = ROOT / "templates" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Analysis endpoints
# ---------------------------------------------------------------------------

@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...)):
    """Parse an uploaded JaCoCo XML and return the full metrics tree."""
    content = await file.read()
    try:
        results = analyze_bytes(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse XML: {e}")
    results["_filename"] = file.filename
    return JSONResponse(results)


@app.post("/api/compare")
async def api_compare(
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
):
    """Parse two JaCoCo XMLs and return both result trees."""
    try:
        content_a = await file_a.read()
        content_b = await file_b.read()
        results_a = analyze_bytes(content_a)
        results_b = analyze_bytes(content_b)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse XML: {e}")
    results_a["_filename"] = file_a.filename
    results_b["_filename"] = file_b.filename
    return JSONResponse({"a": results_a, "b": results_b})


# ---------------------------------------------------------------------------
# LLM endpoints — these stream so the UI can render text as it arrives
# ---------------------------------------------------------------------------

class ExplainRequest(BaseModel):
    target: dict
    level: str  # "project" | "package" | "class" | "method"
    show_prompt: bool = False


def _stream_llm(system: str, user: str, prompt_visible: bool):
    """
    Yields server-sent-event lines. Each event is JSON-encoded.

    First event (if requested) is the prompt itself (for transparency).
    Subsequent events are text deltas as the model streams.
    Final event is {"done": true}.
    """
    client = _make_groq_client()

    if prompt_visible:
        yield f"data: {json.dumps({'prompt': {'system': system, 'user': user}})}\n\n"

    try:
        stream = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=1024,
            stream=True,
        )
        for chunk in stream:
            text = chunk.choices[0].delta.content
            if text:
                yield f"data: {json.dumps({'text': text})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        return

    yield f"data: {json.dumps({'done': True})}\n\n"


@app.post("/api/explain")
async def api_explain(req: ExplainRequest):
    """Stream an LLM interpretation of a single target."""
    system, user = single_target_interpretation(req.target, req.level)
    return StreamingResponse(
        _stream_llm(system, user, req.show_prompt),
        media_type="text/event-stream",
    )


class CompareExplainRequest(BaseModel):
    target_a: dict
    target_b: dict
    label_a: str
    label_b: str
    level: str
    show_prompt: bool = False


@app.post("/api/explain/compare")
async def api_explain_compare(req: CompareExplainRequest):
    """Stream an LLM comparison of two targets."""
    system, user = comparison_interpretation(
        req.target_a, req.target_b, req.label_a, req.label_b, req.level
    )
    return StreamingResponse(
        _stream_llm(system, user, req.show_prompt),
        media_type="text/event-stream",
    )


class AskRequest(BaseModel):
    target: dict
    level: str
    question: str
    show_prompt: bool = False


@app.post("/api/ask")
async def api_ask(req: AskRequest):
    """Answer a user-supplied question about a target."""
    system, user = focused_question(req.target, req.level, req.question)
    return StreamingResponse(
        _stream_llm(system, user, req.show_prompt),
        media_type="text/event-stream",
    )


class SensitivityRequest(BaseModel):
    classes: list[dict]
    packages: list[dict] = []   # accepted but not needed; classes carry all metric fields


@app.post("/api/sensitivity")
async def api_sensitivity(req: SensitivityRequest):
    """Re-score packages under each ±0.10 weight perturbation and return rank shifts."""
    try:
        results = sensitivity_analysis(req.classes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Sensitivity analysis failed: {e}")
    return JSONResponse(results)


class PdfRequest(BaseModel):
    results: dict


@app.post("/api/report/pdf")
async def api_report_pdf(req: PdfRequest):
    """Generate a PDF report from a full analysis result dict."""
    try:
        pdf_bytes = generate_pdf(req.results)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=coverage-report.pdf"},
    )


# Health check (handy when wiring up the frontend)
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "llm_configured": bool(os.environ.get("GROQ_API_KEY")),
        "llm_installed": _groq_available,
    }
