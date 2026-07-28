"""
Telegram File Indexer + Search Bot (v2 - interactive UI)
---------------------------------------------------------
- Add this bot as an ADMIN to your channel.
- Every file posted to the channel (document, photo, video, audio, voice,
  video_note) gets auto-saved into MongoDB with its filename/caption +
  Telegram file_id.
- DM the bot any text -> inline-button results list -> tap a result to see
  a preview card (name/type/size/date) -> tap "Send file" to receive it.
- /browse -> browse indexed files by type, with pagination.
- /recent -> last 10 uploads.
- Owner-only Delete/Rename buttons on each preview card.

Run modes:
- Polling (default): good for local/dev or a Render "Background Worker".
- Webhook: good for a Render "Web Service" (set WEBHOOK_URL env var).
"""

import asyncio
import logging
import math
import os
import secrets
from datetime import datetime, timezone

from aiohttp import web
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, TEXT
from pymongo.errors import PyMongoError
from rapidfuzz import fuzz, process as fuzz_process
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "tgfilebot")
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # optional: restrict indexing to this channel id
OWNER_ID = os.environ.get("OWNER_ID")  # optional: restrict who can search/receive/manage files
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", "8080"))

# Shared secret the webapp (or any other uploader) must send in the
# X-Ingest-Secret header when calling POST /ingest below. This lets files
# uploaded straight through the Bot API by another tool get indexed even
# though Telegram never delivers a bot its own outgoing messages as an
# update (a bot never receives an update for a message it itself sent —
# this is a hard platform limitation, not something fixable via filters or
# allowed_updates). If unset, a random one is generated at startup and
# logged once so you can copy it into the uploader's config; set it
# explicitly via env var instead so it's stable across restarts/deploys.
INGEST_SECRET = os.environ.get("INGEST_SECRET") or secrets.token_urlsafe(24)
INGEST_PORT = int(os.environ.get("INGEST_PORT", "8090"))

PAGE_SIZE = 8  # results per page in lists

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tgfilebot")

# ---------------------------------------------------------------------------
# Mongo setup
# ---------------------------------------------------------------------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client[MONGO_DB_NAME]
files_col = db["files"]

files_col.create_index([("file_unique_id", ASCENDING)], unique=True)
files_col.create_index([("name", TEXT), ("caption", TEXT)], name="name_caption_text")
files_col.create_index([("file_type", ASCENDING), ("saved_at", ASCENDING)])

TYPE_ICONS = {
    "document": "📄",
    "video": "🎥",
    "photo": "🖼",
    "audio": "🎵",
    "voice": "🎙",
    "video_note": "⭕",
}


def save_file_record(*, file_id, file_unique_id, name, caption, file_type,
                      chat_id, message_id, file_size=None):
    doc = {
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "name": name or "",
        "caption": caption or "",
        "file_type": file_type,
        "chat_id": chat_id,
        "message_id": message_id,
        "file_size": file_size,
        "saved_at": datetime.now(timezone.utc),
    }
    try:
        files_col.update_one(
            {"file_unique_id": file_unique_id},
            {"$set": doc},
            upsert=True,
        )
        logger.info("Saved file record: %s (%s)", name or caption, file_type)
    except PyMongoError:
        logger.exception("Failed to save file record to MongoDB")


# ---------------------------------------------------------------------------
# Ingest endpoint: for files uploaded to the channel by another tool using
# the Bot API directly (e.g. a webapp calling sendDocument/sendPhoto).
#
# Why this exists: Telegram never delivers a bot an update (channel_post or
# otherwise) for a message that bot itself just sent — this is a documented
# platform limitation, true regardless of chat type, and true even when the
# uploader and this indexer share the same bot token. So index_channel_post
# above can only ever see files posted by a human/admin (or a genuinely
# different bot account acting independently); it will never see this bot's
# own sendDocument/sendPhoto calls. The uploader has all the data already
# (file_id, file_unique_id, name, etc. are in the sendDocument/sendPhoto
# response) — this endpoint just lets it hand that off to be indexed, without
# needing direct database access (which would mean exposing the Mongo URI in
# client-side code).
# ---------------------------------------------------------------------------
async def handle_ingest(request: web.Request) -> web.Response:
    if request.headers.get("X-Ingest-Secret") != INGEST_SECRET:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    required = ("file_id", "file_unique_id", "file_type")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        return web.json_response(
            {"ok": False, "error": f"missing required field(s): {', '.join(missing)}"},
            status=400,
        )

    if payload["file_type"] not in TYPE_ICONS:
        return web.json_response(
            {"ok": False, "error": f"unknown file_type: {payload['file_type']!r}"},
            status=400,
        )

    save_file_record(
        file_id=payload["file_id"],
        file_unique_id=payload["file_unique_id"],
        name=payload.get("name"),
        caption=payload.get("caption"),
        file_type=payload["file_type"],
        chat_id=payload.get("chat_id"),
        message_id=payload.get("message_id"),
        file_size=payload.get("file_size"),
    )
    return web.json_response({"ok": True})


