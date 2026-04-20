import json
import os
import re
from collections import Counter
from datetime import datetime, timezone

import requests
from flask import Flask, current_app, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
UTC = timezone.utc
DEFAULT_SQLITE_URI = "sqlite:///:memory:"
MAX_NOTE_CHARACTERS = 20000
SYMPTOM_KEYWORDS = [
    "cough",
    "fever",
    "fatigue",
    "headache",
    "dizziness",
    "nausea",
    "chest pain",
    "shortness of breath",
    "wheezing",
    "edema",
    "palpitations",
]


class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    anonymized_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)
    notes = db.relationship(
        "ClinicalNote",
        back_populates="patient",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ClinicalNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False, index=True)
    source_filename = db.Column(db.String(255))
    raw_note = db.Column(db.Text, nullable=False)
    structured_data = db.Column(db.JSON, nullable=False)
    encountered_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    patient = db.relationship("Patient", back_populates="notes")


def normalize_database_uri(database_uri):
    if not database_uri:
        return ""
    if database_uri.startswith("mysql://"):
        return database_uri.replace("mysql://", "mysql+pymysql://", 1)
    return database_uri


def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = normalize_database_uri(os.getenv("DATABASE_URL")) or DEFAULT_SQLITE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "2")) * 1024 * 1024
    app.config["API_BEARER_TOKEN"] = os.getenv("API_BEARER_TOKEN", "").strip()
    app.config["LLM_API_URL"] = os.getenv("LLM_API_URL", "").strip()
    app.config["LLM_API_KEY"] = os.getenv("LLM_API_KEY", "").strip()
    app.config["LLM_MODEL"] = os.getenv("LLM_MODEL", "gpt-4.1-mini").strip()
    app.config["REQUEST_TIMEOUT_SECONDS"] = int(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
    db.init_app(app)

    @app.before_request
    def enforce_api_token():
        token = current_app.config["API_BEARER_TOKEN"]
        if not token:
            return None
        if not request.path.startswith("/api/") or request.method == "GET":
            return None
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {token}":
            return jsonify({"error": "Unauthorized"}), 401
        return None

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            app_config={
                "apiTokenRequired": bool(current_app.config["API_BEARER_TOKEN"]),
                "maxUploadMb": max(current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024), 1),
            },
        )

    @app.route("/api/health")
    def health_check():
        return jsonify(
            {
                "status": "ok",
                "database_engine": current_app.config["SQLALCHEMY_DATABASE_URI"].split(":", 1)[0],
                "llm_mode": "live" if llm_is_configured() else "fallback",
                "generated_at": isoformat(datetime.now(UTC)),
            }
        )

    @app.route("/api/dashboard")
    def dashboard_api():
        patient_rows = Patient.query.all()
        note_rows = ClinicalNote.query.order_by(ClinicalNote.encountered_at.desc()).all()
        default_patient_id = patient_rows[0].anonymized_id if patient_rows else None
        if note_rows:
            default_patient_id = note_rows[0].patient.anonymized_id
        return jsonify(
            {
                "overview": build_overview(note_rows, patient_rows),
                "patients": build_patient_cards(patient_rows),
                "recent_notes": [serialize_note(note, include_raw_preview=True) for note in note_rows[:6]],
                "default_patient_id": default_patient_id,
            }
        )

    @app.route("/api/patients/<anonymized_id>/timeline")
    def patient_timeline_api(anonymized_id):
        patient = Patient.query.filter_by(anonymized_id=normalize_patient_id(anonymized_id)).first()
        if patient is None:
            return jsonify({"error": "Patient not found"}), 404
        notes = sorted(patient.notes, key=lambda note: note.encountered_at)
        return jsonify(build_timeline_payload(patient, notes))

    @app.route("/api/notes", methods=["POST"])
    def create_note_api():
        payload, errors = extract_note_submission(request)
        if errors:
            return jsonify({"error": errors[0]}), 400

        patient_id = normalize_patient_id(payload["patient_id"])
        patient = Patient.query.filter_by(anonymized_id=patient_id).first()
        if patient is None:
            patient = Patient(anonymized_id=patient_id)
            db.session.add(patient)
            db.session.flush()

        structured = extract_clinical_data(payload["note_text"])
        clinical_note = ClinicalNote(
            patient_id=patient.id,
            source_filename=payload.get("source_filename"),
            raw_note=payload["note_text"],
            structured_data=structured,
            encountered_at=payload["encountered_at"],
        )
        db.session.add(clinical_note)
        db.session.commit()

        notes = ClinicalNote.query.filter_by(patient_id=patient.id).order_by(ClinicalNote.encountered_at.asc()).all()
        return jsonify(
            {
                "message": "Clinical note stored successfully.",
                "note": serialize_note(clinical_note, include_raw_preview=True),
                "timeline": build_timeline_payload(patient, notes),
            }
        ), 201

    with app.app_context():
        db.create_all()
        seed_demo_data()

    return app


