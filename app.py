#!/usr/bin/env python3
"""
Flask backend for Manager Notification UI.
- Read sheet : public CSV (no auth)
- Write sheet: gspread with Google Service Account OR simple password protection
- Send DMs   : Slack user token
- Auth       : Simple password (set ADMIN_PASSWORD in .env)
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from flask import Flask, jsonify, redirect, request, send_from_directory, session

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "fallback-secret-key-change-me")

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID        = os.environ.get("GOOGLE_SHEET_ID", "1sXvJ_FS_2Wz--CZin3rcXFCIQaE_-24pvX0NzTYrDVc")
SHEET_GID       = os.environ.get("SHEET_GID", "0")
USER_TOKEN      = os.environ.get("USER_TOKEN", "")
SUMMARY_CHANNEL = os.environ.get("SUMMARY_CHANNEL_ID", "C0BBY7CJRMY")
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD", "admin123")
TIMEZONE        = os.environ.get("TIMEZONE", "Asia/Kolkata")
SCHEDULES_FILE  = Path(__file__).resolve().parent / "schedules.json"

# ── APScheduler setup ─────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def is_authenticated() -> bool:
    return session.get("authenticated") is True


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            return jsonify({"ok": False, "error": "NOT_AUTHED"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Google Sheet helpers ──────────────────────────────────────────────────────

def read_sheet_public() -> list[dict]:
    """Read sheet via public CSV export."""
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    reader = csv.DictReader(io.StringIO(resp.text))
    rows = []
    for i, raw in enumerate(reader):
        row = {k.strip().lower(): v.strip() for k, v in raw.items() if k}
        title = row.get("titile") or row.get("title") or ""
        email = row.get("email") or ""
        name  = row.get("mentor name") or row.get("manager name") or ""
        if email or name:
            rows.append({"id": i + 1, "name": name, "email": email, "title": title})
    return rows


def get_gspread_sheet():
    """Get gspread worksheet using service account or OAuth token."""
    import gspread

    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds_path:
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
    else:
        # Try token from env (for Render without service account)
        token_data = os.environ.get("GOOGLE_TOKEN")
        if not token_data:
            raise RuntimeError("No Google credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_TOKEN.")
        from google.oauth2.credentials import Credentials
        data = json.loads(token_data)
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
        )

    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).get_worksheet(0)


# ── Slack helpers ─────────────────────────────────────────────────────────────

def slack_get(method: str, **params: Any) -> dict:
    resp = requests.get(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
        params=params, timeout=30,
    )
    return resp.json()


def slack_post(method: str, **payload: Any) -> dict:
    resp = requests.post(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {USER_TOKEN}"},
        json=payload, timeout=30,
    )
    return resp.json()


def lookup_user_by_email(email: str) -> str:
    data = slack_get("users.lookupByEmail", email=email)
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "lookup failed"))
    return data["user"]["id"]


def send_dm(user_id: str, message: str) -> None:
    opened = slack_post("conversations.open", users=user_id)
    if not opened.get("ok"):
        raise RuntimeError(opened.get("error", "conversations.open failed"))
    channel_id = opened["channel"]["id"]
    result = slack_post("chat.postMessage", channel=channel_id, text=message, as_user=True)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "chat.postMessage failed"))


# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json or {}
    if data.get("password") == ADMIN_PASSWORD:
        session["authenticated"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Incorrect password"}), 401


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/status")
def auth_status():
    return jsonify({"authed": is_authenticated()})


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/mentors", methods=["GET"])
def get_mentors():
    try:
        rows = read_sheet_public()
        return jsonify({"ok": True, "mentors": rows})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/mentors", methods=["POST"])
@require_auth
def add_mentor():
    try:
        data = request.json
        sheet = get_gspread_sheet()
        sheet.append_row([data.get("name", ""), data.get("email", ""), data.get("title", "")])
        return jsonify({"ok": True, "message": "Manager added"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/mentors/<int:row_id>", methods=["PUT"])
@require_auth
def update_mentor(row_id):
    try:
        data = request.json
        sheet = get_gspread_sheet()
        sheet_row = row_id + 1
        sheet.update(f"A{sheet_row}:C{sheet_row}", [[
            data.get("name", ""),
            data.get("email", ""),
            data.get("title", ""),
        ]])
        return jsonify({"ok": True, "message": "Manager updated"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/mentors/<int:row_id>", methods=["DELETE"])
@require_auth
def delete_mentor(row_id):
    try:
        sheet = get_gspread_sheet()
        sheet_row = row_id + 1
        sheet.delete_rows(sheet_row)
        return jsonify({"ok": True, "message": "Manager deleted"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/send", methods=["POST"])
def send_notifications():
    if not USER_TOKEN:
        return jsonify({"ok": False, "error": "USER_TOKEN not set"}), 500

    logs = []

    def log(msg: str, status: str = "info"):
        logs.append({"message": msg, "status": status})

    try:
        mentors = read_sheet_public()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not read sheet: {exc}", "logs": []}), 500

    sent = skipped = 0
    failed = []

    for mentor in mentors:
        name  = mentor["name"] or mentor["email"]
        email = mentor["email"]
        title = mentor["title"]

        if not email:
            continue
        if not title:
            log(f"⏭️ Skipped {name} — no message/title", "warning")
            skipped += 1
            continue

        try:
            user_id = lookup_user_by_email(email)
            send_dm(user_id, title)
            log(f"✅ Sent to {name} ({email})", "success")
            sent += 1
            time.sleep(1.0)
        except Exception as exc:
            log(f"❌ Failed: {name} ({email}) — {exc}", "error")
            failed.append(f"{name} ({email}): {exc}")

    try:
        summary = f"*📢 Weekly Manager Notifications*\n✅ Sent: {sent}\n⏭️ Skipped: {skipped}"
        if failed:
            summary += "\n❌ Failed:\n• " + "\n• ".join(failed)
        slack_post("chat.postMessage", channel=SUMMARY_CHANNEL, text=summary, as_user=True)
        log("📬 Summary posted to channel", "info")
    except Exception as exc:
        log(f"⚠️ Summary post failed: {exc}", "warning")

    return jsonify({"ok": True, "sent": sent, "skipped": skipped, "failed": failed, "logs": logs})


# ── Schedule helpers ──────────────────────────────────────────────────────────

def load_schedules() -> list[dict]:
    # Priority: local file → env var SCHEDULES_DATA
    if SCHEDULES_FILE.exists():
        try:
            return json.loads(SCHEDULES_FILE.read_text())
        except Exception:
            pass
    env_data = os.environ.get("SCHEDULES_DATA")
    if env_data:
        try:
            return json.loads(env_data)
        except Exception:
            pass
    return []


def save_schedules(schedules: list[dict]) -> None:
    # Save to local file
    SCHEDULES_FILE.write_text(json.dumps(schedules, indent=2))
    # Also update env var in memory (so reload works after file loss)
    os.environ["SCHEDULES_DATA"] = json.dumps(schedules)
    # Try to update Render env var via API if available
    _sync_to_render(schedules)


def _sync_to_render(schedules: list[dict]) -> None:
    """Sync schedules to Render environment variable so they survive redeploy."""
    render_api_key    = os.environ.get("RENDER_API_KEY")
    render_service_id = os.environ.get("RENDER_SERVICE_ID")
    if not render_api_key or not render_service_id:
        return  # Not configured — skip silently
    try:
        # Use PATCH on specific env var key — does NOT touch other env vars
        requests.put(
            f"https://api.render.com/v1/services/{render_service_id}/env-vars/SCHEDULES_DATA",
            headers={
                "Authorization": f"Bearer {render_api_key}",
                "Content-Type": "application/json",
            },
            json={"value": json.dumps(schedules)},
            timeout=10,
        )
    except Exception as exc:
        print(f"[Scheduler] Render sync failed (non-critical): {exc}")


def run_scheduled_send(schedule_id: str) -> None:
    """Called by APScheduler to send DMs."""
    print(f"[Scheduler] Running schedule: {schedule_id}")
    schedules = load_schedules()

    # Mark as last_run
    for s in schedules:
        if s["id"] == schedule_id:
            s["last_run"] = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
            # If one-time, mark as done
            if s["type"] == "onetime":
                s["status"] = "done"
    save_schedules(schedules)

    # Send notifications (reuse existing logic)
    try:
        mentors = read_sheet_public()
        sent = skipped = 0
        failed = []
        for mentor in mentors:
            name  = mentor["name"] or mentor["email"]
            email = mentor["email"]
            title = mentor["title"]
            if not email:
                continue
            if not title:
                skipped += 1
                continue
            try:
                user_id = lookup_user_by_email(email)
                send_dm(user_id, title)
                sent += 1
                time.sleep(1.0)
            except Exception as exc:
                failed.append(f"{name} ({email}): {exc}")

        summary = f"*📢 Scheduled Manager Notifications*\n✅ Sent: {sent}\n⏭️ Skipped: {skipped}"
        if failed:
            summary += "\n❌ Failed:\n• " + "\n• ".join(failed)
        slack_post("chat.postMessage", channel=SUMMARY_CHANNEL, text=summary, as_user=True)
        print(f"[Scheduler] Done — sent: {sent}, skipped: {skipped}, failed: {len(failed)}")
    except Exception as exc:
        print(f"[Scheduler] Error: {exc}")


def register_schedule(s: dict) -> None:
    """Add a job to APScheduler from a schedule dict."""
    try:
        if s.get("status") == "done":
            return
        sid = s["id"]
        tz  = ZoneInfo(TIMEZONE)

        if s["type"] == "weekly":
            # day_of_week: 0=Monday ... 6=Sunday
            trigger = CronTrigger(
                day_of_week=int(s["day_of_week"]),
                hour=int(s["hour"]),
                minute=int(s["minute"]),
                timezone=tz,
            )
        else:  # onetime
            run_dt = datetime.fromisoformat(s["run_at"])
            if run_dt < datetime.now(tz):
                return  # past — skip
            trigger = DateTrigger(run_date=run_dt, timezone=tz)

        scheduler.add_job(
            run_scheduled_send,
            trigger=trigger,
            id=sid,
            args=[sid],
            replace_existing=True,
        )
        print(f"[Scheduler] Registered job: {sid}")
    except Exception as exc:
        print(f"[Scheduler] Failed to register {s.get('id')}: {exc}")


def reload_all_schedules() -> None:
    """Load all saved schedules into APScheduler."""
    for s in load_schedules():
        register_schedule(s)


# Load schedules on startup
reload_all_schedules()


# ── Schedule API Routes ───────────────────────────────────────────────────────

@app.route("/api/schedule/test", methods=["POST"])
def test_schedule():
    """Manually trigger scheduled send — for testing."""
    run_scheduled_send("manual-test")
    return jsonify({"ok": True, "message": "Test send triggered"})


@app.route("/api/schedules", methods=["GET"])
def get_schedules():
    schedules = load_schedules()
    # Attach next_run from APScheduler
    for s in schedules:
        job = scheduler.get_job(s["id"])
        s["next_run"] = job.next_run_time.isoformat() if job and job.next_run_time else None
    return jsonify({"ok": True, "schedules": schedules})


@app.route("/api/schedules", methods=["POST"])
@require_auth
def add_schedule():
    try:
        data   = request.json
        stype  = data.get("type")  # "weekly" or "onetime"
        import uuid
        sid    = str(uuid.uuid4())[:8]
        tz     = ZoneInfo(TIMEZONE)

        if stype == "weekly":
            day    = int(data["day_of_week"])   # 0=Mon, 6=Sun
            hour   = int(data["hour"])
            minute = int(data["minute"])
            label  = data.get("label", "")
            s = {
                "id": sid, "type": "weekly",
                "day_of_week": day, "hour": hour, "minute": minute,
                "label": label, "status": "active", "last_run": None,
            }
        elif stype == "onetime":
            run_at = data["run_at"]   # ISO format: 2026-06-28T10:00:00
            label  = data.get("label", "")
            # Make timezone-aware
            dt = datetime.fromisoformat(run_at).replace(tzinfo=tz)
            s = {
                "id": sid, "type": "onetime",
                "run_at": dt.isoformat(), "label": label,
                "status": "active", "last_run": None,
            }
        else:
            return jsonify({"ok": False, "error": "Invalid type"}), 400

        schedules = load_schedules()
        schedules.append(s)
        save_schedules(schedules)
        register_schedule(s)

        return jsonify({"ok": True, "schedule": s})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/schedules/<sid>", methods=["DELETE"])
@require_auth
def delete_schedule(sid):
    try:
        schedules = [s for s in load_schedules() if s["id"] != sid]
        save_schedules(schedules)
        try:
            scheduler.remove_job(sid)
        except Exception:
            pass
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000, host="127.0.0.1")
