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
import random
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
            kind        TEXT NOT NULL DEFAULT 'reminder',  -- reminder | timer | alarm
            status      TEXT NOT NULL DEFAULT 'pending' -- pending | sent | done
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_user_due "
        "ON reminders (user_id, due_at, recurrence)"
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            user_id     INTEGER NOT NULL,
            role        TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            id          INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_user "
        "ON conversations (user_id, id DESC)"
    )
    conn.commit()
    conn.close()


def add_reminder(
    user_id: int,
    content: str,
    due_at: Optional[str] = None,
    recurrence: str = "none",
    kind: str = "reminder",
) -> str:
    """Insert a new reminder and return its ID. Supports one-time or recurring."""
    rid = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO reminders (id, user_id, content, created_at, due_at, recurrence, kind, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (rid, user_id, content.strip(), now, due_at, recurrence, kind),
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
# Chat-memory / conversation history
# ---------------------------------------------------------------------------
def record_conversation(user_id: int, role: str, content: str) -> None:
    """Persist one message (user or assistant) to the user's conversation memory."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO conversations (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (user_id, role, content, now),
    )
    conn.commit()
    conn.close()


def get_conversation_history(user_id: int, limit: int = 6) -> list[tuple[str, str]]:
    """Return the most recent (role, content) pairs for a user, oldest-first."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content FROM conversations WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    items = [(row["role"], row["content"]) for row in rows]
    items.reverse()  # newest limit items -> chronological
    return items


def clear_conversation_memory(user_id: int) -> int:
    """Delete all stored conversation history for a user. Returns the count removed."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
    conn.commit()
    count = cur.rowcount
    conn.close()
    logger.info("Cleared %d conversation-history entries for user %s", count, user_id)
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


# ---------------------------------------------------------------------------
# Response formatting — hierarchical, list-organized, structurally constrained
# Per user preference: bold headers for categories, '-' bullets for items,
# blank line between each section.
# ---------------------------------------------------------------------------
ENVELOPE_START = "═══ RESPONSE ═══"
ENVELOPE_END = "═══ END ═══"


def _section(title: str) -> str:
    """Open a category header: bold markdown bold (**Header**)."""
    return f"**{title}**"


def _item(text: str, level: int = 0) -> str:
    """Emit a bullet list item at a given nesting level.

    Rules: exactly 2 spaces per level, then dash + space.
    """
    indent = "  " * level
    return f"{indent}- {text}"


def _subitem(text: str, level: int = 0) -> str:
    """Emit a sub-detail line under a bullet item.

    Uses nested '-' at deeper indentation.
    """
    indent = "  " * (level + 1)
    return f"{indent}- {text}"


def _blank() -> str:
    """Blank line between sections."""
    return ""


def _response(lines: list[str]) -> list[str]:
    """Wrap a list of body lines in the standard response envelope.

    The envelope is invariant: every bot reply must begin with
    ENVELOPE_START and end with ENVELOPE_END.
    """
    return [ENVELOPE_START] + list(lines) + [ENVELOPE_END]


def send_message(chat_id: int, text: str, parse_mode: str = "MarkdownV2") -> None:
    """Send a text message to a chat."""
    _api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": parse_mode})


def send_help(chat_id: int) -> None:
    """Welcome a user and list what the assistant can do (hierarchical, outlaid)."""
    response = []
    response.append(_section("INTRODUCTION"))
    response.append(_item("Say hello with /start, /help, or just type a plain message.", level=1))
    response.append(_item("No / prefix needed for quick commands — chat naturally.", level=1))
    response.append("")

    response.append(_section("REMINDERS"))
    response.append(_item("Remind me to read email", level=1))
    response.append(_subitem("Natural-language add: “Remind me to read email”"))
    response.append(_item("Remind me in 5 minutes to call mom", level=1))
    response.append(_subitem("Timed reminder: “Remind me in 5 minutes to call mom”"))
    response.append(_item("Use /add <text> [HH:MM] for a scheduled reminder", level=1))
    response.append(_subitem("Example: /add call mom 17:30"))
    response.append("")

    response.append(_section("TIMERS & ALARMS"))
    response.append(_item("Set a 10 minute timer", level=1))
    response.append(_subitem("Duration-based count-down"))
    response.append(_item("Countdown for 5 minutes", level=1))
    response.append(_subitem("Alternative phrasing for timers"))
    response.append(_item("Wake me up at 7am", level=1))
    response.append(_subitem("Alarm with time-of-day"))
    response.append(_item("Set an alarm for 7:30pm", level=1))
    response.append(_subitem("Explicit alarm: “set an alarm for 7:30pm”"))
    response.append("")

    response.append(_section("WEATHER"))
    response.append(_item("How's the weather in Paris?", level=1))
    response.append(_subitem("Natural question with place name"))
    response.append(_item("What's the weather in Tokyo?", level=1))
    response.append(_subitem("Explicit weather query"))
    response.append("")

    response.append(_section("JOKES & FACTS"))
    response.append(_item("Tell me a joke", level=1))
    response.append(_subitem("Ask for a classic joke"))
    response.append(_item("Tell me a fact", level=1))
    response.append(_subitem("Ask for an interesting fact"))
    response.append("")

    response.append(_section("ECHO & CHAT"))
    response.append(_item("Echo hello", level=1))
    response.append(_subitem("Repeat your message back verbatim"))
    response.append(_item("Just talk to me about anything", level=1))
    response.append(_subitem("“How's the weather?” or “Tell me a joke.”"))
    response.append("")

    response.append(_section("SLASH COMMANDS"))
    response.append(_item("Type /help anytime for the full command reference.", level=1))
    response.append("")

    response.append(_section("QUICK REFERENCE"))
    response.append(_item("Type /status to see the bot status and active reminder count.", level=1))
    response.append("")
    send_message(chat_id, "\n".join(_response(response)))


# ---------------------------------------------------------------------------
# Background scheduler (fires due reminders)
# ---------------------------------------------------------------------------
def scheduler_loop() -> None:
    """Poll every 30s: fire due-time reminders, watch for recurring ones.
    Timers are compared in the same local wall-clock time the reminder was
    stored in, so the due_at strings and this cutoff are directly comparable.
    """
    logger.info("Scheduler loop started (30s interval)")
    while True:
        try:
            now = datetime.datetime.now()
            due_cutoff = now.isoformat()

            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 1. One-off due reminders
            cur.execute(
                """SELECT id, user_id, content, due_at, kind
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
                    send_message(uid, "\n".join(_response([
                        _section("RECURRING REMINDER"),
                        _item(f"Recurring reminder: {content} (next: {next_due.split('T')[1]})", level=1),
                    ])))
                except Exception as e:
                    logger.error("Failed to send recurring %s to %s: %s", rid, uid, e)

            # 4. Fire one-off reminders (mark as done after firing)
            for row in due_rows:
                rid = row["id"]
                uid = row["user_id"]
                content = row["content"]
                kind = row["kind"] if row["kind"] in ("reminder", "timer", "alarm") else "reminder"
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("UPDATE reminders SET status = 'done' WHERE id = ?", (rid,))
                conn.commit()
                conn.close()
                try:
                    icon = {"alarm": "🔔", "timer": "⏰", "reminder": "⏰"}.get(kind, "⏰")
                    send_message(uid, "\n".join(_response([
                        _section("REMINDER"),
                        _item(f"{icon} {kind.capitalize()}! {content}", level=1),
                    ])))
                except Exception as e:
                    logger.error("Failed to send one-off %s to %s: %s", rid, uid, e)

        except Exception as e:
            logger.error("Scheduler error: %s", e)

        time.sleep(30)


def get_recurrence_type(recurrence: str) -> str:
    """Normalize recurrence string to a stable label."""
    return recurrence  # 'daily' | 'weekly' | 'monthly'


def compute_next_due(due_at: str, recurrence: str) -> str:
    """Calculate the next occurrence in local ISO-8601 format.

    The result uses the same local wall-clock time the reminder was stored in,
    so it is directly comparable with the due_at values in SQLite.
    """
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





def _cmd_add(chat_id: int, args: list[str]) -> None:
    """Parse /add <text> [HH:MM] and store."""
    if not args:
        send_message(chat_id, "\n".join(_response([
            _section("ADD REMINDER"),
            _item("Usage: /add <text> [HH:MM]", level=1),
        ])))
        return

    text = " ".join(args)
    due_str = None

    # Look for a trailing HH:MM pattern (e.g. 17:30 or 5:30 PM)
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
        send_message(chat_id, "\n".join(_response([
            _section("ADD REMINDER"),
            _item("⚠️ No reminder text provided.", level=1),
        ])))
        return

    add_reminder(chat_id, text, due_str)
    if due_str:
        send_message(chat_id, "\n".join(_response([
            _section("ADD REMINDER"),
            _item(f"✅ Reminder added (due {due_str.split('T')[1]}): {text}", level=1),
        ])))
    else:
        send_message(chat_id, "\n".join(_response([
            _section("ADD REMINDER"),
            _item(f"✅ Reminder added: {text}", level=1),
        ])))


def _cmd_list(chat_id: int) -> None:
    """Show all pending reminders for the user, each with an ID (hierarchical)."""
    reminders = get_reminders(chat_id, status="pending")
    if not reminders:
        send_message(chat_id, "\n".join(_response([
            _section("PENDING REMINDERS"),
            _item("You have no pending reminders.", level=1),
        ])))
        return

    response = [
        _section("PENDING REMINDERS"),
    ]
    for r in reminders:
        response.append(_item(f"`{r['id']}`  {r['content']}", level=1))
        if r.get("due_at"):
            dt = datetime.datetime.fromisoformat(r["due_at"])
            response.append(_item(f"(due {dt.strftime('%H:%M')})", level=1))
    send_message(chat_id, "\n".join(_response(response)))


def _cmd_now(chat_id: int) -> None:
    """Show reminders due today (at or before now)."""
    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    # Get pending reminders that are due today and at or before now.
    # due_at and today_str are both naive local ISO-8601 with seconds, so the
    # string comparison is exact (no seconds-less cutoff truncation).
    today = now.strftime("%Y-%m-%d")
    all_rem = get_reminders(chat_id, status="pending")
    due_now = [
        r for r in all_rem
        if r.get("due_at")
        and r["due_at"] <= today_str
        and r["due_at"].startswith(today)
    ]

    if not due_now:
        send_message(chat_id, "\n".join(_response([
            _section("DUE NOW"),
            _item("No reminders due right now.", level=1),
        ])))
        return

    response = [_section("DUE NOW")]
    for r in due_now:
        response.append(_item(f"`{r['id']}`  {r['content']}", level=1))
        if r.get("due_at"):
            dt = datetime.datetime.fromisoformat(r["due_at"])
            response.append(_item(f"(due {dt.strftime('%H:%M')})", level=1))
    send_message(chat_id, "\n".join(_response(response)))


def _cmd_schedule(chat_id: int) -> None:
    """Show today's full schedule (all pending reminders, grouped by time)."""
    now = datetime.datetime.now()
    # Get all reminders for the user
    all_rem = get_reminders(chat_id, status="pending")
    # Group by date (today's date only)
    today = now.strftime("%Y-%m-%d")
    today_rems = [r for r in all_rem if r.get("due_at") and r["due_at"].startswith(today)]

    if not today_rems:
        send_message(chat_id, "\n".join(_response([
            _section("TODAY'S SCHEDULE"),
            _item("Your schedule is empty.", level=1),
        ])))
        return

    response = [_section("TODAY'S SCHEDULE")]
    for entry in today_rems:
        due = entry.get("due_at")
        content = entry.get("content", "")
        if due:
            dt = datetime.datetime.fromisoformat(due)
            rel = dt.strftime("%H:%M")
            response.append(_item(f"{content}  ({rel})", level=1))
        else:
            response.append(_item(content, level=1))
    send_message(chat_id, "\n".join(_response(response)))


def _cmd_delete(chat_id: int, args: list[str]) -> None:
    """Delete a specific reminder by ID (hierarchical response)."""
    if not args:
        send_message(chat_id, "\n".join(_response([
            _section("DELETE REMINDER"),
            _item("Usage: /delete <reminder_id>", level=1),
        ])))
        return

    rid = args[0]
    deleted = delete_reminder(rid)
    if deleted:
        send_message(chat_id, "\n".join(_response([
            _section("DELETE REMINDER"),
            _item(f"Reminder `{rid}` deleted.", level=1),
        ])))
    else:
        send_message(chat_id, "\n".join(_response([
            _section("DELETE REMINDER"),
            _item(f"No reminder found with ID `{rid}`.", level=1),
        ])))


def reply_plain_text(chat_id: int, text: str, user_id: Optional[int] = None) -> None:
    """Reply to free-form chat messages through the local AI model.

    The reply is generated conversationally and lightly polished before it is
    sent back to Telegram. If the model returns an empty or too-short answer
    it is asked once more; on any persistent failure a friendly fallback is
    sent instead of a raw error.
    """
    # Resolve the user so conversation memory is scoped per Telegram user.
    if user_id is None:
        user_id = chat_id
    record_conversation(user_id, "user", text)

    # Prior turns (oldest-first). Drop the just-recorded current message so the
    # model sees the right context without duplicating it.
    prior = get_conversation_history(user_id, limit=6)
    prior = prior[:-1]

    base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    model = os.getenv("LLM_MODEL", "phi4-mini:3.8b")
    temperature = float(os.getenv("LLM_TEMPERATURE", "0.7"))

    system_prompt = """You are a friendly, conversational Telegram personal assistant.

RESPONSE OUTLINING PROTOCOL (INVARIANT):
Every reply MUST start with "═══ RESPONSE ═══" and end with "═══ END ═══".
Use bold markdown headers (**Header**) for each category.
Under each header, list individual items as bullet points (- item).
Leave a blank line between each section for maximum readability.
Keep bullets concise — one line each where possible.
If the user asks for a list or bullets, expand every item into its own line
under the proper section, adding sub-detail lines where useful.

Rules:
- Keep it short and match the length of the question.
- Use a normal tone. Add an emoji only if it genuinely fits.
- If you did not understand, ask one short, friendly clarifying question.
- Never write "As an AI...", "I'm sorry", or repeat the user.
"""

    def extract_reply(data):
        return ((data.get("choices") or [{}])[0].get("message", {}).get("content", "") or "").strip()

    def polish(reply):
        if not reply:
            return ""
        # Unwrap common markdown fencing the model may have added
        if len(reply) > 6 and reply.startswith("```") and reply.endswith("```"):
            reply = reply[3:-3].strip()
        elif len(reply) >= 2 and reply.startswith("`") and reply.endswith("`"):
            reply = reply[1:-1].strip()
        # Collapse internal whitespace but preserve line breaks so hierarchy
        # (indentation, numbered sections, sub-details) is not destroyed.
        return "\n".join(" ".join(line.split()) for line in reply.splitlines())

    last_error = None
    for attempt in range(2):
        user_content = text
        if attempt == 1:
            user_content = text + "\n\n(Reply with a single, clear, natural answer.)"
        try:
            resp = requests.post(
                base_url + "/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                    ]
                    + [{"role": role, "content": content} for role, content in prior]
                    + [{"role": "user", "content": user_content}],
                    "temperature": temperature,
                },
                timeout=90,
            )
            resp.raise_for_status()
            reply = polish(extract_reply(resp.json()))
            if reply:
                send_message(chat_id, reply)
                record_conversation(user_id, "assistant", reply)
                return
            last_error = "model returned an empty reply"
        except Exception as e:
            last_error = e
            logger.error("Plain-text reply failed: %s", e)

    send_message(chat_id, "Sorry, I wasn't able to reply just now — could you try rephrasing or ask again in a moment?")