async def handle_ingest_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "tg-file-bot ingest"})


def build_ingest_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/ingest", handle_ingest)
    app.router.add_get("/ingest/health", handle_ingest_health)
    return app


FUZZY_SCORE_CUTOFF = 60  # 0-100; lower = more forgiving of typos/near-spellings


def search_files(query: str):
    """Match files whose name/caption contain the query as a substring
    (fast path, handles the common case) OR whose name/caption are a close
    fuzzy match to the query (catches typos / "almost matches spelling"),
    regardless of who uploaded the file — indexing doesn't distinguish
    uploader, so every indexed file is eligible.
    """
    query = query.strip()
    if not query:
        return []

    regex_filter = {
        "$or": [
            {"name": {"$regex": query, "$options": "i"}},
            {"caption": {"$regex": query, "$options": "i"}},
        ]
    }
    substring_matches = list(files_col.find(regex_filter))
    matched_ids = {doc["_id"] for doc in substring_matches}

    # Fuzzy pass over everything else, so near-misspellings still surface.
    remaining = list(files_col.find({"_id": {"$nin": list(matched_ids)}} if matched_ids else {}))
    fuzzy_matches = []
    for doc in remaining:
        label = f"{doc.get('name', '')} {doc.get('caption', '')}".strip()
        if not label:
            continue
        score = fuzz.partial_ratio(query.lower(), label.lower())
        if score >= FUZZY_SCORE_CUTOFF:
            fuzzy_matches.append((score, doc))
    fuzzy_matches.sort(key=lambda pair: pair[0], reverse=True)

    # Substring hits first (most confident), then fuzzy hits ordered by
    # closeness of match, each group already internally sorted.
    substring_matches.sort(key=lambda d: d.get("saved_at"), reverse=True)
    return substring_matches + [doc for _, doc in fuzzy_matches]


def list_by_type(file_type: str):
    return list(files_col.find({"file_type": file_type}).sort("saved_at", -1))


def list_recent(limit: int = 10):
    return list(files_col.find({}).sort("saved_at", -1).limit(limit))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_owner(update: Update) -> bool:
    """True only for OWNER_ID. Used to gate management actions (browse,
    recent, stats, rename, delete) that should never be public, even when
    search is open to everyone."""
    if not OWNER_ID:
        return True
    user = update.effective_user
    return user is not None and str(user.id) == str(OWNER_ID)


def human_size(num_bytes):
    if not num_bytes:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def display_label(doc) -> str:
    return doc.get("name") or doc.get("caption") or f"Untitled {doc.get('file_type', 'file')}"


def build_results_keyboard(docs, page: int, context_tag: str):
    """context_tag distinguishes callback namespaces: search / type:<x> / recent"""
    start = page * PAGE_SIZE
    page_docs = docs[start:start + PAGE_SIZE]
    total_pages = max(1, math.ceil(len(docs) / PAGE_SIZE))

    rows = []
    for doc in page_docs:
        icon = TYPE_ICONS.get(doc["file_type"], "📎")
        label = display_label(doc)
        if len(label) > 40:
            label = label[:37] + "..."
        rows.append([
            InlineKeyboardButton(f"{icon} {label}", callback_data=f"view:{doc['_id']}")
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"page:{context_tag}:{page-1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if start + PAGE_SIZE < len(docs):
        nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"page:{context_tag}:{page+1}"))
    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(rows)


