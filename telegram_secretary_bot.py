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
    # Learned user profile: durable facts the bot extracts from your chat over
    # time (name, reply style, recurring topics). Stored per-user.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS profile (
            user_id     INTEGER NOT NULL,
            key         TEXT NOT NULL,
            value       TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'unknown',   -- learned | manual | default
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        )
        """
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
# Learned user profile (learns as you chat)
# ---------------------------------------------------------------------------
# Persistent, per-user facts the bot extracts over time and injects into every
# prompt so replies feel personal: your name, preferred reply style, and the
# topics you actually chat about (not just the last few turns).
# ---------------------------------------------------------------------------
import re as _re

def get_profile(user_id: int) -> dict[str, dict]:
    """Return the full profile for a user as {key: {value, source, updated_at}}."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT key, value, source, updated_at FROM profile WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    return {row["key"]: {
        "value": row["value"],
        "source": row["source"],
        "updated_at": row["updated_at"],
    } for row in rows}


def set_profile(user_id: int, key: str, value: str, source: str = "manual") -> None:
    """Upsert a single profile fact. source is one of: learned, manual, default."""
    conn = sqlite3.connect(DB_PATH)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO profile (user_id, key, value, source, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, "
        "source = excluded.source, updated_at = excluded.updated_at",
        (user_id, key, value, source, now),
    )
    conn.commit()
    conn.close()


def clear_profile(user_id: int) -> int:
    """Delete all profile facts for a user. Returns the count removed."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM profile WHERE user_id = ?", (user_id,))
    conn.commit()
    count = cur.rowcount
    conn.close()
    logger.info("Cleared %d profile fact(s) for user %s", count, user_id)
    return count


def get_profile_summary(user_id: int) -> str:
    """Return a concise, LLM-ready summary of the learned profile (empty if none)."""
    profile = get_profile(user_id)
    if not profile:
        return ""
    lines = ["Learned profile (adapted over time from your chats):"]
    for key in sorted(profile):
        lines.append(f"- {key}: {profile[key]['value']}")
    return "\n".join(lines)


def extract_profile_facts(user_id: int, history: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Deterministically extract durable profile facts from recent conversation.

    Purely heuristic (no LLM call) so it works instantly and offline.
    Returns a list of (key, value) pairs found in the user's own messages:
      - name:    user's name, if they stated it
      - style:   preferred reply style (bullets, concise, friendly, short)
      - topics:  top recurring topics
    """
    user_text = " ".join(content for role, content in history if role == "user")
    user_text = _re.sub(r"\s+", " ", user_text).strip().lower()

    found = []

    # Name: "my name is X" / "call me X"
    name = None
    m = _re.search(r"my name is (\w+)", user_text)
    if m:
        name = m.group(1)
        found.append(("name", name))
    else:
        m = _re.search(r"call me (\w+)", user_text)
        if m:
            name = m.group(1)
            found.append(("name", name))

    # Reply style
    style_toks = []
    if _re.search(r"bullet", user_text):
        style_toks.append("bullets")
    if _re.search(r"concise|short", user_text):
        style_toks.append("concise")
    if _re.search(r"friendly|casual|chatty", user_text):
        style_toks.append("friendly")
    if style_toks:
        found.append(("style", ", ".join(style_toks)))
    style_words = set(style_toks + ([name] if name else []))

    # Recurring topics: top keywords (excluding stop words and style terms)
    stop = {
        "the", "a", "an", "and", "or", "is", "are", "was", "were", "be", "been",
        "to", "for", "in", "on", "at", "of", "it", "its", "this", "that", "i",
        "me", "my", "you", "your", "he", "she", "they", "we", "what", "when",
        "where", "how", "why", "please", "can", "could", "would", "should",
        "remind", "set", "tell", "give", "show", "now", "tomorrow", "today",
        "also", "just", "really", "very", "about", "from", "name",
    }
    counts: dict[str, int] = {}
    for w in _re.findall(r"[a-z']+", user_text):
        if w in stop or len(w) <= 2 or w in style_words:
            continue
        counts[w] = counts.get(w, 0) + 1
    if counts:
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        found.append(("topics", " ".join(w for w, _ in top[:5])))

    return found

# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------
BOT_USERNAME = None


def _api(method: str, payload: dict, timeout: int = 35) -> dict:
    """Make a Telegram Bot API request. Raises on error."""
    resp = requests.post(f"{BASE_URL}/{method}", json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error {data.get('error_code')}: {data.get('description')}")
    return data


def get_bot_username() -> Optional[str]:
    """Return cached bot username (without @), fetching from Telegram if needed."""
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    try:
        data = _api("getMe", {}, timeout=15)
        BOT_USERNAME = (data.get("result") or {}).get("username")
    except Exception as e:
        logger.error("Failed to fetch bot username via getMe: %s", e)
        BOT_USERNAME = None
    return BOT_USERNAME


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
    """Return response body lines without an envelope wrapper."""
    return list(lines)


def send_message(chat_id: int, text: str, parse_mode: str = "MarkdownV2") -> None:
    """Send a text message to a chat.

    Telegram MarkdownV2 is strict; if parsing fails (400) we retry as plain text
    so replies still get delivered.
    """
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        _api("sendMessage", payload)
    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        if parse_mode and resp is not None and resp.status_code == 400:
            logger.warning("sendMessage rejected with parse_mode=%s; retrying as plain text", parse_mode)
            _api("sendMessage", {"chat_id": chat_id, "text": text})
        else:
            raise


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
def _get_known_chat_ids() -> list[int]:
    """Return chat/user IDs known from reminders, conversations, or profile."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT user_id FROM reminders
        UNION
        SELECT DISTINCT user_id FROM conversations
        UNION
        SELECT DISTINCT user_id FROM profile
        """
    )
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return [int(r) for r in rows if r is not None]


def _build_agenda_text(chat_id: int) -> Optional[str]:
    """Build a daily agenda/check-in message for a chat, or None if nothing useful."""
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    reminders = get_reminders(chat_id, status="pending")
    today_items = [r for r in reminders if r.get("due_at") and r["due_at"].startswith(today)]
    undated = [r for r in reminders if not r.get("due_at")]

    if not reminders:
        return None

    lines = ["**Daily agenda**"]
    if today_items:
        lines.append("")
        lines.append("Today:")
        for r in today_items:
            dt = datetime.datetime.fromisoformat(r["due_at"])
            lines.append(f"- {dt.strftime('%H:%M')} — {r['content']}")
    else:
        lines.append("")
        lines.append("No timed items scheduled for today.")

    if undated:
        lines.append("")
        lines.append("Pending without a time:")
        for r in undated[:5]:
            lines.append(f"- {r['content']}")
        if len(undated) > 5:
            lines.append(f"- ...and {len(undated) - 5} more")

    # Gentle check-in note
    if not today_items and undated:
        lines.append("")
        lines.append("Want me to help you pick a time for any of these?")

    return "\n".join(lines)


