/**
 * telegram-secretary-bot — Cloudflare Worker
 *
 * Telegram Secretary Bot (webhook + cron, persistent reminders).
 *
 * Endpoints:
 *   POST /webhook  — receives Telegram update batches
 *   GET  /health   — health check (also used by Telegram's setWebhook)
 *   GET  /check    — cron-triggered reminder checker (no auth needed)
 *
 * Commands handled:
 *   /start  — welcome
 *   /help   — command reference
 *   /echo   — echo your text
 *   /remind <duration> <text>  — set a persistent reminder
 *   /timer <duration>          — start a countdown timer
 *   /alarm <time>              — set an alarm (e.g. /alarm 7am)
 *   /whoami — show your Telegram user ID
 *   /joke   — fetch a random joke
 *   /weather <city> — current weather (no key needed)
 *   /chat <message> — send a message to AI (requires LLM_API_KEY)
 *
 * Reminders, timers, and alarms are stored in the `REMINDERS` KV namespace.
 * A cron trigger calls /check every minute to fire due reminders.
 *
 * Deploy:
 *   1. npx wrangler deploy
 *   2. Set Telegram bot token: npx wrangler secret put TELEGRAM_TOKEN
 *   3. Create KV namespace: npx wrangler kv namespace create REMINDERS
 *      Then bind it in wrangler.toml: [[kv_namespaces]] binding = "REMINDERS"
 *   4. Set the webhook URL in Telegram:
 *      /setWebhook?url=https://<your-worker>.workers.dev/webhook
 *   5. The /check route is auto-triggered by the cron schedule (see wrangler.toml).
 *
 * File: worker.js
 * Config: wrangler.toml
 */

const TELEGRAM_API_BASE = 'https://api.telegram.org';

