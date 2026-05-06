"""
parse_jacoco_extended.py
========================

Extended JaCoCo XML parser with derived quality metrics for a bachelor's
thesis on coverage-based code quality assessment.

This file builds on the original `parse_jacoco.py` in two ways:

  1. It extracts RAW counters (covered/missed/total) at every level
     (project, package, class, method, line). The original parser only
     kept percentages, which is enough for display but loses information
     needed to compute derived metrics correctly.

  2. It computes a small set of DERIVED metrics on top of the raw
     JaCoCo counters. Each metric is grounded in published research and
     designed to capture an aspect of test quality that JaCoCo's own
     percentages miss. The metrics are:

       * mean_line_branch               (interpretability metric)
       * coverage_geo_mean              (design choice, not from a paper)
       * branch_density                 (lightweight complexity proxy)
       * weighted_line_coverage         (instruction-density-weighted; an
                                         SFC-style approximation)
       * complexity_adjusted_coverage   (complexity-penalty proxy; displayed
                                         as a standalone indicator only —
                                         NOT included in quality_score)
       * quality_score                  (the composite UI-facing number)

DESIGN CHOICES THAT MATTER FOR THE THESIS
-----------------------------------------

* Everything is computed bottom-up. We compute per-class metrics first,
  then roll those up to packages by complexity-weighted averaging. This
  matters because a package with one big complex class scoring 60% and
  ten trivial classes scoring 100% should NOT report as ~96%.

* JaCoCo's XML records WHETHER a line/instruction was executed, not
  HOW MANY TIMES. True Statement Frequency Coverage (Aghamohammadi et
  al., 2021) needs execution counts. We DO NOT have those. What we DO
  have is per-line `ci`/`mi` (covered/missed instructions), which lets
  us weight lines by how much bytecode work they contain — a covered
  line with 20 instructions contributes more than a covered line with
  1. This is a defensible proxy for "thoroughness of coverage"; it is
  NOT SFC and the thesis text should say so.

* The composite quality_score uses fixed weights. Those weights are
  defaults, not truths. The thesis should either (a) justify them
  theoretically, or (b) treat them as tunable and report sensitivity
  analysis ("how much do package rankings change if we shift weights
  by ±0.1?"). Both are defensible; (b) is stronger.
"""

from __future__ import annotations
import os
import csv
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Section 1: Data containers
# ---------------------------------------------------------------------------
#
# Plain dataclasses that hold raw counts. We use these instead of nested
# dicts because they're explicit about what fields exist, and they make
# the JSON shape predictable when this gets fed to a UI later.

@dataclass
class Counter:
    """One JaCoCo counter (e.g. INSTRUCTION, BRANCH, LINE...)."""
    covered: int = 0
    missed: int = 0

    @property
    def total(self) -> int:
        return self.covered + self.missed

    @property
    def percent(self) -> float:
        # Defensive: some counters legitimately have total=0 (e.g. a class
        # with no branches has BRANCH total=0). We treat those as 100% by
        # convention because there was nothing TO miss.
        # NOTE: this is the same convention JaCoCo's HTML report uses.
        return 100.0 if self.total == 0 else round(self.covered / self.total * 100, 2)


@dataclass
class LineHit:
    """Per-line counters from <line nr="..." mi="..." ci="..." mb="..." cb="..."/>"""
    line_no: int
    instr_covered: int   # ci
    instr_missed: int    # mi
    branch_covered: int  # cb
    branch_missed: int   # mb

    @property
    def instr_total(self) -> int:
        return self.instr_covered + self.instr_missed

    @property
    def is_executed(self) -> bool:
        # A line is considered executed if at least one of its instructions
        # ran. This is the same definition JaCoCo's LINE counter uses.
        return self.instr_covered > 0


@dataclass
class MethodEntry:
    name: str
    desc: str            # JVM method descriptor, e.g. "(Ljava/util/Locale;)Z"
    line_no: int         # source line where the method starts
    counters: dict[str, Counter] = field(default_factory=dict)


@dataclass
class ClassEntry:
    name: str            # short name only, e.g. "LocaleUtils"
    fqn: str             # fully qualified, e.g. "org/apache/commons/lang3/LocaleUtils"
    sourcefile: str      # e.g. "LocaleUtils.java"
    counters: dict[str, Counter] = field(default_factory=dict)
    methods: list[MethodEntry] = field(default_factory=list)
    # line-level data lives on the matching <sourcefile>, not on <class>,
    # so we attach it after a separate pass below.
    lines: list[LineHit] = field(default_factory=list)


@dataclass
class PackageEntry:
    name: str            # dotted form, e.g. "org.apache.commons.lang3"
    counters: dict[str, Counter] = field(default_factory=dict)
    classes: list[ClassEntry] = field(default_factory=list)