def _cmd_clear(chat_id: int) -> None:
    """Clear all user's reminders (hierarchical response)."""
    count = clear_all_reminders(chat_id)
    send_message(chat_id, "\n".join(_response([
        _section("CLEAR"),
        _item(f"Cleared {count} reminder(s).", level=1),
    ])))


# ---------------------------------------------------------------------------
# Natural-language (no slash) features
#   timers · alarms · weather · jokes · facts · echo · open chat
# ---------------------------------------------------------------------------
JOKES = [
    "Why did the robot go to therapy? Because it had too many issues to debug!",
    "What do you call a fake noodle? An impasta!",
    "Why was the robot cold? It left the oven on!",
    "What do you call a robot that sings? A robo-ho!",
    "Why did the smartphone go to the doctor? It dropped a few pixels!",
    "What do you call a snake that works in construction? A boa constructor!",
    "Why did the coffee file a police report? It got mugged!",
    "What do you call a fish with a bowtie? Sofishtication!",
]

FACTS = [
    "Octopuses have three hearts.",
    "A day on Venus is longer than a year on Venus.",
    "Honey never spoils — archaeologists have found edible honey in ancient tombs.",
    "The Eiffel Tower grows in summer, expanding by about 15 cm in the heat.",
    "Bananas are berries; strawberries aren't.",
    "There are more possible chess games than atoms in the observable universe.",
    "Wombat poop is cube-shaped so it never rolls away.",
    "A group of flamingos is called a flamboyance.",
]

WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Foggy (freezing)",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle (freezing rain)",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain (heavy)",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light rain showers",
    81: "Rain showers",
    82: "Heavy rain showers",
    85: "Light snow showers",
    86: "Snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm (light hail)",
    99: "Thunderstorm (heavy hail)",
}


def _random_choice(items):
    return random.choice(items)


def _is_joke_t(t):
    return any(k in t for k in ("joke", "laugh", "funny", "humor"))


def _is_fact_t(t):
    return any(k in t for k in ("fact", "interesting", "did you know", "trivia"))


def _is_weather_t(t):
    return "weather" in t


def _is_alarm_intent(t):
    return "alarm" in t or "wake me up" in t or "wake up at" in t


def _is_reminder(t):
    return "remind" in t


def _echo_rest(text):
    return re.sub(r"^echo\b\s*", "", text, flags=re.IGNORECASE).strip()


def _add_seconds(dt, secs):
    return dt + datetime.timedelta(seconds=secs)


def _extract_duration(text):
    """Return (total_seconds, after_text) for '10 minutes', '5m', '30s', etc.
    Returns None if no duration is found."""
    t = text.lower()
    patterns = [
        (r"(\d+)\s*(?:h|hr|hrs|hour|hours)(?![a-z])", 3600),
        (r"(\d+)\s*(?:m|min|mins|minute|minutes)(?![a-z])", 60),
        (r"(\d+)\s*(?:s|sec|secs|second|seconds)(?![a-z])", 1),
    ]
    for pat, mult in patterns:
        m = re.search(pat, t)
        if m:
            return int(m.group(1)) * mult, text[m.end():]
    return None


