"""AI Internship Matcher - Flask backend.

Flow: upload a CV (PDF/DOCX) or pick skills -> extract skills -> score every
active program in catalogue.json -> show the top matches with a skill-gap.

Scoring is deterministic and explainable (see score_program):
    match % = 80% * (matched required skills / total required skills)
            + 20% * experience fit (1.0 if level >= ideal level, else 0.5)
Programs above the student's level (min_level) are never recommended.
Interests only break ties and add a badge; they never change the score.
"""
import io
import json
import re
from pathlib import Path

from docx import Document
from flask import Flask, render_template, request
from pypdf import PdfReader

BASE_DIR = Path(__file__).resolve().parent
MAX_UPLOAD_MB = 5
TOP_N = 3
MIN_SCORE = 20  # hide weak matches below this percentage

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

LEVELS = {"beginner": 1, "intermediate": 2, "advanced": 3}
LEVEL_OPTIONS = [
    ("beginner", "Beginner", "Learning, small projects"),
    ("intermediate", "Intermediate", "Several projects built"),
    ("advanced", "Advanced", "Professional or large projects"),
]

# Extra spellings so "js", "sklearn" etc. in a CV map to catalogue skills.
SKILL_ALIASES = {
    "JavaScript": ["javascript", "js", "ecmascript", "es6"],
    "HTML": ["html", "html5"],
    "CSS": ["css", "css3", "sass", "tailwind", "bootstrap"],
    "React": ["react", "reactjs", "react.js"],
    "Node.js": ["node.js", "nodejs", "node js"],
    "Responsive Layouts": ["responsive design", "responsive layout", "responsive layouts", "mobile-first", "media queries"],
    "Git": ["git", "github", "gitlab", "version control"],
    "Python": ["python", "python3"],
    "Flask": ["flask", "django", "fastapi"],
    "SQL": ["sql", "mysql", "postgresql", "postgres", "sqlite"],
    "Databases": ["database", "databases", "mongodb", "nosql", "dbms"],
    "REST APIs": ["rest api", "rest apis", "restful", "api development"],
    "Machine Learning": ["machine learning", "ml", "supervised learning", "deep learning", "neural network", "neural networks"],
    "Pandas": ["pandas"],
    "NumPy": ["numpy"],
    "Scikit-learn": ["scikit-learn", "sklearn", "scikit learn"],
    "Data Analysis": ["data analysis", "data analytics", "eda", "matplotlib", "seaborn", "data visualization"],
    "Figma": ["figma"],
    "Wireframing": ["wireframe", "wireframes", "wireframing"],
    "Prototyping": ["prototype", "prototypes", "prototyping"],
    "UX Research": ["ux research", "user research", "usability testing", "user testing"],
    "Docker": ["docker", "containerization"],
    "Linux": ["linux", "ubuntu", "bash", "shell scripting"],
    "CI/CD": ["ci/cd", "ci cd", "github actions", "jenkins", "continuous integration"],
    "Cloud": ["aws", "azure", "gcp", "google cloud", "cloud computing"],
    "Monitoring": ["monitoring", "uptime", "prometheus", "grafana"],
    "Web Scraping": ["web scraping", "scraping", "beautifulsoup", "selenium", "scrapy"],
    "Automation": ["automation"],
    "Excel": ["excel", "csv", "spreadsheets"],
    "Prompt Engineering": ["prompt engineering", "prompting", "prompt design"],
    "LLMs": ["llm", "llms", "large language model", "large language models", "chatgpt", "openai"],
    "NLP": ["nlp", "natural language processing", "text mining"],
}


class UserError(Exception):
    """An error whose message is safe to show to the student."""


def load_catalogue():
    with open(BASE_DIR / "catalogue.json", encoding="utf-8") as f:
        data = json.load(f)
    return [p for p in data["programs"] if p.get("status") == "active"]


def build_vocabulary(programs):
    """Canonical skill -> list of lowercase aliases, built from the catalogue."""
    vocab = {}
    for program in programs:
        for skill in program["required_skills"]:
            vocab.setdefault(skill, {skill.lower()})
    for skill, aliases in SKILL_ALIASES.items():
        if skill in vocab:
            vocab[skill].update(a.lower() for a in aliases)
    return {skill: sorted(a, key=len, reverse=True) for skill, a in vocab.items()}


PROGRAMS = load_catalogue()
VOCAB = build_vocabulary(PROGRAMS)
ALL_SKILLS = sorted(VOCAB)
TRACKS = sorted({p["track"] for p in PROGRAMS})