@dataclass
class ProjectReport:
    name: str
    counters: dict[str, Counter] = field(default_factory=dict)
    packages: list[PackageEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Section 2: Parsing — turn XML into the dataclasses above
# ---------------------------------------------------------------------------
#
# JaCoCo XML structure (relevant parts):
#
#   <report name="...">
#     <counter type="INSTRUCTION" missed="..." covered="..."/>      <- project totals
#     ...
#     <package name="org/apache/commons/lang3">
#       <class name="..." sourcefilename="LocaleUtils.java">
#         <method name="..." desc="..." line="...">
#           <counter .../>                                          <- method counters
#         </method>
#         <counter .../>                                            <- class counters
#       </class>
#       ...
#       <sourcefile name="LocaleUtils.java">
#         <line nr="51" mi="0" ci="6" mb="0" cb="0"/>                <- per-line hits
#         ...
#         <counter .../>                                            <- file counters
#       </sourcefile>
#       <counter .../>                                              <- package counters
#     </package>
#   </report>
#
# Two important gotchas the original parse_jacoco.py didn't have to handle
# but we do:
#
#   - Per-line data lives under <sourcefile>, NOT under <class>. Multiple
#     classes in the same file (inner classes, anonymous classes) share
#     one sourcefile. So we have to match classes back to lines via the
#     sourcefilename attribute.
#
#   - Some classes have no <method> children (synthetic classes, marker
#     interfaces). We keep them in but they'll have empty methods lists.

def _parse_counters(element: ET.Element) -> dict[str, Counter]:
    """Read all direct <counter> children of an XML element."""
    out = {}
    for c in element.findall("counter"):
        out[c.get("type")] = Counter(
            covered=int(c.get("covered", 0)),
            missed=int(c.get("missed", 0)),
        )
    return out


def parse_jacoco(xml_path: str) -> ProjectReport:
    """Parse a JaCoCo XML report into a ProjectReport tree."""
    tree = ET.parse(xml_path)
    return _parse_jacoco_tree(tree.getroot())


def parse_jacoco_bytes(xml_bytes: bytes) -> ProjectReport:
    """Parse a JaCoCo XML report from raw bytes (for uploaded files)."""
    root = ET.fromstring(xml_bytes)
    return _parse_jacoco_tree(root)


def _parse_jacoco_tree(root: ET.Element) -> ProjectReport:
    """Shared logic for both file-based and bytes-based parsing."""

    project = ProjectReport(
        name=root.get("name", "Unnamed"),
        counters=_parse_counters(root),
    )

    for pkg_el in root.findall("package"):
        pkg_name = pkg_el.get("name", "").replace("/", ".")
        package = PackageEntry(
            name=pkg_name,
            counters=_parse_counters(pkg_el),
        )

        # First pass: collect classes (with their methods).
        classes_by_sourcefile: dict[str, list[ClassEntry]] = {}
        for cls_el in pkg_el.findall("class"):
            cls_fqn = cls_el.get("name", "")
            cls_short = cls_fqn.split("/")[-1]
            sourcefile = cls_el.get("sourcefilename", "")

            cls = ClassEntry(
                name=cls_short,
                fqn=cls_fqn,
                sourcefile=sourcefile,
                counters=_parse_counters(cls_el),
            )

            for m_el in cls_el.findall("method"):
                cls.methods.append(MethodEntry(
                    name=m_el.get("name", ""),
                    desc=m_el.get("desc", ""),
                    line_no=int(m_el.get("line", 0)),
                    counters=_parse_counters(m_el),
                ))

            package.classes.append(cls)
            classes_by_sourcefile.setdefault(sourcefile, []).append(cls)

        # Second pass: attach line-level data from <sourcefile> elements.
        # If multiple classes (e.g. inner classes) share one sourcefile,
        # we attach a copy of the line list to each. This is intentional —
        # we want every class to be self-contained for later analysis.
        # If you need stricter "lines belong to one class", use the
        # method line ranges to split them; that's beyond scope here.
        for sf_el in pkg_el.findall("sourcefile"):
            sf_name = sf_el.get("name", "")
            line_hits = [
                LineHit(
                    line_no=int(ln.get("nr", 0)),
                    instr_covered=int(ln.get("ci", 0)),
                    instr_missed=int(ln.get("mi", 0)),
                    branch_covered=int(ln.get("cb", 0)),
                    branch_missed=int(ln.get("mb", 0)),
                )
                for ln in sf_el.findall("line")
            ]
            for cls in classes_by_sourcefile.get(sf_name, []):
                cls.lines = line_hits

        project.packages.append(package)

    return project


# ---------------------------------------------------------------------------
# Section 3: Derived metrics — the core thesis contribution
# ---------------------------------------------------------------------------
#
# Each function below takes raw JaCoCo data and returns a single number
# in [0, 100]. The functions are deliberately small and pure (no global
# state, no side effects) so they're easy to test individually.
#
# I use a 0-100 scale throughout for UI consistency. Internally most
# formulas naturally produce 0-1; we just multiply at the end.

def _safe_pct(counter: dict[str, Counter], key: str) -> float:
    """
    Get a percent value safely.

    IMPORTANT: when a counter is entirely absent (key not in dict),
    that means there was nothing of that kind to cover — typical for
    BRANCH on a class with no conditionals (data holders, builders),
    or for COMPLEXITY on a marker interface.

    JaCoCo's HTML report treats "nothing to cover" as 100% (vacuously
    covered). We follow the same convention. Returning 0% would
    incorrectly punish simple classes for the absence of conditionals
    they never had.
    """
    c = counter.get(key)
    if c is None:
        return 100.0
    return c.percent


# --- Metric 1: Mean of line and branch coverage --------------------------
#
# A simple arithmetic mean of two of JaCoCo's most-displayed coverage
# dimensions. Surfaces the common pathology of a class with high line
# coverage but low branch coverage, which JaCoCo does not aggregate
# into a single number.
#
# IMPORTANT: We do not claim this metric predicts test effectiveness
# better than its constituents. Inozemtseva and Holmes (ICSE 2014)
# show empirically that line, branch, and modified condition coverage
# are highly pairwise-correlated (Kendall τ ≥ 0.91 across five large
# Java projects) and that the type of coverage measured has little
# effect on its correlation with fault detection. We include this
# metric for interpretability — it captures a common pathology in a
# single number — not as a predictive instrument.

def metric_mean_line_branch(counters: dict[str, Counter]) -> float:
    line = _safe_pct(counters, "LINE")
    branch = _safe_pct(counters, "BRANCH")
    return round((line + branch) / 2, 2)


# --- Metric 2: Geometric mean of coverage dimensions ---------------------
#
# Geometric mean of JaCoCo's four coverage percentages (instruction,
# branch, line, method). The geometric mean is chosen over the
# arithmetic mean because it disproportionately punishes weakness in
# any single dimension: if any one coverage dimension is very low,
# the whole score is dragged down. This matches the intuition that
# untested code in any dimension represents a real risk that should
# not be masked by strong coverage in others.
#
# This is a design choice, not drawn from a specific paper.

def metric_geo_mean(counters: dict[str, Counter]) -> float:
    # Three dimensions only — METHOD is intentionally excluded.
    # JaCoCo's METHOD counter includes compiler-generated synthetic and
    # bridge methods (generics, lambdas, covariant returns) that have no
    # source lines. A class can show 100% LINE/BRANCH/INSTRUCTION while
    # METHOD is very low simply because those synthetic methods were never
    # called. Including METHOD would penalise well-tested classes for
    # compiler artefacts. INSTRUCTION already captures whether any bytecode
    # in a method ran, so METHOD adds no signal that INSTRUCTION doesn't.
    dims = [
        _safe_pct(counters, "INSTRUCTION"),
        _safe_pct(counters, "BRANCH"),
        _safe_pct(counters, "LINE"),
    ]
    eps = 1.0
    safe_dims = [max(d, eps) for d in dims]
    product = 1.0
    for d in safe_dims:
        product *= d / 100.0
    return round((product ** (1 / len(safe_dims))) * 100, 2)


# --- Metric 3: Branch density --------------------------------------------
#
# Branch density = total branches / total lines.
# Not a coverage metric per se — it's a structural property — but useful
# to display in the UI as context. A class with high branch density that
# also has high branch coverage is genuinely well-tested. A class with
# low branch density and high branch coverage might just be trivial.

def metric_branch_density(counters: dict[str, Counter]) -> float:
    branches = counters.get("BRANCH")
    lines = counters.get("LINE")
    if not branches or not lines or lines.total == 0:
        return 0.0
    return round(branches.total / lines.total, 3)


# --- Metric 4: Instruction-weighted line coverage ------------------------
#
# This is our SFC-approximation. True Statement Frequency Coverage
# weights statements by how OFTEN they were executed; we don't have
# execution counts. What we have is `ci` and `mi` per line — covered
# and missed bytecode instructions. So we weight each line by its
# instruction count.
#
# Effect: a line containing a complex expression (many bytecode
# instructions) contributes more to the score than a trivial one-liner
# like `return null;`. Lines that are only partially covered (some
# instructions covered, some missed — happens with short-circuit
# operators and exception handlers) get partial credit proportional to
# how much of their bytecode actually ran.
#
# IMPORTANT FOR THESIS: explicitly position this as a JaCoCo-compatible
# proxy for SFC, not SFC itself. Aghamohammadi et al.'s SFC requires
# instrumented execution counts; we approximate "coverage thoroughness"
# using static instruction density.

def metric_weighted_line_coverage(lines: list[LineHit]) -> float:
    if not lines:
        return 0.0
    total_instr = 0
    covered_instr = 0
    for ln in lines:
        total_instr += ln.instr_total
        covered_instr += ln.instr_covered
    if total_instr == 0:
        return 0.0
    return round(covered_instr / total_instr * 100, 2)


# --- Metric 5: Complexity-adjusted coverage ------------------------------

def metric_complexity_adjusted_coverage(counters: dict[str, Counter]) -> float:
    """
    Complexity-adjusted coverage: a coverage score scaled down by a
    cyclomatic-complexity penalty.

    Inspired by Zakeri-Nasrabadi and Parsa (2022)'s Coverageability
    concept, which captures testability as the ratio of achieved
    coverage to test-suite size. Since JaCoCo does not record
    test-suite size, we substitute cyclomatic complexity as an
    effort proxy: more complex classes represent greater testing
    effort and so warrant a stronger penalty.

    NOTE: This is NOT a reimplementation of the paper's
    Coverageability metric. The paper defines Coverageability via
    a 296-feature ML model trained on labels computed from runtime
    test-suite size — neither of which is available from JaCoCo.
    Our metric is a JaCoCo-derivable proxy informed by the same
    testability intuition.
    """
    base = metric_mean_line_branch(counters) / 100.0
    cc = counters.get("COMPLEXITY")
    cc_total = cc.total if cc else 0
    penalty = 1.0 / (1.0 + math.log1p(cc_total))
    return round(base * penalty * 100, 2)


# --- Metric 6: Composite quality score -----------------------------------
#
# Three-metric composite. complexity_adjusted_coverage is intentionally
# excluded: its log-penalty formula produces values that are structurally
# low for any non-trivial codebase (a project with average class CC≈50 and
# 95% coverage scores ~19%), making it uninformative in a composite and
# confusing to readers unfamiliar with the calibration. It is retained as
# a standalone display metric so the complexity burden is still visible.
#
# Weights sum to 1.0. For the thesis sensitivity chapter, vary each ±0.10
# and report whether package rankings hold.

DEFAULT_WEIGHTS = {
    "mean_line_branch":  0.35,
    "coverage_geo_mean": 0.35,
    "weighted_line_cov": 0.30,
}


def metric_quality_score(
    counters: dict[str, Counter],
    lines: list[LineHit],
    weights: Optional[dict[str, float]] = None,
) -> float:
    if weights is None:
        weights = DEFAULT_WEIGHTS
    components = {
        "mean_line_branch":  metric_mean_line_branch(counters),
        "coverage_geo_mean": metric_geo_mean(counters),
        "weighted_line_cov": metric_weighted_line_coverage(lines) if lines else
                             metric_mean_line_branch(counters),
    }
    score = sum(weights[k] * components[k] for k in weights)
    return round(score, 2)


# --- Quality "grade" — the UI-friendliest output -------------------------
#
# A non-technical user sees "your project: 87" and doesn't know if 87
# is good. Map to a letter grade. Thresholds are conventional, not
# scientific — adjust to taste, but document your choice.

def quality_grade(score: float) -> str:
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 70: return "C"
    if score >= 60: return "D"
    return "F"


# ---------------------------------------------------------------------------
# Section 4: Aggregation — roll up class metrics to packages and project
# ---------------------------------------------------------------------------
#
# When you compute quality_score per class and want to show one number
# per package, the wrong way is to take a simple mean. A package with
# one giant class scoring 60% and ten tiny classes scoring 100% should
# NOT report as ~96% — the giant class probably contains most of the
# work and most of the risk. We weight by complexity_total instead,
# which approximates "how much code is in this class".
#
# Same logic at the project level over packages.

def _weighted_avg(values: list[tuple[float, float]]) -> float:
    """values is list of (score, weight) pairs."""
    total_w = sum(w for _, w in values)
    if total_w == 0:
        return 0.0
    return round(sum(v * w for v, w in values) / total_w, 2)


def compute_class_metrics(cls: ClassEntry) -> dict:
    """All derived metrics for a single class."""
    cc = cls.counters.get("COMPLEXITY")
    cc_total = cc.total if cc else 0

    score = metric_quality_score(cls.counters, cls.lines)

    return {
        "package_name":        None,  # filled in by caller
        "class_name":          cls.name,
        "complexity_total":    cc_total,

        # Raw JaCoCo (kept for the UI to show alongside derived ones)
        "instruction_pct":     _safe_pct(cls.counters, "INSTRUCTION"),
        "branch_pct":          _safe_pct(cls.counters, "BRANCH"),
        "line_pct":            _safe_pct(cls.counters, "LINE"),
        "method_pct":          _safe_pct(cls.counters, "METHOD"),

        # Derived
        "mean_line_branch":  metric_mean_line_branch(cls.counters),
        "coverage_geo_mean": metric_geo_mean(cls.counters),
        "branch_density":    metric_branch_density(cls.counters),
        "weighted_line_cov": metric_weighted_line_coverage(cls.lines),
        "quality_score":     score,
        "quality_grade":       quality_grade(score),
    }


def compute_package_metrics(pkg: PackageEntry, class_results: list[dict]) -> dict:
    """Roll up per-class results into one row for the package."""
    own = [c for c in class_results if c["package_name"] == pkg.name]
    if not own:
        return {}

    def cc_avg(field: str) -> float:
        """Complexity-weighted average — only for metrics whose formula embeds
        per-class complexity. Using this for coverage-only metrics would
        over-weight complex classes twice (once in the formula, once here)."""
        return _weighted_avg([(c[field], max(c["complexity_total"], 1)) for c in own])

    score = cc_avg("quality_score")

    return {
        "package_name":        pkg.name,
        "n_classes":           len(own),
        "complexity_total":    sum(c["complexity_total"] for c in own),

        "instruction_pct":     _safe_pct(pkg.counters, "INSTRUCTION"),
        "branch_pct":          _safe_pct(pkg.counters, "BRANCH"),
        "line_pct":            _safe_pct(pkg.counters, "LINE"),
        "method_pct":          _safe_pct(pkg.counters, "METHOD"),

        # Re-computed from aggregate JaCoCo counters — no complexity term,
        # so the package counter gives the right answer without distortion.
        "mean_line_branch":  metric_mean_line_branch(pkg.counters),
        "coverage_geo_mean": metric_geo_mean(pkg.counters),
        "weighted_line_cov": _safe_pct(pkg.counters, "INSTRUCTION"),

        "quality_score":     score,
        "quality_grade":       quality_grade(score),
    }


def compute_project_metrics(project: ProjectReport, package_results: list[dict]) -> dict:
    """Roll up per-package results into one row for the whole project."""
    pkgs = [p for p in package_results if p]
    if not pkgs:
        return {}

    def cc_avg(field: str) -> float:
        return _weighted_avg([(p[field], max(p["complexity_total"], 1)) for p in pkgs])

    score = cc_avg("quality_score")

    return {
        "project_name":        project.name,
        "n_packages":          len(pkgs),
        "complexity_total":    sum(p["complexity_total"] for p in pkgs),

        "instruction_pct":     _safe_pct(project.counters, "INSTRUCTION"),
        "branch_pct":          _safe_pct(project.counters, "BRANCH"),
        "line_pct":            _safe_pct(project.counters, "LINE"),
        "method_pct":          _safe_pct(project.counters, "METHOD"),

        "mean_line_branch":  metric_mean_line_branch(project.counters),
        "coverage_geo_mean": metric_geo_mean(project.counters),
        "weighted_line_cov": _safe_pct(project.counters, "INSTRUCTION"),

        "quality_score":     score,
        "quality_grade":       quality_grade(score),
    }


# ---------------------------------------------------------------------------
# Section 5: Hotspot detection
# ---------------------------------------------------------------------------
#
# Surfaces the classes most likely to harbour untested behaviour across
# four independent risk dimensions. Each category returns up to 10 entries
# sorted worst-first so the developer's attention lands on the right class
# immediately.
#
# All categories exclude trivial classes (complexity_total < 3) — a
# two-line data-holder with 0% branch coverage is not interesting.

_HOTSPOT_FIELDS = (
    "package_name", "class_name", "complexity_total",
    "line_pct", "branch_pct", "weighted_line_cov",
    "quality_score", "quality_grade",
)


def _hspot(row: dict, reason: str) -> dict:
    entry = {k: row[k] for k in _HOTSPOT_FIELDS}
    entry["reason"] = reason
    return entry


def compute_hotspots(class_rows: list[dict]) -> dict:
    """
    Four hotspot categories derived from per-class metric dicts.
    Returns a dict with keys matching the category names; each value is a
    list of slim dicts (see _HOTSPOT_FIELDS + 'reason').
    """
    eligible = [c for c in class_rows if c["complexity_total"] >= 3]

    # 1. Coverage gap risk — complex classes that lag meaningfully behind
    #    the project norm.
    #
    #    A class is only a candidate if its quality_score is both:
    #      (a) at least GAP_PP percentage points below the eligible-class mean, AND
    #      (b) below GAP_ABS_CEIL (absolute ceiling — avoids flagging near-perfect
    #          classes in low-quality codebases when the mean itself is very low).
    #
    #    Within those candidates, we rank by:
    #      (100 − quality_score) × log(1 + complexity)
    #    so that poorly-covered classes on large/complex code surface first.
    GAP_PP       = 10   # must be at least this many pp below the project mean
    GAP_ABS_CEIL = 85   # must also be below this absolute score ceiling

    if eligible:
        mean_score = sum(c["quality_score"] for c in eligible) / len(eligible)
    else:
        mean_score = 100.0
    gap_threshold = min(mean_score - GAP_PP, GAP_ABS_CEIL)

    gap_risk = sorted(
        [c for c in eligible if c["quality_score"] < gap_threshold],
        key=lambda c: (100 - c["quality_score"]) * math.log1p(c["complexity_total"]),
        reverse=True,
    )
    gap_risk = [_hspot(c, f"Score {c['quality_score']}% on complexity-{c['complexity_total']} code")
                for c in gap_risk]

    # 2. Branch blind spots — tests reach lines but never exercise decisions
    branch_blind = sorted(
        [c for c in eligible if c["line_pct"] >= 80 and c["branch_pct"] < 50],
        key=lambda c: c["line_pct"] - c["branch_pct"],
        reverse=True,
    )[:10]
    branch_blind = [_hspot(c, f"{c['line_pct']}% line coverage but only {c['branch_pct']}% branch coverage")
                    for c in branch_blind]

    # 3. Shallow tests — tests touch lines but skip bytecode work
    def _gap(c: dict) -> float:
        return round(c["line_pct"] - c["weighted_line_cov"], 1)

    shallow = sorted(
        [c for c in eligible if _gap(c) > 5],
        key=_gap,
        reverse=True,
    )[:1000]
    shallow = [_hspot(c, f"Line {c['line_pct']}% but weighted line only {c['weighted_line_cov']}% (gap: {_gap(c)}pp)")
               for c in shallow]

    # 4. Untested complexity — high-CC classes with poor overall quality
    untested = sorted(
        [c for c in eligible if c["complexity_total"] >= 20 and c["quality_score"] < 70],
        key=lambda c: c["complexity_total"],
        reverse=True,
    )[:1000]
    untested = [_hspot(c, f"Complexity {c['complexity_total']} with quality score {c['quality_score']}%")
                for c in untested]

    return {
        "coverage_gap_risk":        gap_risk,
        "gap_risk_project_mean":    round(mean_score, 1),
        "gap_risk_threshold":       round(gap_threshold, 1),
        "branch_blind_spots":       branch_blind,
        "shallow_tests":            shallow,
        "untested_complexity":      untested,
    }


# ---------------------------------------------------------------------------
# Section 6: Quality distribution
# ---------------------------------------------------------------------------
#
# Buckets classes into ten 10-point quality_score ranges and returns
# summary stats for each bucket. The histogram reveals project shape:
# whether the codebase is mostly well-tested with a few outliers, or
# spread evenly, or clustered in the danger zone.

def compute_distribution(class_rows: list[dict]) -> list[dict]:
    """
    Return a list of 10 dicts, one per 10-point quality_score bucket.

    Buckets: [0,10), [10,20), ..., [80,90), [90,100]
    Each dict:
      bucket_label    – e.g. "0–10"
      count           – number of classes in this range
      mean_complexity – average complexity_total of classes in the bucket
      sample_class_names – up to 3 class names for tooltip display
    """
    buckets: list[dict] = []
    for i in range(10):
        lo = i * 10
        hi = lo + 10
        label = f"{lo}–{hi}"
        if i == 9:
            members = [c for c in class_rows if c["quality_score"] >= 90]
        else:
            members = [c for c in class_rows if lo <= c["quality_score"] < hi]
        mean_cc = (
            round(sum(c["complexity_total"] for c in members) / len(members), 1)
            if members else 0.0
        )
        buckets.append({
            "bucket_label":       label,
            "count":              len(members),
            "mean_complexity":    mean_cc,
            "sample_class_names": [c["class_name"] for c in members[:3]],
        })
    return buckets


# ---------------------------------------------------------------------------
# Section 7: Top-level driver — call this from the UI / CLI
# ---------------------------------------------------------------------------

def analyze(xml_path: str) -> dict:
    """One-shot entry point taking a file path."""
    return _analyze_project(parse_jacoco(xml_path))


def analyze_bytes(xml_bytes: bytes) -> dict:
    """One-shot entry point taking raw XML bytes (for uploads)."""
    return _analyze_project(parse_jacoco_bytes(xml_bytes))


def _analyze_project(project: ProjectReport) -> dict:
    """
    Compute all metrics at every level. Returns a structured dict ready to be:
      - dumped to JSON for a web UI
      - written to CSV files for inspection
      - used directly by analysis scripts
    """
    # Per-class results (the leaves).
    #
    # We filter out classes with NO testable content — typically marker
    # interfaces (e.g. java.io.Serializable-style "tag" interfaces) and
    # functional interfaces with only abstract method declarations. They
    # have no instructions, no lines, no methods to run, and no
    # complexity. Including them just inflates the F count and gives a
    # misleading picture: they're not "untested", they have nothing to
    # test. The original parse_jacoco.py applied the same filter for
    # the same reason.
    class_rows = []
    method_rows = []
    for pkg in project.packages:
        for cls in pkg.classes:
            instr = cls.counters.get("INSTRUCTION")
            instr_total = instr.total if instr else 0
            cc = cls.counters.get("COMPLEXITY")
            cc_total = cc.total if cc else 0
            if instr_total == 0 and cc_total == 0:
                continue  # marker / functional interface, nothing to assess
            row = compute_class_metrics(cls)
            row["package_name"] = pkg.name
            class_rows.append(row)

            # Method-level rows (for drill-down in the UI)
            for m in cls.methods:
                m_cc = m.counters.get("COMPLEXITY")
                m_cc_total = m_cc.total if m_cc else 0
                m_score = metric_quality_score(m.counters, [])
                method_rows.append({
                    "package_name":     pkg.name,
                    "class_name":       cls.name,
                    "method_name":      m.name,
                    "complexity_total": m_cc_total,
                    "instruction_pct":  _safe_pct(m.counters, "INSTRUCTION"),
                    "branch_pct":       _safe_pct(m.counters, "BRANCH"),
                    "line_pct":         _safe_pct(m.counters, "LINE"),
                    "method_pct":       _safe_pct(m.counters, "METHOD"),
                    "mean_line_branch": metric_mean_line_branch(m.counters),
                    "coverage_geo_mean": metric_geo_mean(m.counters),
                    "quality_score":    m_score,
                    "quality_grade":    quality_grade(m_score),
                })

    # Per-package roll-ups
    package_rows = [compute_package_metrics(pkg, class_rows) for pkg in project.packages]
    package_rows = [p for p in package_rows if p]

    # Project-level roll-up
    project_row = compute_project_metrics(project, package_rows)

    return {
        "project":      project_row,
        "packages":     package_rows,
        "classes":      class_rows,
        "methods":      method_rows,
        "hotspots":     compute_hotspots(class_rows),
        "distribution": compute_distribution(class_rows),
    }


def write_csv_outputs(results: dict, out_dir: str) -> None:
    """Emit three CSVs ready for the UI or for spreadsheet inspection."""
    os.makedirs(out_dir, exist_ok=True)

    # Project (single row)
    if results["project"]:
        with open(os.path.join(out_dir, "1_project_quality.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results["project"].keys()))
            w.writeheader()
            w.writerow(results["project"])

    # Packages
    if results["packages"]:
        with open(os.path.join(out_dir, "2_package_quality.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results["packages"][0].keys()))
            w.writeheader()
            w.writerows(results["packages"])

    # Classes
    if results["classes"]:
        with open(os.path.join(out_dir, "3_class_quality.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results["classes"][0].keys()))
            w.writeheader()
            w.writerows(results["classes"])


# ---------------------------------------------------------------------------
# Section 6: Sensitivity analysis
# ---------------------------------------------------------------------------
#
# For the thesis evaluation chapter: re-score every package under each of
# 8 weight perturbations (each of the 4 weights nudged ±0.10, with the
# remaining three rescaled proportionally so weights still sum to 1.0).
#
# We can do this without the original XML because all four component
# metric values are already stored in the class_rows from _analyze_project.
# The only thing that changes is how they are blended into quality_score.

_WEIGHT_LABELS = {
    "mean_line_branch":  "Mean(L,B)",
    "coverage_geo_mean": "Geo mean",
    "weighted_line_cov": "Wtd line",
}


def _perturb_weights(base: dict, key: str, delta: float) -> Optional[dict]:
    """Return a new weight dict with `key` nudged by `delta`, others rescaled."""
    new_val = round(base[key] + delta, 4)
    if new_val < 0 or new_val > 1:
        return None
    new = dict(base)
    new[key] = new_val
    others = [k for k in new if k != key]
    remaining_sum = sum(base[k] for k in others)
    if remaining_sum == 0:
        return None
    scale = (1.0 - new_val) / remaining_sum
    for k in others:
        new[k] = round(base[k] * scale, 6)
    return new


def sensitivity_analysis(class_rows: list[dict]) -> dict:
    """
    Perturb each composite weight by ±0.10 and measure how stable package
    rankings are under each scenario.

    For each of the 6 perturbations (3 weights × ±0.10) this computes:
      - Spearman ρ between baseline and perturbed package score vectors
      - Maximum absolute rank shift across all packages
      - Per-package score change and rank shift

    Returns a dict with keys:
      default_weights, perturbations, summary
      (plus legacy baseline_scores / baseline_ranking / variants for
       backward-compatibility with any callers that still use the old shape)
    """
    from scipy.stats import spearmanr

    DELTA = 0.10

    def pkg_scores_for(weights: dict) -> dict:
        buckets: dict[str, list[tuple[float, float]]] = {}
        for row in class_rows:
            pkg = row["package_name"]
            score = round(sum(weights[k] * row[k] for k in weights), 2)
            w = max(row["complexity_total"], 1)
            buckets.setdefault(pkg, []).append((score, w))
        return {pkg: _weighted_avg(pairs) for pkg, pairs in buckets.items()}

    baseline = pkg_scores_for(DEFAULT_WEIGHTS)
    # Canonical order for rank comparisons: best → worst by baseline score
    pkg_order = sorted(baseline, key=lambda p: baseline[p], reverse=True)
    baseline_vals = [baseline[p] for p in pkg_order]

    perturbations = []
    for key in DEFAULT_WEIGHTS:
        for delta in (DELTA, -DELTA):
            new_w = _perturb_weights(DEFAULT_WEIGHTS, key, delta)
            if new_w is None:
                continue

            scores = pkg_scores_for(new_w)
            pert_vals = [scores[p] for p in pkg_order]

            # Spearman ρ on the score vectors (same package order)
            rho, _ = spearmanr(baseline_vals, pert_vals)

            pert_ranking = sorted(scores, key=lambda p: scores[p], reverse=True)
            pkg_changes = []
            max_shift_local = 0
            for pkg in pkg_order:
                orig_rank  = pkg_order.index(pkg) + 1        # 1-based
                pert_rank  = pert_ranking.index(pkg) + 1
                shift      = orig_rank - pert_rank            # positive = moved up
                if abs(shift) > max_shift_local:
                    max_shift_local = abs(shift)
                pkg_changes.append({
                    "package_name": pkg,
                    "original":     round(baseline[pkg], 2),
                    "perturbed":    round(scores[pkg],   2),
                    "rank_shift":   shift,
                })

            perturbations.append({
                "weight_changed":    key,
                "weight_label":      _WEIGHT_LABELS[key],
                "delta":             round(delta, 2),
                "perturbed_weights": {k: round(v, 4) for k, v in new_w.items()},
                "spearman_rho":      round(float(rho), 4),
                "max_rank_shift":    max_shift_local,
                "package_score_changes": pkg_changes,
            })

    min_rho       = min(p["spearman_rho"]  for p in perturbations) if perturbations else 1.0
    max_shift_all = max(p["max_rank_shift"] for p in perturbations) if perturbations else 0

    if min_rho >= 0.95 and max_shift_all <= 2:
        interpretation = "stable"
    elif min_rho >= 0.85:
        interpretation = "moderately stable"
    else:
        interpretation = "unstable"

    # Legacy fields kept for any code still reading the old shape
    legacy_variants = [
        {
            "label":       f"{_WEIGHT_LABELS[p['weight_changed']]} {'+' if p['delta'] > 0 else ''}{p['delta']:.2f}",
            "weights":     p["perturbed_weights"],
            "scores":      {c["package_name"]: c["perturbed"] for c in p["package_score_changes"]},
            "ranking":     [c["package_name"] for c in sorted(p["package_score_changes"], key=lambda x: x["perturbed"], reverse=True)],
            "rank_shifts": {c["package_name"]: c["rank_shift"] for c in p["package_score_changes"]},
        }
        for p in perturbations
    ]

    return {
        "default_weights":  DEFAULT_WEIGHTS,
        "perturbations":    perturbations,
        "summary": {
            "min_spearman":         round(min_rho, 4),
            "max_rank_shift_overall": max_shift_all,
            "interpretation":       interpretation,
        },
        # Legacy fields
        "baseline_scores":  {p: round(baseline[p], 2) for p in pkg_order},
        "baseline_ranking": pkg_order,
        "variants":         legacy_variants,
        "max_rank_shift":   max_shift_all,
        "stable":           interpretation == "stable",
    }


# ---------------------------------------------------------------------------
# Section 7: CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        # Default paths matching your existing setup; override on CLI.
        xml_path = r"d:\Projektas\commons-lang\target\site\jacoco\jacoco.xml"
        out_dir  = r"d:\Projektas\commons-lang\quality_csv"
    else:
        xml_path = sys.argv[1]
        out_dir  = sys.argv[2] if len(sys.argv) > 2 else "./quality_csv"

    results = analyze(xml_path)
    write_csv_outputs(results, out_dir)

    # Friendly stdout summary
    p = results["project"]
    print(f"\n=== {p['project_name']} ===")
    print(f"Quality score:  {p['quality_score']}  (grade {p['quality_grade']})")
    print(f"Packages:       {p['n_packages']}")
    print(f"Total complexity: {p['complexity_total']}")
    print(f"\nLegacy JaCoCo: instr={p['instruction_pct']}%  branch={p['branch_pct']}%  "
          f"line={p['line_pct']}%  method={p['method_pct']}%")
    print(f"\nDerived: mean(L,B)={p['mean_line_branch']}  geo_mean={p['coverage_geo_mean']}  "
          f"weighted_line={p['weighted_line_cov']}")

    print(f"\nCSVs written to: {out_dir}")