def build_preview_keyboard(doc_id, back_tag):
    rows = [[InlineKeyboardButton("📤 Send file", callback_data=f"send:{doc_id}")]]
    owner_row = [
        InlineKeyboardButton("✏️ Rename", callback_data=f"rename:{doc_id}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{doc_id}"),
    ]
    rows.append(owner_row)
    if back_tag:
        rows.append([InlineKeyboardButton("🔙 Back to results", callback_data=f"back:{back_tag}")])
    return InlineKeyboardMarkup(rows)


def format_preview_text(doc) -> str:
    icon = TYPE_ICONS.get(doc["file_type"], "📎")
    label = display_label(doc)
    size = human_size(doc.get("file_size"))
    saved_at = doc.get("saved_at")
    date_str = saved_at.strftime("%Y-%m-%d %H:%M UTC") if saved_at else "unknown date"
    caption = doc.get("caption") or "—"

    return (
        f"{icon} *{escape_md(label)}*\n\n"
        f"*Type:* {doc['file_type'].replace('_', ' ').title()}\n"
        f"*Size:* {size}\n"
        f"*Added:* {date_str}\n"
        f"*Caption:* {escape_md(caption)}"
    )


def escape_md(text: str) -> str:
    # Minimal escaping for Telegram legacy Markdown parse mode
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# In-memory cache of last list per (chat_id, context_tag) so pagination/back
# doesn't need to re-run the query. Fine for a single-owner bot; for multi-user
# heavy use you'd move this into Mongo or Redis.
_list_cache = {}


