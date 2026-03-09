import os
import re
import sqlite3
from datetime import datetime, date
from functools import wraps
from typing import Any, Dict, List

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, abort
)

# ==========================================
# Configuration
# ==========================================
DB_PATH = os.environ.get("DB_PATH", "app.db")

# Admin credentials (set these via environment variables in production)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")

SITE_NAME = os.environ.get("SITE_NAME", "Krista Carson Elhai")
SITE_TAGLINE = os.environ.get("SITE_TAGLINE", "Claremont School Board • District #1")

# Donation settings for the public donate page (link if using an external platform)
DONATION_LINK = os.environ.get("DONATION_LINK", "")
DONATION_MAIL_TO = os.environ.get("DONATION_MAIL_TO", "[Campaign Mailing Address]")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "[campaign email]")

# Admin donations dashboard goal
DONATION_GOAL = float(os.environ.get("DONATION_GOAL", "10000"))

# Flask secret
SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(24)


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.secret_key = SECRET_KEY

    with app.app_context():
        init_db()

    @app.before_request
    def ensure_csrf():
        if "csrf" not in session:
            session["csrf"] = os.urandom(16).hex()

    def csrf_check():
        token = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
        if not token or token != session.get("csrf"):
            abort(400, description="CSRF token missing or invalid.")

    def admin_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("is_admin"):
                # Staff login is accessed by the "Staff" tab (route: /admin)
                return redirect(url_for("admin_entry"))
            return fn(*args, **kwargs)
        return wrapper

    @app.context_processor
    def inject_globals():
        return {
            "site_name": SITE_NAME,
            "site_tagline": SITE_TAGLINE,
            "year": datetime.now().year,
            "csrf": session.get("csrf", ""),
        }

    # ==========================================
    # Public pages
    # ==========================================
    @app.get("/")
    def home():
        events = db_list_events(limit=6)
        return render_template("home.html", title="Home", events=events)

    @app.get("/events")
    def events():
        events = db_list_events(limit=200)
        return render_template("events.html", title="Events", events=events)

    @app.get("/donate")
    def donate():
        return render_template(
            "donate_public.html",
            title="Donate",
            donation_link=DONATION_LINK,
            donation_mail_to=DONATION_MAIL_TO,
            contact_email=CONTACT_EMAIL,
        )

    @app.get("/resources")
    def resources():
        return render_template("resources.html", title="Resources")

    @app.get("/contact")
    def contact():
        return render_template("contact.html", title="Contact")

    @app.post("/api/message")
    def api_message():
        csrf_check()
        full_name = (request.form.get("full_name") or "").strip()
        email = (request.form.get("email") or "").strip()
        message = (request.form.get("message") or "").strip()
        if not full_name or not message:
            flash("Please fill in your name and message.", "error")
            return redirect(url_for("contact"))

        db_insert_submission(
            kind="message",
            full_name=full_name,
            email=email,
            message=message,
            phone="",
            zip_code="",
            organization="",
            role_title="",
            address="",
            city="",
            state="",
            opt_in_updates=0,
        )
        flash("Thanks — your message was sent.", "ok")
        return redirect(url_for("contact"))

    # Pages that wrap embedded content blocks (stored in ./uploads)
    @app.get("/how-to-vote")
    def how_to_vote():
        content = load_embedded_block("how-to-vote.html")
        return render_template("how-to-vote.html", title="How to Vote", content=content)

    @app.get("/get-involved")
    def get_involved():
        content = load_embedded_block("get-involved.html")
        return render_template("get-involved.html", title="Get Involved", content=content)

    # API endpoint used by Get Involved page JS
    @app.post("/api/submit")
    def api_submit():
        data = request.get_json(silent=True) or {}

        # Honeypot field (bots often fill this)
        if (data.get("company_website") or "").strip():
            return jsonify({"ok": True})

        kind = (data.get("kind") or "").strip().lower()
        if kind not in {"endorsement", "volunteer", "yardsign"}:
            return jsonify({"ok": False, "error": "Invalid submission type."}), 400

        full_name = (data.get("full_name") or "").strip()
        email = (data.get("email") or "").strip()
        phone = (data.get("phone") or "").strip()
        zip_code = (data.get("zip") or "").strip()
        organization = (data.get("organization") or "").strip()
        role_title = (data.get("role_title") or "").strip()
        address = (data.get("address") or "").strip()
        city = (data.get("city") or "").strip()
        state = (data.get("state") or "").strip()
        message = (data.get("message") or "").strip()
        opt_in_updates = 1 if data.get("opt_in_updates") else 0

        # Required fields
        if not full_name or not message:
            return jsonify({"ok": False, "error": "Missing required fields."}), 400

        db_insert_submission(
            kind=kind,
            full_name=full_name,
            email=email,
            phone=phone,
            zip_code=zip_code,
            organization=organization,
            role_title=role_title,
            address=address,
            city=city,
            state=state,
            message=message,
            opt_in_updates=opt_in_updates,
        )
        return jsonify({"ok": True})

    # Health endpoint (useful for hosting)
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}

    # ==========================================
    # Staff / Admin
    # ==========================================
    @app.get("/admin")
    def admin_entry():
        """
        Staff tab entry point. If logged in, show dashboard.
        Otherwise show the staff login screen.
        """
        if session.get("is_admin"):
            return redirect(url_for("admin_dashboard"))
        return render_template("admin/login.html", title="Staff Login")

    @app.post("/admin/login")
    def admin_login():
        csrf_check()
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            flash("Logged in.", "ok")
            return redirect(url_for("admin_dashboard"))

        flash("Invalid staff credentials.", "error")
        return redirect(url_for("admin_entry"))

    @app.post("/admin/logout")
    def admin_logout():
        csrf_check()
        session.clear()
        flash("Logged out.", "ok")
        return redirect(url_for("home"))

    @app.get("/admin/dashboard")
    @admin_required
    def admin_dashboard():
        counts = db_count_submissions()
        latest = db_latest_submissions(limit=10)
        insights = build_insights(sample_size=75)
        return render_template("admin/dashboard.html", counts=counts, latest=latest, insights=insights, title="Staff Dashboard")

    @app.get("/admin/donations")
    @admin_required
    def admin_donations():
        donations = db_list_donations(limit=200)
        total_raised = sum(d["amount"] for d in donations)
        donation_count = len(donations)
        progress_pct = int(min(100, round((total_raised / DONATION_GOAL) * 100))) if DONATION_GOAL > 0 else 0
        remaining = max(0.0, DONATION_GOAL - total_raised)
        return render_template(
            "admin/donations.html",
            donations=donations,
            total_raised=total_raised,
            donation_count=donation_count,
            goal_amount=DONATION_GOAL,
            progress_pct=progress_pct,
            remaining=remaining,
            title="Donation Tracker",
        )

    @app.post("/admin/donations/add")
    @admin_required
    def admin_donations_add():
        csrf_check()
        donor_name = (request.form.get("donor_name") or "").strip()
        source = (request.form.get("source") or "").strip()
        amount_raw = (request.form.get("amount") or "").strip()

        if not re.match(r"^\\d+(\\.\\d{1,2})?$", amount_raw):
            flash("Amount must be a number like 50 or 50.00.", "error")
            return redirect(url_for("admin_donations"))

        amount = float(amount_raw)
        db_insert_donation(donor_name=donor_name, amount=amount, source=source)
        flash("Donation added.", "ok")
        return redirect(url_for("admin_donations"))

    @app.post("/admin/donations/delete")
    @admin_required
    def admin_donations_delete():
        csrf_check()
        donation_id = (request.form.get("id") or "").strip()
        if not donation_id.isdigit():
            abort(400, description="Invalid donation id.")
        db_delete_donation(int(donation_id))
        flash("Donation deleted.", "ok")
        return redirect(url_for("admin_donations"))

    @app.get("/admin/events")
    @admin_required
    def admin_events():
        events = db_list_events(limit=500)
        return render_template("admin/events.html", events=events, title="Manage Events")

    @app.post("/admin/events/add")
    @admin_required
    def admin_events_add():
        csrf_check()
        title = (request.form.get("title") or "").strip()
        start_date = (request.form.get("start_date") or "").strip()
        location = (request.form.get("location") or "").strip()
        audience = (request.form.get("audience") or "").strip()
        description = (request.form.get("description") or "").strip()

        try:
            date.fromisoformat(start_date)
        except Exception:
            flash("Date must be in YYYY-MM-DD format.", "error")
            return redirect(url_for("admin_events"))

        if not title:
            flash("Title is required.", "error")
            return redirect(url_for("admin_events"))

        db_insert_event(title=title, start_date=start_date, location=location, audience=audience, description=description)
        flash("Event added.", "ok")
        return redirect(url_for("admin_events"))

    @app.post("/admin/events/delete")
    @admin_required
    def admin_events_delete():
        csrf_check()
        event_id = (request.form.get("id") or "").strip()
        if not event_id.isdigit():
            abort(400, description="Invalid event id.")
        db_delete_event(int(event_id))
        flash("Event deleted.", "ok")
        return redirect(url_for("admin_events"))

    @app.get("/admin/snippets")
    @admin_required
    def admin_snippets():
        return render_template("admin/snippets.html", snippet="", title="HTML Snippets")

    @app.post("/admin/snippets/build")
    @admin_required
    def admin_snippets_build():
        csrf_check()
        title = (request.form.get("title") or "").strip()
        label = (request.form.get("label") or "").strip()
        body = (request.form.get("body") or "").strip()
        button_text = (request.form.get("button_text") or "").strip()
        button_href = (request.form.get("button_href") or "").strip()

        snippet = build_snippet(title=title, label=label, body=body, button_text=button_text, button_href=button_href)
        return render_template("admin/snippets.html", snippet=snippet, title="HTML Snippets")

    # Optional: export endorsements for staff
    @app.get("/api/endorsements")
    @admin_required
    def api_endorsements():
        rows = db_list_submissions(kind="endorsement", limit=200)
        return jsonify(rows)

    return app


