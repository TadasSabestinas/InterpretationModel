from __future__ import annotations
import os
import csv
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Counter:
    #one JaCoCo counter (e.g. INSTRUCTION, BRANCH, LINE...).
    covered: int = 0
    missed: int = 0

    @property
    def total(self) -> int:
        return self.covered + self.missed

    @property
    def percent(self) -> float:
        #nothing to cover (e.g. no branches) = 100% by JaCoCo convention
        return 100.0 if self.total == 0 else round(self.covered / self.total * 100, 2)


@dataclass
class LineHit:
    #per-line counters from <line nr="..." mi="..." ci="..." mb="..." cb="..."/>
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
        return self.instr_covered > 0


@dataclass
class MethodEntry:
    name: str
    desc: str            #JVM method descriptor, pvz"(Ljava/util/Locale;)Z"
    line_no: int         #source line where the method starts
    counters: dict[str, Counter] = field(default_factory=dict)


@dataclass
class ClassEntry:
    name: str            #short name only, e.g. "LocaleUtils"
    fqn: str             #fully qualified, e.g. "org/apache/commons/lang3/LocaleUtils"
    sourcefile: str      #e.g. "LocaleUtils.java"
    counters: dict[str, Counter] = field(default_factory=dict)
    methods: list[MethodEntry] = field(default_factory=list)
    lines: list[LineHit] = field(default_factory=list)


@dataclass
class PackageEntry:
    name: str            #dotted form, e.g. "org.apache.commons.lang3"
    counters: dict[str, Counter] = field(default_factory=dict)
    classes: list[ClassEntry] = field(default_factory=list)


@dataclass
class ProjectReport:
    name: str
    counters: dict[str, Counter] = field(default_factory=dict)
    packages: list[PackageEntry] = field(default_factory=list)


def _parse_counters(element: ET.Element) -> dict[str, Counter]:
    #read all direct <counter> children of an XML element.
    out = {}
    for c in element.findall("counter"):
        out[c.get("type")] = Counter(
            covered=int(c.get("covered", 0)),
            missed=int(c.get("missed", 0)),
        )
    return out


def parse_jacoco(xml_path: str) -> ProjectReport:
    #parse a JaCoCo XML report into a ProjectReport tree.
    tree = ET.parse(xml_path)
    return _parse_jacoco_tree(tree.getroot())


def parse_jacoco_bytes(xml_bytes: bytes) -> ProjectReport:
    #parse a JaCoCo XML report from raw bytes (for uploaded files)
    root = ET.fromstring(xml_bytes)
    return _parse_jacoco_tree(root)


def _parse_jacoco_tree(root: ET.Element) -> ProjectReport:
    #Shared logic for both file-based and bytes-based parsing.
    if root.tag != "report":
        raise ValueError(
            f"Not a JaCoCo report: root element is <{root.tag}>, expected <report>."
        )
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

        #first pass: collect classes (with their methods).
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

        #second pass: attach per-line data from sourcefile elements
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


def _safe_pct(counter: dict[str, Counter], key: str) -> float:
    #absent key means nothing of that type to cover, treated as 100% per JaCoCo convention
    c = counter.get(key)
    if c is None:
        return 100.0
    return c.percent


def metric_mean_line_branch(counters: dict[str, Counter]) -> float:
    #arithmetic mean of line and branch coverage
    line = _safe_pct(counters, "LINE")
    branch = _safe_pct(counters, "BRANCH")
    return round((line + branch) / 2, 2)


def metric_geo_mean(counters: dict[str, Counter]) -> float:
    #geometric mean of instruction, branch, and line coverage
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


#unweighted mean of per-method instruction coverage
def metric_mean_method_coverage(methods: list[MethodEntry]) -> float:
    if not methods:
        return 0.0
    pcts = [_safe_pct(m.counters, "INSTRUCTION") for m in methods]
    return round(sum(pcts) / len(pcts), 2)


