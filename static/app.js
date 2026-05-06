// app.js — Alpine.js component powering the SPA

function app() {
  return {
    // ===== Top-level state =====
    view: 'analyze',           // 'analyze' | 'compare' | 'about'
    llmReady: false,
    loading: false,
    uploadError: '',

    // Single-report analysis
    report: null,              // { project, packages, classes, methods }
    drillPath: { package: null, cls: null },

    // Comparison
    cmpFileA: null,
    cmpFileB: null,
    cmpError: '',
    comparison: null,          // { a: report, b: report }
    comparedPackages: null,    // built once when comparison loads; avoids repeated Map/sort in template

    // LLM panel
    llmText: '',
    llmStreaming: false,
    llmPrompt: '',
    userQuestion: '',

    // Sensitivity analysis
    sensitivity: null,
    sensitivityLoading: false,

    // PDF export
    pdfLoading: false,

    // Hotspots
    hotspotTab: 'coverage_gap_risk',
    hotspotOpen: true,
    hotspotExpanded: false,
    hotspotTabs: [
      { key: 'coverage_gap_risk',   label: 'Coverage gap risk'   },
      { key: 'branch_blind_spots',  label: 'Branch blind spots'  },
      { key: 'shallow_tests',       label: 'Shallow tests'       },
      { key: 'untested_complexity', label: 'Untested complexity' },
    ],

    // Charts
    _radar: null,

    // ===== Lifecycle =====
    async init() {
      try {
        const r = await fetch('/api/health').then(r => r.json());
        this.llmReady = r.llm_configured && r.llm_installed;
      } catch (e) {
        this.llmReady = false;
      }

      // When drilling into a class, render the radar chart on next tick
      this.$watch('drillPath.cls', val => {
        this.resetLlm();
        if (val) {
          this.$nextTick(() => this.renderClassRadar());
        }
      });
      this.$watch('drillPath.package', () => this.resetLlm());

    },

    // ===== Single-report upload =====
    async uploadSingle(event) {
      const file = event.target.files[0];
      if (!file) return;
      this.loading = true;
      this.uploadError = '';
      try {
        const fd = new FormData();
        fd.append('file', file);
        const res = await fetch('/api/analyze', { method: 'POST', body: fd });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        this.report = await res.json();
        this.drillPath = { package: null, cls: null };
        this.sensitivity = null;
        this.hotspotTab = 'coverage_gap_risk';
        this.hotspotOpen = true;
        this.hotspotExpanded = false;
      } catch (e) {
        this.uploadError = e.message || 'Upload failed';
      } finally {
        this.loading = false;
      }
    },

    drillTo(level) {
      if (level === 'project') this.drillPath = { package: null, cls: null };
      if (level === 'package') this.drillPath.cls = null;
    },

    // ===== Comparison upload =====
    async tryCompare() {
      if (!this.cmpFileA || !this.cmpFileB) return;
      this.loading = true;
      this.cmpError = '';
      try {
        const fd = new FormData();
        fd.append('file_a', this.cmpFileA);
        fd.append('file_b', this.cmpFileB);
        const res = await fetch('/api/compare', { method: 'POST', body: fd });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        this.comparison = await res.json();
        this.comparedPackages = this._buildComparedPackages();
        this.resetLlm();
      } catch (e) {
        this.cmpError = e.message || 'Compare failed';
      } finally {
        this.loading = false;
      }
    },

    resetCompare() {
      this.comparison = null;
      this.comparedPackages = null;
      this.cmpFileA = null;
      this.cmpFileB = null;
      this.resetLlm();
    },

    // ===== Derived data accessors =====
    sortedPackages() {
      return [...(this.report?.packages || [])].sort((a, b) => a.quality_score - b.quality_score);
    },

    classesInPackage() {
      const pkg = this.drillPath.package;
      if (!pkg || !this.report) return [];
      return [...this.report.classes]
        .filter(c => c.package_name === pkg)
        .sort((a, b) => a.quality_score - b.quality_score);
    },

    currentClass() {
      const cls = this.drillPath.cls;
      const pkg = this.drillPath.package;
      if (!cls || !pkg || !this.report) return null;
      return this.report.classes.find(c => c.package_name === pkg && c.class_name === cls);
    },

    methodsInClass() {
      const cls = this.drillPath.cls;
      const pkg = this.drillPath.package;
      if (!cls || !pkg || !this.report) return [];
      return this.report.methods
        .filter(m => m.package_name === pkg && m.class_name === cls)
        .sort((a, b) => a.quality_score - b.quality_score);
    },

    // ===== Sensitivity analysis =====
    async runSensitivity() {
      if (!this.report?.classes?.length) return;
      this.sensitivityLoading = true;
      this.sensitivity = null;
      try {
        const res = await fetch('/api/sensitivity', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ classes: this.report.classes }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        this.sensitivity = await res.json();
      } catch (e) {
        console.error('Sensitivity analysis failed:', e);
      } finally {
        this.sensitivityLoading = false;
      }
    },

    sensitivityShiftLabel(shift) {
      if (shift > 0) return `↑${shift}`;
      if (shift < 0) return `↓${Math.abs(shift)}`;
      return '—';
    },

    sensitivityShiftClass(shift) {
      if (shift > 0) return 'text-green-600 font-semibold';
      if (shift < 0) return 'text-red-600 font-semibold';
      return 'text-ink/30';
    },

    sensitivityInterpretationClass(interp) {
      if (interp === 'stable')            return 'border-green-200 bg-green-50 text-green-800';
      if (interp === 'moderately stable') return 'border-amber-200 bg-amber-50 text-amber-800';
      return 'border-red-200 bg-red-50 text-red-800';
    },

    sensitivityRhoClass(rho) {
      if (rho >= 0.95) return 'text-green-700 font-semibold';
      if (rho >= 0.85) return 'text-amber-700 font-semibold';
      return 'text-red-700 font-semibold';
    },

    // ===== Metric card definitions =====
    projectMetricCards() {
      return this.projectMetricCardsFor(this.report?.project || {});
    },

    projectMetricCardsFor(p) {
      return [
        { key: 'line',   label: 'Line',          value: p.line_pct,                    unit: '%', note: 'JaCoCo' },
        { key: 'branch', label: 'Branch',        value: p.branch_pct,                  unit: '%', note: 'JaCoCo' },
        { key: 'instr',  label: 'Instruction',   value: p.instruction_pct,             unit: '%', note: 'JaCoCo' },
        { key: 'method', label: 'Method',        value: p.method_pct,                  unit: '%', note: 'JaCoCo' },
        { key: 'mean',   label: 'Mean(L,B)',     value: p.mean_line_branch,            unit: '%', note: 'derived' },
        { key: 'geo',    label: 'Geo mean',      value: p.coverage_geo_mean,           unit: '%', note: 'derived' },
        { key: 'wlc',    label: 'Wtd line',      value: p.weighted_line_cov,           unit: '%', note: 'derived' },
      ];
    },

    rawJacocoCardsFor(p) {
      return this.projectMetricCardsFor(p).filter(m => m.note === 'JaCoCo');
    },

    derivedCardsFor(p) {
      return this.projectMetricCardsFor(p).filter(m => m.note === 'derived');
    },

    classRawCards() {
      const c = this.currentClass();
      if (!c) return [];
      return [
        { key: 'line',   label: 'Line',        value: c.line_pct,        unit: '%' },
        { key: 'branch', label: 'Branch',      value: c.branch_pct,      unit: '%' },
        { key: 'instr',  label: 'Instruction', value: c.instruction_pct, unit: '%' },
      ];
    },

    classMetricCards() {
      const c = this.currentClass();
      if (!c) return [];
      return [
        { key: 'mean', label: 'Mean(L,B)',     value: c.mean_line_branch,  unit: '%' },
        { key: 'wlc',  label: 'Weighted line', value: c.weighted_line_cov, unit: '%' },
      ];
    },

    // ===== Visual helpers =====
    gradeColor(g) {
      // Returns Tailwind classes for the grade pill
      const map = {
        'A': 'bg-green-100 text-green-800',
        'B': 'bg-emerald-100 text-emerald-800',
        'C': 'bg-amber-100 text-amber-800',
        'D': 'bg-orange-100 text-orange-800',
        'F': 'bg-red-100 text-red-800',
      };
      return map[g] || 'bg-gray-100 text-gray-800';
    },

    // ===== Heat color helper =====
    heatColor(value, type = 'coverage') {
      if (type === 'diff') {
        // Maps [-30, 30] → red … transparent … green. Used for Δ Line column.
        const v = Math.max(-30, Math.min(30, value ?? 0));
        if (Math.abs(v) < 0.5) return '';
        const hue  = v > 0 ? 120 : 0;
        const alpha = (Math.abs(v) / 30 * 0.15).toFixed(3);
        return `background-color: hsla(${hue}, 70%, 50%, ${alpha})`;
      }
      // coverage: 0 % → red (hue 0), 50 % → amber (hue 60), 100 % → green (hue 120)
      const hue = ((value ?? 0) / 100 * 120).toFixed(1);
      return `background-color: hsla(${hue}, 70%, 50%, 0.12)`;
    },

    // ===== Charts =====
    renderClassRadar() {
      const c = this.currentClass();
      if (!c) return;
      const ctx = document.getElementById('classRadar');
      if (!ctx) return;
      if (this._radar) this._radar.destroy();

      this._radar = new Chart(ctx, {
        type: 'radar',
        data: {
          labels: ['Line', 'Branch', 'Instruction', 'Mean(L,B)', 'Wtd Line'],
          datasets: [{
            label: c.class_name,
            data: [
              c.line_pct, c.branch_pct, c.instruction_pct,
              c.mean_line_branch, c.weighted_line_cov,
            ],
            backgroundColor: 'rgba(184, 84, 61, 0.15)',
            borderColor: '#b8543d',
            borderWidth: 2,
            pointBackgroundColor: '#b8543d',
            pointRadius: 3,
          }],
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: {
            r: {
              suggestedMin: 0, suggestedMax: 100,
              ticks: { display: false, stepSize: 25 },
              grid: { color: 'rgba(0,0,0,0.08)' },
              angleLines: { color: 'rgba(0,0,0,0.08)' },
              pointLabels: { font: { size: 11, family: 'Inter Tight' }, color: '#1a1a1a' },
            },
          },
        },
      });
    },

    compareMetrics() {
      if (!this.comparison) return [];
      const a = this.comparison.a.project;
      const b = this.comparison.b.project;
      return [
        { key: 'line',   label: 'Line %',        a: a.line_pct,          b: b.line_pct },
        { key: 'branch', label: 'Branch %',      a: a.branch_pct,        b: b.branch_pct },
        { key: 'instr',  label: 'Instruction %', a: a.instruction_pct,   b: b.instruction_pct },
        { key: 'method', label: 'Method %',      a: a.method_pct,        b: b.method_pct },
        { key: 'mean',   label: 'Mean(L,B)',     a: a.mean_line_branch,  b: b.mean_line_branch },
        { key: 'geo',    label: 'Geo mean',      a: a.coverage_geo_mean, b: b.coverage_geo_mean },
        { key: 'wlc',    label: 'Wtd line',      a: a.weighted_line_cov, b: b.weighted_line_cov },
        { key: 'score',  label: 'Quality score', a: a.quality_score,     b: b.quality_score },
      ];
    },

    compareDeltaClass(delta) {
      if (delta > 0.05)  return 'text-green-700 font-semibold';
      if (delta < -0.05) return 'text-red-700 font-semibold';
      return 'text-ink/30';
    },

    compareDeltaLabel(delta) {
      if (delta == null || Math.abs(delta) < 0.05) return '—';
      return (delta > 0 ? '+' : '') + delta.toFixed(1);
    },

    _buildComparedPackages() {
      if (!this.comparison) return [];
      const pkgsA = new Map(this.comparison.a.packages.map(p => [p.package_name, p]));
      const pkgsB = new Map(this.comparison.b.packages.map(p => [p.package_name, p]));
      const allNames = new Set([...pkgsA.keys(), ...pkgsB.keys()]);
      const delta = (b, a, field) => b && a ? +(b[field] - a[field]).toFixed(2) : null;
      return [...allNames].map(name => {
        const a = pkgsA.get(name);
        const b = pkgsB.get(name);
        return {
          name,
          status:      a && b ? 'matched' : b ? 'new' : 'removed',
          score_a:     a?.quality_score ?? null,
          score_b:     b?.quality_score ?? null,
          delta_score: delta(b, a, 'quality_score'),
          grade_a:     a?.quality_grade ?? null,
          grade_b:     b?.quality_grade ?? null,
          line_a:      a?.line_pct      ?? null,
          line_b:      b?.line_pct      ?? null,
          delta_line:  delta(b, a, 'line_pct'),
        };
      }).sort((x, y) => {
        if (x.delta_score !== null && y.delta_score !== null) return x.delta_score - y.delta_score;
        if (x.delta_score === null) return 1;
        if (y.delta_score === null) return -1;
        return x.name.localeCompare(y.name);
      });
    },

    cmpMatchCount() {
      return (this.comparedPackages || []).filter(p => p.status === 'matched').length;
    },

    hotspotList() {
      const list = this.report?.hotspots?.[this.hotspotTab] ?? [];
      return this.hotspotExpanded ? list : list.slice(0, 20);
    },

    async downloadPdf() {
      if (!this.report || this.pdfLoading) return;
      this.pdfLoading = true;
      try {
        const res = await fetch('/api/report/pdf', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ results: this.report }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        const blob = await res.blob();
        const url  = URL.createObjectURL(blob);
        const a    = document.createElement('a');
        const date = new Date().toISOString().slice(0, 10);
        const name = (this.report.project?.project_name || 'project')
                       .replace(/[^a-zA-Z0-9-_]/g, '-').toLowerCase();
        a.href     = url;
        a.download = `coverage-report-${name}-${date}.pdf`;
        a.click();
        URL.revokeObjectURL(url);
      } catch (e) {
        alert('PDF generation failed: ' + e.message);
      } finally {
        this.pdfLoading = false;
      }
    },

    distMaxCount() {
      const dist = this.report?.distribution;
      if (!dist) return 1;
      return Math.max(...dist.map(b => b.count), 1);
    },

    // ===== LLM streaming =====
    resetLlm() {
      this.llmText = '';
      this.llmPrompt = '';
      this.llmStreaming = false;
      this.userQuestion = '';
    },

    async explainTarget(target, level) {
      if (!target) return;
      await this._streamLlm('/api/explain', {
        target, level, show_prompt: true,
      });
    },

    async explainPackage() {
      const pkg = this.report.packages.find(p => p.package_name === this.drillPath.package);
      if (pkg) await this.explainTarget(pkg, 'package');
    },

    async explainComparison() {
      if (!this.comparison) return;
      const a = this.comparison.a.project;
      const b = this.comparison.b.project;
      await this._streamLlm('/api/explain/compare', {
        target_a: a, target_b: b,
        label_a: a.project_name, label_b: b.project_name,
        level: 'project',
        show_prompt: true,
      });
    },

    async askQuestion(target, level) {
      if (!this.userQuestion.trim() || !target) return;
      const q = this.userQuestion;
      this.userQuestion = '';
      this.llmText = (this.llmText ? this.llmText + '\n\n— — —\n\nQ: ' + q + '\n\n' : '');
      await this._streamLlm('/api/ask', {
        target, level, question: q, show_prompt: false,
      }, /* append */ true);
    },

    async _streamLlm(endpoint, body, append = false) {
      this.llmStreaming = true;
      if (!append) {
        this.llmText = '';
        this.llmPrompt = '';
      }

      try {
        const res = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok || !res.body) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }

        // Server-sent events: parse line by line
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let idx;
          while ((idx = buffer.indexOf('\n\n')) !== -1) {
            const event = buffer.slice(0, idx).trim();
            buffer = buffer.slice(idx + 2);
            if (!event.startsWith('data: ')) continue;
            const payload = JSON.parse(event.slice(6));
            if (payload.text)   this.llmText += payload.text;
            if (payload.prompt) this.llmPrompt = `SYSTEM:\n${payload.prompt.system}\n\nUSER:\n${payload.prompt.user}`;
            if (payload.error)  this.llmText += `\n\n[Error: ${payload.error}]`;
            if (payload.done)   { /* ignore, loop will end */ }
          }
        }
      } catch (e) {
        this.llmText += `\n\n[Error: ${e.message}]`;
      } finally {
        this.llmStreaming = false;
      }
    },
  };
}
