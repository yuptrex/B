"""
Telegram File Indexer + Search Bot
----------------------------------
- Add this bot as an ADMIN to your channel.
- Every file posted to the channel (document, photo, video, audio, voice, video_note)
  gets auto-saved into MongoDB with its filename/caption + Telegram file_id.
- DM the bot any text -> it searches stored filenames/captions (case-insensitive,
  partial match) and sends matching files back to you.

Run modes:
- Polling (default): good for local/dev or a Render "Background Worker".
- Webhook: good for a Render "Web Service" (set WEBHOOK_URL env var).
"""

import logging
import os
import random
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, TEXT
from pymongo.errors import PyMongoError
from telegram import Update, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# The full set of standard (non-custom) emoji Telegram allows for message
# reactions via the Bot API's ReactionTypeEmoji. Bots can only use one of
# these fixed emoji per reaction (custom emoji reactions require the emoji
# to already be present on the message, or explicit admin permission).
ALL_REACTION_EMOJIS = [
    "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊️", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤️‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍️", "🤗", "🫡", "🎅", "🎄", "☃️", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂️",
    "🤷", "🤷‍♀️", "😡",
]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "tgfilebot")
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # optional: restrict indexing to this channel id (e.g. -1001234567890)
OWNER_ID = os.environ.get("OWNER_ID")  # optional: restrict who can search/receive files
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # if set, run in webhook mode
PORT = int(os.environ.get("PORT", "8080"))

# File filtering: keep this bot to small, non-video files only.
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "20"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
# file_type values that are never indexed regardless of size (movies/clips)
BLOCKED_FILE_TYPES = {"video", "video_note"}

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

# Indexes: fast lookup + text search fallback
files_col.create_index([("file_unique_id", ASCENDING)], unique=True)
files_col.create_index([("name", TEXT), ("caption", TEXT)], name="name_caption_text")


def save_file_record(*, file_id, file_unique_id, name, caption, file_type,
                      chat_id, message_id, file_size=None):
    """Upsert a file record. Safe to call repeatedly (unique on file_unique_id)."""
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


def search_files(query: str, limit: int = 10):
    """Partial, case-insensitive match against name OR caption."""
    query = query.strip()
    if not query:
        return []
    regex_filter = {
        "$or": [
            {"name": {"$regex": query, "$options": "i"}},
            {"caption": {"$regex": query, "$options": "i"}},
        ]
    }
    return list(files_col.find(regex_filter).sort("saved_at", -1).limit(limit))


# ---------------------------------------------------------------------------
# Reactions - big animated emoji burst on every user-originated message
# ---------------------------------------------------------------------------
async def react_random(message):
    """Set a random big animated-burst reaction on a user's message.
    is_big=True is what triggers Telegram's fullscreen burst animation on
    the sender's screen (same as a long-press reaction in the app). The Bot
    API only lets us pick the emoji; the animation itself is client-side."""
    try:
        emoji = random.choice(ALL_REACTION_EMOJIS)
        await message.set_reaction(reaction=emoji, is_big=True)
    except Exception:
        logger.exception("Failed to set reaction on message %s", getattr(message, "message_id", "?"))


# ---------------------------------------------------------------------------
# Access control helper
# ---------------------------------------------------------------------------
def is_authorized(update: Update) -> bool:
    if not OWNER_ID:
        return True  # no restriction configured
    return str(update.effective_user.id) == str(OWNER_ID)


# ---------------------------------------------------------------------------
# Handlers: indexing channel posts
# ---------------------------------------------------------------------------
async def index_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if msg is None:
        return

    if CHANNEL_ID and str(msg.chat.id) != str(CHANNEL_ID):
        return  # ignore other channels if restricted

    file_obj = None
    file_type = None
    name = None

    if msg.document:
        file_obj = msg.document
        file_type = "document"
        name = msg.document.file_name
    elif msg.video:
        file_obj = msg.video
        file_type = "video"
        name = msg.video.file_name
    elif msg.audio:
        file_obj = msg.audio
        file_type = "audio"
        name = msg.audio.file_name or msg.audio.title
    elif msg.voice:
        file_obj = msg.voice
        file_type = "voice"
        name = None
    elif msg.video_note:
        file_obj = msg.video_note
        file_type = "video_note"
        name = None
    elif msg.photo:
        # photo is a list of sizes; take the largest
        file_obj = msg.photo[-1]
        file_type = "photo"
        name = None

    if file_obj is None:
        return  # not a file post (e.g. plain text) - ignore

    # Strictly exclude movies/video content of any size.
    if file_type in BLOCKED_FILE_TYPES:
        logger.info("Skipped %s (%s): video content is not indexed", name or "(untitled)", file_type)
        return

    file_size = getattr(file_obj, "file_size", None)
    # Skip anything over the size cap. If Telegram didn't report a size,
    # err on the side of skipping rather than indexing something unverified.
    if file_size is None or file_size > MAX_FILE_SIZE_BYTES:
        logger.info(
            "Skipped %s (%s bytes): over %sMB limit or size unknown",
            name or "(untitled)", file_size, MAX_FILE_SIZE_MB,
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
# Handlers: user search in DM
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await react_random(update.message)
    await update.message.reply_text(
        "Send me part of a file name and I'll search the channel's indexed files "
        "and send back any matches."
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await react_random(update.message)
    count = files_col.count_documents({})
    await update.message.reply_text(f"Indexed files: {count}")


async def handle_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("You're not authorized to use this bot.")
        return

    await react_random(update.message)

    query = update.message.text
    results = search_files(query)

    if not results:
        await update.message.reply_text(f"No files found matching \"{query}\".")
        return

    await update.message.reply_text(f"Found {len(results)} match(es). Sending...")

    for doc in results:
        label = doc.get("name") or doc.get("caption") or "(untitled)"
        try:
            ftype = doc["file_type"]
            file_id = doc["file_id"]
            if ftype == "document":
                await context.bot.send_document(update.effective_chat.id, file_id, caption=label)
            elif ftype == "video":
                await context.bot.send_video(update.effective_chat.id, file_id, caption=label)
            elif ftype == "audio":
                await context.bot.send_audio(update.effective_chat.id, file_id, caption=label)
            elif ftype == "voice":
                await context.bot.send_voice(update.effective_chat.id, file_id)
            elif ftype == "video_note":
                await context.bot.send_video_note(update.effective_chat.id, file_id)
            elif ftype == "photo":
                await context.bot.send_photo(update.effective_chat.id, file_id, caption=label)
        except Exception:
            logger.exception("Failed to resend file %s", doc.get("_id"))
            await update.message.reply_text(f"Couldn't resend: {label}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_app() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))

    # Any file posted to a channel this bot administers
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL, index_channel_post))

    # Any private text message = a search query
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_search_text)
    )

    return application


def main():
    application = build_app()

    if WEBHOOK_URL:
        logger.info("Starting in webhook mode on port %s", PORT)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Starting in polling mode")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