def _send_daily_agendas_if_due(now: datetime.datetime) -> None:
    """Send proactive daily agenda/check-in messages at the configured time."""
    enabled = os.getenv("DAILY_AGENDA_ENABLED", "true").lower() in ("1", "true", "yes")
    if not enabled:
        return
    time_str = os.getenv("DAILY_AGENDA_TIME", "08:00").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if not m:
        hh, mm = 8, 0
    else:
        hh, mm = int(m.group(1)), int(m.group(2))
    if now.hour != hh or now.minute != mm:
        return

    today = now.strftime("%Y-%m-%d")
    for chat_id in _get_known_chat_ids():
        try:
            profile = get_profile(chat_id)
            last = (profile.get("last_agenda_date") or {}).get("value")
            if last == today:
                continue
            # Be conservative in groups: only send if there are reminders for the chat
            if chat_id < 0 and not get_reminders(chat_id, status="pending"):
                continue
            agenda = _build_agenda_text(chat_id)
            if not agenda:
                # Still mark as sent to avoid repeated checks today
                set_profile(chat_id, "last_agenda_date", today, source="default")
                continue
            send_message(chat_id, agenda, parse_mode=None)
            set_profile(chat_id, "last_agenda_date", today, source="default")
            logger.info("Sent daily agenda to chat %s", chat_id)
        except Exception as e:
            logger.error("Failed to send daily agenda to %s: %s", chat_id, e)


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

            conn.close()

            # 3. Reschedule recurring reminders (next occurrence)
            for row in recur_rows:
                rid, uid, content, due_at = row["id"], row["user_id"], row["content"], row["due_at"]
                recur = get_recurrence_type(row["recurrence"])
                next_due = compute_next_due(due_at, recur)
                conn2 = sqlite3.connect(DB_PATH)
                cur2 = conn2.cursor()
                cur2.execute(
                    """UPDATE reminders
                       SET due_at = ?, status = 'pending', id = ?
                       WHERE id = ?""",
                    (next_due, rid, rid),
                )
                conn2.commit()
                conn2.close()
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
                conn2 = sqlite3.connect(DB_PATH)
                cur2 = conn2.cursor()
                cur2.execute("UPDATE reminders SET status = 'done' WHERE id = ?", (rid,))
                conn2.commit()
                conn2.close()
                try:
                    icon = {"alarm": "🔔", "timer": "⏰", "reminder": "⏰"}.get(kind, "⏰")
                    send_message(uid, "\n".join(_response([
                        _section("REMINDER"),
                        _item(f"{icon} {kind.capitalize()}! {content}", level=1),
                    ])))
                except Exception as e:
                    logger.error("Failed to send one-off %s to %s: %s", rid, uid, e)

            # 5. Proactive daily agenda/check-ins
            try:
                _send_daily_agendas_if_due(now)
            except Exception as e:
                logger.error("Daily agenda check failed: %s", e)

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


def _format_reminders_text(user_id: int) -> str:
    reminders = get_reminders(user_id, status="pending")
    if not reminders:
        return "You have no pending reminders."
    lines = []
    for r in reminders:
        line = f"- {r['content']}"
        if r.get("due_at"):
            dt = datetime.datetime.fromisoformat(r["due_at"])
            line += f" (due {dt.strftime('%Y-%m-%d %H:%M')})"
        lines.append(line)
    return "\n".join(lines)


def _format_schedule_text(user_id: int) -> str:
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    all_rem = get_reminders(user_id, status="pending")
    today_rems = [r for r in all_rem if r.get("due_at") and r["due_at"].startswith(today)]
    if not today_rems:
        return "Today's schedule is empty."
    lines = []
    for entry in today_rems:
        due = entry.get("due_at")
        content = entry.get("content", "")
        if due:
            dt = datetime.datetime.fromisoformat(due)
            lines.append(f"- {dt.strftime('%H:%M')} — {content}")
        else:
            lines.append(f"- {content}")
    return "\n".join(lines)


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


def _extract_tool_call(text: str):
    """Parse a tool call from model output.

    Expected JSON: {"tool":"name","args":{...}}
    """
    if not text:
        return None
    s = text.strip()
    # Try whole message first, then fenced/embedded JSON object.
    candidates = [s]
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL | re.IGNORECASE)
    if m:
        candidates.insert(0, m.group(1).strip())
    if "{" in s and "}" in s:
        start = s.find("{")
        end = s.rfind("}") + 1
        if start >= 0 and end > start:
            candidates.append(s[start:end])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
            args = obj.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            return obj["tool"], args
    return None


