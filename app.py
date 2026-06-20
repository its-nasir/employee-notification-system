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
from functools import wraps
from pathlib import Path
from typing import Any

import requests
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


if __name__ == "__main__":
    app.run(debug=True, port=5000, host="127.0.0.1")