def _extract_alarm_time(text):
    """Return an iso due time for 'at 7:30pm', 'for 7:30pm', 'wake me up at 7am'.
    Returns None if no usable clock time is found."""
    t = text.lower()
    m = re.search(r"(?:at|for|by|upon)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        return None
    if m.group(2) is None and m.group(3) is None:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3)
    if ampm:
        if ampm == "am" and hour == 12:
            hour = 0
        elif ampm == "pm" and hour != 12:
            hour += 12
    now = datetime.datetime.now()
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= now:
        due += datetime.timedelta(days=1)
    return due.isoformat()


def _extract_city(text):
    m = re.search(r"(?:weather\s+(?:in|of|for)\s+)([A-Za-z][A-Za-z\- ]+?)(?=\s|$|[^A-Za-z])", text.lower())
    if not m:
        return None
    return m.group(1).strip()


def _cmd_echo(chat_id, text):
    rest = re.sub(r"^echo\b\s*", "", text, flags=re.IGNORECASE).strip()
    if rest:
        send_message(chat_id, "\n".join(_response([
            _section("ECHO"),
            _item(rest, level=1),
        ])))
    else:
        send_message(chat_id, "\n".join(_response([
            _section("ECHO"),
            _item("Echo? Tell me something to repeat.", level=1),
        ])))


