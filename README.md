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

### Optional: webhook mode instead

Set `WEBHOOK_URL` to your Render Web Service's public URL and the bot
registers a webhook at startup instead of polling. Note: `requirements.txt`
already includes the `[webhooks]` extra (pulls in `tornado`), which
`run_webhook()` needs — without it, webhook mode fails on startup with an
import error. Free-tier Render web services sleep after inactivity, which
delays webhook delivery; a Background Worker with polling avoids this
entirely, which is why it's the default recommendation here.

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
- **Bot doesn't reply in DM at all:** confirm `BOT_TOKEN` is correct in
  Render's Environment tab (not just locally) and you're messaging the
  right bot username.
- **Deploy succeeds, no crash, but nothing happens at all:** check the
  Render **Logs** tab for `Starting in polling mode` — if that line never
  appears, the process may be crashing on an unset required env var
  (`BOT_TOKEN` or `MONGO_URI` are required and will raise `KeyError` if
  missing — check Render's Environment tab for typos in the variable
  names).

## 7. How it works

- `index_channel_post` — fires on every post in a channel the bot admins
  (registered via `MessageHandler(filters.ChatType.CHANNEL, ...)`, and
  `allowed_updates=Update.ALL_TYPES` on both polling and webhook startup
  ensures Telegram actually delivers channel posts to the bot — omitting
  this is a common reason bots silently never receive them). Detects file
  type, pulls the Telegram `file_id` (used to resend) and
  `file_unique_id` (dedupe key), and upserts into the `files` collection.
- `handle_search_text` — case-insensitive regex search against stored
  `name`/`caption`, returns results as inline buttons.
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

## 8. Known limitations

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
