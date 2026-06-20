#!/usr/bin/env python3
"""
Send Slack DMs to mentors listed in a Google Sheet.
Messages are sent using USER_TOKEN so they appear to come from that user (owner).

Google Sheet columns expected: Mentor Name | Email | Titile (or Title)

Required env vars:
  USER_TOKEN        - xoxp-... user token (messages sent as this user)

Optional env vars:
  GOOGLE_SHEET_ID    - spreadsheet ID (default: hardcoded sheet)
  SHEET_GID          - sheet tab GID (default: 0 = first tab)
  SUMMARY_CHANNEL_ID - Slack channel to post summary
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_SHEET_ID      = "1sXvJ_FS_2Wz--CZin3rcXFCIQaE_-24pvX0NzTYrDVc"
DEFAULT_SHEET_GID     = "0"          # first tab
DEFAULT_SUMMARY_CHANNEL = "C0BBY7CJRMY"  # research-aitalk slack channel


# ── Helpers ───────────────────────────────────────────────────────────────────

def read_sheet_rows(sheet_id: str, gid: str = "0") -> list[dict[str, str]]:
    """
    Download the Google Sheet as CSV (no auth needed if sheet is publicly viewable).
    Returns list of dicts with keys: name, email, title
    """
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    reader = csv.DictReader(io.StringIO(resp.text))

    rows: list[dict[str, str]] = []
    for raw in reader:
        # Normalize column names (strip spaces, lowercase)
        row = {k.strip().lower(): v.strip() for k, v in raw.items() if k}

        # Support both "titile" (typo in sheet) and "title"
        title = row.get("titile") or row.get("title") or ""
        email = row.get("email") or ""
        name  = row.get("mentor name") or ""

        if email:  # skip completely empty rows
            rows.append({"name": name, "email": email, "title": title})

    return rows


def slack_get(token: str, method: str, **params: Any) -> dict[str, Any]:
    resp = requests.get(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    return resp.json()


def slack_post(token: str, method: str, **payload: Any) -> dict[str, Any]:
    resp = requests.post(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=30,
    )
    return resp.json()


def lookup_user_by_email(token: str, email: str) -> str:
    data = slack_get(token, "users.lookupByEmail", email=email)
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "lookup failed"))
    return data["user"]["id"]


def send_dm(user_token: str, user_id: str, message: str) -> None:
    # Open DM channel using user token (appears as DM from that user)
    opened = slack_post(user_token, "conversations.open", users=user_id)
    if not opened.get("ok"):
        raise RuntimeError(opened.get("error", "conversations.open failed"))
    channel_id = opened["channel"]["id"]
    # Send message using user token so it appears from that user
    result = slack_post(
        user_token, "chat.postMessage",
        channel=channel_id,
        text=message,
        as_user=True,
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "chat.postMessage failed"))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    user_token = os.environ.get("USER_TOKEN")
    if not user_token:
        print("❌ USER_TOKEN not set in .env", file=sys.stderr)
        return 1

    sheet_id        = os.environ.get("GOOGLE_SHEET_ID", DEFAULT_SHEET_ID)
    sheet_gid       = os.environ.get("SHEET_GID", DEFAULT_SHEET_GID)
    summary_channel = os.environ.get("SUMMARY_CHANNEL_ID", DEFAULT_SUMMARY_CHANNEL)

    # ── Fetch sheet data ──────────────────────────────────────────────────────
    print("📋 Reading Google Sheet...")
    try:
        mentors = read_sheet_rows(sheet_id, sheet_gid)
    except Exception as exc:
        print(f"❌ Could not read sheet: {exc}", file=sys.stderr)
        print(
            "   Make sure the sheet is set to 'Anyone with the link can view'",
            file=sys.stderr,
        )
        return 1

    if not mentors:
        print("⚠️  No rows found in sheet.")
        return 0

    print(f"✅ Found {len(mentors)} mentor row(s)\n")

    # ── Send DMs ──────────────────────────────────────────────────────────────
    sent: int = 0
    skipped: int = 0
    failed: list[str] = []

    for mentor in mentors:
        name  = mentor["name"]  or mentor["email"]
        email = mentor["email"]
        title = mentor["title"]

        if not title:
            print(f"⏭️  Skipping {name} — no title/message")
            skipped += 1
            continue

        try:
            user_id = lookup_user_by_email(user_token, email)
            send_dm(user_token, user_id, title)
            print(f"✅ Sent to {name} <{email}>: {title[:60]}")
            sent += 1
            time.sleep(1.2)  # stay within Slack rate limits

        except Exception as exc:  # noqa: BLE001
            msg = f"{name} ({email}): {exc}"
            failed.append(msg)
            print(f"❌ Failed: {msg}", file=sys.stderr)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n📊 Done — Sent: {sent} | Skipped: {skipped} | Failed: {len(failed)}")

    summary_text = (
        f"*📢 Weekly Mentor Notifications*\n"
        f"✅ Sent: {sent}\n"
        f"⏭️ Skipped (no title): {skipped}"
    )
    if failed:
        summary_text += "\n❌ Failed:\n• " + "\n• ".join(failed)

    try:
        result = slack_post(
            user_token, "chat.postMessage",
            channel=summary_channel,
            text=summary_text,
        )
        if result.get("ok"):
            print(f"📬 Summary posted to channel {summary_channel}")
        else:
            print(f"⚠️  Could not post summary: {result.get('error')}", file=sys.stderr)
    except Exception as exc:
        print(f"⚠️  Summary post failed: {exc}", file=sys.stderr)

    print(json.dumps({"sent": sent, "skipped": skipped, "failed": failed}, indent=2))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
