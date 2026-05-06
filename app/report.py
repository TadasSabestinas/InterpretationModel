"""
report.py
=========
PDF report generation using xhtml2pdf (ReportLab backend, pure Python,
no system-level GTK/Pango dependencies required).
"""

from __future__ import annotations
import datetime
import io
import pathlib
from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa

TEMPLATE_DIR = pathlib.Path(__file__).parent.parent / "templates"


def generate_pdf(results: dict) -> bytes:
    """
    Render the analysis results as a self-contained PDF.
    Returns raw PDF bytes ready to stream to the browser.
    """
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("pdf_report.html")

    project    = results.get("project", {})
    packages   = sorted(results.get("packages", []), key=lambda p: p.get("quality_score", 0))
    hotspots   = results.get("hotspots", {})
    distribution = results.get("distribution", [])
    n_classes  = sum(p.get("n_classes", 0) for p in packages)
    max_count  = max((b["count"] for b in distribution), default=1) or 1

    html_str = template.render(
        project=project,
        packages=packages,
        hotspots=hotspots,
        distribution=distribution,
        n_classes=n_classes,
        max_count=max_count,
        generated_at=datetime.date.today().strftime("%B %d, %Y"),
    )

    buf = io.BytesIO()
    result = pisa.CreatePDF(html_str, dest=buf, encoding="utf-8")
    if result.err:
        raise RuntimeError(f"PDF generation failed with {result.err} error(s)")
    return buf.getvalue()
