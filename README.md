# AI Internship Matcher

Matches student CVs/skills against a verified internship catalogue with transparent scoring.

    python -m venv venv
    venv\Scripts\activate        (Windows)
    pip install -r requirements.txt
    python app.py                -> http://127.0.0.1:5000

Deploy: build `pip install -r requirements.txt`, start `gunicorn app:app`.
Edit `catalogue.json` to change programs.
