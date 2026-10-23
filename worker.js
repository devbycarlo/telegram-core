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
    desc: 'Start the bot and receive a warm welcome.',
    handler: async (chatId, args) => {
      await sendMessage(chatId, [
        '👋 *Hey there! Welcome to Telegram Secretary!*',
        '',
        'I\'m your friendly assistant running on Cloudflare\'s edge — think of me as a little robot butler who\'s always ready to help.',
        '',
        'Here\'s what I can do for you:',
        '• *Set reminders* — just say \"remind me to read email\" or \"remind me in 5m to call mom\"',
        '• *Timers* — try \"set a 10 minute timer\" or \"countdown for 5 minutes\"',
        '• *Alarms* — tell me \"wake me up at 7am\" or \"set an alarm for 7:30pm\"',
        '• *Weather* — ask \"what\'s the weather in Tokyo\" or \"how\'s the weather in Paris\"',
        '• *Jokes* — say \"tell me a joke\" or \"something funny\" and I\'ll cheer you up',
        '• *Chat* — talk to me about anything! Just say \"chat with me\" or \"tell me a fact\"',
        '• *Echo* — I can repeat things back: \"echo hello\"',
        '',
        'No need for the / prefix — just chat with me naturally!',
        '',
        'Type /help anytime if you want to see all my commands.',
      ].join('\\n'), 'Markdown');
    },
  },
  help: {
    desc: 'Show the command reference.',
    handler: async (chatId, args) => {
      await sendMessage(chatId, [
        '📖 *Telegram Secretary — Commands*',
        '',
        '/*Slash commands*/',
        '/*start* */            Show this welcome message',
        '/*help* */             Show this list',
        '/*echo <text>* */       Repeat your text back',
        '/*remind <dur> <text>* */ Set a persistent reminder',
        '/*timer <dur>* */        Start a countdown timer',
        '/*alarm <time>* */       Set an alarm (7am, 7:30pm, 14:00)',
        '/*whoami* */             Show your user ID',
        '/*joke* */               Fetch a random joke',
        '/*weather <city>* */      Get current weather (no key needed)',
        '/*chat <message>* */      Send a message to AI (LLM_API_KEY set)',
        '',
        '/*Natural language commands* */',
        '• *Reminders:*  "remind me to read email" → "remind me in 5m to call mom"',
        '• *Timers:*      "set a 10 minute timer" → "countdown for 30 seconds"',
        '• *Alarms:*      "wake me up at 7am" → "set an alarm for 7:30pm"',
        '• *Weather:*     "what\'s the weather in Tokyo" → "how\'s the weather in London"',
        '• *Jokes:*       "tell me a joke" → "something funny"',
        '• *Chat:*        "chat with me" → "ask me anything"',
        '• *Echo:*        "echo hello" → "say goodbye"',
        '• *Help:*        "what can you do" → "show me commands"',
        '',
        'You can use /commands or just chat naturally — either way works!',
        '',
        'Tip: use *Markdown* for bold (*text*) and italics (_text_).',
      ].join('\\n'), 'Markdown');
    },
  },
  echo: {
    desc: 'Echo your text back.',
    handler: async (chatId, args) => {
      const text = args.join(' ');
      if (!text) {
        await sendMessage(chatId, '✏️ *Echo needs some text.* Just say "echo hello" or "say hi" and I will repeat it back.');
        return;
      }
      await sendMessage(chatId, `🔁 *Got it!* You said: *${text}* — I\'ll say it right back.`,
        'Markdown');
    },
  },
  remind: {
    desc: 'Set a persistent reminder that pings you when the time comes.',
    handler: async (chatId, args) => {
      if (args.length < 2) {
        await sendMessage(chatId, '⏰ *Reminder needs a time and a message.*\n\nTry: "remind me in 5m to read email" or "remind me to call mom"');
        return;
      }
      const duration = args[0];
      const message = args.slice(1).join(' ');
      const durationMs = parseDuration(duration);
      if (durationMs === null) {
        await sendMessage(chatId, '⏰ *Invalid time.* Use "5m" for minutes, "1h" for hours, "30s" for seconds, "1d" for days.');
        return;
      }
      if (!BOT_ENV.REMINDERS) {
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
      await sendMessage(chatId, `⏰ *Reminder set!* I\'ll ping you in ~${mins} min with: *${message}*`,
        'Markdown');
    },
  },
  timer: {
    desc: 'Start a countdown timer (e.g. `/timer 10m` for 10 minutes).',
    handler: async (chatId, args) => {
      if (args.length < 1) {
        await sendMessage(chatId, '⏱️ *Timer needs a duration.* Use: "set a 10 minute timer" or "countdown for 5 minutes"');
        return;
      }
      const duration = args.join(' ');
      const durationMs = parseDuration(duration);
      if (durationMs === null) {
        await sendMessage(chatId, '⏱️ *Invalid duration.* Try: "10m", "1h30m", "30s", "1d"');
        return;
      }
      if (!BOT_ENV.REMINDERS) {
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
      await sendMessage(chatId, `⏱️ *Timer started!* I\'ll ping you when ${mins} minutes are up.`,
        'Markdown');
    },
  },
  alarm: {
    desc: 'Set an alarm (e.g. `/alarm 7am` for daily, `/alarm 7:30pm` for one-time).',
    handler: async (chatId, args) => {
      if (args.length < 1) {
        await sendMessage(chatId, '🔔 *Alarm needs a time.* Try: "wake me up at 7am" or "set an alarm for 7:30pm"');
        return;
      }
      const timeStr = args.join(' ');
      const now = new Date();
      let dueMs;
      if (/am|pm/i.test(timeStr)) {
        // 12-hour format: "7am", "7:30pm", "7:00 AM", "7 PM"
        const match = timeStr.match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)/i);
        if (!match) {
          await sendMessage(chatId, '🔔 *Invalid time format.* Use: "7am", "7:30pm", "7:00 AM", "7 PM"');
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
          await sendMessage(chatId, '🔔 *Invalid time.* Use: "14:00" (24h) or "7am"/"7:30pm" (AM/PM).');
          return;
        }
        const hour = parseInt(match[1], 10);
        const min = parseInt(match[2], 10);
        if (hour > 23 || min > 59) {
          await sendMessage(chatId, '🔔 *Invalid time.* Use 00:00–23:59 in 24h format.');
          return;
        }
        const today = new Date(now);
        today.setHours(hour, min, 0, 0);
        if (today.getTime() <= now.getTime()) {
          today.setDate(today.getDate() + 1);
        }
        dueMs = today.getTime();
      }
      if (!BOT_ENV.REMINDERS) {
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
      await sendMessage(chatId, `🔔 *Alarm set!* I will wake you up at *${displayHours}:${displayMin} ${ampm}*.`,
        'Markdown');
    },
  },
  whoami: {
    desc: 'Show your Telegram user ID.',
    handler: async (chatId, args) => {
      await sendMessage(chatId, `👤 *Your user ID:* <code>${chatId}</code> — I can ping you at this number!`);
    },
  },
  joke: {
    desc: 'Fetch a random joke.',
    handler: async (chatId, args) => {
      const joke = await fetchJoke();
      if (!joke) {
        await sendMessage(chatId, '😕 Hmm, I couldn\'t find a joke right now. Try again in a moment!');
        return;
      }
      const setup = joke.setup || '';
      const punchline = joke.punchline || '';
      await sendMessage(chatId, [
        '😂 *Here\'s a joke for you:*',
        '',
        setup,
        '',
        `_${punchline}_`,
      ].join('\\n'));
    },
  },
  weather: {
    desc: 'Get current weather for a city (Open-Meteo, free, no key).',
    handler: async (chatId, args) => {
      const city = args.join(' ') || 'Berlin';
      if (!city) {
        await sendMessage(chatId, '🌤️ *Weather needs a city.* Ask: "what\'s the weather in Tokyo" or "how\'s the weather in London"');
        return;
      }
      const summary = await fetchWeather(city);
      if (!summary) {
        await sendMessage(chatId, `🌤️ Could not fetch weather for *${city}*. Try another city!`);
        return;
      }
      await sendMessage(chatId, `🌤️ *Here\'s the weather in ${city}:* ${summary}`);
    },
  },
  chat: {
    desc: 'Send a message to an AI.',
    handler: async (chatId, args) => {
      const prompt = args.join(' ');
      if (!prompt) {
        await sendMessage(chatId, '💬 *Chat needs a message.* Try: "tell me a fact" or "ask me something"');
        return;
      }
      // Fallback if no LLM configured
      await sendMessage(chatId, '🤖 *AI is not configured yet.* Set LLM_API_KEY to talk to an AI model.');
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

  // Check natural language command (no / prefix)
  if (!cmdName.startsWith('/') && cmdName.length <= 20) {
    const natural = parseNaturalLanguage(trimmed);
    if (natural) return natural;
  }

  return { command: cmdName, args };
}

/**
 * Parse natural language commands (no / prefix).
 * Returns { command, args } or null if no match.
 */
function parseNaturalLanguage(text) {
  const lower = text.toLowerCase();

  // --- Joke ---
  if (/^(tell me|say|make me|please|suggest) (a )?joke|something funny|make me laugh|are you funny/i.test(text)) {
    return { command: 'joke', args: [] };
  }

  // --- Weather: "weather in <city>", "what's the weather in <city>", "how's the weather in <city>" ---
  const weatherMatch = text.match(/^(?:what('|’)?s|what is|how('|’)?s|how is)? (?:the )?weather in (.+)/i);
  if (weatherMatch) {
    const city = weatherMatch[1].trim();
    if (city) return { command: 'weather', args: [city] };
  }

  // --- Echo: "echo <text>", "say <text>", "repeat <text>" ---
  const echoMatch = text.match(/^(?:echo|say|repeat) (.+)/i);
  if (echoMatch) {
    return { command: 'echo', args: [echoMatch[1].trim()] };
  }

  // --- Reminder: "remind me to <message>", "set a reminder to <message>", "remind me in <dur> to <message>" ---
  const reminderMatch = text.match(/^(?:remind me|set (?:a )?reminder)(?:\s+to)?\s+(.+)/i);
  if (reminderMatch) {
    let message = reminderMatch[1].trim();
    // Check for duration prefix: "in 5m", "after 5m", "in 5 minutes"
    const durationMatch = message.match(/^(in|after)\s+(\d+\s*(?:m|ms|s|h|d|minutes?|hours?|seconds?|days?))\s+(.+)/i);
    if (durationMatch) {
      const duration = durationMatch[2];
      const msg = durationMatch[3].trim();
      return { command: 'remind', args: [duration, msg] };
    }
    // No duration: treat the whole message as the reminder text
    return { command: 'remind', args: [message] };
  }

  // --- Timer: "set a timer for <dur>", "start a timer (for)? <dur>", "timer for <dur>" ---
  const timerMatch = text.match(/^(?:set|start) (?:a )?timer(?: for)? (\d+\s*(?:m|ms|s|h|d|minutes?|hours?|seconds?|days?))/i);
  if (timerMatch) {
    const duration = timerMatch[1].trim();
    return { command: 'timer', args: [duration] };
  }
  // Also: "timer for 5m", "countdown for 5m"
  const timerMatch2 = text.match(/^(?:timer|countdown)(?: for)? (\d+\s*(?:m|ms|s|h|d|minutes?|hours?|seconds?|days?))/i);
  if (timerMatch2) {
    const duration = timerMatch2[1].trim();
    return { command: 'timer', args: [duration] };
  }

  // --- Alarm: "set an alarm for <time>", "wake me up at <time>", "alarm at <time>" ---
  const alarmMatch = text.match(/^(?:set (?:an )?alarm|wake me up)(?:\s+(?:at|for))?\s+(.+)/i);
  if (alarmMatch) {
    const time = alarmMatch[1].trim();
    return { command: 'alarm', args: [time] };
  }

  // --- Chat: "chat with me", "ask me something", "tell me <something>" ---
  if (/^chat with me|ask me (about|something)|tell me/i.test(text)) {
    const message = text.replace(/^(?:chat with me|ask me (?:about|something)|tell me)/i, '').trim();
    return { command: 'chat', args: [message] };
  }

  // --- Start: "hi", "hello", "hey" ---
  if (/^(hi|hello|hey|start|hi there|hello there)/i.test(text)) {
    return { command: 'start', args: [] };
  }

  // --- Help: "help", "what can you do", "show me commands" ---
  if (/^(help|what can you do|what do you do|show me commands|how do you work)/i.test(text)) {
    return { command: 'help', args: [] };
  }

  return null;
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