def _floor_time(dt: datetime.datetime) -> datetime.datetime:
    """Truncate seconds/microseconds for a clean reminder due time."""
    return dt.replace(second=0, microsecond=0)


def _extract_reminder_time(text: str) -> Optional[str]:
    """Return an ISO-8601 due datetime for relative-time words, or None.

    Supports day-level words (tomorrow, today, tonight, next week/month/year)
    and numeric units (N minutes/hours/days). Normalizes common misspellings
    of "tomorrow" (e.g. "tommorow") to "tomorrow".
    """
    t = re.sub(r"tommorow|tomarrow|tmmrw", "tomorrow", text, flags=re.IGNORECASE)
    t_lower = t.lower()

    now = datetime.datetime.now()

    # Day-level relative words.
    if re.search(r"\b(?:tmr|day after tomorrow)\b", t_lower):
        return _floor_time(now + datetime.timedelta(days=1)).isoformat()
    if re.search(r"\b(?:tomorrow|day after tomorrow)\b", t_lower):
        return _floor_time(now + datetime.timedelta(days=1)).isoformat()
    if re.search(r"\b(?:today|tonight|this evening)\b", t_lower):
        due = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if due <= now:
            due += datetime.timedelta(days=1)
        return due.isoformat()
    if re.search(r"\b(?:next week)\b", t_lower):
        days_ahead = (7 - now.weekday()) % 7 or 7  # next Monday
        return _floor_time(now + datetime.timedelta(days=days_ahead)).isoformat()
    if re.search(r"\b(?:next month)\b", t_lower):
        if now.month == 12:
            next_month, next_year = 1, now.year + 1
        else:
            next_month, next_year = now.month + 1, now.year
        day = min(now.day, 28)
        due = datetime.datetime(next_year, next_month, day,
                                now.hour, now.minute, second=0, microsecond=0)
        if due <= now:
            due += datetime.timedelta(days=1)
        return due.isoformat()
    if re.search(r"\b(?:next year)\b", t_lower):
        due = datetime.datetime(now.year + 1, now.month, min(now.day, 28),
                                now.hour, now.minute, second=0, microsecond=0)
        if due <= now:
            due += datetime.timedelta(days=1)
        return due.isoformat()

    # Numeric units (minutes / hours / days).
    m = re.search(r"\b(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|seconds?|secs?)\b", t_lower)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        mult = {"minutes": 60, "mins": 60, "hours": 3600, "hrs": 3600,
                "days": 86400, "seconds": 1, "secs": 1}[unit]
        return _floor_time(now + datetime.timedelta(seconds=n * mult)).isoformat()

    return None