#composite: quality_score = 0.35 * mean_line_branch + 0.35 * coverage_geo_mean + 0.30 * mean_method_cov
DEFAULT_WEIGHTS = {
    "mean_line_branch":  0.35,
    "coverage_geo_mean": 0.35,
    "mean_method_cov": 0.30,
}


def metric_quality_score(
    counters: dict[str, Counter],
    methods: list[MethodEntry],
    weights: Optional[dict[str, float]] = None,
) -> float:
    if weights is None:
        weights = DEFAULT_WEIGHTS
    components = {
        "mean_line_branch":  metric_mean_line_branch(counters),
        "coverage_geo_mean": metric_geo_mean(counters),
        #fall back to mean_line_branch when no methods available (e.g. method-level calls)
        "mean_method_cov": metric_mean_method_coverage(methods) if methods else
                             metric_mean_line_branch(counters),
    }
    score = sum(weights[k] * components[k] for k in weights)
    return round(score, 2)


def quality_grade(score: float) -> str:
    # A>=90, B>=80, C>=70, D>=60, F<60
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 70: return "C"
    if score >= 60: return "D"
    return "F"


def _weighted_avg(values: list[tuple[float, float]]) -> float:
    #values is list of (score, weight) pairs.
    total_w = sum(w for _, w in values)
    if total_w == 0:
        return 0.0
    return round(sum(v * w for v, w in values) / total_w, 2)


def compute_class_metrics(cls: ClassEntry) -> dict:
    #all derived metrics for a single class.
    cc = cls.counters.get("COMPLEXITY")
    cc_total = cc.total if cc else 0

    score = metric_quality_score(cls.counters, cls.methods)

    return {
        "package_name":        None,  # filled in by caller
        "class_name":          cls.name,
        "complexity_total":    cc_total,

        #raw JaCoCo (kept for the UI to show alongside derived ones)
        "instruction_pct":     _safe_pct(cls.counters, "INSTRUCTION"),
        "branch_pct":          _safe_pct(cls.counters, "BRANCH"),
        "line_pct":            _safe_pct(cls.counters, "LINE"),
        "method_pct":          _safe_pct(cls.counters, "METHOD"),

        #derived
        "mean_line_branch":  metric_mean_line_branch(cls.counters),
        "coverage_geo_mean": metric_geo_mean(cls.counters),
        "mean_method_cov": metric_mean_method_coverage(cls.methods),
        "quality_score":     score,
        "quality_grade":       quality_grade(score),
    }


def compute_package_metrics(pkg: PackageEntry, class_results: list[dict]) -> dict:
    #roll up per-class results into one row for the package
    own = [c for c in class_results if c["package_name"] == pkg.name]
    if not own:
        return {}

    #complexity-weighted average: large, complex classes contribute more than trivial ones
    def cc_avg(field: str) -> float:
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

        "mean_line_branch":  metric_mean_line_branch(pkg.counters),
        "coverage_geo_mean": metric_geo_mean(pkg.counters),
        "mean_method_cov": cc_avg("mean_method_cov"),

        "quality_score":     score,
        "quality_grade":       quality_grade(score),
    }


def compute_project_metrics(project: ProjectReport, package_results: list[dict]) -> dict:
    #roll up per-package results into one row for the whole project.
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
        "mean_method_cov": cc_avg("mean_method_cov"),

        "quality_score":     score,
        "quality_grade":       quality_grade(score),
    }


_HOTSPOT_FIELDS = (
    "package_name", "class_name", "complexity_total",
    "line_pct", "branch_pct", "mean_method_cov",
    "quality_score", "quality_grade",
)


def _hspot(row: dict, reason: str) -> dict:
    entry = {k: row[k] for k in _HOTSPOT_FIELDS}
    entry["reason"] = reason
    return entry


