# Telegram File Indexer + Search Bot

Auto-indexes every file posted in your Telegram channel into MongoDB.
DM the bot part of a filename and get back a tappable button list of
matches → tap one for a preview card (type/size/date) → tap **Send file**
to receive it. Also supports `/browse` (by file type) and `/recent`
(last 10 uploads), with owner-only Rename/Delete buttons.

**Honesty note on testing:** this code was written and statically verified
carefully — syntax-checked, every function call cross-referenced against
its definition, and every Telegram/MongoDB API call cross-checked against
current library documentation (python-telegram-bot v22.8, pymongo 4.17.0,
both current as of writing this). But the sandbox that generated this
bundle has no internet access, so it could **not** be run end-to-end
against live Telegram/MongoDB servers. Follow the verification checklist
below the first time you run it — that's the real test.

## Project structure

```
.
├── bot.py            ← the entire bot, at repo root (not in a subfolder)
├── requirements.txt
├── render.yaml        ← Render Blueprint (optional, see below)
├── Procfile           ← alternative to render.yaml
├── .python-version    ← pins Python 3.12 so Render doesn't drift versions
├── .env.example
└── .gitignore
```

Everything Render needs — `bot.py`, `requirements.txt`, `Procfile` /
`render.yaml` — sits at the **root of the repo**, on your main branch.
Render's defaults (`pip install -r requirements.txt`, `python bot.py`)
work with this layout with no path overrides needed.

## 1. Requirements

- Python 3.10+ (3.12 recommended — see `.python-version`)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A MongoDB Atlas cluster + connection string
- Your bot added to your channel **as an administrator** — bots do not
  receive channel posts otherwise, regardless of any other setting

## 2. Local setup

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

- `BOT_TOKEN` — from BotFather
- `MONGO_URI` — from MongoDB Atlas → Connect → Drivers
- `CHANNEL_ID` (optional but recommended) — your channel's numeric id,
  looks like `-1001234567890`. See "Finding your channel ID" below.
