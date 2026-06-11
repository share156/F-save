import pyrogram
from pyrogram import Client, filters
from pyrogram.errors import (
    UserAlreadyParticipant, InviteHashExpired, FloodWait,
    PeerIdInvalid, SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    ChannelPrivate, UserNotParticipant, ChatForbidden,
    RPCError
)
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

import asyncio
import time
import os
import re
from datetime import datetime
from pymongo import MongoClient

# --------------- Configuration ---------------
bot_token = os.environ.get("TOKEN", "")
api_hash  = os.environ.get("HASH", "")

try:
    api_id = int(os.environ.get("ID", 0))
except ValueError:
    api_id = 0

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except ValueError:
    ADMIN_ID = 0

bot = Client("mybot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

# --------------- Data Persistence Layers (MongoDB) ---------------
MONGO_URI = os.environ.get("MONGO_URI", "")

if MONGO_URI:
    mongo_client = MongoClient(MONGO_URI)
    db           = mongo_client["telegram_bot_db"]
    sessions_col = db["sessions"]
    auth_col     = db["authorized_users"]
    settings_col = db["settings"]
    history_col  = db["history"]
else:
    print("⚠️ WARNING: MONGO_URI is missing. Database will not work properly.")
    mongo_client = None
    sessions_col = auth_col = settings_col = history_col = None

USER_SESSIONS    = {}
AUTHORIZED_USERS = {}
USER_SETTINGS    = {}
PROCESSED_HISTORY = {}

USER_STATES    = {}
ACTIVE_LOGINS  = {}
CANCEL_BATCH   = {}
RUNNING_CLIENTS = {}

# Async lock for shared data + MongoDB writes
data_lock = asyncio.Lock()

DEFAULT_FILTERS = {
    "forward_tag":    False,
    "text":           True,
    "document":       True,
    "video":          True,
    "photo":          True,
    "audio":          True,
    "voice":          True,
    "animation":      True,
    "sticker":        True,
    "poll":           True,
    "skip_duplicate": True,
    "secure_message": False,
    "size_limit":     0,
    "extensions":     [],
    "keywords":       []
}

class DownloadCancelled(Exception):
    """Raised when user cancels a download/upload via progress callback."""
    pass

# --------------- Async-safe MongoDB helpers ---------------
async def _sync_to_async(func, *args, **kwargs):
    """Run a synchronous pymongo call in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)

async def load_data():
    global USER_SESSIONS, AUTHORIZED_USERS, USER_SETTINGS, PROCESSED_HISTORY
    if not MONGO_URI:
        return
    async with data_lock:
        try:
            s_doc = await _sync_to_async(sessions_col.find_one, {"_id": "sessions"})
            USER_SESSIONS = s_doc.get("data", {}) if s_doc else {}
        except Exception as e:
            print(f"Error loading sessions: {e}")
        try:
            a_doc = await _sync_to_async(auth_col.find_one, {"_id": "auth"})
            AUTHORIZED_USERS = a_doc.get("data", {}) if a_doc else {}
        except Exception as e:
            print(f"Error loading authorized users: {e}")
        try:
            set_doc = await _sync_to_async(settings_col.find_one, {"_id": "settings"})
            USER_SETTINGS = set_doc.get("data", {}) if set_doc else {}
        except Exception as e:
            print(f"Error loading settings: {e}")
        try:
            h_doc = await _sync_to_async(history_col.find_one, {"_id": "history"})
            PROCESSED_HISTORY = h_doc.get("data", {}) if h_doc else {}
        except Exception as e:
            print(f"Error loading history: {e}")

async def save_sessions():
    if MONGO_URI:
        async with data_lock:
            await _sync_to_async(
                sessions_col.update_one,
                {"_id": "sessions"},
                {"$set": {"data": USER_SESSIONS}},
                upsert=True
            )

async def save_authorized_users():
    if MONGO_URI:
        async with data_lock:
            await _sync_to_async(
                auth_col.update_one,
                {"_id": "auth"},
                {"$set": {"data": AUTHORIZED_USERS}},
                upsert=True
            )

async def save_user_settings():
    if MONGO_URI:
        async with data_lock:
            await _sync_to_async(
                settings_col.update_one,
                {"_id": "settings"},
                {"$set": {"data": USER_SETTINGS}},
                upsert=True
            )

async def save_history():
    if MONGO_URI:
        async with data_lock:
            await _sync_to_async(
                history_col.update_one,
                {"_id": "history"},
                {"$set": {"data": PROCESSED_HISTORY}},
                upsert=True
            )

# --------------- Subscription Protection Filter ---------------
async def is_subscribed(_, __, message: Message):
    if message.from_user and message.from_user.id == ADMIN_ID:
        return True

    user_id   = message.from_user.id if message.from_user else message.chat.id
    str_uid   = str(user_id)
    current_time = time.time()

    async with data_lock:
        if str_uid in AUTHORIZED_USERS:
            expiry_time = AUTHORIZED_USERS[str_uid]
            if current_time < expiry_time:
                return True
            else:
                AUTHORIZED_USERS.pop(str_uid, None)
                await save_authorized_users()
                if user_id in RUNNING_CLIENTS:
                    try: await RUNNING_CLIENTS[user_id].stop()
                    except: pass
                    RUNNING_CLIENTS.pop(user_id, None)

    try: await message.reply_text("⚠️ **Access Denied / Expired**\nPlease contact the Administrator.")
    except: pass
    return False

subscribed_only = filters.create(is_subscribed)

# --------------- Menu/Keyboard Helpers ---------------
def init_user_config(uid_str):
    if uid_str not in USER_SETTINGS:
        USER_SETTINGS[uid_str] = {
            "caption": "", "use_caption": False,
            "thumb": "",   "use_thumb": False,
            "filters": DEFAULT_FILTERS.copy()
        }
    else:
        if "filters" not in USER_SETTINGS[uid_str]:
            USER_SETTINGS[uid_str]["filters"] = DEFAULT_FILTERS.copy()
        else:
            for key, val in DEFAULT_FILTERS.items():
                if key not in USER_SETTINGS[uid_str]["filters"]:
                    USER_SETTINGS[uid_str]["filters"][key] = val
    return USER_SETTINGS[uid_str]

def get_main_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖊️ CAPTION",  callback_data="menu_caption"),
         InlineKeyboardButton("🖼️ THUMBNAIL", callback_data="menu_thumb")],
        [InlineKeyboardButton("🧹 FILTERS",   callback_data="menu_filters_page1")],
        [InlineKeyboardButton("🗑️ Reset All Profiles", callback_data="clear_all")]
    ])

def get_filters_page1_keyboard(uid_str):
    config = init_user_config(uid_str)["filters"]
    def si(key): return "✅" if config.get(key, False) else "❌"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷️ Forward tag", callback_data="f1_toggle:forward_tag"), InlineKeyboardButton(si("forward_tag"), callback_data="f1_toggle:forward_tag")],
        [InlineKeyboardButton("🖊️ Text",        callback_data="f1_toggle:text"),        InlineKeyboardButton(si("text"),        callback_data="f1_toggle:text")],
        [InlineKeyboardButton("📁 Document",    callback_data="f1_toggle:document"),    InlineKeyboardButton(si("document"),    callback_data="f1_toggle:document")],
        [InlineKeyboardButton("📦 Video",       callback_data="f1_toggle:video"),       InlineKeyboardButton(si("video"),       callback_data="f1_toggle:video")],
        [InlineKeyboardButton("📷 Photo",       callback_data="f1_toggle:photo"),       InlineKeyboardButton(si("photo"),       callback_data="f1_toggle:photo")],
        [InlineKeyboardButton("🎧 Audio",       callback_data="f1_toggle:audio"),       InlineKeyboardButton(si("audio"),       callback_data="f1_toggle:audio")],
        [InlineKeyboardButton("🎤 Voice",       callback_data="f1_toggle:voice"),       InlineKeyboardButton(si("voice"),       callback_data="f1_toggle:voice")],
        [InlineKeyboardButton("🎭 Animation",   callback_data="f1_toggle:animation"),   InlineKeyboardButton(si("animation"),   callback_data="f1_toggle:animation")],
        [InlineKeyboardButton("🃏 Sticker",     callback_data="f1_toggle:sticker"),     InlineKeyboardButton(si("sticker"),     callback_data="f1_toggle:sticker")],
        [InlineKeyboardButton("📊 Poll",        callback_data="f1_toggle:poll"),        InlineKeyboardButton(si("poll"),        callback_data="f1_toggle:poll")],
        [InlineKeyboardButton("◀️ back", callback_data="menu_main"), InlineKeyboardButton("next ▶️", callback_data="menu_filters_page2")]
    ])

def get_filters_page2_keyboard(uid_str):
    config    = init_user_config(uid_str)["filters"]
    dup_st    = "✅" if config.get("skip_duplicate", True)  else "❌"
    sec_st    = "✅" if config.get("secure_message", False) else "❌"
    size_lbl  = f"🔩 size limit ({config.get('size_limit', 0)}MB)"
    ext_lbl   = f"💾 Extension ({len(config.get('extensions', []))} active)"
    kw_lbl    = f"🕵️ keywords ({len(config.get('keywords', []))} saved)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Skip duplicate",  callback_data="f2_toggle:skip_duplicate"), InlineKeyboardButton(dup_st, callback_data="f2_toggle:skip_duplicate")],
        [InlineKeyboardButton("🔒 Secure message", callback_data="f2_toggle:secure_message"), InlineKeyboardButton(sec_st, callback_data="f2_toggle:secure_message")],
        [InlineKeyboardButton(size_lbl, callback_data="f2_set:size_limit"), InlineKeyboardButton(ext_lbl, callback_data="f2_set:extensions")],
        [InlineKeyboardButton(kw_lbl,   callback_data="f2_set:keywords")],
        [InlineKeyboardButton("◀️ back", callback_data="menu_filters_page1")]
    ])

def _build_caption_menu(config):
    cap_status = "🟢 ON" if config["use_caption"] else "🔴 OFF"
    text = f"📝 **Caption Configuration**\nCurrent: `{config['caption'] or 'None'}`"
    kb   = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Toggle Caption Mode: {cap_status}", callback_data="toggle_cap_mode")],
        [InlineKeyboardButton("📝 Provide New Caption Text String",  callback_data="prompt_set_cap")],
        [InlineKeyboardButton("◀️ Return to Settings",               callback_data="menu_main")]
    ])
    return text, kb

def _build_thumb_menu(config):
    thumb_status = "🟢 ON" if config["use_thumb"] else "🔴 OFF"
    text = "🖼️ **Custom Thumbnail Panels**"
    kb   = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Toggle Custom Thumb: {thumb_status}", callback_data="toggle_thumb_mode")],
        [InlineKeyboardButton("🖼️ Register Custom Image",             callback_data="prompt_set_thumb")],
        [InlineKeyboardButton("◀️ Return to Settings",                callback_data="menu_main")]
    ])
    return text, kb

# --------------- Admin Commands ---------------
@bot.on_message(filters.command(["adduser"]))
async def add_subscriber(bot: Client, m: Message):
    if m.from_user.id != ADMIN_ID: return
    if len(m.command) < 3: return
    target_id, duration_str = m.command[1], m.command[2].lower()
    if not target_id.isdigit(): return
    match = re.match(r"^(\d+)([mhd])$", duration_str)
    if not match: return
    value, unit    = int(match.group(1)), match.group(2)
    seconds_delta  = value * 60 if unit == 'm' else (value * 3600 if unit == 'h' else value * 86400)
    async with data_lock:
        AUTHORIZED_USERS[str(target_id)] = time.time() + seconds_delta
        await save_authorized_users()
    await m.reply_text(f"✅ Subscriber Account Added: `{target_id}`")

@bot.on_message(filters.command(["remuser"]))
async def remove_subscriber(bot: Client, m: Message):
    if m.from_user.id != ADMIN_ID: return
    if len(m.command) < 2: return
    async with data_lock:
        AUTHORIZED_USERS.pop(m.command[1], None)
        await save_authorized_users()
    await m.reply_text("🗑️ Target privileges revoked.")

# --------------- Interactive Callback Queries Engine ---------------
@bot.on_callback_query()
async def handle_settings_callbacks(client: Client, cb: CallbackQuery):
    uid_str = str(cb.from_user.id)
    data    = cb.data
    config  = init_user_config(uid_str)

    if data == "menu_main":
        await cb.message.edit_text("⚙️ **Main Custom Output Settings Panel**",
                                   reply_markup=get_main_settings_keyboard())
    elif data == "menu_caption":
        text, kb = _build_caption_menu(config)
        await cb.message.edit_text(text, reply_markup=kb)
    elif data == "toggle_cap_mode":
        config["use_caption"] = not config["use_caption"]
        await save_user_settings()
        text, kb = _build_caption_menu(config)
        await cb.message.edit_text(text, reply_markup=kb)
    elif data == "prompt_set_cap":
        USER_STATES[uid_str] = "SETTING_CAPTION"
        await cb.message.reply_text("📝 Send your custom caption string below:")
    elif data == "menu_thumb":
        text, kb = _build_thumb_menu(config)
        await cb.message.edit_text(text, reply_markup=kb)
    elif data == "toggle_thumb_mode":
        config["use_thumb"] = not config["use_thumb"]
        await save_user_settings()
        text, kb = _build_thumb_menu(config)
        await cb.message.edit_text(text, reply_markup=kb)
    elif data == "prompt_set_thumb":
        USER_STATES[uid_str] = "SETTING_THUMBNAIL"
        await cb.message.reply_text("🖼️ Send a photo file to use as the thumbnail:")
    elif data == "menu_filters_page1":
        await cb.message.edit_text("⭐ **Content Filtering (Page 1)**",
                                   reply_markup=get_filters_page1_keyboard(uid_str))
    elif data == "menu_filters_page2":
        await cb.message.edit_text("⚙️ **Advanced Filtering (Page 2)**",
                                   reply_markup=get_filters_page2_keyboard(uid_str))
    elif data.startswith("f1_toggle:") or data.startswith("f2_toggle:"):
        key = data.split(":")[1]
        config["filters"][key] = not config["filters"].get(key, False)
        await save_user_settings()
        kb = get_filters_page1_keyboard(uid_str) if "f1_" in data else get_filters_page2_keyboard(uid_str)
        await cb.message.edit_reply_markup(reply_markup=kb)
    elif data.startswith("f2_set:"):
        target = data.split(":")[1]
        if target == "size_limit":
            USER_STATES[uid_str] = "SETTING_SIZE_LIMIT"
            await cb.message.reply_text("🔩 Enter max allowed file size in MB (Send `0` for unlimited):")
        elif target == "extensions":
            USER_STATES[uid_str] = "SETTING_EXTENSIONS"
            await cb.message.reply_text("💾 Send allowed extensions separated by spaces (e.g. `mp4 pdf`). Send `clear` to reset:")
        elif target == "keywords":
            USER_STATES[uid_str] = "SETTING_KEYWORDS"
            await cb.message.reply_text("🕵️ Send keywords separated by commas. Send `clear` to remove all:")
    elif data == "clear_all":
        async with data_lock:
            USER_SETTINGS[uid_str] = {
                "caption": "", "use_caption": False,
                "thumb": "",   "use_thumb": False,
                "filters": DEFAULT_FILTERS.copy()
            }
            await save_user_settings()
        await cb.message.edit_text("⚙️ Configurations cleared.", reply_markup=get_main_settings_keyboard())

    try: await cb.answer()
    except: pass

# --------------- Content Extraction Validation ---------------
def passed_content_filters(uid_str, msg: Message, chat_link_context: str) -> bool:
    config = init_user_config(uid_str)["filters"]

    if config.get("skip_duplicate", True):
        if chat_link_context in PROCESSED_HISTORY.get(uid_str, []):
            return False

    msg_text = (msg.text or msg.caption or "").lower()
    keywords = config.get("keywords", [])
    if keywords:
        if not any(kw.strip().lower() in msg_text for kw in keywords if kw.strip()):
            return False

    is_text_only = not any([msg.document, msg.video, msg.photo, msg.audio,
                            msg.voice, msg.animation, msg.sticker, msg.poll])
    if is_text_only and not config.get("text", True):       return False
    if msg.document  and not config.get("document", True):  return False
    if msg.video     and not config.get("video", True):     return False
    if msg.photo     and not config.get("photo", True):     return False
    if msg.audio     and not config.get("audio", True):     return False
    if msg.voice     and not config.get("voice", True):     return False
    if msg.animation and not config.get("animation", True): return False
    if msg.sticker   and not config.get("sticker", True):   return False
    if msg.poll      and not config.get("poll", True):      return False

    media_obj = msg.document or msg.video or msg.audio or msg.voice or msg.animation
    if media_obj:
        size_limit_mb = config.get("size_limit", 0)
        if size_limit_mb > 0:
            if (getattr(media_obj, "file_size", 0) / (1024 * 1024)) > size_limit_mb:
                return False
        allowed_exts = config.get("extensions", [])
        if allowed_exts:
            file_name = getattr(media_obj, "file_name", "").lower()
            if not any(file_name.endswith(f".{ext.lower()}") for ext in allowed_exts):
                return False

    return True

async def record_processed_history(uid_str, chat_link_context):
    async with data_lock:
        if uid_str not in PROCESSED_HISTORY:
            PROCESSED_HISTORY[uid_str] = []
        if chat_link_context not in PROCESSED_HISTORY[uid_str]:
            PROCESSED_HISTORY[uid_str].append(chat_link_context)
            await save_history()

# --------------- Account Persistence Helpers ---------------
async def get_user_client(uid, user_data):
    if uid in RUNNING_CLIENTS:
        client = RUNNING_CLIENTS[uid]
        if client.is_connected:
            return client
        try:
            await client.start()
            return client
        except:
            RUNNING_CLIENTS.pop(uid, None)
    client = Client(
        name=f"run_{uid}",
        api_id=user_data["api_id"],
        api_hash=user_data["api_hash"],
        session_string=user_data["session_string"],
        in_memory=True
    )
    await client.start()
    RUNNING_CLIENTS[uid] = client
    return client

def get_readable_size(bytes_count):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_count < 1024.0:
            return f"{bytes_count:.2f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.2f} PB"

def get_readable_time(seconds):
    if seconds < 60: return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60: return f"{int(minutes)}m {int(seconds % 60)}s"
    return f"{int(minutes // 60)}h {int(minutes % 60)}m"

async def progress_cb(current, total, status_message, action_text, tracking_ctx, uid):
    if CANCEL_BATCH.get(uid, False):
        raise DownloadCancelled()
    if not total:
        return
    now = time.time()
    if now - tracking_ctx.get("last_edit", 0.0) >= 3.5 or current == total:
        tracking_ctx["last_edit"] = now
        elapsed_time = max(now - tracking_ctx["start_time"], 0.01)
        speed_bps    = current / elapsed_time
        percent      = current * 100 / total
        eta_seconds  = (total - current) / speed_bps if speed_bps > 0 else 0
        filled       = int(20 * percent / 100)
        bar          = f"[{'█' * filled}{'░' * (20 - filled)}] {percent:.1f}%"
        panel = (
            f"**{action_text}**\n\n"
            f"📊 {bar}\n"
            f"📁 **Size:** {get_readable_size(current)} / {get_readable_size(total)}\n"
            f"⚡ **Speed:** {get_readable_size(speed_bps)}/s\n"
            f"⏳ **ETA:** {get_readable_time(eta_seconds)}"
        )
        try: await status_message.edit_text(panel)
        except: pass

# --------------- Base Commands ---------------
@bot.on_message(filters.command(["start"]) & subscribed_only)
async def start(bot: Client, m: Message):
    await m.reply_text(
        "**Welcome!**\n\n"
        "Commands:\n"
        "🔑 /login   — Link your Telegram account\n"
        "🚪 /logout  — Unlink your account\n"
        "📊 /status  — Check login & subscription info\n"
        "⚙ /settings — Configure filters & output\n"
        "❌ /cancel  — Abort ongoing tasks"
    )

@bot.on_message(filters.command(["login"]) & filters.private & subscribed_only)
async def login_cmd(bot: Client, m: Message):
    str_uid = str(m.from_user.id)
    if str_uid in USER_SESSIONS:
        await m.reply_text("✅ Already logged in. Use /logout to disconnect first.")
        return
    USER_STATES[str_uid] = "WAITING_API_ID"
    await m.reply_text("🔑 Send your **API ID** (from my.telegram.org/apps):")

@bot.on_message(filters.command(["logout"]) & filters.private & subscribed_only)
async def logout_cmd(bot: Client, m: Message):
    user_id = m.from_user.id
    str_uid = str(user_id)
    if str_uid not in USER_SESSIONS:
        await m.reply_text("❌ You are not logged in.")
        return
    if user_id in RUNNING_CLIENTS:
        try: await RUNNING_CLIENTS[user_id].stop()
        except: pass
        RUNNING_CLIENTS.pop(user_id, None)
    async with data_lock:
        USER_SESSIONS.pop(str_uid, None)
        await save_sessions()
    ACTIVE_LOGINS.pop(user_id, None)
    USER_STATES.pop(str_uid, None)
    await m.reply_text("✅ Logged out successfully.")

@bot.on_message(filters.command(["status"]) & filters.private & subscribed_only)
async def status_cmd(bot: Client, m: Message):
    user_id = m.from_user.id
    str_uid = str(user_id)
    logged_in = "✅ Logged In" if str_uid in USER_SESSIONS else "❌ Not Logged In"
    if user_id == ADMIN_ID:
        sub_str = "♾️ Admin (Unlimited)"
    else:
        expiry = AUTHORIZED_USERS.get(str_uid)
        if expiry:
            remaining = max(0, int(expiry - time.time()))
            sub_str = f"✅ Active ({remaining // 3600}h {(remaining % 3600) // 60}m remaining)"
        else:
            sub_str = "❌ Not Authorized"
    await m.reply_text(f"📊 **Account Status**\n\n👤 Account: {logged_in}\n🔐 Subscription: {sub_str}")

@bot.on_message(filters.command(["settings"]) & filters.private & subscribed_only)
async def open_settings(bot: Client, m: Message):
    uid_str = str(m.from_user.id)
    init_user_config(uid_str)
    await m.reply_text("⚙️ **Main Custom Output Settings Panel**",
                       reply_markup=get_main_settings_keyboard())

@bot.on_message(filters.command(["cancel"]) & subscribed_only)
async def cancel_cmd(bot: Client, m: Message):
    user_id = m.from_user.id if m.from_user else m.chat.id
    USER_STATES.pop(str(user_id), None)
    CANCEL_BATCH[user_id] = True
    await m.reply_text("🛑 Sequences stopped.")

# --------------- Conversation & Extraction Engine ---------------
@bot.on_message((filters.text | filters.caption | filters.photo) & subscribed_only)
async def handle_text_inputs(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    str_uid = str(user_id)
    text    = (message.text or message.caption or "").strip()

    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0]
        if cmd in ["/start", "/login", "/logout", "/cancel",
                   "/adduser", "/remuser", "/status", "/settings"]:
            return

    if str_uid in USER_STATES and message.chat.type == pyrogram.enums.ChatType.PRIVATE:
        state  = USER_STATES[str_uid]
        config = init_user_config(str_uid)

        if state == "SETTING_CAPTION":
            if text:
                config["caption"]     = text
                config["use_caption"] = True
                await save_user_settings()
                USER_STATES.pop(str_uid, None)
                await message.reply_text("✅ Custom caption saved.")
            return

        elif state == "SETTING_THUMBNAIL":
            if message.photo:
                config["thumb"]     = message.photo.file_id
                config["use_thumb"] = True
                await save_user_settings()
                USER_STATES.pop(str_uid, None)
                await message.reply_text("✅ Thumbnail saved.")
            else:
                await message.reply_text("⚠️ Please send a photo image for the thumbnail.")
            return

        elif state == "SETTING_SIZE_LIMIT":
            try:
                size = int(text)
                if size < 0:
                    raise ValueError
                config["filters"]["size_limit"] = size
                await save_user_settings()
                USER_STATES.pop(str_uid, None)
                label = "unlimited" if size == 0 else f"{size}MB max"
                await message.reply_text(f"✅ Size limit set to `{label}`.")
            except ValueError:
                await message.reply_text("❌ Invalid. Please send a non-negative whole number (e.g. `50`).")
            return

        elif state == "SETTING_EXTENSIONS":
            if text.lower() == "clear":
                config["filters"]["extensions"] = []
                await save_user_settings()
                USER_STATES.pop(str_uid, None)
                await message.reply_text("✅ Extension filter cleared.")
            else:
                exts = [e.strip().lower().lstrip(".") for e in text.split() if e.strip()]
                config["filters"]["extensions"] = exts
                await save_user_settings()
                USER_STATES.pop(str_uid, None)
                await message.reply_text(f"✅ Extensions saved: `{', '.join(exts)}`.")
            return

        elif state == "SETTING_KEYWORDS":
            if text.lower() == "clear":
                config["filters"]["keywords"] = []
                await save_user_settings()
                USER_STATES.pop(str_uid, None)
                await message.reply_text("✅ Keywords cleared.")
            else:
                kws = [kw.strip() for kw in text.split(",") if kw.strip()]
                config["filters"]["keywords"] = kws
                await save_user_settings()
                USER_STATES.pop(str_uid, None)
                await message.reply_text(f"✅ Keywords saved: `{', '.join(kws)}`.")
            return

        # Login states
        if state == "WAITING_API_ID":
            if not text.isdigit():
                await message.reply_text("❌ API ID must be a number. Try again:")
                return
            ACTIVE_LOGINS[user_id] = {"api_id": int(text)}
            USER_STATES[str_uid]   = "WAITING_API_HASH"
            await message.reply_text("⚙️ Send your **API HASH**:")
            return

        elif state == "WAITING_API_HASH":
            if user_id not in ACTIVE_LOGINS:
                USER_STATES.pop(str_uid, None)
                await message.reply_text("❌ Session expired. Please use /login again.")
                return
            ACTIVE_LOGINS[user_id]["api_hash"] = text
            USER_STATES[str_uid] = "WAITING_PHONE"
            await message.reply_text("📱 Send your **Phone Number** (with country code, e.g. +12345678900):")
            return

        elif state == "WAITING_PHONE":
            if user_id not in ACTIVE_LOGINS:
                USER_STATES.pop(str_uid, None)
                await message.reply_text("❌ Session expired. Please use /login again.")
                return
            ACTIVE_LOGINS[user_id]["phone"] = text
            temp_client = None
            try:
                temp_client = Client(
                    name=f"login_{user_id}",
                    api_id=ACTIVE_LOGINS[user_id]["api_id"],
                    api_hash=ACTIVE_LOGINS[user_id]["api_hash"],
                    in_memory=True
                )
                await temp_client.connect()
                code_info = await temp_client.send_code(text)
                ACTIVE_LOGINS[user_id]["client"]          = temp_client
                ACTIVE_LOGINS[user_id]["phone_code_hash"] = code_info.phone_code_hash
                USER_STATES[str_uid] = "WAITING_OTP"
                await message.reply_text("📩 Send the **OTP Code** (spaces are fine):")
            except Exception as e:
                if temp_client:
                    try: await temp_client.disconnect()
                    except: pass
                ACTIVE_LOGINS.pop(user_id, None)
                USER_STATES.pop(str_uid, None)
                await message.reply_text(f"❌ Connection error: `{e}`")
            return

        elif state == "WAITING_OTP":
            login_data = ACTIVE_LOGINS.get(user_id)
            if not login_data:
                USER_STATES.pop(str_uid, None)
                await message.reply_text("❌ Session expired. Please use /login again.")
                return
            try:
                await login_data["client"].sign_in(
                    phone_number=login_data["phone"],
                    phone_code_hash=login_data["phone_code_hash"],
                    phone_code=text.replace(" ", "")
                )
                session_str = await login_data["client"].export_session_string()
                async with data_lock:
                    USER_SESSIONS[str_uid] = {
                        "api_id":         login_data["api_id"],
                        "api_hash":       login_data["api_hash"],
                        "session_string": session_str
                    }
                    await save_sessions()
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(user_id, None)
                await message.reply_text("🎉 **Login Successful!**")
            except SessionPasswordNeeded:
                USER_STATES[str_uid] = "WAITING_2FA"
                await message.reply_text("🔐 Enter your **2FA Password**:")
            except (PhoneCodeInvalid, PhoneCodeExpired) as e:
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(user_id, None)
                await message.reply_text(f"❌ Invalid/Expired OTP: `{e}`\nPlease use /login to try again.")
            except Exception as e:
                await message.reply_text(f"❌ Error: `{e}`")
            return

        elif state == "WAITING_2FA":
            login_data = ACTIVE_LOGINS.get(user_id)
            if not login_data:
                USER_STATES.pop(str_uid, None)
                await message.reply_text("❌ Session expired. Please use /login again.")
                return
            try:
                await login_data["client"].check_password(password=text)
                session_str = await login_data["client"].export_session_string()
                async with data_lock:
                    USER_SESSIONS[str_uid] = {
                        "api_id":         login_data["api_id"],
                        "api_hash":       login_data["api_hash"],
                        "session_string": session_str
                    }
                    await save_sessions()
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(user_id, None)
                await message.reply_text("🎉 **2FA Login Successful!**")
            except Exception as e:
                await message.reply_text(f"❌ Error: `{e}`")
            return

    if not text:
        return

    # Batch Processors
    if "https://t.me/+" in text or "https://t.me/joinchat/" in text:
        if str_uid not in USER_SESSIONS:
            return
        try:
            user_acc = await get_user_client(user_id, USER_SESSIONS[str_uid])
            await user_acc.join_chat(text)
            await bot.send_message(message.chat.id, "✅ Joined chat.", reply_to_message_id=message.id)
        except Exception as e:
            await bot.send_message(message.chat.id, f"❌ Join Failed: `{e}`")
        return

    CANCEL_BATCH[user_id] = False
    range_match = re.match(
        r'(https://t\.me/(?:c/)?[^/\s]+)(?:/\d+)?/(\d+)\s*-\s*(\d+)$', text
    )
    if range_match:
        base_link = range_match.group(1)
        start_id  = int(range_match.group(2))
        end_id    = int(range_match.group(3))
        if end_id < start_id:
            return
        for msg_id in range(start_id, end_id + 1):
            if CANCEL_BATCH.get(user_id, False):
                break
            await process_single_link(f"{base_link}/{msg_id}", message, uid=user_id)
            await asyncio.sleep(2)
        return

    links = re.findall(r'https://t\.me/(?:c/)?[^/\s]+(?:\/\d+)?/\d+', text)
    for link in links:
        if CANCEL_BATCH.get(user_id, False):
            break
        await process_single_link(link, message, uid=user_id)
        await asyncio.sleep(2)

# --------------- FloodWait Resilient Engine Loop ---------------
async def process_single_link(link, original_msg, uid=0):
    CANCEL_BATCH[uid] = False

    datas       = link.split("/")
    msgid       = int(datas[-1])
    str_uid     = str(uid)
    user_config = init_user_config(str_uid)
    filter_rules = user_config["filters"]

    async def reply(text):
        return await bot.send_message(original_msg.chat.id, text,
                                      reply_to_message_id=original_msg.id)

    while True:
        if CANCEL_BATCH.get(uid, False):
            await reply("🛑 Extraction cancelled.")
            return

        file = thumb = status_msg = None

        try:
            # ---- Private channel (t.me/c/...) ----
            if "https://t.me/c/" in link:
                if str_uid not in USER_SESSIONS:
                    return
                chatid   = int("-100" + datas[4])
                user_acc = await get_user_client(uid, USER_SESSIONS[str_uid])
                msg = await user_acc.get_messages(chatid, msgid)
                if not msg:
                    break
                if not passed_content_filters(str_uid, msg, f"{chatid}_{msgid}"):
                    break

                if filter_rules.get("forward_tag", False):
                    try:
                        await user_acc.forward_messages(original_msg.chat.id, chatid, msgid)
                        await record_processed_history(str_uid, f"{chatid}_{msgid}")
                        break
                    except FloodWait as e:
                        raise e
                    except:
                        pass

                has_media = any([msg.document, msg.video, msg.animation, msg.sticker,
                                 msg.voice, msg.audio, msg.photo])
                final_caption  = user_config["caption"] if user_config["use_caption"] else (msg.caption or "")
                final_entities = None if user_config["use_caption"] else (msg.caption_entities or msg.entities)

                if not has_media:
                    text_to_send = user_config["caption"] if user_config["use_caption"] else (msg.text or msg.caption)
                    if text_to_send:
                        await bot.send_message(original_msg.chat.id, text_to_send,
                                               entities=final_entities,
                                               reply_to_message_id=original_msg.id)
                        await record_processed_history(str_uid, f"{chatid}_{msgid}")
                    break

                status_msg   = await reply("📥 Fetching encrypted storage payloads...")
                download_ctx = {"start_time": time.time(), "last_edit": 0.0}
                upload_ctx   = {"start_time": time.time(), "last_edit": 0.0}

                file = await user_acc.download_media(
                    msg, progress=progress_cb,
                    progress_args=(status_msg, "📥 Processing Download", download_ctx, uid)
                )

                if user_config["use_thumb"] and user_config["thumb"]:
                    try: thumb = await bot.download_media(user_config["thumb"])
                    except: pass

                if msg.document:
                    await bot.send_document(
                        original_msg.chat.id, file, thumb=thumb,
                        caption=final_caption, caption_entities=final_entities,
                        reply_to_message_id=original_msg.id,
                        progress=progress_cb,
                        progress_args=(status_msg, "📤 Uploading Document", upload_ctx, uid)
                    )
                elif msg.video:
                    await bot.send_video(
                        original_msg.chat.id, file,
                        duration=msg.video.duration, width=msg.video.width, height=msg.video.height,
                        thumb=thumb, caption=final_caption, caption_entities=final_entities,
                        reply_to_message_id=original_msg.id,
                        progress=progress_cb,
                        progress_args=(status_msg, "📤 Uploading Video", upload_ctx, uid)
                    )
                elif msg.audio:
                    await bot.send_audio(
                        original_msg.chat.id, file,
                        caption=final_caption, caption_entities=final_entities,
                        reply_to_message_id=original_msg.id
                    )
                elif msg.voice:
                    await bot.send_voice(
                        original_msg.chat.id, file,
                        caption=final_caption, caption_entities=final_entities,
                        reply_to_message_id=original_msg.id
                    )
                elif msg.animation:
                    await bot.send_animation(
                        original_msg.chat.id, file,
                        caption=final_caption, caption_entities=final_entities,
                        reply_to_message_id=original_msg.id
                    )
                elif msg.sticker:
                    await bot.send_sticker(original_msg.chat.id, file,
                                           reply_to_message_id=original_msg.id)
                elif msg.photo:
                    await bot.send_photo(
                        original_msg.chat.id, file,
                        caption=final_caption, caption_entities=final_entities,
                        reply_to_message_id=original_msg.id
                    )

                await record_processed_history(str_uid, f"{chatid}_{msgid}")
                break

            # ---- Public channel (t.me/username/...) ----
            else:
                username = datas[-2]
                msg = await bot.get_messages(username, msgid)
                if not msg:
                    break
                if not passed_content_filters(str_uid, msg, f"{username}_{msgid}"):
                    break

                if filter_rules.get("forward_tag", False):
                    await bot.forward_messages(original_msg.chat.id, username, msgid)
                    await record_processed_history(str_uid, f"{username}_{msgid}")
                    break

                final_caption  = user_config["caption"] if user_config["use_caption"] else (msg.caption or "")
                final_entities = None if user_config["use_caption"] else (msg.caption_entities or msg.entities)

                if msg.document:
                    await bot.send_document(original_msg.chat.id, msg.document.file_id,
                                            caption=final_caption, caption_entities=final_entities,
                                            reply_to_message_id=original_msg.id)
                elif msg.video:
                    await bot.send_video(original_msg.chat.id, msg.video.file_id,
                                         caption=final_caption, caption_entities=final_entities,
                                         reply_to_message_id=original_msg.id)
                elif msg.audio:
                    await bot.send_audio(original_msg.chat.id, msg.audio.file_id,
                                         caption=final_caption, caption_entities=final_entities,
                                         reply_to_message_id=original_msg.id)
                elif msg.voice:
                    await bot.send_voice(original_msg.chat.id, msg.voice.file_id,
                                         caption=final_caption, caption_entities=final_entities,
                                         reply_to_message_id=original_msg.id)
                elif msg.animation:
                    await bot.send_animation(original_msg.chat.id, msg.animation.file_id,
                                             caption=final_caption, caption_entities=final_entities,
                                             reply_to_message_id=original_msg.id)
                elif msg.sticker:
                    await bot.send_sticker(original_msg.chat.id, msg.sticker.file_id,
                                           reply_to_message_id=original_msg.id)
                elif msg.photo:
                    await bot.send_photo(original_msg.chat.id, msg.photo.file_id,
                                         caption=final_caption, caption_entities=final_entities,
                                         reply_to_message_id=original_msg.id)
                elif msg.text:
                    send_text = user_config["caption"] if user_config["use_caption"] else msg.text
                    await bot.send_message(original_msg.chat.id, send_text,
                                           entities=final_entities,
                                           reply_to_message_id=original_msg.id)

                await record_processed_history(str_uid, f"{username}_{msgid}")
                break

        except DownloadCancelled:
            await reply("🛑 Download cancelled.")
            break
        except FloodWait as e:
            sleep_time = e.value + 2
            await reply(f"⏳ **Rate Limit (FloodWait)**\nPausing `{sleep_time}s` then retrying...")
            await asyncio.sleep(sleep_time)
            continue
        except Exception as e:
            await reply(f"⚠️ Extraction Error: `{e}`")
            break
        finally:
            for f in (file, thumb):
                if f and os.path.exists(f):
                    try: os.remove(f)
                    except: pass
            if status_msg:
                try: await status_msg.delete()
                except: pass

# --------------- Start Execution ---------------
if __name__ == "__main__":
    bot.run()