def compute_hotspots(class_rows: list[dict]) -> dict:
    #identify risky classes across four independent dimensions.
    eligible = [c for c in class_rows if c["complexity_total"] >= 3]

    #coverage gap: classes lagging behind the project mean, ranked by risk
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

    #branch blind spots: high line% but conditional logic under-exercised
    branch_blind = sorted(
        [c for c in eligible
         if c["line_pct"] >= 70
         and c["branch_pct"] < 75
         and (c["line_pct"] - c["branch_pct"]) >= 20],
        key=lambda c: c["line_pct"] - c["branch_pct"],
        reverse=True,
    )
    branch_blind = [_hspot(c, f"{c['line_pct']}% line but only {c['branch_pct']}% branch (gap {round(c['line_pct']-c['branch_pct'],1)}pp)")
                    for c in branch_blind]

    #shallow tests: instruction% looks fine but many small methods are untested
    def _gap(c: dict) -> float:
        return round(c["instruction_pct"] - c["mean_method_cov"], 1)

    shallow = sorted(
        [c for c in eligible if _gap(c) > 10],
        key=_gap,
        reverse=True,
    )[:1000]
    shallow = [_hspot(c, f"Instruction {c['instruction_pct']}% but method mean {c['mean_method_cov']}% (gap: {_gap(c)}pp), coverage concentrated in large methods")
               for c in shallow]

    #untested complexity: large, risky classes where tests barely scratch the surface
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


def compute_distribution(class_rows: list[dict]) -> list[dict]:
    #bucket classes into ten 10-point quality_score ranges for the histogram view
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


def analyze(xml_path: str) -> dict:
    return _analyze_project(parse_jacoco(xml_path))


def analyze_bytes(xml_bytes: bytes) -> dict:
    return _analyze_project(parse_jacoco_bytes(xml_bytes))


def _analyze_project(project: ProjectReport) -> dict:
    #skip marker interfaces and synthetic classes with nothing to test
    class_rows = []
    method_rows = []
    for pkg in project.packages:
        for cls in pkg.classes:
            instr = cls.counters.get("INSTRUCTION")
            instr_total = instr.total if instr else 0
            cc = cls.counters.get("COMPLEXITY")
            cc_total = cc.total if cc else 0
            if instr_total == 0 and cc_total == 0:
                continue  #marker / functional interface, nothing to assess
            row = compute_class_metrics(cls)
            row["package_name"] = pkg.name
            class_rows.append(row)

            #method-level rows
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

    #per-package roll-ups
    package_rows = [compute_package_metrics(pkg, class_rows) for pkg in project.packages]
    package_rows = [p for p in package_rows if p]

    #project-level roll-up
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
    #write project, package, and class results to csv files (used by the standalone script)
    os.makedirs(out_dir, exist_ok=True)

    if results["project"]:
        with open(os.path.join(out_dir, "1_project_quality.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results["project"].keys()))
            w.writeheader()
            w.writerow(results["project"])

    if results["packages"]:
        with open(os.path.join(out_dir, "2_package_quality.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results["packages"][0].keys()))
            w.writeheader()
            w.writerows(results["packages"])

    if results["classes"]:
        with open(os.path.join(out_dir, "3_class_quality.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results["classes"][0].keys()))
            w.writeheader()
            w.writerows(results["classes"])



if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        xml_path = r"d:\Projektas\commons-lang\target\site\jacoco\jacoco.xml"
        out_dir  = r"d:\Projektas\commons-lang\quality_csv"
    else:
        xml_path = sys.argv[1]
        out_dir  = sys.argv[2] if len(sys.argv) > 2 else "./quality_csv"

    results = analyze(xml_path)
    write_csv_outputs(results, out_dir)

    p = results["project"]
    print(f"\n=== {p['project_name']} ===")
    print(f"Quality score:  {p['quality_score']}  (grade {p['quality_grade']})")
    print(f"Packages:       {p['n_packages']}")
    print(f"Total complexity: {p['complexity_total']}")
    print(f"\nLegacy JaCoCo: instr={p['instruction_pct']}%  branch={p['branch_pct']}%  "
          f"line={p['line_pct']}%  method={p['method_pct']}%")
    print(f"\nDerived: mean(L,B)={p['mean_line_branch']}  geo_mean={p['coverage_geo_mean']}  "
          f"weighted_line={p['mean_method_cov']}")

    print(f"\nCSVs written to: {out_dir}")
