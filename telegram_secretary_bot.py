#!/usr/bin/env python3
"""
Telegram Secretary Bot
======================
A standalone personal assistant running inside Telegram.
Features:
  - Reminders with optional due time
  - Daily schedule/organizer
  - Quick-add, list, delete, clear
  - Persistent storage (SQLite)
  - Background scheduler for due-time reminders
  - Long-polling Telegram API (no webhook needed for local dev)

Usage:
  1. Create a bot via @BotFather → get the token.
  2. Set the BOT_TOKEN environment variable or edit it in the config.
  3. python3 telegram_secretary.py
"""

import os
import sys
import json
import sqlite3
import threading
import time
import uuid
import datetime
import re
import logging
import requests
import aiohttp
import asyncio
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Fail fast if token is still the placeholder
if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    sys.stderr.write(
        "❌ FATAL: BOT_TOKEN is not set.\n"
        "   Export it before running:\n"
        "   export BOT_TOKEN='123456789:ABCdefGHIjklMNOpqrsTUVwxyz'\n"
        "   or edit this file.\n"
    )
    sys.exit(1)

# Sanity-check token format (Telegram token pattern: digits:digits)
if not re.match(r"^\d+:[A-Za-z0-9_-]{35}$", BOT_TOKEN):
    sys.stderr.write(
        f"⚠️  WARNING: BOT_TOKEN '{BOT_TOKEN[:12]}...' does not match Telegram's "
        f"token pattern (digits:digits). Polling/webhooks will 404.\n"
    )

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
LOG_FILE = Path(__file__).resolve().parent / "secretary.log"

# Webhook / production config
USE_WEBHOOK = os.getenv("WEBHOOK", "").lower() in ("1", "true", "yes")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8443"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", f"https://your-domain.com:8443/bot{BOT_TOKEN}")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("secretary_bot")

# Directory and database paths
DATA_DIR = Path(__file__).resolve().parent
DB_PATH = DATA_DIR / "secretary.db"

# Webhook / production config
USE_WEBHOOK = os.getenv("WEBHOOK", "").lower() in ("1", "true", "yes")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8443"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", f"https://your-domain.com:8443/bot{BOT_TOKEN}")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ---------------------------------------------------------------------------
# SQLite storage
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Create tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id          TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            due_at      TEXT,
            recurrence  TEXT NOT NULL DEFAULT 'none',   -- none | daily | weekly | monthly
            status      TEXT NOT NULL DEFAULT 'pending' -- pending | sent | done
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_user_due "
        "ON reminders (user_id, due_at, recurrence)"
    )
    conn.commit()
    conn.close()


