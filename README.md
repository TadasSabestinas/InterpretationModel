# JaCoCo Coverage Interpretation Tool

A web application that parses JaCoCo XML reports, computes derived quality
metrics, displays changes within packages, classes, shows risky code areas and uses an LLM to produce natural-language interpretations.

## Features

- Upload a JaCoCo XML and get a quality breakdown: composite score, grade (A-F),
  package rankings, class-level metrics, and hotspot detection
- Side-by-side comparison of two JaCoCo reports
- AI interpretation of any metric view via Groq's `llama-3.3-70b-versatile`
- Quality distribution histogram

## Prerequisites

- Python 3.10 or newer
- A Groq API key (free) for AI features, the rest of the app works without it

## Setup

```bash
# Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\activate    # for windows
source .venv/bin/activate       # for macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Add your API key
echo GROQ_API_KEY=gsk_your_key_here > .env
```

## Running

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** in your browser.

## Project layout

```
webapp/
├── app/
│   ├── main.py      FastAPI endpoints
│   ├── metrics.py   JaCoCo parser and all derived metrics
│   └── prompts.py   LLM prompt templates
├── static/
│   ├── app.js       Alpine.js frontend
│   └── style.css
├── templates/
│   └── index.html   Single-page application
├── .env             API key (not committed)
└── requirements.txt
```

## Generating a JaCoCo XML

```bash
# Maven
mvn test jacoco:report
# output: target/site/jacoco/jacoco.xml

# Gradle
./gradlew test jacocoTestReport
# output: build/reports/jacoco/test/jacocoTestReport.xml
```

## Troubleshooting

**Nothing shows after upload** - hard-refresh with `Ctrl+Shift+R`.

**`ModuleNotFoundError: No module named 'app'`** - run uvicorn from inside the `webapp/` directory.

**`GROQ_API_KEY not set`** - create `.env` with your key; only AI buttons are affected.

**Port 8000 in use** - use `--port 8001` or kill the occupying process.