def _execute_assistant_tool(user_id: int, tool: str, args: dict) -> str:
    """Execute a supported assistant tool and return a textual result."""
    try:
        if tool == "list_reminders":
            return _format_reminders_text(user_id)
        if tool == "get_schedule":
            return _format_schedule_text(user_id)
        if tool == "get_current_time":
            return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if tool == "get_weather":
            city = str(args.get("city") or "").strip()
            if not city:
                return "Missing required argument: city"
            return _get_weather_text(city)
        if tool == "add_reminder":
            content = str(args.get("content") or args.get("text") or "").strip()
            when = str(args.get("when") or args.get("time") or "").strip()
            if not content:
                return "Missing required argument: content"
            due_at = _extract_reminder_time(when) if when else None
            if due_at is None and when:
                # Try parsing absolute HH:MM via existing logic by synthesizing text
                due_at = _extract_reminder_time(content + " " + when)
            if due_at is None:
                add_reminder(user_id, content, None)
                return f"Reminder added (no due time parsed): {content}"
            add_reminder(user_id, content, due_at)
            return f"Reminder set for {due_at}: {content}"
        return f"Unknown tool: {tool}"
    except Exception as e:
        logger.error("Tool execution failed (%s): %s", tool, e)
        return f"Tool '{tool}' failed: {e}"


def _extract_llm_reply(data: dict) -> str:
    return ((data.get("choices") or [{}])[0].get("message", {}).get("content", "") or "").strip()


def _polish_llm_reply(reply: str) -> str:
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