def add_reminder(
    user_id: int,
    content: str,
    due_at: Optional[str] = None,
    recurrence: str = "none",
) -> str:
    """Insert a new reminder and return its ID. Supports one-time or recurring."""
    rid = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO reminders (id, user_id, content, created_at, due_at, recurrence, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """,
        (rid, user_id, content.strip(), now, due_at, recurrence),
    )
    conn.commit()
    conn.close()
    logger.info("Added reminder %s for user %s: %r (due: %s, recur: %s)",
                rid, user_id, content, due_at, recurrence)
    return rid


def get_reminders(user_id: int, status: str | None = None) -> list[dict]:
    """Fetch reminders for a user, optionally filtered by status."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if status:
        cur.execute(
            "SELECT * FROM reminders WHERE user_id = ? AND status = ? ORDER BY due_at ASC",
            (user_id, status),
        )
    else:
        cur.execute(
            "SELECT * FROM reminders WHERE user_id = ? ORDER BY due_at ASC",
            (user_id,),
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def delete_reminder(reminder_id: str) -> bool:
    """Remove a single reminder."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def clear_all_reminders(user_id: int) -> int:
    """Remove all reminders belonging to a user."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,))
    conn.commit()
    count = cur.rowcount
    conn.close()
    logger.info("Cleared %d reminders for user %s", count, user_id)
    return count


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------
def _api(method: str, payload: dict) -> dict:
    """Make a Telegram Bot API request. Raises on error."""
    resp = requests.post(f"{BASE_URL}/{method}", json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error {data.get('error_code')}: {data.get('description')}")
    return data


def send_message(chat_id: int, text: str, parse_mode: str = "MarkdownV2") -> None:
    """Send a text message to a chat."""
    _api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": parse_mode})


def send_schedule(chat_id: int, entries: list[dict]) -> None:
    """Send a formatted schedule/today-list message."""
    if not entries:
        send_message(chat_id, "📅 Your schedule is empty.")
        return

    lines = ["📅 Today's Schedule:", ""]
    for entry in entries:
        due = entry.get("due_at")
        content = entry.get("content", "")
        if due:
            dt = datetime.datetime.fromisoformat(due)
            rel = dt.strftime("%H:%M")
            lines.append(f"• {content}  ({rel})")
        else:
            lines.append(f"• {content}")
        lines.append("")

    send_message(chat_id, "\n".join(lines).rstrip())


def send_help(chat_id: int) -> None:
    """Send the help / command reference."""
    text = """
🤖 *Telegram Secretary* — commands

*Add a reminder*
  /add <text> [HH:MM]  – add reminder now; optional HH:MM = due time (24h)

*View*
  /list  – all pending reminders
  /now   – due-today reminders
  /schedule  – today's agenda (empty slots if none)

*Manage*
  /delete <id>  – delete a specific reminder (id shown in /list)
  /clear         – remove everything

*Misc*
  /help  – this message

*Examples*
  /add finish report 17:30
  /add buy groceries
  /add call mom 09:15
"""
    send_message(chat_id, text, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Background scheduler (fires due reminders)
# ---------------------------------------------------------------------------
def scheduler_loop() -> None:
    """Poll every 30s: fire due-time reminders, watch for recurring ones."""
    logger.info("Scheduler loop started (30s interval)")
    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            due_cutoff = now.isoformat()

            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 1. One-off due reminders
            cur.execute(
                """SELECT id, user_id, content, due_at
                   FROM reminders
                   WHERE status = 'pending'
                     AND due_at IS NOT NULL
                     AND due_at <= ?
                """,
                (due_cutoff,),
            )
            due_rows = cur.fetchall()

            # 2. Recurring due (not yet fired this cycle)
            cur.execute(
                """SELECT id, user_id, content, due_at
                   FROM reminders
                   WHERE status = 'pending'
                     AND recurrence != 'none'
                     AND due_at IS NOT NULL
                     AND due_at <= ?
                """,
                (due_cutoff,),
            )
            recur_rows = cur.fetchall()

            # 3. Reschedule recurring reminders (next occurrence)
            for row in recur_rows:
                rid, uid, content, due_at = row["id"], row["user_id"], row["content"], row["due_at"]
                recur = get_recurrence_type(row["recurrence"])
                next_due = compute_next_due(due_at, recur)
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute(
                    """UPDATE reminders
                       SET due_at = ?, status = 'pending', id = ?
                       WHERE id = ?""",
                    (next_due, rid, rid),
                )
                conn.commit()
                conn.close()
                try:
                    send_message(uid, f"🔁 *Recurring reminder:* {content} (next: {next_due.split('T')[1]})")
                except Exception as e:
                    logger.error("Failed to send recurring %s to %s: %s", rid, uid, e)

            # 4. Fire one-off reminders (mark as done after firing)
            for row in due_rows:
                rid = row["id"]
                uid = row["user_id"]
                content = row["content"]
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("UPDATE reminders SET status = 'done' WHERE id = ?", (rid,))
                conn.commit()
                conn.close()
                try:
                    send_message(uid, f"⏰ *Reminder:* {content}")
                except Exception as e:
                    logger.error("Failed to send one-off %s to %s: %s", rid, uid, e)

        except Exception as e:
            logger.error("Scheduler error: %s", e)

        time.sleep(30)


def get_recurrence_type(recurrence: str) -> str:
    """Normalize recurrence string to a stable label."""
    return recurrence  # 'daily' | 'weekly' | 'monthly'


def compute_next_due(due_at: str, recurrence: str) -> str:
    """Calculate the next occurrence in UTC ISO-8601 format."""
    dt = datetime.datetime.fromisoformat(due_at)
    now = datetime.datetime.now()

    if recurrence == "daily":
        # Same time, next day
        next_day = now + datetime.timedelta(days=1)
        return next_day.replace(hour=dt.hour, minute=dt.minute,
                                second=0, microsecond=0).isoformat()

    if recurrence == "weekly":
        # Same weekday & time, next week
        weekday = dt.weekday()  # Monday=0
        days_ahead = (weekday - now.weekday()) % 7
        if days_ahead <= 0:
            days_ahead += 7
        next_day = now + datetime.timedelta(days=days_ahead)
        return next_day.replace(hour=dt.hour, minute=dt.minute,
                                second=0, microsecond=0).isoformat()

    if recurrence == "monthly":
        # Same day-of-month, next month
        if dt.month == 12:
            next_month, next_year = 1, dt.year + 1
        else:
            next_month, next_year = dt.month + 1, dt.year
        day = min(dt.day, 28)
        next_dt = datetime.datetime(next_year, next_month, day,
                                    dt.hour, dt.minute, second=0, microsecond=0)
        if next_dt <= now:
            next_dt += datetime.timedelta(days=1)
        return next_dt.isoformat()

    return due_at


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------
def handle_message(chat_id: int, text: str) -> None:
    """Dispatch inbound /command text to the right handler."""
    parts = text.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("/start", "/help"):
        send_help(chat_id)
        return

    if cmd == "/add":
        _cmd_add(chat_id, args)
        return

    if cmd == "/list":
        _cmd_list(chat_id)
        return

    if cmd == "/now":
        _cmd_now(chat_id)
        return

    if cmd == "/schedule":
        _cmd_schedule(chat_id)
        return

    if cmd == "/delete":
        _cmd_delete(chat_id, args)
        return

    if cmd == "/clear":
        _cmd_clear(chat_id)
        return

    # Unknown command
    send_message(chat_id, "⚠️ Unknown command. Type /help for available commands.")


def _cmd_add(chat_id: int, args: list[str]) -> None:
    """Parse /add <text> [HH:MM] and store."""
    if not args:
        send_message(chat_id, "Usage: /add <text> [HH:MM]")
        return

    text = " ".join(args)
    due_str = None

    # Look for trailing HH:MM pattern
    if text.endswith("") or True:
        # Try to extract a time token like 17:30 or 5:30 PM
        import re
        m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?$", text, re.IGNORECASE)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2))
            ampm = m.group(3)
            if ampm:
                if ampm.upper() == "PM" and hh != 12:
                    hh += 12
                if ampm.upper() == "AM" and hh == 12:
                    hh = 0
            now = datetime.datetime.now()
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if due <= now:
                due = due + datetime.timedelta(days=1)
            due_str = due.isoformat()
            # Strip the time from the text
            text = re.sub(r"\s*\d{1,2}:\d{2}\s*(AM|PM)?$", "", text, flags=re.IGNORECASE).strip()

    if not text:
        send_message(chat_id, "⚠️ No reminder text provided.")
        return

    add_reminder(chat_id, text, due_str)
    if due_str:
        send_message(chat_id, f"✅ Reminder added (due {due_str.split('T')[1]}): {text}")
    else:
        send_message(chat_id, f"✅ Reminder added: {text}")


def _cmd_list(chat_id: int) -> None:
    """Show all pending reminders for the user, each with an ID."""
    reminders = get_reminders(chat_id, status="pending")
    if not reminders:
        send_message(chat_id, "📭 You have no pending reminders.")
        return

    lines = ["📋 Your reminders:", ""]
    for r in reminders:
        lines.append(f"`{r['id']}`  {r['content']}")
        if r.get("due_at"):
            dt = datetime.datetime.fromisoformat(r["due_at"])
            lines[-1] += f"  (due {dt.strftime('%H:%M')})"
        lines.append("")
    send_message(chat_id, "\n".join(lines).rstrip())


def _cmd_now(chat_id: int) -> None:
    """Show reminders due today."""
    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%dT%H:%M")
    # Get all pending reminders that are due at or before now (due today)
    # We'll just fetch all pending and filter client-side
    all_rem = get_reminders(chat_id, status="pending")
    due_now = [r for r in all_rem if r.get("due_at") and r["due_at"] <= today_str]

    if not due_now:
        send_message(chat_id, "✅ No reminders due right now.")
        return

    lines = ["🔔 Due now:", ""]
    for r in due_now:
        lines.append(f"`{r['id']}`  {r['content']}")
        if r.get("due_at"):
            dt = datetime.datetime.fromisoformat(r["due_at"])
            lines[-1] += f"  (due {dt.strftime('%H:%M')})"
        lines.append("")
    send_message(chat_id, "\n".join(lines).rstrip())


def _cmd_schedule(chat_id: int) -> None:
    """Show today's full schedule (all pending reminders, grouped by time)."""
    now = datetime.datetime.now()
    # Get all reminders for the user
    all_rem = get_reminders(chat_id, status="pending")
    # Group by date (today's date only)
    today = now.strftime("%Y-%m-%d")
    today_rems = [r for r in all_rem if r.get("due_at") and r["due_at"].startswith(today)]

    send_schedule(chat_id, today_rems)


def _cmd_delete(chat_id: int, args: list[str]) -> None:
    """Delete a specific reminder by ID."""
    if not args:
        send_message(chat_id, "Usage: /delete <reminder_id>")
        return

    rid = args[0]
    deleted = delete_reminder(rid)
    if deleted:
        send_message(chat_id, f"🗑️ Reminder `{rid}` deleted.")
    else:
        send_message(chat_id, f"⚠️ No reminder found with ID `{rid}`.")


def reply_plain_text(chat_id: int, text: str) -> None:
    """Forward a plain-text message through the local phi4-mini:3.8b model."""
    try:
        resp = requests.post(
            "http://localhost:11434/v1/chat/completions",
            json={
                "model": "phi4-mini:3.8b",
                "messages": [
                    {"role": "system", "content": "You are a helpful, concise Telegram assistant. Keep answers short and actionable."},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.2,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        send_message(chat_id, reply or "⚠️ I couldn't generate a reply.")
    except Exception as e:
        logger.error("Plain-text reply failed: %s", e)
        send_message(chat_id, f"⚠️ Could not reach local model: {e}")


def _cmd_clear(chat_id: int) -> None:
    """Clear all user's reminders."""
    count = clear_all_reminders(chat_id)
    send_message(chat_id, f"🧹 Cleared {count} reminder(s).")


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------
def handle_message(chat_id: int, text: str) -> None:
    """Dispatch inbound message text to the right handler."""
    parts = text.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("/start", "/help"):
        send_help(chat_id)
        return

    if cmd == "/add":
        _cmd_add(chat_id, args)
        return

    if cmd == "/list":
        _cmd_list(chat_id)
        return

    if cmd == "/now":
        _cmd_now(chat_id)
        return

    if cmd == "/schedule":
        _cmd_schedule(chat_id)
        return

    if cmd == "/delete":
        _cmd_delete(chat_id, args)
        return

    if cmd == "/clear":
        _cmd_clear(chat_id)
        return

    if cmd == "/status":
        _cmd_status(chat_id)
        return

    # Plain text (non-command) -> ask local model for a reply
    reply_plain_text(chat_id, text)


def _cmd_status(chat_id: int) -> None:
    """Show bot status and active reminder count."""
    count = len(get_reminders(chat_id, status="pending"))
    text = f"🔧 *Bot Status*\n\n"
    text += f"User ID: `{chat_id}`\n"
    text += f"Pending reminders: `{count}`\n"
    text += f"Database: `{DB_PATH.name}`\n"
    if USE_WEBHOOK:
        text += f"Mode: *webhook* (port {WEBHOOK_PORT})\n"
        text += f"Endpoint: `{WEBHOOK_URL}`\n"
    else:
        text += f"Mode: *long-polling*\n"
    send_message(chat_id, text, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Main bot runner (long-polling)
# ---------------------------------------------------------------------------
def run_bot() -> None:
    """Start long-polling the Telegram API and dispatch messages."""
    init_db()

    # Start the background scheduler
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    logger.info("Bot started (token: %s...)", BOT_TOKEN[:8])
    logger.info("Database: %s", DB_PATH)

    offset = 0
    while True:
        try:
            resp = _api(
                "getUpdates",
                {"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
            )
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "").strip()
                if chat_id and text:
                    handle_message(chat_id, text)
        except Exception as e:
            logger.error("Polling error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
