# Telegram File Indexer + Search Bot

Auto-indexes every file posted in your Telegram channel into MongoDB.
DM the bot part of a filename and it sends back any matches.

**Important honesty note:** this code was written carefully against the
documented `python-telegram-bot` v21 and `pymongo` APIs, but it has **not**
been run end-to-end against live Telegram/MongoDB servers (no internet
access in the environment that generated this bundle). Follow the
verification checklist below step by step the first time you run it, and
watch the logs — that's how you confirm it actually works in your setup.

## 1. Requirements

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A MongoDB Atlas cluster + connection string
- Your bot added to your channel **as an administrator** (it must be admin
  to receive `channel_post` updates)

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
  user id, so strangers can't search your files. Get it from
  [@userinfobot](https://t.me/userinfobot).
- Leave `WEBHOOK_URL` blank for now — polling is simplest to verify first.

Run it:

```bash
python app/bot.py
```

You should see log lines like:

```
... - tgfilebot - INFO - Starting in polling mode
```

## 3. Verification checklist (do this in order)

1. **Bot is admin in the channel.** Channel → Administrators → Add Admin →
   your bot. Without this, Telegram never sends it channel posts.
2. Post a file (any document/photo/video) to the channel.
3. Check the terminal running the bot — you should see:
   `Saved file record: <filename> (document)`
   If you don't see this line, the bot isn't receiving channel posts —
   double check admin rights and `CHANNEL_ID` (or unset `CHANNEL_ID` while
   testing to rule it out).
4. Open a private chat with your bot, send `/start` — confirm it replies.
5. Send `/stats` — confirm it reports at least 1 indexed file.
6. Send part of the filename you posted (e.g. `report` for
   `Q3_report.pdf`) — confirm the bot sends the file back.
7. Try a name that doesn't exist — confirm you get "No files found".

If every step above works, the bot is functioning correctly for your setup.

## 4. Finding your channel ID

Easiest method: forward any message from the channel to
[@userinfobot](https://t.me/userinfobot) or
[@JsonDumpBot](https://t.me/JsonDumpBot) — it will show the channel's numeric
id (starts with `-100`).

## 5. Deploying to Render

This bot uses **long polling**, so deploy it as a **Background Worker**
(not a Web Service) — this is simpler and avoids webhook/SSL setup.

1. Push this folder to a GitHub repo.
2. On Render: New → Background Worker → connect the repo.
3. Render will detect `render.yaml`. Alternatively set manually:
   - Build command: `pip install -r requirements.txt`
   - Start command: `python app/bot.py`
4. Add environment variables in the Render dashboard (Environment tab):
   `BOT_TOKEN`, `MONGO_URI`, `MONGO_DB_NAME`, `CHANNEL_ID`, `OWNER_ID`.
5. Deploy, then open the **Logs** tab and repeat the verification checklist
   above using the live deployment.

### Optional: webhook mode instead

If you'd rather run this as a Render **Web Service**, set `WEBHOOK_URL` to
your Render service's public URL (e.g. `https://your-app.onrender.com`) and
the bot will auto-register the webhook at startup. Free-tier Render web
services sleep after inactivity, which can delay webhook delivery — a
Background Worker with polling avoids that entirely.

## 6. File filtering (movies excluded, 20MB cap)

To keep this as a small-file document/photo/audio indexer only:

- Any post typed as `video` or `video_note` by Telegram is **never** indexed,
  regardless of size.
- Any file (including documents) larger than `MAX_FILE_SIZE_MB` (default 20)
  is skipped. If Telegram doesn't report a size for a file, it's skipped too
  (better to skip than index something unverified).
- Skipped posts are logged (`Skipped ...`) but otherwise ignored — nothing
  is sent back to the channel.
- This is filtering on Telegram's own type/size metadata, not on filename —
  someone could still rename a movie file so it looks like a document and
  post it under 20MB. This isn't a content-moderation system; if you need to
  guarantee no copyrighted media gets shared, that requires human review of
  what's posted to the source channel.

## 7. How it works

- `index_channel_post` — fires on every post in a channel the bot admins.
  Detects file type (document/video/audio/voice/video_note/photo), pulls
  the Telegram `file_id` (used to resend later) and `file_unique_id` (used
  as a dedupe key), and upserts a record into the `files` collection.
- `handle_search_text` — fires on any private text message to the bot.
  Runs a case-insensitive regex search against stored `name` and `caption`
  fields, and resends each match using its stored `file_id`.
- `OWNER_ID`, if set, restricts search access to just you.
- `CHANNEL_ID`, if set, restricts indexing to just that one channel (useful
  if the bot is ever added to more than one).

## 7. Troubleshooting

- **Bot doesn't index anything:** it's very likely not an admin in the
  channel, or `CHANNEL_ID` doesn't match. Channel posts are invisible to
  non-admin bots.
- **`pymongo.errors.ServerSelectionTimeoutError`:** check your Atlas
  Network Access list allows connections from anywhere (`0.0.0.0/0`) if
  deploying to Render, since Render's IPs aren't static on the free tier.
- **Bot doesn't reply in DM at all:** confirm `BOT_TOKEN` is correct and
  you're messaging the right bot username.
- **No files found even though you posted one:** the file's `name`/`caption`
  might not contain your search text — voice notes and video notes have no
  filename, so they're only searchable by caption text (which Telegram
  doesn't allow on those types) — consider replying to those with a text
  caption workaround, or only test search against documents/videos first.