def _chat_complete_with_backends(backends, messages, temperature):
    """Return (reply_text, backend_name) or (None, last_error)."""
    last_error = None
    for backend in backends:
        max_attempts = 2
        for attempt in range(max_attempts):
            req_messages = list(messages)
            if attempt == 1 and req_messages and req_messages[-1].get("role") == "user":
                req_messages[-1] = {
                    "role": "user",
                    "content": req_messages[-1]["content"] + "\n\n(Reply with a single, clear, natural answer.)",
                }
            try:
                headers = {"Content-Type": "application/json"}
                headers.update(backend.get("headers") or {})
                if backend.get("api_key"):
                    headers["Authorization"] = f"Bearer {backend['api_key']}"

                resp = requests.post(
                    backend["base_url"] + "/chat/completions",
                    headers=headers,
                    json={
                        "model": backend["model"],
                        "messages": req_messages,
                        "temperature": temperature,
                    },
                    timeout=90,
                )
                response_body = resp.text
                gateway_draining = re.search(r"Gateway draining:\s*not_ready", response_body, re.IGNORECASE)
                retryable_status = resp.status_code in (408, 425, 429) or resp.status_code >= 500
                if gateway_draining or retryable_status:
                    last_error = RuntimeError(
                        f"[{backend['name']}] LLM API error {resp.status_code}: {response_body[:500]}"
                    )
                    if attempt + 1 < max_attempts:
                        retry_after = resp.headers.get("Retry-After")
                        try:
                            delay = min(max(float(retry_after), 0), 30) if retry_after else 2 ** attempt
                        except ValueError:
                            delay = 2 ** attempt
                        logger.warning(
                            "LLM backend '%s' temporarily unavailable; retrying in %.1f seconds (attempt %d/%d)",
                            backend["name"], delay, attempt + 2, max_attempts,
                        )
                        time.sleep(delay)
                        continue
                    logger.error("LLM backend '%s' failed after retries: %s", backend["name"], last_error)
                    break

                if resp.status_code >= 400:
                    last_error = RuntimeError(
                        f"[{backend['name']}] LLM API error {resp.status_code}: {response_body[:500]}"
                    )
                    logger.error("Plain-text reply failed on '%s': %s", backend["name"], last_error)
                    break

                reply = _polish_llm_reply(_extract_llm_reply(resp.json()))
                if reply:
                    return reply, backend["name"]
                last_error = RuntimeError(f"[{backend['name']}] model returned an empty reply")
                logger.error("Plain-text reply failed on '%s': %s", backend["name"], last_error)
                break
            except Exception as e:
                last_error = e
                logger.error("Plain-text reply failed on '%s': %s", backend["name"], e)
                break

        if backend["name"] != backends[-1]["name"]:
            logger.warning("Falling back from '%s' to next LLM backend after error: %s", backend["name"], last_error)
    return None, last_error


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

    # Learn durable profile facts from recent conversation (deterministic,
    # no extra LLM hop). Stored per-user and injected into the prompt below.
    for key, value in extract_profile_facts(user_id, get_conversation_history(user_id, limit=6)):
        set_profile(user_id, key, value, source="learned")

    temperature = float(os.getenv("LLM_TEMPERATURE", "0.7"))

    # LLM backends in priority order:
    # 1) Primary: local Ollama (or any OpenAI-compatible endpoint)
    # 2) Fallback: OpenRouter with a Nous model (used when primary is unavailable/fails)
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    backends = [
        {
            "name": "ollama",
            "base_url": os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
            "model": os.getenv("LLM_MODEL", "phi4-mini:3.8b"),
            "api_key": os.getenv("LLM_API_KEY", "").strip() or None,
            "headers": {},
        },
    ]
    if openrouter_key:
        backends.append(
            {
                "name": "openrouter",
                "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
                "model": os.getenv("OPENROUTER_MODEL", "nousresearch/hermes-4-70b"),
                "api_key": openrouter_key,
                "headers": {
                    "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://telegram-secretary-bot.local"),
                    "X-Title": os.getenv("OPENROUTER_APP_NAME", "Telegram Secretary Bot"),
                },
            }
        )

    system_prompt = """You are a friendly, conversational Telegram personal assistant.

You can help with reminders, schedules, weather, and general chat.
When the user's request requires an action you can perform, you may call a tool by responding with ONLY a JSON object (no extra text):
{"tool":"<name>","args":{...}}

Available tools:
- list_reminders: args {}
- get_schedule: args {}  (today's schedule)
- get_current_time: args {}
- get_weather: args {"city":"<city name>"}
- add_reminder: args {"content":"<what to remind>", "when":"<time like 'in 10 minutes', 'tomorrow', 'at 7pm'>"} 

If no tool is needed, answer the user directly in plain text.
After a tool result is provided, give a clear, helpful final answer to the user (not JSON).

Rules:
- Keep it short and match the length of the question.
- Use a normal tone. Add an emoji only if it genuinely fits.
- If you did not understand, ask one short, friendly clarifying question.
- Never write "As an AI...", "I'm sorry", or repeat the user.
"""

    # Inject the learned profile so the model personalizes replies over time.
    profile_summary = get_profile_summary(user_id)
    if profile_summary:
        system_prompt += "\n\n" + profile_summary

    messages = (
        [{"role": "system", "content": system_prompt}]
        + [{"role": role, "content": content} for role, content in prior]
        + [{"role": "user", "content": text}]
    )

    reply, _backend = _chat_complete_with_backends(backends, messages, temperature)
    if not reply:
        send_message(chat_id, "Sorry, I wasn't able to reply just now — could you try rephrasing or ask again in a moment?")
        return

    # Tool-use loop (simple, model-agnostic JSON protocol)
    for _ in range(2):
        tool_call = _extract_tool_call(reply)
        if not tool_call:
            break
        tool_name, tool_args = tool_call
        tool_result = _execute_assistant_tool(user_id, tool_name, tool_args)
        messages = messages + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"Tool result for {tool_name}: {tool_result}\n\nNow reply to the user with the final answer."},
        ]
        reply, _backend = _chat_complete_with_backends(backends, messages, temperature)
        if not reply:
            send_message(chat_id, "Sorry, I wasn't able to reply just now — could you try rephrasing or ask again in a moment?")
            return

    try:
        send_message(chat_id, reply)
    except Exception as send_err:
        logger.error("Failed to deliver LLM reply: %s", send_err)
        try:
            send_message(chat_id, "I generated a reply but couldn't deliver it just now. Please try again.", parse_mode=None)
        except Exception:
            pass
    else:
        record_conversation(user_id, "assistant", reply)


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

    # Numeric units (minutes / hours / days / seconds).
    m = re.search(r"\b(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|seconds?|secs?)\b", t_lower)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        mult_map = {
            "minute": 60, "minutes": 60, "min": 60, "mins": 60,
            "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
            "day": 86400, "days": 86400,
            "second": 1, "seconds": 1, "sec": 1, "secs": 1,
        }
        mult = mult_map.get(unit)
        if mult is not None:
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