# ==========================================
# Helpers
# ==========================================
def load_embedded_block(filename: str) -> str:
    """
    Reads HTML blocks stored in ./uploads for easy editing.
    (Use this for How to Vote / Get Involved blocks.)
    """
    here = os.path.dirname(__file__)
    upload_path = os.path.join(here, "uploads", filename)
    if os.path.exists(upload_path):
        with open(upload_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    return "<div class='small'>Content not found.</div>"


def build_snippet(title: str, label: str, body: str, button_text: str, button_href: str) -> str:
    # Minimal allowlist: escape HTML special chars
    def esc(s: str) -> str:
        return (
            s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
        )

    title_e = esc(title)
    label_e = esc(label)
    body_e = esc(body).replace("\\n", "<br/>")
    btn = ""
    if button_text and button_href:
        btn = (
            f'<div style="margin-top:10px;">'
            f'<a class="btn" href="{esc(button_href)}" target="_blank" rel="noopener">{esc(button_text)}</a>'
            f'</div>'
        )

    return f"""
<div class="card">
  <div class="k">{label_e}</div>
  <h2 style="margin:6px 0 0 0;">{title_e}</h2>
  <div style="margin-top:8px;">{body_e}</div>
  {btn}
</div>
""".strip()


# ==========================================
# Database
# ==========================================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("""
          CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            full_name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            zip_code TEXT,
            organization TEXT,
            role_title TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            message TEXT NOT NULL,
            opt_in_updates INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
          )
        """)
        cur.execute("""
          CREATE TABLE IF NOT EXISTS donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            donor_name TEXT,
            amount REAL NOT NULL,
            source TEXT,
            created_at TEXT NOT NULL
          )
        """)
        cur.execute("""
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            start_date TEXT NOT NULL,
            location TEXT,
            audience TEXT,
            description TEXT,
            created_at TEXT NOT NULL
          )
        """)
        conn.commit()


def db_insert_submission(**kwargs):
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with db_connect() as conn:
        conn.execute("""
          INSERT INTO submissions
          (kind, full_name, email, phone, zip_code, organization, role_title, address, city, state, message, opt_in_updates, created_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            kwargs.get("kind", ""),
            kwargs.get("full_name", ""),
            kwargs.get("email", ""),
            kwargs.get("phone", ""),
            kwargs.get("zip_code", ""),
            kwargs.get("organization", ""),
            kwargs.get("role_title", ""),
            kwargs.get("address", ""),
            kwargs.get("city", ""),
            kwargs.get("state", ""),
            kwargs.get("message", ""),
            int(kwargs.get("opt_in_updates", 0)),
            now
        ))
        conn.commit()


