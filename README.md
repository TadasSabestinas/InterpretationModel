# JaCoCo Coverage Interpretation Tool

A web application that parses JaCoCo XML reports, computes derived quality
metrics, and uses an LLM to produce natural-language interpretations. Built
as part of a bachelor's thesis on coverage-based code quality assessment.

---

## Features

- Upload a JaCoCo XML and get an instant quality breakdown: composite score,
  grade (A–F), package rankings, class-level metrics, and hotspot detection
- Side-by-side comparison of two JaCoCo reports (before/after a refactor,
  two different projects, etc.)
- AI interpretation of any metric view via Groq's `llama-3.3-70b-versatile`
- Sensitivity analysis: shows how much package rankings change when composite
  weights are shifted by ±0.10
- Quality distribution histogram: how classes spread across score buckets
- Download a PDF report of the full analysis
- All metrics computed locally — only the AI interpretation calls an external API

---

## Prerequisites

- **Python 3.10 or newer** — check with `python --version`
- **pip** — bundled with Python; check with `pip --version`
- A **Groq API key** (free) for the AI features — get one at https://console.groq.com

The application works without a Groq key; the AI interpretation buttons will
return an error but everything else functions normally.

---

## Setup

### 1. Get the code

```bash
git clone https://github.com/your-username/your-repo-name.git
cd your-repo-name/webapp
```

### 2. Create a virtual environment

```bash
# Windows (PowerShell)
python -m venv .venv
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\.venv\Scripts\activate

# macOS / Linux
python -m venv .venv
source .venv/bin/activate
```

You should see `(.venv)` at the start of your terminal prompt once it is active.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

This installs FastAPI, uvicorn, the Groq SDK, scipy, xhtml2pdf, and gitpython.
It takes about 30–60 seconds on a fresh environment.

### 4. Configure the API key

Create a file named `.env` inside the `webapp/` directory:

```
GROQ_API_KEY=gsk_your_key_here
```

The application reads this file on startup. The key is never sent anywhere
except Groq's API when you click an AI interpretation button.

If you do not have a Groq key yet, create an empty `.env` file — the app
starts fine without it.

---

## Running the server

From inside the `webapp/` directory, with the virtual environment active:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000** in your browser.

The `--reload` flag makes the server restart automatically when you edit
source files. Leave it out in a production environment.

To stop the server, press `Ctrl+C` in the terminal.

### If port 8000 is already in use

Either use a different port:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

Or find and stop the process occupying 8000:

```powershell
# Windows — find the PID
netstat -ano | findstr :8000

# Kill it (replace 12345 with the actual PID)
taskkill /F /PID 12345
```

```bash
# macOS / Linux
lsof -ti :8000 | xargs kill -9
```

---

## Project layout

```
webapp/
├── app/
│   ├── main.py        — FastAPI endpoints
│   ├── metrics.py     — JaCoCo parser + all derived metrics (the core model)
│   ├── prompts.py     — LLM prompt templates (kept separate for thesis transparency)
│   └── report.py      — PDF report generation
├── static/
│   ├── app.js         — Alpine.js front-end component
│   └── style.css
├── templates/
│   ├── index.html     — Single-page application
│   └── pdf_report.html — Jinja2 template for PDF export
├── .env               — Your API key (not committed to git)
├── requirements.txt
└── README.md
```

The `analysis/` directory (one level up, at the project root) contains a
standalone script for the thesis empirical evaluation — see
[analysis/README.md](../analysis/README.md).

---

## API endpoints

| Method | Path                   | Description                                                      |
| ------ | ---------------------- | ---------------------------------------------------------------- |
| `GET`  | `/`                    | Serves the single-page app                                       |
| `POST` | `/api/analyze`         | Upload one JaCoCo XML; returns full metrics tree                 |
| `POST` | `/api/compare`         | Upload two XMLs; returns both trees for side-by-side view        |
| `POST` | `/api/explain`         | Stream LLM interpretation of a single metric target              |
| `POST` | `/api/explain/compare` | Stream LLM comparison of two targets                             |
| `POST` | `/api/ask`             | Stream LLM answer to a user-supplied question                    |
| `POST` | `/api/sensitivity`     | Re-score packages under weight perturbations; returns Spearman ρ |
| `POST` | `/api/report/pdf`      | Generate and download a PDF report                               |
| `GET`  | `/api/health`          | Check whether the Groq key is configured                         |

Interactive API docs (auto-generated by FastAPI) are available at
**http://127.0.0.1:8000/docs** while the server is running.

---

## Generating a JaCoCo XML

The tool expects a standard JaCoCo XML report. To generate one for a Maven project:

```bash
# Run tests with JaCoCo instrumentation
mvn test jacoco:report

# The XML is written to:
# target/site/jacoco/jacoco.xml
```

For a Gradle project, apply the `jacoco` plugin and run:

```bash
./gradlew test jacocoTestReport
# XML is at: build/reports/jacoco/test/jacocoTestReport.xml
```

---

## Bug-fix correlation analysis (thesis empirical evaluation)

The `analysis/bugfix_correlation.py` script correlates quality scores against
bug-fix commit history from a project's git repository. It is a standalone
script — it does not require the web server to be running.

```bash
# From the project root (one level above webapp/)
python analysis/bugfix_correlation.py \
    --jacoco   path/to/jacoco.xml \
    --repo     path/to/git/repo \
    --output   analysis/results/project-name.json
```

See [analysis/README.md](../analysis/README.md) for full documentation.

---

## Troubleshooting

**The page loads but shows nothing after uploading a file.**  
Hard-refresh the browser with `Ctrl+Shift+R` (Windows/Linux) or `Cmd+Shift+R`
(macOS) to clear the cached JavaScript.

**`ModuleNotFoundError: No module named 'app'`**  
Make sure you are running uvicorn from inside the `webapp/` directory, not the
project root.

**`Error: GROQ_API_KEY environment variable is not set`**  
The `.env` file is missing or empty. Create it with your key as shown in the
setup section. This only affects the AI interpretation buttons — the rest of
the app works without it.

**`Address already in use` on startup.**  
Another server process is occupying port 8000. Use the port-kill commands
shown in the _Running the server_ section above, or start on a different port
with `--port 8001`.

**`xhtml2pdf` errors when downloading the PDF.**  
Make sure `xhtml2pdf` installed correctly: `pip show xhtml2pdf`. On some
systems you may need to install it separately: `pip install xhtml2pdf`.