def _cmd_reminder(chat_id, text):
    # Normalize misspellings so "tommorow" behaves as "tomorrow".
    t = re.sub(r"tommorow|tomarrow|tmmrw", "tomorrow", text.lower().strip(), flags=re.IGNORECASE)
    due_at = _extract_reminder_time(t)

    # Extract the reminder content by dropping time words and boilerplate.
    content = t
    content = re.sub(r"\b(tomorrow|tmr|today|tonight|this evening|day after tomorrow|next week|next month|next year)\b", " ", content, flags=re.IGNORECASE)
    content = re.sub(r"^(can you|could you|please|please remind me|remember to)\s+", "", content, flags=re.IGNORECASE)
    content = re.sub(r"\s+", " ", content).strip().rstrip(".,!?;:").strip()
    if not content:
        content = "a reminder"

    if due_at is not None:
        add_reminder(chat_id, content, due_at)
        # Natural confirmation for a yes/no question with no explicit item.
        if re.search(r"\b(can you|could you|would you|please)\b", text.lower()) and content == "remind me":
            send_message(chat_id, "\n".join(_response([
                _section("REMINDER"),
                _item("Sure — I'll remind you tomorrow.", level=1),
            ])))
        else:
            send_message(chat_id, "\n".join(_response([
                _section("REMINDER"),
                _item(f"Reminder set: {content}", level=1),
            ])))
    else:
        if not content:
            content = "a reminder"
        send_message(chat_id, "\n".join(_response([
            _section("REMINDER"),
            _item(f"No due time found for '{content}' — tell me when to remind you.", level=1),
        ])))