# ---------- CV parsing ----------
def extract_text(file_storage):
    """Return plain text from an uploaded PDF or DOCX, or raise UserError."""
    filename = (file_storage.filename or "").lower()
    data = file_storage.read()
    if not data:
        raise UserError("The uploaded file is empty. Choose another file or enter your skills manually.")

    try:
        if filename.endswith(".pdf"):
            if not data.startswith(b"%PDF"):
                raise UserError("This file is not a valid PDF. Export your CV as PDF again and retry.")
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise UserError("This PDF is password-protected. Remove the password and upload it again.")
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif filename.endswith(".docx"):
            if not data.startswith(b"PK"):
                raise UserError("This file is not a valid DOCX. Save your CV as .docx and retry.")
            doc = Document(io.BytesIO(data))
            parts = [p.text for p in doc.paragraphs]
            for table in doc.tables:
                for row in table.rows:
                    parts.extend(cell.text for cell in row.cells)
            text = "\n".join(parts)
        else:
            raise UserError("Unsupported file type. Upload a PDF or DOCX file.")
    except UserError:
        raise
    except Exception:
        raise UserError("We could not read this file. It may be corrupted. Try another file or enter your skills manually.")

    if not text.strip():
        raise UserError("No readable text found. Scanned or image-only CVs are not supported. Upload a text-based file or enter your skills manually.")
    return text


def find_skills(text):
    """Return the set of catalogue skills mentioned in text."""
    lowered = text.lower()
    found = set()
    for skill, aliases in VOCAB.items():
        for alias in aliases:
            pattern = r"(?<![a-z0-9+#])" + re.escape(alias) + r"(?![a-z0-9+#])"
            if re.search(pattern, lowered):
                found.add(skill)
                break
    return found


def parse_manual_skills(selected, typed):
    """Combine ticked skills with comma-separated free text."""
    skills = {s for s in selected if s in VOCAB}
    if typed:
        skills |= find_skills(typed.replace(",", " , "))
    return skills


# ---------- Matching ----------
def score_program(program, user_skills, user_level):
    required = program["required_skills"]
    matched = [s for s in required if s in user_skills]
    missing = [s for s in required if s not in user_skills]
    skill_ratio = len(matched) / len(required)
    level_ok = LEVELS[user_level] >= LEVELS[program["ideal_level"]]
    level_ratio = 1.0 if level_ok else 0.5
    score = round(100 * (0.8 * skill_ratio + 0.2 * level_ratio))
    return {
        "program": program,
        "score": score,
        "matched": matched,
        "missing": missing,
        "total": len(required),
        "skill_points": round(80 * skill_ratio),
        "level_points": round(20 * level_ratio),
        "level_ok": level_ok,
        "why": f"You already cover {len(matched)} of {len(required)} required skills for this program.",
    }


def recommend(user_skills, user_level, interests):
    results, hidden = [], 0
    for program in PROGRAMS:
        if LEVELS[user_level] < LEVELS[program["min_level"]]:
            hidden += 1  # not eligible, never recommended
            continue
        result = score_program(program, user_skills, user_level)
        result["interest"] = program["track"] in interests
        if result["matched"] and result["score"] >= MIN_SCORE:
            results.append(result)
    results.sort(key=lambda r: (-r["score"], not r["interest"], r["program"]["name"]))
    return results[:TOP_N], hidden


# ---------- Routes ----------
def render_form(error=None, status=400, **form):
    return (
        render_template(
            "index.html",
            skills=ALL_SKILLS,
            tracks=TRACKS,
            programs=PROGRAMS,
            levels=LEVEL_OPTIONS,
            error=error,
            form=form,
            max_mb=MAX_UPLOAD_MB,
        ),
        status if error else 200,
    )


@app.route("/")
def index():
    return render_form()


@app.route("/match", methods=["POST"])
def match():
    level = request.form.get("level", "beginner")
    selected = request.form.getlist("skills")
    interests = [t for t in request.form.getlist("interests") if t in TRACKS]
    typed = request.form.get("typed_skills", "").strip()
    upload = request.files.get("cv")
    form = {"level": level, "selected": selected, "interests": interests, "typed": typed}

    if level not in LEVELS:
        form["level"] = "beginner"
        return render_form("Choose an experience level.", **form)

    try:
        user_skills = parse_manual_skills(selected, typed)
        source = "your selected skills"
        if upload and upload.filename:
            user_skills |= find_skills(extract_text(upload))
            source = "your CV" + (" and selected skills" if selected or typed else "")
            if not user_skills:
                raise UserError("We read your CV but found no skills from our catalogue. Add your skills manually below.")
        elif not user_skills:
            raise UserError("Upload a CV or select at least one skill to continue.")
    except UserError as err:
        return render_form(str(err), **form)

    results, hidden = recommend(user_skills, level, interests)
    return render_template(
        "results.html",
        results=results,
        user_skills=sorted(user_skills),
        level=level,
        source=source,
        hidden=hidden,
    )


@app.errorhandler(413)
def too_large(_):
    return render_form(f"That file is larger than {MAX_UPLOAD_MB} MB. Upload a smaller file.")


@app.errorhandler(404)
def not_found(_):
    return render_form("That page does not exist.", status=404)


import os
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)