# ---------------------------------------------------------------------------
# Handlers: indexing channel posts
# ---------------------------------------------------------------------------
async def index_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NOTE: This handler indexes a file no matter who put it there — the
    # channel's own identity, a named admin, a regular member posting into a
    # linked discussion group, or another bot. Telegram channel posts do not
    # carry a distinguishable "uploader" the way group messages do (they show
    # as the channel/author signature), so there was never any per-user check
    # here to remove. What *can* silently hide files is (a) a stale/incorrect
    # CHANNEL_ID env var making the chat-id check reject everything except
    # whatever chat you tested with, or (b) files landing in a linked
    # discussion group as ordinary group messages rather than channel_post
    # updates. Both are handled below, with logging instead of silent drops
    # so a misconfiguration is visible instead of looking like "only some
    # people's uploads are indexed."
    msg = update.channel_post or update.message
    if msg is None:
        return

    if CHANNEL_ID and str(msg.chat.id) != str(CHANNEL_ID):
        logger.info(
            "Ignoring post from chat %s (%s) because CHANNEL_ID is set to %s. "
            "If this chat should be indexed, update CHANNEL_ID or unset it.",
            msg.chat.id, msg.chat.title or msg.chat.type, CHANNEL_ID,
        )
        return

    file_obj = None
    file_type = None
    name = None

    if msg.document:
        file_obj, file_type, name = msg.document, "document", msg.document.file_name
    elif msg.video:
        file_obj, file_type, name = msg.video, "video", msg.video.file_name
    elif msg.audio:
        file_obj, file_type, name = msg.audio, "audio", (msg.audio.file_name or msg.audio.title)
    elif msg.voice:
        file_obj, file_type, name = msg.voice, "voice", None
    elif msg.video_note:
        file_obj, file_type, name = msg.video_note, "video_note", None
    elif msg.photo:
        file_obj, file_type, name = msg.photo[-1], "photo", None

    if file_obj is None:
        logger.info(
            "Post %s in chat %s had no recognized file attachment; skipping.",
            msg.message_id, msg.chat.id,
        )
        return

    save_file_record(
        file_id=file_obj.file_id,
        file_unique_id=file_obj.file_unique_id,
        name=name,
        caption=msg.caption,
        file_type=file_type,
        chat_id=msg.chat.id,
        message_id=msg.message_id,
        file_size=getattr(file_obj, "file_size", None),
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *File Search Bot*\n\n"
        "Send me part of a file name and I'll search the channel's indexed "
        "files.\n\n"
        "Commands:\n"
        "• /browse — browse files by type\n"
        "• /recent — last 10 uploads\n"
        "• /stats — indexed file count",
        parse_mode="Markdown",
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("This command is only available to the bot owner.")
        return
    count = files_col.count_documents({})
    by_type = files_col.aggregate([{"$group": {"_id": "$file_type", "n": {"$sum": 1}}}])
    lines = [f"*Total indexed:* {count}"]
    for row in by_type:
        icon = TYPE_ICONS.get(row["_id"], "📎")
        lines.append(f"{icon} {row['_id'].title()}: {row['n']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def browse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("This command is only available to the bot owner.")
        return
    rows = [
        [InlineKeyboardButton("📄 Documents", callback_data="browsetype:document")],
        [InlineKeyboardButton("🎥 Videos", callback_data="browsetype:video")],
        [InlineKeyboardButton("🖼 Photos", callback_data="browsetype:photo")],
        [InlineKeyboardButton("🎵 Audio", callback_data="browsetype:audio")],
    ]
    await update.message.reply_text(
        "📂 *Browse by type*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


async def recent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("This command is only available to the bot owner.")
        return
    docs = list_recent(10)
    if not docs:
        await update.message.reply_text("No files indexed yet.")
        return
    tag = f"recent:{update.effective_chat.id}"
    _list_cache[(update.effective_chat.id, tag)] = docs
    await update.message.reply_text(
        "🕓 *Recent uploads*",
        parse_mode="Markdown",
        reply_markup=build_results_keyboard(docs, 0, tag),
    )


# ---------------------------------------------------------------------------
# Search (plain text in DM)
# ---------------------------------------------------------------------------
async def handle_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Intentionally open to every Telegram user: searching and receiving
    # files is the bot's public-facing feature. Management actions
    # (rename/delete/browse/recent/stats) remain owner-only via is_owner().
    query = update.message.text
    results = search_files(query)

    if not results:
        await update.message.reply_text(f"🔍 No files found matching \"{query}\".")
        return

    tag = f"search:{update.effective_chat.id}:{abs(hash(query)) % 100000}"
    _list_cache[(update.effective_chat.id, tag)] = results

    await update.message.reply_text(
        f"🔍 *Found {len(results)} match(es) for* \"{escape_md(query)}\"",
        parse_mode="Markdown",
        reply_markup=build_results_keyboard(results, 0, tag),
    )


# ---------------------------------------------------------------------------
# Callback query handling (all inline button taps)
# ---------------------------------------------------------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id

    # No blanket auth gate here: viewing search results, paging, and
    # sending a file are public actions any user should be able to tap on
    # their own search. Delete and rename are checked individually below,
    # since those must stay owner-only even when search is public.

    if data == "noop":
        await query.answer()
        return

    # --- Browse: pick a type ---
    if data.startswith("browsetype:"):
        file_type = data.split(":", 1)[1]
        docs = list_by_type(file_type)
        await query.answer()
        if not docs:
            await query.edit_message_text(f"No {file_type}s indexed yet.")
            return
        tag = f"type:{file_type}:{chat_id}"
        _list_cache[(chat_id, tag)] = docs
        icon = TYPE_ICONS.get(file_type, "📎")
        await query.edit_message_text(
            f"{icon} *{file_type.title()}s* ({len(docs)})",
            parse_mode="Markdown",
            reply_markup=build_results_keyboard(docs, 0, tag),
        )
        return

    # --- Pagination ---
    if data.startswith("page:"):
        _, tag, page_str = data.split(":", 2)
        page = int(page_str)
        docs = _list_cache.get((chat_id, tag))
        await query.answer()
        if docs is None:
            await query.edit_message_text("This list expired — please search again.")
            return
        await query.edit_message_reply_markup(reply_markup=build_results_keyboard(docs, page, tag))
        return

    # --- Back to a cached list ---
    if data.startswith("back:"):
        tag = data.split(":", 1)[1]
        docs = _list_cache.get((chat_id, tag))
        await query.answer()
        if docs is None:
            await query.edit_message_text("This list expired — please search again.")
            return
        await query.edit_message_text(
            f"🔍 *Results* ({len(docs)})",
            parse_mode="Markdown",
            reply_markup=build_results_keyboard(docs, 0, tag),
        )
        return

    # --- View a specific file's preview card ---
    if data.startswith("view:"):
        doc_id = data.split(":", 1)[1]
        doc = _find_by_id(doc_id)
        await query.answer()
        if doc is None:
            await query.edit_message_text("File not found (it may have been deleted).")
            return
        back_tag = _guess_back_tag(chat_id, doc_id)
        await query.edit_message_text(
            format_preview_text(doc),
            parse_mode="Markdown",
            reply_markup=build_preview_keyboard(doc_id, back_tag),
        )
        return

    # --- Send the actual file ---
    if data.startswith("send:"):
        doc_id = data.split(":", 1)[1]
        doc = _find_by_id(doc_id)
        await query.answer("Sending...")
        if doc is None:
            await context.bot.send_message(chat_id, "File not found (it may have been deleted).")
            return
        await _send_stored_file(context, chat_id, doc)
        return

    # --- Delete (owner-only) ---
    if data.startswith("delete:"):
        if not is_owner(update):
            await query.answer("Only the bot owner can delete files.", show_alert=True)
            return
        doc_id = data.split(":", 1)[1]
        doc = _find_by_id(doc_id)
        if doc is None:
            await query.answer("Already deleted.")
            return
        files_col.delete_one({"_id": doc["_id"]})
        await query.answer("Deleted.")
        await query.edit_message_text(f"🗑 Deleted: {display_label(doc)}")
        return

    # --- Rename: ask for new name via a follow-up text message (owner-only) ---
    if data.startswith("rename:"):
        if not is_owner(update):
            await query.answer("Only the bot owner can rename files.", show_alert=True)
            return
        doc_id = data.split(":", 1)[1]
        doc = _find_by_id(doc_id)
        if doc is None:
            await query.answer("File not found.")
            return
        await query.answer()
        context.user_data["awaiting_rename_for"] = doc_id
        await context.bot.send_message(
            chat_id,
            f"✏️ Send the new name for *{escape_md(display_label(doc))}*:",
            parse_mode="Markdown",
        )
        return

    await query.answer()


def _find_by_id(doc_id: str):
    try:
        return files_col.find_one({"_id": ObjectId(doc_id)})
    except InvalidId:
        return None


def _guess_back_tag(chat_id, doc_id):
    """Find a cached list tag for this chat that contains doc_id, so the
    preview card's Back button returns to the right list."""
    for (cid, tag), docs in _list_cache.items():
        if cid != chat_id:
            continue
        if any(str(d["_id"]) == doc_id for d in docs):
            return tag
    return None


async def _send_stored_file(context, chat_id, doc):
    label = display_label(doc)
    ftype = doc["file_type"]
    file_id = doc["file_id"]
    try:
        if ftype == "document":
            await context.bot.send_document(chat_id, file_id, caption=f"📄 {label}")
        elif ftype == "video":
            await context.bot.send_video(chat_id, file_id, caption=f"🎥 {label}")
        elif ftype == "audio":
            await context.bot.send_audio(chat_id, file_id, caption=f"🎵 {label}")
        elif ftype == "voice":
            await context.bot.send_voice(chat_id, file_id)
        elif ftype == "video_note":
            await context.bot.send_video_note(chat_id, file_id)
        elif ftype == "photo":
            await context.bot.send_photo(chat_id, file_id, caption=f"🖼 {label}")
    except Exception:
        logger.exception("Failed to resend file %s", doc.get("_id"))
        await context.bot.send_message(chat_id, f"⚠️ Couldn't resend: {label}")


# ---------------------------------------------------------------------------
# Plain text router: rename reply vs. search query
# ---------------------------------------------------------------------------
async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No owner check needed here: user_data is per-user, and
    # "awaiting_rename_for" is only ever set inside the owner-gated
    # rename: callback branch above. A non-owner's user_data can never
    # contain this key, so this path is unreachable for them.
    pending_doc_id = context.user_data.get("awaiting_rename_for")
    if pending_doc_id:
        new_name = update.message.text.strip()
        doc = _find_by_id(pending_doc_id)
        context.user_data.pop("awaiting_rename_for", None)
        if doc is None:
            await update.message.reply_text("That file no longer exists.")
            return
        files_col.update_one({"_id": doc["_id"]}, {"$set": {"name": new_name}})
        await update.message.reply_text(f"✅ Renamed to: {new_name}")
        return

    await handle_search_text(update, context)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_app() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("browse", browse_cmd))
    application.add_handler(CommandHandler("recent", recent_cmd))

    file_filter = (
        filters.Document.ALL | filters.VIDEO | filters.PHOTO
        | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE
    )
    # CHANNEL covers posts made directly in the channel (any admin, the
    # channel's own identity, or another bot). GROUPS covers the case where
    # the channel has a linked discussion group and a file is posted there
    # by an ordinary member — those arrive as normal group messages, not
    # channel_post updates, and were previously invisible to this bot.
    application.add_handler(
        MessageHandler((filters.ChatType.CHANNEL | filters.ChatType.GROUPS) & file_filter, index_channel_post)
    )
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_text)
    )

    return application


