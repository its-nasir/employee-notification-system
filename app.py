#!/usr/bin/env python3
"""
Flask backend for Manager Notification UI.
- Read sheet : public CSV (no auth)
- Write sheet: Google OAuth 2.0 (browser login once, token saved)
- Send DMs   : Slack user token
- Deploy     : Render.com ready (credentials via env vars)
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import secrets
import time
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
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID        = os.environ.get("GOOGLE_SHEET_ID", "1sXvJ_FS_2Wz--CZin3rcXFCIQaE_-24pvX0NzTYrDVc")
SHEET_GID       = os.environ.get("SHEET_GID", "0")
USER_TOKEN      = os.environ.get("USER_TOKEN", "")
SUMMARY_CHANNEL = os.environ.get("SUMMARY_CHANNEL_ID", "C0BBY7CJRMY")
CLIENT_ID       = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET   = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL        = os.environ.get("BASE_URL", "http://127.0.0.1:5000")
REDIRECT_URI    = f"{BASE_URL}/auth/callback"

TOKEN_FILE = Path(__file__).resolve().parent / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


# ── Google client config (built from env vars) ────────────────────────────────

def get_client_config() -> dict:
    """Build OAuth client config from env vars (no JSON file needed in prod)."""
    return {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "project_id": os.environ.get("GOOGLE_PROJECT_ID", ""),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [REDIRECT_URI],
        }
    }


# ── Google Auth helpers ───────────────────────────────────────────────────────

def _token_data() -> dict | None:
    """Read token from file (local) or env var GOOGLE_TOKEN (production)."""
    # Production: token stored in env var
    token_env = os.environ.get("GOOGLE_TOKEN")
    if token_env:
        try:
            return json.loads(token_env)
        except Exception:
            return None
    # Local: token stored in file
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text())
        except Exception:
            return None
    return None


def _save_token(creds_json: str) -> None:
    """Save token to file locally (on Render, use env var instead)."""
    TOKEN_FILE.write_text(creds_json)


def get_google_creds():
    """Return valid Google credentials, or None if not authed."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    data = _token_data()
    if not data:
        return None

    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id") or CLIENT_ID,
        client_secret=data.get("client_secret") or CLIENT_SECRET,
        scopes=data.get("scopes", SCOPES),
    )

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds.to_json())
        except Exception:
            return None

    return creds if creds.valid else None


def get_gspread_sheet():
    """Get gspread worksheet using OAuth credentials."""
    import gspread

    creds = get_google_creds()
    if not creds:
        raise RuntimeError("NOT_AUTHED")

    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).get_worksheet(0)


# ── Sheet read (public CSV) ───────────────────────────────────────────────────

def read_sheet_public() -> list[dict]:
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


# ── OAuth Routes ──────────────────────────────────────────────────────────────

@app.route("/auth/google")
def google_auth():
    """Start Google OAuth flow — simple, no PKCE (avoids session issues on Render)."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        get_client_config(),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/auth/callback")
def google_callback():
    """Handle Google OAuth callback."""
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    from google_auth_oauthlib.flow import Flow

    state = request.args.get("state", "")

    # Debug: log what URLs we're working with
    actual_url = request.url
    print(f"DEBUG callback - request.url: {actual_url}")
    print(f"DEBUG REDIRECT_URI: {REDIRECT_URI}")

    flow = Flow.from_client_config(
        get_client_config(),
        scopes=SCOPES,
        state=state,
        redirect_uri=REDIRECT_URI,
    )

    # Force https if Render (production)
    auth_response = actual_url
    if "onrender.com" in REDIRECT_URI and auth_response.startswith("http://"):
        auth_response = auth_response.replace("http://", "https://", 1)
    elif "localhost" in auth_response:
        auth_response = auth_response.replace("localhost", "127.0.0.1", 1)

    print(f"DEBUG auth_response: {auth_response}")

    flow.fetch_token(authorization_response=auth_response)
    creds = flow.credentials
    _save_token(creds.to_json())
    return redirect("/")


@app.route("/api/auth/status")
def auth_status():
    creds = get_google_creds()
    return jsonify({"authed": creds is not None})


@app.route("/auth/logout")
def google_logout():
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    # Also clear env token hint
    os.environ.pop("GOOGLE_TOKEN", None)
    return redirect("/")


@app.route("/api/token/export")
def export_token():
    """
    Returns current token JSON — copy this value and set as
    GOOGLE_TOKEN env var on Render dashboard to persist login.
    """
    if TOKEN_FILE.exists():
        token_data = TOKEN_FILE.read_text()
        return jsonify({
            "ok": True,
            "instruction": "Copy 'GOOGLE_TOKEN' value below and paste it in Render → Environment → GOOGLE_TOKEN",
            "GOOGLE_TOKEN": token_data,
        })
    env_token = os.environ.get("GOOGLE_TOKEN")
    if env_token:
        return jsonify({
            "ok": True,
            "instruction": "Token is already set via env var",
            "GOOGLE_TOKEN": env_token,
        })
    return jsonify({"ok": False, "error": "Not authenticated yet. Login with Google first."}), 401

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
def add_mentor():
    try:
        data = request.json
        sheet = get_gspread_sheet()
        sheet.append_row([data.get("name", ""), data.get("email", ""), data.get("title", "")])
        return jsonify({"ok": True, "message": "Manager added"})
    except RuntimeError as e:
        if "NOT_AUTHED" in str(e):
            return jsonify({"ok": False, "error": "NOT_AUTHED"}), 401
        return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/mentors/<int:row_id>", methods=["PUT"])
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
    except RuntimeError as e:
        if "NOT_AUTHED" in str(e):
            return jsonify({"ok": False, "error": "NOT_AUTHED"}), 401
        return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/mentors/<int:row_id>", methods=["DELETE"])
def delete_mentor(row_id):
    try:
        sheet = get_gspread_sheet()
        sheet_row = row_id + 1
        sheet.delete_rows(sheet_row)
        return jsonify({"ok": True, "message": "Manager deleted"})
    except RuntimeError as e:
        if "NOT_AUTHED" in str(e):
            return jsonify({"ok": False, "error": "NOT_AUTHED"}), 401
        return jsonify({"ok": False, "error": str(e)}), 500
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
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    app.run(debug=True, port=5000, host="127.0.0.1")
