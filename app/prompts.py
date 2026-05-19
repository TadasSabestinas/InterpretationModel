from __future__ import annotations
import json


SYSTEM_PROMPT = """\
You are a software quality analyst helping a developer interpret JaCoCo \
code coverage data that has been augmented with derived metrics.

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

Raw JaCoCo percentages (covered / total, as %):
- instruction_pct: bytecode instructions executed. The most granular \
JaCoCo signal; large methods dominate the denominator.
- branch_pct: conditional branches taken. Each if/switch/loop creates \
two branches; both must be exercised for full coverage.
- line_pct: source lines touched. Coarser than instruction_pct; a line \
is "covered" if any instruction on it ran.
- method_pct: methods entered at least once. Does not measure how \
thoroughly the method body was executed.

Derived metrics:
- mean_line_branch: arithmetic mean of line_pct and branch_pct. \
Balances line reach against decision coverage; a stronger single signal \
than either dimension alone.
- coverage_geo_mean: geometric mean of instruction_pct, branch_pct, \
and line_pct. Because it multiplies the three dimensions, it collapses \
toward zero when any one dimension is weak — it cannot be inflated by \
strength in the other two.
- mean_method_cov: unweighted mean of instruction coverage across every \
method of a class, treating each method equally regardless of size. \
JaCoCo's instruction_pct is size-weighted, so large methods dominate; \
mean_method_cov exposes the small untested methods that instruction_pct \
hides. When mean_method_cov is noticeably lower than instruction_pct, \
coverage is concentrated in large methods while smaller ones go untested.
Composite:
- quality_score: 0–100 composite: \
0.35 × mean_line_branch + 0.35 × coverage_geo_mean + \
0.30 × mean_method_cov. Weights branch and line reach equally, then \
adds per-method uniformity. Penalises both low coverage and uneven \
coverage across methods.
- quality_grade: letter grade derived from quality_score \
(A ≥ 90, B ≥ 75, C ≥ 60, D ≥ 45, F < 45).
- complexity_total: sum of cyclomatic complexity across all methods \
in the target. Higher values mean more paths to test.

Key interpretation heuristics:
- High line_pct but low branch_pct: tests reach the code but never \
exercise its conditional logic — the classic JaCoCo blind spot.
- instruction_pct >> mean_method_cov: coverage is carried by a few \
large methods; many small methods are untested.
- coverage_geo_mean << instruction_pct: at least one coverage dimension \
is weak despite the headline number looking acceptable.\
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
    for k in ("mean_line_branch", "coverage_geo_mean", "mean_method_cov"):
        if k in target:
            lines.append(f"  {k}: {target[k]}%")
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
    """Build a (system, user) prompt pair comparing two targets.

    If both targets share the same project_name they are treated as two
    versions of the same project; the framing and questions shift from
    cross-project comparison to regression/improvement analysis.
    """
    name_a = target_a.get("project_name", "")
    name_b = target_b.get("project_name", "")
    same_project = bool(name_a and name_b and name_a == name_b)

    if same_project:
        tag_a = f"version A: {label_a}"
        tag_b = f"version B: {label_b}"
        intro = f"Please compare test quality between two versions of the same {level}."
        questions = """\
Specifically:
1. Did test quality improve or regress from version A to version B, \
and how significant is the change?
2. Which metrics changed the most, and what do those shifts imply \
about how the test suite evolved between versions?
3. Are there any places where the derived metrics tell a different \
story than the raw JaCoCo percentages — for example, a metric that \
improved while another worsened?"""
    else:
        tag_a = f"project A: {label_a}"
        tag_b = f"project B: {label_b}"
        intro = f"Please compare the test quality of these two {level}s."
        questions = """\
Specifically:
1. Which one has stronger tests overall, and by what margin?
2. What are the most meaningful differences between them — not just \
which numbers are higher, but what the differences imply about how the \
projects are tested?
3. Are there any places where the derived metrics tell a different \
story than the raw JaCoCo percentages?"""

    block_a = _format_metrics_block(target_a, tag_a)
    block_b = _format_metrics_block(target_b, tag_b)

    user = f"""{intro}

{block_a}

{block_b}

{questions}

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