def db_list_submissions(kind: str, limit: int = 200) -> List[Dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute("""
          SELECT * FROM submissions WHERE kind=? ORDER BY id DESC LIMIT ?
        """, (kind, limit)).fetchall()
    return [dict(r) for r in rows]


def db_latest_submissions(limit: int = 10):
    with db_connect() as conn:
        rows = conn.execute("""
          SELECT id, kind, full_name, email, message, created_at
          FROM submissions ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    return rows


def db_count_submissions() -> Dict[str, int]:
    with db_connect() as conn:
        rows = conn.execute("""
          SELECT kind, COUNT(*) as c FROM submissions GROUP BY kind
        """).fetchall()
    out: Dict[str, int] = {}
    for r in rows:
        out[r["kind"]] = int(r["c"])
    return out


def db_insert_donation(donor_name: str, amount: float, source: str):
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with db_connect() as conn:
        conn.execute("""
          INSERT INTO donations (donor_name, amount, source, created_at)
          VALUES (?, ?, ?, ?)
        """, (donor_name, amount, source, now))
        conn.commit()


def db_list_donations(limit: int = 200):
    with db_connect() as conn:
        rows = conn.execute("""
          SELECT * FROM donations ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    return rows


def db_delete_donation(donation_id: int):
    with db_connect() as conn:
        conn.execute("DELETE FROM donations WHERE id=?", (donation_id,))
        conn.commit()


def db_insert_event(title: str, start_date: str, location: str, audience: str, description: str):
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with db_connect() as conn:
        conn.execute("""
          INSERT INTO events (title, start_date, location, audience, description, created_at)
          VALUES (?, ?, ?, ?, ?, ?)
        """, (title, start_date, location, audience, description, now))
        conn.commit()


def db_list_events(limit: int = 200):
    today = date.today().isoformat()
    with db_connect() as conn:
        rows = conn.execute("""
          SELECT * FROM events
          WHERE start_date >= ?
          ORDER BY start_date ASC, id ASC
          LIMIT ?
        """, (today, limit)).fetchall()
    return rows


def db_delete_event(event_id: int):
    with db_connect() as conn:
        conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        conn.commit()


def build_insights(sample_size: int = 75):
    """
    Lightweight keyword extraction for staff dashboard:
    - Top terms (excluding stopwords)
    - Top ZIPs
    - Heuristic recommendations
    """
    stop = set("""
        a an the and or but if then else to of in on for with without from by is are was were be been being
        i you we they it this that these those as at into over under about your our their
    """.split())

    with db_connect() as conn:
        rows = conn.execute("""
          SELECT kind, message, zip_code
          FROM submissions
          ORDER BY id DESC
          LIMIT ?
        """, (sample_size,)).fetchall()

    words: List[str] = []
    zips: List[str] = []
    kinds: Dict[str, int] = {}

    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        if r["zip_code"]:
            zips.append(r["zip_code"])
        msg = (r["message"] or "").lower()
        for w in re.findall(r"[a-z0-9']{3,}", msg):
            if w in stop:
                continue
            words.append(w)

    from collections import Counter
    top_terms = Counter(words).most_common(12)
    top_zips = Counter(zips).most_common(8)

    recs: List[str] = []
    if kinds.get("volunteer", 0) > 10:
        recs.append("Strong volunteer interest — consider adding a volunteer FAQ and weekly shifts.")
    if kinds.get("yardsign", 0) > 10:
        recs.append("High yard sign demand — schedule a weekly delivery route and confirm inventory.")
    if kinds.get("endorsement", 0) > 10:
        recs.append("Endorsements are trending — publish periodic endorsement updates to maintain momentum.")
    if not recs:
        recs.append("Add a featured event and a clear donation CTA to increase engagement.")

    return {
        "sample_size": sample_size,
        "top_terms": top_terms,
        "top_zips": top_zips,
        "recommendations": recs,
    }


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=True)