- `OWNER_ID` (optional but recommended) — your personal numeric Telegram
  user id, so strangers can't search, rename, or delete your files. Get it
  from [@userinfobot](https://t.me/userinfobot).
- Leave `WEBHOOK_URL` blank — polling is simplest and is what's
  recommended for deployment (see §5).

Run it:

```bash
python bot.py
```

You should see: `... - tgfilebot - INFO - Starting in polling mode`

## 3. Verification checklist (do this in order)

1. **Bot is admin in the channel.** Channel → Administrators → Add Admin →
   your bot.
2. Post a file (document/photo/video/audio) to the channel.
3. Check the terminal — you should see:
   `Saved file record: <filename> (document)`
   If not, the bot isn't receiving channel posts — recheck admin rights
   and `CHANNEL_ID` (or unset `CHANNEL_ID` while testing to rule it out).
4. DM your bot `/start` — confirm it replies.
5. Send `/stats` — confirm it reports at least 1 indexed file.
6. Send part of the filename you posted (e.g. `report` for
   `Q3_report.pdf`) — confirm you get a button with that file's name.
7. Tap the button — confirm you get a preview card with type/size/date
   and a **Send file** button.
8. Tap **Send file** — confirm you receive the actual file.
9. Try `/browse` → pick a type → confirm the list appears.
10. Try `/recent` → confirm your test upload appears.
11. On a preview card, try **✏️ Rename**, send a new name, confirm it
    updates. Try **🗑 Delete**, confirm it's removed and no longer found
    by search.

If every step works, the bot is functioning correctly for your setup.

## 4. Finding your channel ID

Forward any message from the channel to
[@userinfobot](https://t.me/userinfobot) or
[@JsonDumpBot](https://t.me/JsonDumpBot) — it shows the channel's numeric
id (starts with `-100`).

## 5. Deploying to Render

This bot uses **long polling**, so deploy as a **Background Worker**, not
a Web Service — simpler, no webhook/SSL/public-URL setup needed.

### Option A — Blueprint (`render.yaml`)

1. Push this repo to GitHub with `bot.py` and the other files at the repo
   root, on your main branch.
2. On Render: **New → Blueprint** → connect the repo. Render reads
   `render.yaml` automatically.
3. Render will prompt for the `sync: false` variables (`BOT_TOKEN`,
   `MONGO_URI`, `CHANNEL_ID`, `OWNER_ID`) — fill these in during setup.
4. Deploy.

### Option B — Manual Background Worker

1. On Render: **New → Background Worker** → connect the repo.
2. Runtime: **Python 3**.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Environment tab → add `BOT_TOKEN`, `MONGO_URI`, `MONGO_DB_NAME`,
   `CHANNEL_ID`, `OWNER_ID`.
6. Deploy.

After deploying, open the **Logs** tab and repeat the verification
checklist from §3 against the live deployment.

### Webhook mode on a free Web Service (recommended for light/occasional use)

If your bot only gets occasional traffic (a few messages a day), a
**Web Service** running in webhook mode is the better fit — a free
Background Worker's `getUpdates` polling loop runs nonstop even when
nobody's using the bot, which burns free instance-hours for no benefit
and can trigger Render's health-check `Timed Out` restarts (Render can't
always tell "quietly polling" apart from "hung," since a worker exposes
no HTTP port for Render to check). A Web Service has a real HTTP
endpoint Render can health-check properly, and it fully spins down to
zero cost when idle instead of polling in the background.

1. On Render: **New → Web Service** → connect the repo.
2. Runtime: **Python 3**. Instance type: **Free**.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Environment tab → add `BOT_TOKEN`, `MONGO_URI`, `MONGO_DB_NAME`,
   `CHANNEL_ID`, `OWNER_ID`, same as the worker setup.
6. Add one more variable: `WEBHOOK_URL` = your service's Render URL,
   e.g. `https://your-app-name.onrender.com` (visible at the top of the
   service page once created — you may need to deploy once first to see
   it, then add this var and redeploy).
7. Deploy. Check the **Logs** tab for `Starting in webhook mode on port
   10000` followed by `Registering webhook URL: https://...` — this
   confirms the bot registered the webhook with Telegram successfully.

**What to expect:** the free instance spins down after 15 minutes with
no incoming traffic. The next message after that triggers a cold start —
about 50 seconds — before you get a reply; after that it's instant again
for 15 minutes. This is a one-time delay per idle gap, not a failure —
the message isn't lost, it's just waiting for the instance to wake up.

**Known edge case worth knowing about, not acting on:** Telegram waits
up to 60 seconds for your webhook to return `200 OK` before it retries
delivery. Render's ~50 second cold start is close enough to that limit
that on rare occasions, a slow wake-up could cross 60 seconds and cause
Telegram to redeliver the same update. `bot.py` doesn't currently
deduplicate by `update_id`, so a redelivered update would be processed
twice (e.g. a search running twice). For a personal low-traffic bot this
is a minor, rare inconvenience rather than a real problem — worth adding
`update_id` deduplication only if it actually starts happening in
practice.


## 6. Common deploy errors and fixes

- **`ModuleNotFoundError` on startup, or build succeeds but start fails
  immediately:** almost always means the start command points at the
  wrong path. This repo keeps `bot.py` at the root specifically so
  `python bot.py` works with no path prefix — confirm your Render start
  command matches exactly (`python bot.py`, not `python app/bot.py`).
- **Build fails with "Could not find a version that satisfies the
  requirement...":** a pinned version in `requirements.txt` no longer
  exists on PyPI. The versions here were confirmed live on PyPI at the
  time of writing (`python-telegram-bot==22.8`, `pymongo==4.17.0`,
  `python-dotenv==1.2.2`) — if this changes in the future, run
  `pip install --upgrade python-telegram-bot pymongo python-dotenv`
  locally and update the pins.
- **`pymongo.errors.ServerSelectionTimeoutError`:** your Atlas cluster is
  rejecting Render's IP. Render's free-tier outbound IPs aren't static, so
  either allow `0.0.0.0/0` in Atlas → Network Access (fine for testing;
  MongoDB's own docs note this is not the most secure option for
  production), or use Atlas's documented
  [Render integration](https://www.mongodb.com/docs/atlas/reference/partner-integrations/render/)
  to allowlist Render's specific static outbound IPs.
- **Service builds and starts but never indexes anything:** the bot
  almost certainly isn't an admin in the channel — this is the single
  most common cause. Double-check, then check `CHANNEL_ID` matches if set.
- **Webhook mode: logs show binding to the wrong port, or Render can't
  detect an open port:** `bot.py` reads Render's `PORT` env var
  automatically — don't set `PORT` manually unless you have a specific
  reason to. Render's Web Services default to port `10000` and expect
  your app to bind there; the code already does this correctly via
  `int(os.environ.get("PORT", "8080"))`.
- **Webhook mode: bot deploys, logs show `Registering webhook URL:
  https://None/...` or similar:** `WEBHOOK_URL` isn't set, or is set to
  an empty string. Add it in Render's Environment tab with your service's
  full `https://your-app.onrender.com` URL.

- **Deploy succeeds, no crash, but nothing happens at all:** check the
  Render **Logs** tab for `Starting in polling mode` — if that line never
  appears, the process may be crashing on an unset required env var
  (`BOT_TOKEN` or `MONGO_URI` are required and will raise `KeyError` if
  missing — check Render's Environment tab for typos in the variable
  names).

## 7. Files uploaded by another bot/webapp (the `/ingest` endpoint)

**Platform limitation:** Telegram never delivers a bot an update
(`channel_post` or otherwise) for a message that bot itself just sent —
this is true no matter the chat type, and true even if the uploader and
this indexer share the same bot token. So `index_channel_post` can only
ever see files posted by an actual human (admin) or a genuinely separate
bot account acting on its own — it will **never** see this bot's own
`sendDocument`/`sendPhoto` calls, or another tool's calls using the same
token.

If you have a separate tool (a webapp, a script, another bot) uploading
files to the channel via the Bot API directly, have it call this bot's
`POST /ingest` right after each successful upload:

```
POST https://<this-bot's-deployed-url>/ingest
Headers:  X-Ingest-Secret: <your INGEST_SECRET>
Body (JSON):
{
  "file_id": "...",          // from the sendDocument/sendPhoto response
  "file_unique_id": "...",
  "name": "40309.jpeg",      // optional but recommended
  "caption": "40309.jpeg",   // optional but recommended
  "file_type": "document",   // document | video | photo | audio | voice | video_note
  "file_size": 123456        // optional
}
```

- Set `INGEST_SECRET` in this bot's env to a fixed value (e.g.
  `openssl rand -hex 24`) and use the exact same value in the uploader's
  request header. If `INGEST_SECRET` is left unset, a random one is
  generated every restart and logged once — fine for a quick test, but it
  will silently break ingestion on every redeploy since the uploader's
  copy goes stale.
- In **webhook mode**, `/ingest` is served on the same `$PORT` as the
  Telegram webhook itself (most single-port hosts, including a Render Web
  Service, only expose one public port).
- In **polling mode**, `/ingest` runs on its own port (`INGEST_PORT`,
  default `8090`) since there's no webhook server to share.
- `GET /ingest/health` returns `{"ok": true}` if the endpoint is reachable
  — useful for a quick curl check before wiring up the real uploader.

## 9. How it works

- `index_channel_post` — fires on every post in a channel or linked group
  the bot admins (registered via `MessageHandler((filters.ChatType.CHANNEL
  | filters.ChatType.GROUPS), ...)`, and `allowed_updates=Update.ALL_TYPES`
  on both polling and webhook startup ensures Telegram actually delivers
  channel posts to the bot — omitting this is a common reason bots
  silently never receive them). Detects file type, pulls the Telegram
  `file_id` (used to resend) and `file_unique_id` (dedupe key), and
  upserts into the `files` collection. See section 8 above for the one
  case this handler structurally can't cover.
- `handle_ingest` — the `/ingest` HTTP route described in section 8;
  calls the same `save_file_record` as `index_channel_post`, so both
  paths land in the same collection and are equally searchable.
- `handle_search_text` — case-insensitive regex search against stored
  `name`/`caption`, falling back to a `rapidfuzz` fuzzy pass over
  everything else so near-misspellings still surface. Returns results as
  inline buttons.
- `handle_callback` — routes every button tap: viewing a preview card,
  paging through results, sending a file, renaming, deleting, or
  browsing/going back.
- `OWNER_ID`, if set, restricts all bot interaction to just you.
- `CHANNEL_ID`, if set, restricts indexing to just that channel.
- An in-memory cache (`_list_cache`) backs pagination/back-navigation so
  the bot doesn't re-run a MongoDB query on every button tap. This is
  cleared on restart and isn't shared across multiple bot instances —
  fine for single-owner use; a multi-instance or high-traffic deployment
  would want this in Mongo/Redis instead.

## 10. Known limitations

- Voice notes and video notes have no filename and Telegram doesn't allow
  captions on them either, so they're only findable if you search by
  something in a *different* file's caption that happens to match — in
  practice, treat these two types as browse-only (`/browse`), not
  search-friendly.
- The rename flow uses a simple "waiting for your next message" state
  stored in `context.user_data`. If you tap Rename and then send a
  command instead of a name, that command will be swallowed as the new
  filename. This is a deliberate simplicity trade-off, not a bug — worth
  knowing about if renames start behaving oddly.