def llm_is_configured():
    return bool(current_app.config["LLM_API_URL"] and current_app.config["LLM_API_KEY"])


def isoformat(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def normalize_patient_id(value):
    cleaned = re.sub(r"[^A-Z0-9-]", "", (value or "").upper().strip())
    return cleaned or f"PAT-{datetime.now(UTC).strftime('%H%M%S')}"


def parse_encountered_at(value):
    if not value:
        return datetime.now(UTC)
    trimmed = value.strip()
    try:
        parsed = datetime.fromisoformat(trimmed.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(trimmed, "%Y-%m-%d")
        except ValueError:
            return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def extract_note_submission(req):
    content_type = req.content_type or ""
    errors = []
    payload = {
        "patient_id": "",
        "note_text": "",
        "source_filename": None,
        "encountered_at": datetime.now(UTC),
    }

    if "application/json" in content_type:
        json_payload = req.get_json(silent=True) or {}
        payload["patient_id"] = json_payload.get("patient_id", "")
        payload["note_text"] = (json_payload.get("note_text") or "").strip()
        payload["encountered_at"] = parse_encountered_at(json_payload.get("encountered_at"))
    else:
        payload["patient_id"] = req.form.get("patient_id", "")
        payload["note_text"] = (req.form.get("note_text") or "").strip()
        payload["encountered_at"] = parse_encountered_at(req.form.get("encountered_at"))
        upload = req.files.get("note_file")
        if upload and upload.filename:
            payload["source_filename"] = upload.filename
            upload_text = upload.stream.read().decode("utf-8", errors="ignore").strip()
            if upload_text:
                payload["note_text"] = upload_text

    if not payload["patient_id"]:
        errors.append("An anonymized patient ID is required.")
    if not payload["note_text"]:
        errors.append("Provide clinical note text or upload a text-based note file.")
    if len(payload["note_text"]) > MAX_NOTE_CHARACTERS:
        errors.append(f"Clinical note exceeds the {MAX_NOTE_CHARACTERS} character limit.")
    return payload, errors


def build_overview(notes, patients):
    diagnosis_counts = Counter()
    flagged_followups = 0
    for note in notes:
        diagnosis = (note.structured_data or {}).get("primary_diagnosis")
        if diagnosis:
            diagnosis_counts[diagnosis] += 1
        followups = (note.structured_data or {}).get("recommended_follow_up_actions", [])
        if any("urgent" in action.lower() or "er" in action.lower() for action in followups):
            flagged_followups += 1

    top_diagnoses = [
        {"label": label, "count": count}
        for label, count in diagnosis_counts.most_common(4)
    ]

    return {
        "patient_count": len(patients),
        "note_count": len(notes),
        "llm_mode": "Live LLM" if llm_is_configured() else "Fallback Parser",
        "flagged_followups": flagged_followups,
        "top_diagnoses": top_diagnoses,
    }


def build_patient_cards(patients):
    cards = []
    for patient in patients:
        notes = sorted(patient.notes, key=lambda note: note.encountered_at, reverse=True)
        latest_note = notes[0] if notes else None
        cards.append(
            {
                "anonymized_id": patient.anonymized_id,
                "note_count": len(notes),
                "last_seen": isoformat(latest_note.encountered_at if latest_note else patient.created_at),
                "latest_diagnosis": (latest_note.structured_data or {}).get("primary_diagnosis", "No extraction yet")
                if latest_note
                else "No extraction yet",
                "latest_risk_flags": (latest_note.structured_data or {}).get("risk_flags", []) if latest_note else [],
            }
        )
    cards.sort(key=lambda card: card["last_seen"] or "", reverse=True)
    return cards


def build_timeline_payload(patient, notes):
    labels = [note.encountered_at.strftime("%b %d") for note in notes]
    systolic = []
    diastolic = []
    heart_rate = []
    oxygen = []
    weight = []
    symptoms = Counter()
    latest_note = notes[-1] if notes else None

    for note in notes:
        data = note.structured_data or {}
        vitals = data.get("vital_signs", {})
        systolic.append(vitals.get("blood_pressure_systolic"))
        diastolic.append(vitals.get("blood_pressure_diastolic"))
        heart_rate.append(vitals.get("heart_rate"))
        oxygen.append(vitals.get("oxygen_saturation"))
        weight.append(vitals.get("weight_kg"))
        symptoms.update(data.get("symptoms", []))

    return {
        "patient": {
            "anonymized_id": patient.anonymized_id,
            "note_count": len(notes),
            "latest_diagnosis": (latest_note.structured_data or {}).get("primary_diagnosis", "No diagnosis available")
            if latest_note
            else "No diagnosis available",
            "latest_medications": (latest_note.structured_data or {}).get("prescribed_medications", []) if latest_note else [],
            "latest_follow_up": (latest_note.structured_data or {}).get("recommended_follow_up_actions", []) if latest_note else [],
        },
        "notes": [serialize_note(note, include_raw_preview=True) for note in reversed(notes)],
        "trends": {
            "labels": labels,
            "blood_pressure_systolic": systolic,
            "blood_pressure_diastolic": diastolic,
            "heart_rate": heart_rate,
            "oxygen_saturation": oxygen,
            "weight_kg": weight,
        },
        "symptom_frequency": [
            {"label": label, "count": count}
            for label, count in symptoms.most_common(6)
        ],
    }


def serialize_note(note, include_raw_preview=False):
    structured = note.structured_data or {}
    payload = {
        "id": note.id,
        "patient_id": note.patient.anonymized_id,
        "encountered_at": isoformat(note.encountered_at),
        "created_at": isoformat(note.created_at),
        "source_filename": note.source_filename,
        "primary_diagnosis": structured.get("primary_diagnosis", ""),
        "prescribed_medications": structured.get("prescribed_medications", []),
        "recommended_follow_up_actions": structured.get("recommended_follow_up_actions", []),
        "vital_signs": structured.get("vital_signs", {}),
        "symptoms": structured.get("symptoms", []),
        "risk_flags": structured.get("risk_flags", []),
        "extraction_mode": structured.get("extraction_mode", "fallback"),
    }
    if include_raw_preview:
        preview = note.raw_note[:220].strip()
        payload["raw_note_preview"] = f"{preview}..." if len(note.raw_note) > 220 else preview
    return payload


def build_llm_prompt(note_text):
    schema = {
        "primary_diagnosis": "string",
        "prescribed_medications": ["string"],
        "recommended_follow_up_actions": ["string"],
        "vital_signs": {
            "blood_pressure_systolic": "number|null",
            "blood_pressure_diastolic": "number|null",
            "heart_rate": "number|null",
            "temperature_f": "number|null",
            "oxygen_saturation": "number|null",
            "respiratory_rate": "number|null",
            "weight_kg": "number|null",
        },
        "symptoms": ["string"],
        "risk_flags": ["string"],
    }
    return (
        "Extract the primary diagnosis, prescribed medications, and recommended follow-up actions from this text. "
        "Return only valid JSON matching this schema: "
        f"{json.dumps(schema)}. "
        "Use null for missing numeric vital signs and empty arrays for missing list fields.\n\n"
        f"Clinical note:\n{note_text}"
    )


def extract_clinical_data(note_text):
    if llm_is_configured():
        try:
            live_result = call_llm_extractor(note_text)
            live_result["extraction_mode"] = "llm"
            return coerce_structured_payload(live_result)
        except Exception:
            pass
    fallback = fallback_extract_clinical_data(note_text)
    fallback["extraction_mode"] = "fallback"
    return fallback


def call_llm_extractor(note_text):
    response = requests.post(
        current_app.config["LLM_API_URL"],
        headers={
            "Authorization": f"Bearer {current_app.config['LLM_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "model": current_app.config["LLM_MODEL"],
            "messages": [
                {
                    "role": "system",
                    "content": "You extract structured clinical information and return JSON only.",
                },
                {
                    "role": "user",
                    "content": build_llm_prompt(note_text),
                },
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=current_app.config["REQUEST_TIMEOUT_SECONDS"],
    )
    response.raise_for_status()
    payload = response.json()
    content = (
        payload.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    if isinstance(content, list):
        content = "".join(
            segment.get("text", "")
            for segment in content
            if isinstance(segment, dict)
        )
    return parse_json_blob(content)


def parse_json_blob(content):
    cleaned = (content or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def coerce_structured_payload(payload):
    vitals = payload.get("vital_signs", {}) if isinstance(payload, dict) else {}
    return {
        "primary_diagnosis": str(payload.get("primary_diagnosis", "")).strip(),
        "prescribed_medications": normalize_text_list(payload.get("prescribed_medications", [])),
        "recommended_follow_up_actions": normalize_text_list(payload.get("recommended_follow_up_actions", [])),
        "vital_signs": {
            "blood_pressure_systolic": to_number(vitals.get("blood_pressure_systolic")),
            "blood_pressure_diastolic": to_number(vitals.get("blood_pressure_diastolic")),
            "heart_rate": to_number(vitals.get("heart_rate")),
            "temperature_f": to_number(vitals.get("temperature_f")),
            "oxygen_saturation": to_number(vitals.get("oxygen_saturation")),
            "respiratory_rate": to_number(vitals.get("respiratory_rate")),
            "weight_kg": to_number(vitals.get("weight_kg")),
        },
        "symptoms": normalize_text_list(payload.get("symptoms", [])),
        "risk_flags": normalize_text_list(payload.get("risk_flags", [])),
    }


def fallback_extract_clinical_data(note_text):
    diagnosis = extract_primary_diagnosis(note_text)
    medications = extract_labeled_list(note_text, ["prescribed medications", "medications", "discharge meds"])
    follow_up = extract_labeled_list(note_text, ["follow-up", "follow up", "recommended follow-up actions", "plan"])
    symptoms = [keyword for keyword in SYMPTOM_KEYWORDS if keyword in note_text.lower()]
    risk_flags = [
        phrase
        for phrase in ["urgent follow-up", "er precautions", "return to ed", "worsening symptoms", "red flag"]
        if phrase in note_text.lower()
    ]
    return {
        "primary_diagnosis": diagnosis,
        "prescribed_medications": medications,
        "recommended_follow_up_actions": follow_up,
        "vital_signs": extract_vitals(note_text),
        "symptoms": symptoms,
        "risk_flags": normalize_text_list(risk_flags),
    }


def extract_primary_diagnosis(note_text):
    label_patterns = [
        r"(?:primary diagnosis|diagnosis|assessment)\s*[:\-]\s*(.+)",
        r"(?:impression)\s*[:\-]\s*(.+)",
    ]
    for pattern in label_patterns:
        match = re.search(pattern, note_text, re.IGNORECASE)
        if match:
            return truncate_sentence(match.group(1))

    heuristics = {
        "congestive heart failure": "Congestive heart failure exacerbation",
        "heart failure": "Congestive heart failure exacerbation",
        "type 2 diabetes": "Type 2 diabetes mellitus",
        "diabetes": "Type 2 diabetes mellitus",
        "copd": "COPD exacerbation",
        "pneumonia": "Community-acquired pneumonia",
        "asthma": "Asthma flare",
        "hypertension": "Hypertension",
    }
    lower_note = note_text.lower()
    for keyword, label in heuristics.items():
        if keyword in lower_note:
            return label
    return "Diagnosis not clearly stated"


def truncate_sentence(value):
    first_line = value.strip().splitlines()[0]
    return re.split(r"(?<=[.!?])\s", first_line)[0][:180].strip(" .")


def extract_labeled_list(note_text, labels):
    lower_note = note_text.lower()
    for label in labels:
        marker = f"{label.lower()}:"
        index = lower_note.find(marker)
        if index == -1:
            marker = f"{label.lower()} -"
            index = lower_note.find(marker)
        if index == -1:
            continue
        snippet = note_text[index + len(marker):].splitlines()[0]
        items = split_list_items(snippet)
        if items:
            return items

    fallback_matches = re.findall(r"(?:continue|start|started on|prescribed)\s+([^.]+)", note_text, re.IGNORECASE)
    items = []
    for match in fallback_matches:
        items.extend(split_list_items(match))
    return normalize_text_list(items)


def split_list_items(value):
    normalized = re.sub(r"\band\b", ",", value, flags=re.IGNORECASE)
    pieces = re.split(r"[;,/]\s*|\s{2,}", normalized)
    return normalize_text_list(pieces)


def normalize_text_list(items):
    cleaned = []
    for item in items or []:
        text = str(item).strip(" -.\n\t")
        if text and text.lower() not in {existing.lower() for existing in cleaned}:
            cleaned.append(text)
    return cleaned


def to_number(value):
    if value in ("", None):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def extract_vitals(note_text):
    blood_pressure = re.search(r"(?:bp|blood pressure)\s*[:\-]?\s*(\d{2,3})/(\d{2,3})", note_text, re.IGNORECASE)
    heart_rate = re.search(r"(?:hr|heart rate|pulse)\s*[:\-]?\s*(\d{2,3})", note_text, re.IGNORECASE)
    temperature = re.search(r"(?:temp|temperature)\s*[:\-]?\s*(\d{2,3}(?:\.\d+)?)", note_text, re.IGNORECASE)
    oxygen = re.search(r"(?:spo2|oxygen saturation|o2 sat)\s*[:\-]?\s*(\d{2,3})%?", note_text, re.IGNORECASE)
    respiratory = re.search(r"(?:rr|respiratory rate)\s*[:\-]?\s*(\d{1,2})", note_text, re.IGNORECASE)
    weight = re.search(r"(?:weight)\s*[:\-]?\s*(\d{2,3}(?:\.\d+)?)\s*(?:kg)?", note_text, re.IGNORECASE)
    return {
        "blood_pressure_systolic": to_number(blood_pressure.group(1) if blood_pressure else None),
        "blood_pressure_diastolic": to_number(blood_pressure.group(2) if blood_pressure else None),
        "heart_rate": to_number(heart_rate.group(1) if heart_rate else None),
        "temperature_f": to_number(temperature.group(1) if temperature else None),
        "oxygen_saturation": to_number(oxygen.group(1) if oxygen else None),
        "respiratory_rate": to_number(respiratory.group(1) if respiratory else None),
        "weight_kg": to_number(weight.group(1) if weight else None),
    }


def seed_demo_data():
    if ClinicalNote.query.first():
        return

    demo_entries = [
        {
            "patient_id": "PT-1042",
            "encountered_at": "2026-03-28",
            "note_text": (
                "Primary diagnosis: Congestive heart failure exacerbation. "
                "Symptoms include shortness of breath, fatigue, and edema. "
                "BP 148/92, HR 102, SpO2 93%, weight 81 kg. "
                "Prescribed medications: furosemide 40 mg daily, lisinopril 10 mg daily. "
                "Follow-up: Cardiology follow-up in 7 days; return to ED for worsening symptoms."
            ),
        },
        {
            "patient_id": "PT-1042",
            "encountered_at": "2026-04-05",
            "note_text": (
                "Assessment: Congestive heart failure exacerbation improving after diuresis. "
                "Patient still reports mild fatigue. BP 138/86, HR 90, SpO2 95%, weight 78.5 kg. "
                "Medications: furosemide 20 mg daily, lisinopril 10 mg daily. "
                "Plan: Daily weights, low sodium diet, follow up with cardiology clinic in 10 days."
            ),
        },
        {
            "patient_id": "PT-1042",
            "encountered_at": "2026-04-14",
            "note_text": (
                "Diagnosis: Heart failure outpatient review. "
                "No chest pain, mild shortness of breath on exertion. BP 132/82, HR 84, SpO2 96%, weight 76.8 kg. "
                "Discharge meds: furosemide 20 mg daily, lisinopril 10 mg daily, atorvastatin 20 mg nightly. "
                "Follow-up: Repeat BNP labs in 2 weeks; clinic review in 1 month."
            ),
        },
        {
            "patient_id": "PT-2088",
            "encountered_at": "2026-03-30",
            "note_text": (
                "Primary diagnosis: Type 2 diabetes mellitus with poor glycemic control. "
                "Patient reports fatigue, headache, and dizziness. BP 144/88, HR 88, weight 92 kg. "
                "Prescribed medications: metformin 1000 mg twice daily, insulin glargine 12 units nightly. "
                "Recommended follow-up actions: Check fasting glucose log daily; endocrinology follow-up in 14 days."
            ),
        },
        {
            "patient_id": "PT-2088",
            "encountered_at": "2026-04-12",
            "note_text": (
                "Assessment: Type 2 diabetes mellitus showing early improvement. "
                "Symptoms now limited to mild fatigue. BP 136/84, HR 82, weight 89.6 kg. "
                "Medications: metformin 1000 mg twice daily, insulin glargine 10 units nightly. "
                "Plan: Continue glucose monitoring, nutrition counseling, repeat A1c in 3 months."
            ),
        },
    ]

    for entry in demo_entries:
        patient = Patient.query.filter_by(anonymized_id=entry["patient_id"]).first()
        if patient is None:
            patient = Patient(anonymized_id=entry["patient_id"])
            db.session.add(patient)
            db.session.flush()
        db.session.add(
            ClinicalNote(
                patient_id=patient.id,
                raw_note=entry["note_text"],
                structured_data={
                    **fallback_extract_clinical_data(entry["note_text"]),
                    "extraction_mode": "seed",
                },
                encountered_at=parse_encountered_at(entry["encountered_at"]),
            )
        )

    db.session.commit()


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