def _get_weather_text(city: str) -> str:
    coords = _get_city_coords(city)
    if not coords:
        return f"Sorry, I couldn't find a weather forecast for “{city}”."
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
        return "Sorry, I couldn't fetch the weather right now."

    code = data.get("weather_code")
    temp = data.get("temperature_2m")
    feel = data.get("apparent_temperature")
    hum = data.get("relative_humidity_2m")
    prec = data.get("precipitation")

    parts = [f"{resolved}: {temp}°C"]
    if feel is not None and feel != temp:
        parts.append(f"feels like {feel}°C")
    if hum is not None:
        parts.append(f"humidity {hum}%")
    if prec is not None and prec > 0:
        parts.append(f"precipitation {prec} mm")
    return " • ".join(parts) + " — " + _weather_desc(code)


def _send_weather(chat_id, city):
    text = _get_weather_text(city)
    send_message(chat_id, "\n".join(_response([
        _section("WEATHER"),
        _item(text, level=1),
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
def _normalize_command_token(token: str) -> str:
    """Normalize '/cmd@BotName' to '/cmd' for matching."""
    cmd = token.lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    return cmd


def _is_group_chat(chat_type: Optional[str], chat_id: int) -> bool:
    if chat_type in ("group", "supergroup"):
        return True
    return chat_id < 0


def should_respond_in_chat(msg: dict, text: str) -> bool:
    """Decide if the bot should respond to a message.

    - Private chats: respond to messages with text.
    - Group/supergroup chats: respond only when explicitly addressed
      (command, @mention, or reply to the bot) to avoid interrupting chatter.
    """
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    chat_type = chat.get("chat_type") or chat.get("type")
    if not _is_group_chat(chat_type, chat_id if isinstance(chat_id, int) else 0):
        return True

    stripped = (text or "").strip()
    if not stripped:
        return False

    # Commands in groups (including /cmd@BotName)
    if stripped.startswith("/"):
        return True

    # Reply to a bot message
    reply_to = msg.get("reply_to_message") or {}
    reply_from = reply_to.get("from") or {}
    if reply_from.get("is_bot"):
        bot_username = (get_bot_username() or "").lower()
        reply_username = (reply_from.get("username") or "").lower()
        if not bot_username or reply_username == bot_username:
            return True

    # @mention in text/entities
    bot_username = (get_bot_username() or "").lower()
    if bot_username and f"@{bot_username}" in stripped.lower():
        return True
    for ent in (msg.get("entities") or []):
        if ent.get("type") == "mention":
            off = ent.get("offset", 0)
            ln = ent.get("length", 0)
            mention = stripped[off:off + ln].lower()
            if bot_username and mention == f"@{bot_username}":
                return True
        if ent.get("type") == "text_mention":
            user = ent.get("user") or {}
            if user.get("is_bot"):
                uname = (user.get("username") or "").lower()
                if bot_username and uname == bot_username:
                    return True

    return False


def handle_message(chat_id: int, text: str, user_id: Optional[int] = None, chat_type: Optional[str] = None) -> None:
    """Dispatch inbound message text to the right handler."""
    parts = text.strip().split()
    cmd = _normalize_command_token(parts[0]) if parts else ""
    args = parts[1:]

    if user_id is None:
        user_id = chat_id

    # In groups, allow commands and direct mentions/replies; plain chatter is ignored
    # before reaching here by run_bot/should_respond_in_chat.

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

    if cmd == "/profile":
        _cmd_profile(chat_id)
        return

    if cmd == "/memorize":
        _cmd_memorize(chat_id, args)
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

    if args and args[0] == "profile":
        if len(args) > 1 and args[1] == "clear":
            n = clear_profile(chat_id)
            send_message(chat_id, "\n".join(_response([
                _section("MEMORY"),
                _item(f"Cleared {n} learned profile fact(s).", level=1),
            ])))
            return
        summary = get_profile_summary(chat_id)
        if not summary:
            send_message(chat_id, "\n".join(_response([
                _section("MEMORY"),
                _item("No learned profile yet.", level=1),
                _item("Chat normally and I'll start learning your preferences.", level=1),
            ])))
        else:
            lines = [_section("MEMORY / PROFILE")]
            for line in summary.splitlines():
                if line.startswith("- "):
                    lines.append(_item(line[2:]))
            send_message(chat_id, "\n".join(_response(lines)))
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


def _cmd_profile(chat_id: int) -> None:
    """Show the learned profile facts (what the bot remembers about you)."""
    summary = get_profile_summary(chat_id)
    if not summary:
        send_message(chat_id, "\n".join(_response([
            _section("PROFILE"),
            _item("Nothing learned yet.", level=1),
            _item("Chat normally and I'll start remembering your preferences.", level=1),
        ])))
        return
    lines = [_section("PROFILE")]
    for line in summary.splitlines():
        if line.startswith("- "):
            lines.append(_item(line[2:]))
    send_message(chat_id, "\n".join(_response(lines)))


def _cmd_memorize(chat_id: int, args: list[str]) -> None:
    """Set or remove a learned fact manually (/memorize key value, /memorize remove key)."""
    if not args:
        send_message(chat_id, "\n".join(_response([
            _section("MEMORIZE"),
            _item("Usage: /memorize <key> <value>", level=1),
            _item("       /memorize remove <key>", level=1),
        ])))
        return

    if args[0].lower() == "remove":
        if len(args) < 2:
            send_message(chat_id, "\n".join(_response([
                _section("MEMORIZE"),
                _item("Usage: /memorize remove <key>", level=1),
            ])))
            return
        key = args[1]
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("DELETE FROM profile WHERE user_id = ? AND key = ?", (chat_id, key))
        conn.commit()
        count = cur.rowcount
        conn.close()
        send_message(chat_id, "\n".join(_response([
            _section("MEMORIZE"),
            _item(f"Removed {key} ({count} fact(s) removed).", level=1),
        ])))
        return

    if len(args) < 2:
        send_message(chat_id, "\n".join(_response([
            _section("MEMORIZE"),
            _item("Usage: /memorize <key> <value>", level=1),
            _item("Example: /memorize nickname Alice", level=1),
        ])))
        return

    key = args[0]
    value = " ".join(args[1:])
    set_profile(chat_id, key, value, source="manual")
    send_message(chat_id, "\n".join(_response([
        _section("MEMORIZE"),
        _item(f"Saved profile fact: **{key}** → {value}", level=1),
    ])))


# ---------------------------------------------------------------------------
# Main bot runner (long-polling)
# ---------------------------------------------------------------------------
def run_bot() -> None:
    """Start long-polling the Telegram API and dispatch messages."""
    init_db()

    # Cache bot identity for group chat mention/reply handling
    get_bot_username()

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
                timeout=45,
            )
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                chat = msg.get("chat", {}) or {}
                chat_id = chat.get("id")
                chat_type = chat.get("type")
                text = (msg.get("text") or "").strip()
                if chat_id and text:
                    if not should_respond_in_chat(msg, text):
                        continue
                    from_id = (msg.get("from") or {}).get("id")
                    handle_message(chat_id, text, user_id=from_id, chat_type=chat_type)
        except Exception as e:
            logger.error("Polling error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