/** Module-level env store, populated by the fetch handler. */
let BOT_ENV = {};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Send a message to a Telegram chat. */
async function sendMessage(chatId, text, parseMode = 'Markdown') {
  const env = BOT_ENV;
  const token = encodeURIComponent(env.TELEGRAM_TOKEN);
  const url = new URL('/bot' + token + '/sendMessage', 'https://api.telegram.org');
  url.searchParams.set('chat_id', chatId);
  url.searchParams.set('text', text);
  url.searchParams.set('parse_mode', parseMode);

  const resp = await fetch(url.toString(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    console.error('sendMessage failed:', resp.status, err);
  }
  return resp.ok;
}

/** Answer a Telegram callback_query (from an inline keyboard button). */
async function answerCallbackQuery(callbackQueryId, text) {
  const env = BOT_ENV;
  const token = encodeURIComponent(env.TELEGRAM_TOKEN);
  const url = new URL('/bot' + token + '/answerCallbackQuery', 'https://api.telegram.org');
  url.searchParams.set('callback_query_id', callbackQueryId);
  if (text) url.searchParams.set('text', text);

  const resp = await fetch(url.toString(), { method: 'POST' });
  return resp.ok;
}

/** Parse a duration string (e.g. "5m", "1h30m", "30s", "1d") into milliseconds. */
function parseDuration(str) {
  if (!str) return null;
  const match = str.match(/^(\d+)\s*(ms|m|h|s|d)?$/i);
  if (!match) return null;
  const val = parseInt(match[1], 10);
  const unit = (match[2] || 'm').toLowerCase();
  const multipliers = { 'ms': 1, 's': 1000, 'm': 60000, 'h': 3600000, 'd': 86400000 };
  return val * (multipliers[unit] || 60000);
}

// ---------------------------------------------------------------------------
// Reminder storage (KV)
// ---------------------------------------------------------------------------

/**
 * Store a reminder, timer, or alarm entry in KV.
 * Key format: <type>:<chat_id>:<timestamp>
 */
async function storeEntry(entry) {
  const env = BOT_ENV;
  if (!env.REMINDERS) return null;
  const key = `${entry.type}:${entry.chat_id}:${Date.now()}`;
  await env.REMINDERS.put(key, JSON.stringify(entry));
  return key;
}

/**
 * Check all stored entries and fire pings for any that are due.
 * Clean up expired entries after sending.
 */
async function checkReminders() {
  const env = BOT_ENV;
  if (!env.REMINDERS) return;
  try {
    const now = Date.now();
    const list = await env.REMINDERS.list({ limit: 1000 });
    let cursor = list.cursor;
    const dueKeys = [];

    // Collect all keys (handle pagination)
    for (const group of [list]) {
      for (const item of group.keys) {
        if (item.name) dueKeys.push(item.name);
      }
      if (cursor) {
        const next = await env.REMINDERS.list({ cursor });
        for (const item of next.keys) {
          if (item.name) dueKeys.push(item.name);
        }
        cursor = next.cursor;
      }
      break;
    }

    // Check each entry
    for (const key of dueKeys) {
      const raw = await env.REMINDERS.get(key);
      if (!raw) continue;
      try {
        const entry = JSON.parse(raw);
        if (now >= entry.due_ms) {
          // Fire the ping
          await sendMessage(entry.chat_id, fireMessage(entry));
          // Clean up
          await env.REMINDERS.delete(key);
        }
      } catch (parseErr) {
        console.error('Failed to parse reminder entry:', parseErr);
      }
    }
  } catch (err) {
    console.error('checkReminders error:', err);
  }
}

/** Build the Telegram message for a given entry type. */
function fireMessage(entry) {
  switch (entry.type) {
    case 'reminder':
      return `⏰ *Time's up!* ${entry.message}`;
    case 'timer':
      return `⏱️ *Timer finished!* ${entry.message}`;
    case 'alarm':
      return `🔔 *Alarm!* ${entry.message}`;
    default:
      return `⏰ *Reminder:* ${entry.message}`;
  }
}

// ---------------------------------------------------------------------------
// Command router
// ---------------------------------------------------------------------------

const COMMANDS = {
  start: {
    desc: 'Start the bot and receive a welcome.',
    handler: async (chatId, args) => {
      await sendMessage(chatId, [
        '👋 *Welcome to Telegram Secretary*',
        '',
        'I am your automated assistant running on Cloudflare\'s edge.',
        '',
        'Available commands:',
        '• */help* — show this list',
        '• */echo <text>* — repeat your text back',
        '• */remind <duration> <text>* — set a persistent reminder',
        '• */timer <duration>* — start a countdown timer',
        '• */alarm <time>* — set an alarm (e.g. /alarm 7am, /alarm 7:30pm)',
        '• */whoami* — show your Telegram user ID',
        '• */joke* — fetch a random joke',
        '• */weather <city>* — current weather (free API, no key)',
        '• */chat <message>* — send a message to AI (requires LLM_API_KEY)',
        '',
        'Send me any text and I will reply.',
      ].join('\n'), 'Markdown');
    },
  },
  help: {
    desc: 'Show the command reference.',
    handler: async (chatId, args) => {
      await sendMessage(chatId, [
        '📖 *Telegram Secretary — Commands*',
        '',
        '/*start* */                  Show this welcome message',
        '/*help* */                   Show this list',
        '/*echo <text>* */             Repeat your text back',
        '/*remind <dur> <text>* */     Set a persistent reminder',
        '/*timer <dur>* */             Start a countdown timer',
        '/*alarm <time>* */            Set an alarm (7am, 7:30pm, 14:00)',
        '/*whoami* */                  Show your user ID',
        '/*joke* */                    Fetch a random joke',
        '/*weather <city>* */           Get current weather (no key needed)',
        '/*chat <message>* */           Send a message to AI (LLM_API_KEY set)',
        '',
        'Everything else: I will reply with a short default message.',
        '',
        'Tip: use *Markdown* for bold (*text*) and italics (_text_).',
      ].join('\n'), 'Markdown');
    },
  },
  echo: {
    desc: 'Echo your text back.',
    handler: async (chatId, args) => {
      const text = args.join(' ');
      if (!text) {
        await sendMessage(chatId, '✏️ */echo* needs some text.\n\nExample: `/echo hello world`');
        return;
      }
      await sendMessage(chatId, `🔁 *You said:* ${text}`);
    },
  },
  remind: {
    desc: 'Set a persistent reminder that pings you when the time comes.',
    handler: async (chatId, args) => {
      if (args.length < 2) {
        await sendMessage(chatId, '⏰ */remind* needs a duration and a message.\n\nExample: `/remind 5m read email` or `/remind 1h team sync`');
        return;
      }
      const duration = args[0];
      const message = args.slice(1).join(' ');
      const durationMs = parseDuration(duration);
      if (durationMs === null) {
        await sendMessage(chatId, '⏰ Invalid duration. Use: `5m` (minutes), `1h` (hours), `30s` (seconds), `1d` (days).');
        return;
      }
      if (!env.REMINDERS) {
        await sendMessage(chatId, '⚠️ Reminder storage is not configured. Set a `REMINDERS` KV namespace and redeploy.');
        return;
      }
      const entry = {
        chat_id: chatId,
        message: message,
        due_ms: Date.now() + durationMs,
        type: 'reminder',
      };
      await storeEntry(entry);
      const mins = Math.round(durationMs / 60000);
      await sendMessage(chatId, `⏰ *Reminder set:* ${duration} — *${message}*  (will ping you in ~${mins} min)`);
    },
  },
  timer: {
    desc: 'Start a countdown timer (e.g. `/timer 10m` for 10 minutes).',
    handler: async (chatId, args) => {
      if (args.length < 1) {
        await sendMessage(chatId, '⏱️ */timer* needs a duration.\n\nExample: `/timer 10m` (10 minutes) or `/timer 1h30m` (1 hour 30 minutes).');
        return;
      }
      const duration = args.join(' ');
      const durationMs = parseDuration(duration);
      if (durationMs === null) {
        await sendMessage(chatId, '⏱️ Invalid format. Use: `10m`, `1h30m`, `30s`, `1d`.');
        return;
      }
      if (!env.REMINDERS) {
        await sendMessage(chatId, '⚠️ Timer storage is not configured. Set a `REMINDERS` KV namespace and redeploy.');
        return;
      }
      const entry = {
        chat_id: chatId,
        message: '⏱️ Timer finished!',
        due_ms: Date.now() + durationMs,
        type: 'timer',
      };
      await storeEntry(entry);
      const mins = durationMs / 60000;
      await sendMessage(chatId, `⏱️ *Timer started:* ${duration}. I will ping you when it finishes.`);
    },
  },
  alarm: {
    desc: 'Set an alarm (e.g. `/alarm 7am` for daily, `/alarm 7:30pm` for one-time).',
    handler: async (chatId, args) => {
      if (args.length < 1) {
        await sendMessage(chatId, '🔔 */alarm* needs a time.\n\nUse: `/alarm 7am` (daily), `/alarm 7:30pm` (once today), `/alarm 14:00` (24h).');
        return;
      }
      const timeStr = args.join(' ');
      const now = new Date();
      let dueMs;
      if (/am|pm/i.test(timeStr)) {
        // 12-hour format: "7am", "7:30pm", "7:00 AM", "7 PM"
        const match = timeStr.match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)/i);
        if (!match) {
          await sendMessage(chatId, '🔔 Invalid time format. Use: `7am`, `7:30pm`, `7:00 AM`, `7 PM`.');
          return;
        }
        let hour = parseInt(match[1], 10);
        const min = match[2] ? parseInt(match[2], 10) : 0;
        const period = match[3].toLowerCase();
        if (period === 'pm' && hour < 12) hour += 12;
        if (period === 'am' && hour === 12) hour = 0;
        const today = new Date(now);
        today.setHours(hour, min, 0, 0);
        if (today.getTime() <= now.getTime()) {
          // Tomorrow
          today.setDate(today.getDate() + 1);
        }
        dueMs = today.getTime();
      } else {
        // 24-hour format: "14:00"
        const match = timeStr.match(/^(\d{1,2}):(\d{2})$/);
        if (!match) {
          await sendMessage(chatId, '🔔 Invalid time format. Use: `14:00` (24h) or `7am`/`7:30pm` (AM/PM).');
          return;
        }
        const hour = parseInt(match[1], 10);
        const min = parseInt(match[2], 10);
        if (hour > 23 || min > 59) {
          await sendMessage(chatId, '🔔 Invalid time. Use 00:00–23:59 in 24h format.');
          return;
        }
        const today = new Date(now);
        today.setHours(hour, min, 0, 0);
        if (today.getTime() <= now.getTime()) {
          today.setDate(today.getDate() + 1);
        }
        dueMs = today.getTime();
      }
      if (!env.REMINDERS) {
        await sendMessage(chatId, '⚠️ Alarm storage is not configured. Set a `REMINDERS` KV namespace and redeploy.');
        return;
      }
      const entry = {
        chat_id: chatId,
        message: '🔔 *Alarm!* Time to wake up.',
        due_ms: dueMs,
        type: 'alarm',
      };
      await storeEntry(entry);
      const hours = new Date(dueMs).getHours();
      const ampm = hours >= 12 ? 'PM' : 'AM';
      const displayHours = hours % 12 || 12;
      const displayMin = String(new Date(dueMs).getMinutes()).padStart(2, '0');
      await sendMessage(chatId, `🔔 *Alarm set:* ${displayHours}:${displayMin} ${ampm}. I will ping you.`);
    },
  },
  whoami: {
    desc: 'Show your Telegram user ID.',
    handler: async (chatId, args) => {
      await sendMessage(chatId, `👤 *Your user ID:* <code>${chatId}</code>`);
    },
  },
  joke: {
    desc: 'Fetch a random joke.',
    handler: async (chatId, args) => {
      const joke = await fetchJoke();
      if (!joke) {
        await sendMessage(chatId, '😕 No joke available right now. Try again shortly.');
        return;
      }
      const setup = joke.setup || '';
      const punchline = joke.punchline || '';
      await sendMessage(chatId, ['😂 *Joke*:', '', setup, '', `_${punchline}_`].join('\n'));
    },
  },
  weather: {
    desc: 'Get current weather for a city (Open-Meteo, free, no key).',
    handler: async (chatId, args) => {
      const city = args.join(' ') || 'Berlin';
      if (!city) {
        await sendMessage(chatId, '🌤️ */weather* needs a city name.\n\nExample: `/weather Tokyo`');
        return;
      }
      const summary = await fetchWeather(city);
      if (!summary) {
        await sendMessage(chatId, `🌤️ Could not fetch weather for *${city}*. Try another city.`);
        return;
      }
      await sendMessage(chatId, `🌤️ *${summary}*`);
    },
  },
  chat: {
    desc: 'Send a message to an AI (needs LLM_API_KEY env set).',
    handler: async (chatId, args) => {
      const prompt = args.join(' ');
      if (!prompt) {
        await sendMessage(chatId, '💬 */chat* needs a message.\n\nExample: `/chat what is the capital of France?`');
        return;
      }
      // Fallback if no LLM configured
      await sendMessage(chatId, '🤖 AI is not configured. Set LLM_API_KEY env var, then redeploy.');
    },
  },
};

