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
    """Welcome a user and list what the assistant can do (natural language)."""
    lines = [
        "👋 Hey there! Welcome to Telegram Secretary — I'm your friendly assistant.",
        "",
        "No / prefix needed — just chat with me in plain words:",
        "",
        "🗓️ Reminders",
        "  Remind me to read email",
        "  Remind me in 5 minutes to call mom",
        "",
        "⏱️ Timers & alarms",
        "  Set a 10 minute timer",
        "  Countdown for 5 minutes",
        "  Wake me up at 7am",
        "  Set an alarm for 7:30pm",
        "",
        "🌤️ Weather",
        "  What's the weather in Tokyo?",
        "  How's the weather in Paris?",
        "",
        "😂 Jokes & facts",
        "  Tell me a joke",
        "  Tell me a fact",
        "",
        "🔊 Echo",
        "  Echo hello",
        "",
        "💬 Chat",
        "  Just talk to me about anything — “How's the weather?” or “Tell me a joke.”",
        "",
        "Type /help anytime for the full command reference.",
    ]
    send_message(chat_id, "\n".join(lines))


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
                    send_message(uid, f"🔁 *Recurring reminder:* {content} (next: {next_due.split('T')[1]})")
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
                    if kind == "alarm":
                        send_message(uid, f"🔔 *Alarm!* {content}")
                    elif kind == "timer":
                        send_message(uid, f"⏰ *Timer!* {content}")
                    else:
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
        send_message(chat_id, "Usage: /add <text> [HH:MM]")
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
    """Reply to free-form chat messages through the local AI model.

    The reply is generated conversationally and lightly polished before it is
    sent back to Telegram. If the model returns an empty or too-short answer
    it is asked once more; on any persistent failure a friendly fallback is
    sent instead of a raw error.
    """
    base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    model = os.getenv("LLM_MODEL", "phi4-mini:3.8b")
    temperature = float(os.getenv("LLM_TEMPERATURE", "0.7"))

    system_prompt = """You are a friendly, conversational Telegram personal assistant. You answer like a helpful person texting: warm, natural, and concise. Never sound like a robot, a manual, or an AI.

Rules:
- Answer in full, natural sentences. No lists or bullets unless the answer is long or the user specifically asks for them.
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
        return " ".join(reply.split())

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
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": temperature,
                },
                timeout=90,
            )
            resp.raise_for_status()
            reply = polish(extract_reply(resp.json()))
            if reply:
                send_message(chat_id, reply)
                return
            last_error = "model returned an empty reply"
        except Exception as e:
            last_error = e
            logger.error("Plain-text reply failed: %s", e)

    send_message(chat_id, "Sorry, I wasn't able to reply just now — could you try rephrasing or ask again in a moment?")


def _cmd_clear(chat_id: int) -> None:
    """Clear all user's reminders."""
    count = clear_all_reminders(chat_id)
    send_message(chat_id, f"🧹 Cleared {count} reminder(s).")


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
    send_message(chat_id, rest if rest else "Echo? Tell me something to repeat.")


def _cmd_reminder(chat_id, text):
    t = text.lower().strip()
    dur = _extract_duration(t)
    if dur is not None:
        secs, after = dur
        due_at = _add_seconds(datetime.datetime.now(), secs).isoformat()
        content = after.strip()
        content = re.sub(r"^(in|at|for|to)\s+", "", content, flags=re.IGNORECASE).strip()
        content = re.sub(r"^(remind me|please remind me|remember to)\s+", "", content, flags=re.IGNORECASE).strip()
        if not content:
            content = "a reminder"
    else:
        due_at = None
        content = re.sub(r"^(remind me|please remind me|remember to)\s+", "", t, flags=re.IGNORECASE).strip()
        content = re.sub(r"^(to|for)\s+", "", content, flags=re.IGNORECASE).strip()
        if not content:
            content = "a reminder"
    add_reminder(chat_id, content, due_at)
    send_message(chat_id, f"✅ Reminder set: {content}")


def _cmd_timer(chat_id, text, secs, note):
    due = _add_seconds(datetime.datetime.now(), secs).isoformat()
    add_reminder(chat_id, f"⏱ Timer ({secs // 60} min)", due, kind="timer")
    send_message(chat_id, f"🛟 Timer set for {secs // 60} minute(s) — I'll let you know when it's up.")


def _cmd_alarm(chat_id, due_iso, text):
    add_reminder(chat_id, f"⏰ Alarm: {text}", due_iso, kind="alarm")
    send_message(chat_id, f"🔔 Alarm set for {_fmt_time(due_iso)} — I'll wake you up.")


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
        send_message(chat_id, f"Sorry, I couldn't find a weather forecast for “{city}”.")
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
        send_message(chat_id, "Sorry, I couldn't fetch the weather right now.")
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
    send_message(chat_id, " • ".join(parts) + " — " + _weather_desc(code))


def _cmd_fact(chat_id):
    send_message(chat_id, _random_choice(FACTS))


def _cmd_joke(chat_id):
    send_message(chat_id, _random_choice(JOKES))


def _dispatch_nl(chat_id, text):
    """Route a free-text (no slash) message to the right feature."""
    t = text.lower().strip()

    # Liveness check
    if re.match(r"^(ping|pong)\s*$", t):
        send_message(chat_id, "pong! 🎯 I'm alive and running — what can I do for you?")
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
            send_message(chat_id, "I couldn't guess the place. Try “What's the weather in Paris?”")
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
    reply_plain_text(chat_id, text)


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

    # Plain text (non-command) -> route to the natural-language feature
    _dispatch_nl(chat_id, text)


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
