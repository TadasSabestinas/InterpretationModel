"""
prompts.py
==========

Prompt construction for the LLM-assisted interpretation layer.

This module is deliberately kept separate from the API code so that:

  1. The exact prompts used in the thesis can be inspected, cited,
     and reproduced. Reviewers will want to see them.

  2. Prompt versions can be tracked over time. If you tweak a prompt
     during the thesis writing phase, keep the old version commented
     out below the new one — that's a transparency record.

  3. The prompts can be exposed in the UI itself (a "show prompt"
     toggle), which is a methodologically honest design choice.

Three prompt templates are defined here:

  * single_target_interpretation — interprets metrics for one
    project / package / class
  * comparison_interpretation     — interprets the difference between
    two analyzed reports
  * focused_question              — open question about a target
"""

from __future__ import annotations
import json


SYSTEM_PROMPT = """\
You are a software quality analyst helping a developer interpret JaCoCo \
code coverage data that has been augmented with derived metrics from the \
research literature.

You should:
- Be concise. Prefer 3-5 short paragraphs over long lists.
- Highlight where the derived metrics tell a different story than the \
raw JaCoCo percentages, since that is the most actionable insight.
- Be honest about uncertainty. The metrics are approximations, not \
ground truth. Avoid claims like "this code has bugs" — instead say \
"this pattern often correlates with weak tests".
- Do not invent metrics that aren't in the input. Only reason from the \
numbers shown.
- Use plain language. The audience may be a developer who has never \
heard of cyclomatic complexity or mutation score.

Metric reference (use this for interpretation):
- instruction_pct, branch_pct, line_pct, method_pct: standard JaCoCo \
percentages.
- mean_line_branch: average of line% and branch%, a stronger signal \
than either alone.
- coverage_geo_mean: geometric mean of all four JaCoCo dimensions; \
collapses if any dimension is weak.
- weighted_line_cov: line coverage weighted by bytecode instructions \
per line. Lower than line_pct means tests touch lines but don't \
exercise their full bytecode work — a sign of shallow tests.
- quality_score: composite 0-100 score: 0.35 × mean_line_branch + \
0.35 × coverage_geo_mean + 0.30 × weighted_line_cov.
- quality_grade: A/B/C/D/F based on quality_score.

A class with high line_pct but low branch_pct is the classic JaCoCo \
pathology: tests reach the code but never exercise its decisions.\
"""


def _format_metrics_block(target: dict, level: str) -> str:
    """Format a metrics dict as a readable block for the LLM."""
    lines = [f"=== {level.upper()} METRICS ==="]
    for k, v in target.items():
        if k in ("package_name", "class_name", "method_name",
                 "project_name", "n_classes", "n_packages"):
            lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("Coverage (raw JaCoCo):")
    for k in ("instruction_pct", "branch_pct", "line_pct", "method_pct"):
        if k in target:
            lines.append(f"  {k}: {target[k]}%")
    lines.append("")
    lines.append("Derived metrics:")
    for k in ("mean_line_branch", "coverage_geo_mean", "weighted_line_cov",
              "branch_density"):
        if k in target:
            unit = "" if k == "branch_density" else "%"
            lines.append(f"  {k}: {target[k]}{unit}")
    lines.append("")
    lines.append("Composite:")
    if "quality_score" in target:
        lines.append(f"  quality_score: {target['quality_score']}/100")
    if "quality_grade" in target:
        lines.append(f"  quality_grade: {target['quality_grade']}")
    if "complexity_total" in target:
        lines.append(f"  complexity_total: {target['complexity_total']}")
    return "\n".join(lines)


def single_target_interpretation(target: dict, level: str,
                                  context: str | None = None) -> tuple[str, str]:
    """
    Build a (system, user) prompt pair for interpreting one target.

    Args:
        target: dict of metrics for the target (project/package/class)
        level:  one of "project", "package", "class", "method"
        context: optional extra info, e.g. parent package metrics

    Returns:
        (system_prompt, user_prompt)
    """
    block = _format_metrics_block(target, level)
    user = f"""Please interpret the following {level}-level coverage data.

{block}
"""
    if context:
        user += f"\nAdditional context:\n{context}\n"
    user += f"""
Specifically:
1. What does the data suggest about test quality at this level?
2. Where do the derived metrics disagree with the raw JaCoCo percentages, \
and what does that disagreement imply?
3. What would you recommend the developer focus on first?

Keep the response under 250 words."""
    return SYSTEM_PROMPT, user


def comparison_interpretation(target_a: dict, target_b: dict,
                               label_a: str, label_b: str,
                               level: str) -> tuple[str, str]:
    """Build a (system, user) prompt pair comparing two targets."""
    block_a = _format_metrics_block(target_a, f"{level} A: {label_a}")
    block_b = _format_metrics_block(target_b, f"{level} B: {label_b}")

    user = f"""Please compare the test quality of these two {level}s.

{block_a}

{block_b}

Specifically:
1. Which one has stronger tests overall, and by what margin?
2. What are the most meaningful differences between them — not just \
which numbers are higher, but what the differences imply about how the \
projects are tested?
3. Are there any places where the derived metrics tell a different \
story than the raw JaCoCo percentages?

Keep the response under 300 words."""
    return SYSTEM_PROMPT, user


def focused_question(target: dict, level: str, question: str) -> tuple[str, str]:
    """User-supplied question about a specific target."""
    block = _format_metrics_block(target, level)
    user = f"""The user is asking a question about this {level}'s coverage data.

{block}

User question: {question}

Answer concisely (under 200 words) using only the data shown."""
    return SYSTEM_PROMPT, user