def _run_standalone_ingest_server():
    """For polling-mode deployments only (e.g. a Render Background Worker,
    or local/dev): runs the /ingest HTTP server on its own port, in its own
    thread with its own asyncio event loop, independent of run_polling() on
    the main thread. Not used in webhook mode — there, /ingest is mounted on
    the SAME aiohttp app and port as the Telegram webhook (see
    run_webhook_with_ingest below), because a typical single-port host
    (like a Render Web Service) only exposes one public port, so a second
    port here would be unreachable from outside."""
    import asyncio
    import threading

    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _serve():
            app = build_ingest_app()
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", INGEST_PORT)
            await site.start()
            logger.info(
                "Ingest endpoint listening on 0.0.0.0:%s (POST /ingest) — "
                "standalone port, since this is polling mode",
                INGEST_PORT,
            )
            await asyncio.Event().wait()  # keep this loop alive forever

        loop.run_until_complete(_serve())

    thread = threading.Thread(target=_runner, name="ingest-server", daemon=True)
    thread.start()


async def _run_webhook_with_ingest(application: Application, full_webhook_url: str):
    """Webhook mode: builds ONE aiohttp app that serves both the Telegram
    webhook route and /ingest on the same port ($PORT). This is required
    for single-port hosts like a Render Web Service, which only forwards
    one public port — a second port for /ingest would be unreachable from
    the public webapp calling it."""
    async def telegram_webhook(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
        return web.Response()

    app = build_ingest_app()
    app.router.add_post(f"/{BOT_TOKEN}", telegram_webhook)

    await application.initialize()
    await application.bot.set_webhook(url=full_webhook_url, allowed_updates=Update.ALL_TYPES)
    await application.start()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Webhook + ingest server listening on 0.0.0.0:%s", PORT)
    logger.info("Telegram webhook path: /%s", BOT_TOKEN)
    logger.info("Ingest path: /ingest (needs X-Ingest-Secret header)")

    await asyncio.Event().wait()  # keep running until the process is killed


def main():
    if not os.environ.get("INGEST_SECRET"):
        logger.warning(
            "INGEST_SECRET not set — generated a random one for this process "
            "(changes on every restart): %s . Set INGEST_SECRET in your env "
            "to a fixed value and use the SAME value in the uploader's "
            "X-Ingest-Secret header, or every restart will silently break "
            "ingestion until you update the uploader too.",
            INGEST_SECRET,
        )

    application = build_app()

    # On Render (or any host with an expected HTTP port), you MUST run in
    # webhook mode: it binds $PORT immediately, which is what stops the
    # platform from timing out the deploy. Polling never binds a port, so
    # if this falls through to polling on a Web Service, Render waits for a
    # port that never opens and kills the deploy.
    render_url = os.environ.get("RENDER_EXTERNAL_URL")  # auto-set by Render
    on_render = os.environ.get("RENDER") == "true"  # set on every Render service
    webhook_target = WEBHOOK_URL or render_url

    if on_render and not webhook_target:
        # Fail fast and loud instead of quietly starting a poller that will
        # never bind a port and burn the whole deploy timeout.
        raise RuntimeError(
            "Running on Render but no webhook URL could be determined. "
            "Set the WEBHOOK_URL env var to this service's public URL "
            "(Render dashboard -> service -> the https://<name>.onrender.com address)."
        )

    if webhook_target:
        webhook_base = webhook_target.rstrip("/")
        if not webhook_base.startswith(("http://", "https://")):
            webhook_base = f"https://{webhook_base}"
        full_webhook_url = f"{webhook_base}/{BOT_TOKEN}"
        logger.info("Starting in webhook mode on port %s", PORT)
        logger.info("Registering webhook URL: %s", full_webhook_url)
        asyncio.run(_run_webhook_with_ingest(application, full_webhook_url))
    else:
        logger.info("Starting in polling mode (no WEBHOOK_URL / RENDER_EXTERNAL_URL set)")
        _run_standalone_ingest_server()
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
