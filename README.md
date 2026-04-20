# Medical Record Summarizer & Trend Analyzer

This project is a Flask + React dashboard for uploading unstructured clinical notes, extracting structured data with an LLM-style prompt, and visualizing patient trends over time.

## What it does

- Accepts pasted notes or uploaded text files from the dashboard.
- Extracts:
  - primary diagnosis
  - prescribed medications
  - recommended follow-up actions
  - vital signs
  - symptom mentions
- Stores anonymized patient IDs, raw note text, and structured JSON in the database.
- Displays patient timelines and charts for vitals and symptom frequency.
- Supports a local fallback parser so the demo still works without a live LLM key.

## Tech stack

- Frontend: React (via CDN) + Chart.js
- Backend: Flask + Flask-SQLAlchemy
- Database: MySQL via `DATABASE_URL`, with an in-memory SQLite demo fallback
- LLM integration: OpenAI-compatible chat endpoint shape via environment variables

## Quick start

1. Create and activate a virtual environment.
2. Install dependencies:


