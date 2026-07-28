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

import hashlib
import hmac
import logging
import math
import os
import re
import secrets
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, TEXT
from pymongo.errors import PyMongoError
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

PAGE_SIZE = 8  # results per page in lists

LARGE_FILE_BYTES = 50 * 1024 * 1024  # 50 MB
AUTO_DELETE_SECONDS = 5 * 60  # 5 minutes

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

# Matches "pass:1234" (case-insensitive) anywhere in a caption, capturing
# everything up to the next whitespace as the password. Used to let a
# caption both set a per-file password AND carry ordinary searchable text,
# e.g. "family photo pass:1234" -> password "1234", searchable text
# "family photo".
PASSWORD_TAG_RE = re.compile(r"(?i)\bpass:(\S+)")


def extract_password_tag(caption: str):
    """Pulls a 'pass:XXXX' tag out of a caption.

    Returns (password_or_None, caption_with_tag_removed). The returned
    caption has the tag stripped and whitespace collapsed, so the raw
    password never lingers in the searchable/displayed caption text.
    """
    if not caption:
        return None, caption or ""
    match = PASSWORD_TAG_RE.search(caption)
    if not match:
        return None, caption
    password = match.group(1)
    cleaned = PASSWORD_TAG_RE.sub("", caption)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return password, cleaned


def hash_password(password: str, salt: bytes | None = None):
    """Salted SHA-256. Good enough here: passwords are short shared-family
    secrets behind a rate-limited bot UI, not a high-value auth system —
    but we still never store or log the plaintext."""
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.sha256(salt + password.encode("utf-8")).hexdigest()
    return salt.hex(), digest


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    _, candidate_digest = hash_password(password, salt)
    return hmac.compare_digest(candidate_digest, digest_hex)


def save_file_record(*, file_id, file_unique_id, name, caption, file_type,
                      chat_id, message_id, file_size=None):
    password, clean_caption = extract_password_tag(caption)

    doc = {
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "name": name or "",
        "caption": clean_caption or "",
        "file_type": file_type,
        "chat_id": chat_id,
        "message_id": message_id,
        "file_size": file_size,
        "saved_at": datetime.now(timezone.utc),
    }
    if password:
        salt_hex, digest_hex = hash_password(password)
        doc["pw_salt"] = salt_hex
        doc["pw_hash"] = digest_hex
    try:
        files_col.update_one(
            {"file_unique_id": file_unique_id},
            {"$set": doc},
            upsert=True,
        )
        logger.info(
            "Saved file record: %s (%s)%s",
            name or clean_caption, file_type, " [password-protected]" if password else "",
        )
    except PyMongoError:
        logger.exception("Failed to save file record to MongoDB")


def search_files(query: str):
    query = query.strip()
    if not query:
        return []
    regex_filter = {
        "$or": [
            {"name": {"$regex": query, "$options": "i"}},
            {"caption": {"$regex": query, "$options": "i"}},
        ]
    }
    return list(files_col.find(regex_filter).sort("saved_at", -1))


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
    size = human_size(doc.get("file_size"))
    saved_at = doc.get("saved_at")
    date_str = saved_at.strftime("%Y-%m-%d %H:%M UTC") if saved_at else "unknown date"
    lock_line = "\n🔒 *Password required to send*" if doc.get("pw_hash") else ""

    return (
        f"{icon} *File details*\n\n"
        f"*Type:* {doc['file_type'].replace('_', ' ').title()}\n"
        f"*Size:* {size}\n"
        f"*Added:* {date_str}"
        f"{lock_line}\n\n"
        f"_powered by @z5met & @z5meta_"
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
    msg = update.channel_post
    if msg is None:
        return
    if CHANNEL_ID and str(msg.chat.id) != str(CHANNEL_ID):
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
    try:
        count = files_col.count_documents({})
        by_type = list(files_col.aggregate([{"$group": {"_id": "$file_type", "n": {"$sum": 1}}}]))
    except PyMongoError:
        logger.exception("stats_cmd: MongoDB query failed")
        await update.message.reply_text(
            "⚠️ Couldn't reach the database just now — try /stats again in a moment."
        )
        return
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
        # Tags themselves contain colons (e.g. "search:<chat_id>:<hash>",
        # "type:<file_type>:<chat_id>"), so a fixed-count split mangles
        # them. The page number is always the last, numeric segment —
        # split it off from the right instead.
        rest = data[len("page:"):]
        tag, page_str = rest.rsplit(":", 1)
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

    # --- Send the actual file (password-gated if the file has one) ---
    if data.startswith("send:"):
        doc_id = data.split(":", 1)[1]
        doc = _find_by_id(doc_id)
        if doc is None:
            await query.answer()
            await context.bot.send_message(chat_id, "File not found (it may have been deleted).")
            return
        if doc.get("pw_hash"):
            await query.answer()
            context.user_data["awaiting_password_for"] = doc_id
            await context.bot.send_message(
                chat_id,
                f"🔒 *{escape_md(display_label(doc))}* is password-protected.\n"
                f"Send the password to receive it:",
                parse_mode="Markdown",
            )
            return
        await query.answer("Sending...")
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


async def _auto_delete_job(context: ContextTypes.DEFAULT_TYPE):
    """Deletes the file message + its warning message once the auto-delete
    delay for a large (>50MB) file has elapsed. Runs even if the chat has
    moved on — failures (e.g. the user already deleted it, or >48h old) are
    logged and swallowed rather than raised, since there's no one to report
    a job-queue error to."""
    chat_id, message_ids = context.job.data
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id, message_id)
        except Exception:
            logger.exception(
                "Auto-delete: failed to delete message %s in chat %s", message_id, chat_id
            )


async def _send_stored_file(context, chat_id, doc):
    label = display_label(doc)
    ftype = doc["file_type"]
    file_id = doc["file_id"]
    file_size = doc.get("file_size") or 0
    sent_message = None
    try:
        if ftype == "document":
            sent_message = await context.bot.send_document(chat_id, file_id, caption=f"📄 {label}")
        elif ftype == "video":
            sent_message = await context.bot.send_video(chat_id, file_id, caption=f"🎥 {label}")
        elif ftype == "audio":
            sent_message = await context.bot.send_audio(chat_id, file_id, caption=f"🎵 {label}")
        elif ftype == "voice":
            sent_message = await context.bot.send_voice(chat_id, file_id)
        elif ftype == "video_note":
            sent_message = await context.bot.send_video_note(chat_id, file_id)
        elif ftype == "photo":
            sent_message = await context.bot.send_photo(chat_id, file_id, caption=f"🖼 {label}")
    except Exception:
        logger.exception("Failed to resend file %s", doc.get("_id"))
        await context.bot.send_message(chat_id, f"⚠️ Couldn't resend: {label}")
        return

    if sent_message is None or file_size <= LARGE_FILE_BYTES:
        return

    # Large file (>50MB): warn the user and schedule both messages for
    # auto-deletion, since Telegram bots can only delete messages they
    # themselves sent (within 48h), never files already opened/downloaded
    # on the recipient's device.
    warning_message = await context.bot.send_message(
        chat_id,
        "⚠️ *This file is larger than 50MB.*\n"
        "It will be automatically deleted from this chat in *5 minutes*.\n\n"
        "Please forward it to another chat or save it elsewhere *before* "
        "opening it.",
        parse_mode="Markdown",
    )

    if context.job_queue is not None:
        context.job_queue.run_once(
            _auto_delete_job,
            AUTO_DELETE_SECONDS,
            data=(chat_id, [sent_message.message_id, warning_message.message_id]),
            name=f"autodelete:{chat_id}:{sent_message.message_id}",
        )
    else:
        logger.warning(
            "job_queue is unavailable — cannot auto-delete large file %s. "
            "Install the 'job-queue' extra: python-telegram-bot[job-queue]",
            doc.get("_id"),
        )


# ---------------------------------------------------------------------------
# Plain text router: password reply vs. rename reply vs. search query
# ---------------------------------------------------------------------------
# Tracks failed password attempts per (chat_id, doc_id) so a script can't
# brute-force a short password by hammering the bot. Reset on success or
# after MAX_PASSWORD_ATTEMPTS (the user has to re-tap Send file to retry).
_password_attempts = {}
MAX_PASSWORD_ATTEMPTS = 5


async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No owner check needed here: user_data is per-user, and
    # "awaiting_rename_for" / "awaiting_password_for" are only ever set
    # inside their respective callback branches above. A user's own
    # user_data can only ever contain keys their own button taps set.
    chat_id = update.effective_chat.id

    pending_pw_doc_id = context.user_data.get("awaiting_password_for")
    if pending_pw_doc_id:
        attempt_key = (chat_id, pending_pw_doc_id)
        attempts = _password_attempts.get(attempt_key, 0)
        if attempts >= MAX_PASSWORD_ATTEMPTS:
            context.user_data.pop("awaiting_password_for", None)
            _password_attempts.pop(attempt_key, None)
            await update.message.reply_text(
                "❌ Too many incorrect attempts. Tap Send file again to retry."
            )
            return

        submitted = update.message.text.strip()
        doc = _find_by_id(pending_pw_doc_id)
        if doc is None:
            context.user_data.pop("awaiting_password_for", None)
            _password_attempts.pop(attempt_key, None)
            await update.message.reply_text("That file no longer exists.")
            return

        salt_hex, digest_hex = doc.get("pw_salt"), doc.get("pw_hash")
        if salt_hex and digest_hex and verify_password(submitted, salt_hex, digest_hex):
            context.user_data.pop("awaiting_password_for", None)
            _password_attempts.pop(attempt_key, None)
            await update.message.reply_text("✅ Correct — sending...")
            await _send_stored_file(context, chat_id, doc)
        else:
            _password_attempts[attempt_key] = attempts + 1
            remaining = MAX_PASSWORD_ATTEMPTS - _password_attempts[attempt_key]
            await update.message.reply_text(
                f"❌ Incorrect password. {remaining} attempt(s) left."
                if remaining > 0 else "❌ Incorrect password."
            )
        return

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
# Global error handler
# ---------------------------------------------------------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catches any exception not already handled inside a specific handler,
    so failures show up in the logs (and, where possible, to the owner)
    instead of vanishing silently — e.g. a transient Mongo timeout that
    previously made a command just... not reply."""
    logger.exception("Unhandled exception while processing update: %s", update, exc_info=context.error)
    if OWNER_ID and isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                OWNER_ID, f"⚠️ Bot hit an error: {context.error!r}"
            )
        except Exception:
            pass  # never let error reporting itself crash the handler


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_app() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("browse", browse_cmd))
    application.add_handler(CommandHandler("recent", recent_cmd))

    application.add_handler(MessageHandler(filters.ChatType.CHANNEL, index_channel_post))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_text)
    )
    application.add_error_handler(on_error)

    return application


def main():
    application = build_app()

    # On Render (or any host with an expected HTTP port), you MUST run in
    # webhook mode: run_webhook() binds $PORT immediately, which is what
    # stops the platform from timing out the deploy. run_polling() never
    # binds a port, so if this falls through to polling on a Web Service,
    # Render waits for a port that never opens and kills the deploy.
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
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=full_webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        logger.info("Starting in polling mode (no WEBHOOK_URL / RENDER_EXTERNAL_URL set)")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