def _cmd_timer(chat_id, text, secs, note):
    due = _add_seconds(datetime.datetime.now(), secs).isoformat()
    add_reminder(chat_id, f"⏱ Timer ({secs // 60} min)", due, kind="timer")
    send_message(chat_id, "\n".join(_response([
        _section("TIMER"),
        _item(f"Timer set for {secs // 60} minute(s) — I'll let you know when it's up.", level=1),
    ])))


def _cmd_alarm(chat_id, due_iso, text):
    add_reminder(chat_id, f"⏰ Alarm: {text}", due_iso, kind="alarm")
    send_message(chat_id, "\n".join(_response([
        _section("ALARM"),
        _item(f"Alarm set for {_fmt_time(due_iso)} — I'll wake you up.", level=1),
    ])))


def _fmt_time(iso_str):
    try:
        return datetime.datetime.fromisoformat(iso_str).strftime("%H:%M")
    except Exception:
        return iso_str


def _get_city_coords(city):
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("results"):
            return None
        r = data["results"][0]
        return r["latitude"], r["longitude"], r["name"]
    except Exception as e:
        logger.error("Weather geocoding failed for %r: %s", city, e)
        return None


def _weather_desc(code):
    return WEATHER_CODES.get(code, "Unknown")


def _send_weather(chat_id, city):
    coords = _get_city_coords(city)
    if not coords:
        send_message(chat_id, "\n".join(_response([
            _section("WEATHER"),
            _item(f"Sorry, I couldn't find a weather forecast for “{city}.”", level=1),
        ])))
        return
    lat, lon, resolved = coords
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()["current"]
    except Exception as e:
        logger.error("Weather forecast failed for %r: %s", city, e)
        send_message(chat_id, "\n".join(_response([
            _section("WEATHER"),
            _item("Sorry, I couldn't fetch the weather right now.", level=1),
        ])))
        return

    code = data.get("weather_code")
    temp = data.get("temperature_2m")
    feel = data.get("apparent_temperature")
    hum = data.get("relative_humidity_2m")
    prec = data.get("precipitation")

    parts = [f"☀️ {resolved}: {temp}°C"]
    if feel is not None and feel != temp:
        parts.append(f"feels like {feel}°C")
    if hum is not None:
        parts.append(f"humidity {hum}%")
    if prec is not None and prec > 0:
        parts.append(f"precipitation {prec} mm")
    send_message(chat_id, "\n".join(_response([
        _section("WEATHER"),
        _item(" • ".join(parts) + " — " + _weather_desc(code), level=1),
    ])))


def _cmd_fact(chat_id):
    send_message(chat_id, "\n".join(_response([
        _section("FACT"),
        _item(_random_choice(FACTS), level=1),
    ])))


def _cmd_joke(chat_id):
    send_message(chat_id, "\n".join(_response([
        _section("JOKE"),
        _item(_random_choice(JOKES), level=1),
    ])))


