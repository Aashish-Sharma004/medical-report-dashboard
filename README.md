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

```bash
pip install -r requirements.txt
```

3. Set environment variables as needed.
4. Run the app:

```bash
python app.py
```

5. Open `http://127.0.0.1:5000`.

## Environment variables

```env
DATABASE_URL=mysql+pymysql://username:password@localhost/medical_records
API_BEARER_TOKEN=optional-shared-secret
LLM_API_URL=https://api.openai.com/v1/chat/completions
LLM_API_KEY=your_api_key
LLM_MODEL=gpt-4.1-mini
LLM_TIMEOUT_SECONDS=20
MAX_UPLOAD_MB=2
```

Notes:

- If `DATABASE_URL` is omitted, the app uses an in-memory SQLite demo database and reseeds sample records on each restart.
- For persistent local storage, point `DATABASE_URL` at MySQL or an explicit writable SQLite file path.
- If `API_BEARER_TOKEN` is set, all non-GET `/api/*` requests require `Authorization: Bearer <token>`.
- If `LLM_API_URL` and `LLM_API_KEY` are not set, the app falls back to a deterministic regex-based extractor.

## Data model

- `Patient`
  - `anonymized_id`
  - `created_at`
- `ClinicalNote`
  - `patient_id`
  - `source_filename`
  - `raw_note`
  - `structured_data` (JSON)
  - `encountered_at`
  - `created_at`

## API endpoints

- `GET /api/dashboard`
- `GET /api/patients/<anonymized_id>/timeline`
- `POST /api/notes`
- `GET /api/health`

## Demo behavior

The app seeds sample anonymized patients and notes on first run so the charts are populated immediately for portfolio demos.
