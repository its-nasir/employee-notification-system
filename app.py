#!/usr/bin/env python3
"""
Flask backend for Mentor Notification UI.
- Read sheet: public CSV (no auth)
- Write sheet: Google OAuth 2.0 (browser login once, token saved)
- Send DMs: Slack user token
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, redirect, request, send_from_directory, session, url_for

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

app = Flask(__name__, static_folder="static")
app.secret_key = os.urandom(24)

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID         = os.environ.get("GOOGLE_SHEET_ID", "1sXvJ_FS_2Wz--CZin3rcXFCIQaE_-24pvX0NzTYrDVc")
SHEET_GID        = os.environ.get("SHEET_GID", "0")
USER_TOKEN       = os.environ.get("USER_TOKEN", "")
SUMMARY_CHANNEL  = os.environ.get("SUMMARY_CHANNEL_ID", "C0BBY7CJRMY")
OAUTH_CLIENT_JSON = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON", "google-oauth-client.json")
TOKEN_FILE       = Path(__file__).resolve().parent / "token.json"

REDIRECT_URI = "http://127.0.0.1:5000/auth/callback"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


# ── Google Auth helpers ───────────────────────────────────────────────────────

def get_google_creds():
    """Return valid Google credentials, or None if not authed yet."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not TOKEN_FILE.exists():
        return None

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        except Exception:
            return None
    return creds if creds and creds.valid else None


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
        name  = row.get("mentor name") or ""
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
    """Start Google OAuth flow with PKCE."""
    import hashlib, base64, secrets

    from google_auth_oauthlib.flow import Flow

    # Generate PKCE code verifier and challenge
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    session["code_verifier"] = code_verifier

    flow = Flow.from_client_secrets_file(
        OAUTH_CLIENT_JSON,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/auth/callback")
def google_callback():
    """Handle Google OAuth callback with PKCE."""
    import os as _os
    _os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        OAUTH_CLIENT_JSON,
        scopes=SCOPES,
        state=session.get("oauth_state"),
        redirect_uri=REDIRECT_URI,
    )

    auth_response = request.url
    if "localhost" in auth_response:
        auth_response = auth_response.replace("localhost", "127.0.0.1", 1)

    # Pass code_verifier for PKCE
    flow.fetch_token(
        authorization_response=auth_response,
        code_verifier=session.get("code_verifier"),
    )
    creds = flow.credentials
    TOKEN_FILE.write_text(creds.to_json())
    return redirect("/")


@app.route("/api/auth/status")
def auth_status():
    creds = get_google_creds()
    return jsonify({"authed": creds is not None})


@app.route("/auth/logout")
def google_logout():
    """Remove saved token — user will need to re-auth for write operations."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    return redirect("/")


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
def add_mentor():
    try:
        data = request.json
        sheet = get_gspread_sheet()
        sheet.append_row([data.get("name", ""), data.get("email", ""), data.get("title", "")])
        return jsonify({"ok": True, "message": "Mentor added"})
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
        return jsonify({"ok": True, "message": "Mentor updated"})
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
        return jsonify({"ok": True, "message": "Mentor deleted"})
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
        summary = f"*📢 Weekly Mentor Notifications*\n✅ Sent: {sent}\n⏭️ Skipped: {skipped}"
        if failed:
            summary += "\n❌ Failed:\n• " + "\n• ".join(failed)
        slack_post("chat.postMessage", channel=SUMMARY_CHANNEL, text=summary, as_user=True)
        log("📬 Summary posted to channel", "info")
    except Exception as exc:
        log(f"⚠️ Summary post failed: {exc}", "warning")

    return jsonify({"ok": True, "sent": sent, "skipped": skipped, "failed": failed, "logs": logs})


if __name__ == "__main__":
    import os as _os
    _os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    app.run(debug=True, port=5000, host="127.0.0.1")