def _dispatch_nl(chat_id, text, user_id: Optional[int] = None):
    """Route a free-text (no slash) message to the right feature."""
    t = text.lower().strip()
    if user_id is None:
        user_id = chat_id

    # Liveness check
    if re.match(r"^(ping|pong)\s*$", t):
        send_message(chat_id, "\n".join(_response([
            _section("LIVENESS"),
            _item("pong! 🎯 I'm alive and running — what can I do for you?", level=1),
        ])))
        return

    # Echo
    if re.match(r"^echo\b", t):
        _cmd_echo(chat_id, text)
        return

    # Jokes / facts
    if _is_joke_t(t):
        _cmd_joke(chat_id)
        return
    if _is_fact_t(t):
        _cmd_fact(chat_id)
        return

    # Weather
    if _is_weather_t(t):
        city = _extract_city(text)
        if city:
            _send_weather(chat_id, city)
        else:
            send_message(chat_id, "\n".join(_response([
                _section("WEATHER"),
                _item("I couldn't guess the place. Try “What's the weather in Paris?”", level=1),
            ])))
        return

    # Alarms / wake-ups
    alarm = _extract_alarm_time(text)
    if alarm is not None and _is_alarm_intent(t):
        _cmd_alarm(chat_id, alarm, text)
        return

    # Reminders
    if _is_reminder(t):
        _cmd_reminder(chat_id, text)
        return

    # Timers / countdowns
    dur = _extract_duration(text)
    if dur is not None:
        secs, note = dur
        _cmd_timer(chat_id, text, secs, note)
        return

    # Open chat -> local model
    reply_plain_text(chat_id, text, user_id)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------
def handle_message(chat_id: int, text: str, user_id: Optional[int] = None) -> None:
    """Dispatch inbound message text to the right handler."""
    parts = text.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]

    if user_id is None:
        user_id = chat_id

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

    if cmd == "/memory":
        _cmd_memory(chat_id, args)
        return

    # Plain text (non-command) -> route to the natural-language feature
    _dispatch_nl(chat_id, text, user_id)


def _cmd_status(chat_id: int) -> None:
    """Show bot status and active reminder count."""
    count = len(get_reminders(chat_id, status="pending"))
    text = f"🔧 **Bot Status**\n\n"
    text += f"User ID: `{chat_id}`\n"
    text += f"Pending reminders: `{count}`\n"
    text += f"Database: `{DB_PATH.name}`\n"
    if USE_WEBHOOK:
        text += f"Mode: **webhook** (port {WEBHOOK_PORT})\n"
        text += f"Endpoint: `{WEBHOOK_URL}`\n"
    else:
        text += f"Mode: **long-polling**\n"
    send_message(chat_id, text, parse_mode="MarkdownV2")


def _cmd_memory(chat_id: int, args: Optional[list[str]] = None) -> None:
    """Show or clear the user's stored conversation history (/memory, /memory clear)."""
    if args and args[0] == "clear":
        count = clear_conversation_memory(chat_id)
        send_message(chat_id, "\n".join(_response([
            _section("MEMORY"),
            _item(f"Cleared {count} stored conversation entry(ies).", level=1),
        ])))
        return

    history = get_conversation_history(chat_id, limit=8)
    if not history:
        send_message(chat_id, "\n".join(_response([
            _section("MEMORY"),
            _item("No conversation history stored for you yet.", level=1),
            _item("Messages are saved as you chat; use /memory clear to wipe them.", level=1),
        ])))
        return

    lines = [_section("MEMORY")]
    for role, content in history:
        preview = content[:70].replace("\n", " ")
        lines.append(_item(f"[{role}] {preview}..."))
    lines.append(_item(f"(Showing {len(history)} of your most recent turns.)", level=1))
    send_message(chat_id, "\n".join(_response(lines)))


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
                    from_id = msg.get("from", {}).get("id")
                    handle_message(chat_id, text, user_id=from_id)
        except Exception as e:
            logger.error("Polling error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