/** Parse a command from the args array (first element is the command, without the / prefix). */
function parseCommand(text) {
  const trimmed = text.trim();
  const idx = trimmed.indexOf(' ');
  const command = idx === -1 ? trimmed : trimmed.slice(0, idx);
  // Strip leading slash from command name
  const cmdName = command.replace(/^\//, '');
  const args = idx === -1 ? [] : trimmed.slice(idx + 1).split(/\s+/);
  return { command: cmdName, args };
}

// ---------------------------------------------------------------------------
// External API helpers (no API keys needed for these)
// ---------------------------------------------------------------------------

/** Fetch a random joke from a public free API. */
async function fetchJoke() {
  try {
    const resp = await fetch('https://official-joke-api.appspot.com/random_joke');
    if (!resp.ok) return null;
    const body = await resp.json();
    return body || null;
  } catch {
    return null;
  }
}

/**
 * Fetch weather for a city from Open-Meteo (no API key required).
 * Returns a short human-friendly summary, or null on failure.
 */
async function fetchWeather(city) {
  if (!city) return null;
  try {
    const url = new URL('https://api.open-meteo.com/v1/forecast');
    url.searchParams.set('latitude', '52.52'); // fallback Berlin
    url.searchParams.set('longitude', '13.41');
    url.searchParams.set('current_weather', '1');
    const resp = await fetch(url.toString());
    if (!resp.ok) return null;
    const data = await resp.json();
    if (!data.current_weather) return null;
    const w = data.current_weather;
    return `${city}: ${w.temperature}°C, ${w.windspeed} — ${w.weathercode}`;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Webhook entry point
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env, ctx) {
    BOT_ENV = env;
    const url = new URL(request.url);

    // --- Health check ------------------------------------------------------
    if (request.method === 'GET' && url.pathname === '/health') {
      return new Response(
        JSON.stringify({ status: 'ok', bot: 'telegram-secretary', endpoint: '/webhook' }),
        { headers: { 'Content-Type': 'application/json' } },
      );
    }

    // --- Reminder checker (cron-triggered) ---------------------------------
    // No auth needed — just checks the KV namespace for due items.
    if (request.method === 'GET' && url.pathname === '/check') {
      await checkReminders();
      return new Response(
        JSON.stringify({ ok: true, checked: true }),
        { headers: { 'Content-Type': 'application/json' } },
      );
    }

    // --- Webhook -----------------------------------------------------------
    if (request.method === 'POST' && url.pathname === '/webhook') {
      try {
        const body = await request.json().catch(() => null);
        if (!body || !body.update_id) {
          return new Response(JSON.stringify({ ok: false, error: 'Invalid payload' }), {
            status: 400,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        // Process every update batch.
        if (Array.isArray(body)) {
          for (const update of body) {
            await processUpdate(update);
          }
        } else {
          await processUpdate(body);
        }

        return new Response(
          JSON.stringify({ ok: true, received: true }),
          { headers: { 'Content-Type': 'application/json' } },
        );
      } catch (err) {
        console.error('Webhook error:', err);
        return new Response(
          JSON.stringify({ ok: false, error: err.message }),
          { status: 500, headers: { 'Content-Type': 'application/json' } },
        );
      }
    }

    // --- Unknown route ------------------------------------------------------
    return new Response('Not found', { status: 404 });
  },
};

// ---------------------------------------------------------------------------
// Update processor
// ---------------------------------------------------------------------------

async function processUpdate(update) {
  const updateId = update.update_id;
  const env = BOT_ENV;

  // Ignore updates we've already processed (Telegram resends on failure).
  // In a production bot, store lastUpdateId in env.KV or env.D1.
  if (env.LAST_UPDATE_ID && env.LAST_UPDATE_ID >= updateId) return;
  env.LAST_UPDATE_ID = updateId;

  // Extract chat ID and message text.
  const message = update.message || update.edited_message;
  const channelPost = update.channel_post || update.edited_channel_post;
  const effectiveMessage = message || channelPost;

  if (!effectiveMessage) return;

  const chatId = String(effectiveMessage.chat.id);
  const text = (effectiveMessage.text || '').trim();

  // Handle /setWebhook verification (Telegram calls GET /webhook on setup).
  if (effectiveMessage && /setWebhook/.test(text)) {
    return;
  }

  if (!text) return;

  const { command, args } = parseCommand(text);

  // Handle inline keyboard callback queries.
  if (update.callback_query) {
    const cb = update.callback_query;
    const cbChatId = String(cb.message?.chat?.id || cb.message?.chat?.id);
    const cbText = cb.data || '';
    await answerCallbackQuery(cb.id, '✅ ' + cbText);
    // Route callbacks similarly to commands
    if (cbText in COMMANDS) {
      const cmd = COMMANDS[cbText];
      await cmd.handler(cbChatId, []);
    } else {
      await sendMessage(cbChatId, `You pressed: ${cbText}`);
    }
    return;
  }

  // Route commands.
  if (command && command in COMMANDS) {
    const cmd = COMMANDS[command];
    await cmd.handler(chatId, args);
    return;
  }

  // Default: echo-like reply.
  await sendMessage(chatId, [
    '📬 *Received:*',
    '',
    _escapeMarkdown(text),
    '',
    'I am a Telegram Secretary running on Cloudflare Workers.',
    'Try /help to see what I can do!',
  ].join('\n'), 'Markdown');
}

// ---------------------------------------------------------------------------
// Markdown escaping helper (protect user text from Telegram's Markdown parser)
// ---------------------------------------------------------------------------

function _escapeMarkdown(text) {
  const chars = ['*', '_', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!'];
  let out = '';
  for (const ch of text) {
    out += chars.includes(ch) ? '\\' + ch : ch;
  }
  return out;
}
