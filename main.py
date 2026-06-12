import pyrogram
from pyrogram import Client, filters, idle
from pyrogram.errors import (
    UserAlreadyParticipant, InviteHashExpired, FloodWait,
    PeerIdInvalid, SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    ChannelPrivate, UserNotParticipant, ChatForbidden,
    RPCError,
)
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

import asyncio
import os
import re
import time
import json
from datetime import datetime, timezone
from pymongo import MongoClient

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
bot_token = os.environ.get("TOKEN", "")
api_hash  = os.environ.get("HASH",  "")

try:
    api_id = int(os.environ.get("ID", 0))
except ValueError:
    api_id = 0

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except ValueError:
    ADMIN_ID = 0

MONGO_URI = os.environ.get("MONGO_URI", "")
USE_MONGO = bool(MONGO_URI)
print(f"✅ Using {'MongoDB' if USE_MONGO else 'JSON files'} for persistence.")

# ─────────────────────────────────────────────
# Database setup (short timeouts so the bot never hangs)
# ─────────────────────────────────────────────
if USE_MONGO:
    mongo_client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=5000,
        waitQueueTimeoutMS=5000,
    )
    db = mongo_client["telegram_bot_db"]
    sessions_col = db["sessions"]
    auth_col     = db["authorized_users"]
    settings_col = db["settings"]
    history_col  = db["history"]
    daily_usage_col = db["daily_usage"]
else:
    SESSION_FILE   = "sessions.json"
    AUTH_FILE      = "authorized_users.json"
    SETTINGS_FILE  = "user_settings.json"
    HISTORY_FILE   = "processed_history.json"
    DAILY_USAGE_FILE = "daily_usage.json"

bot = Client("mybot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

# ─────────────────────────────────────────────
# In‑memory caches
# ─────────────────────────────────────────────
USER_SESSIONS     : dict = {}
AUTHORIZED_USERS  : dict = {}
USER_SETTINGS     : dict = {}
PROCESSED_HISTORY : dict = {}
DAILY_PUBLIC_USAGE: dict = {}

USER_STATES    : dict = {}
ACTIVE_LOGINS  : dict = {}
CANCEL_BATCH   : dict = {}
RUNNING_CLIENTS: dict = {}

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
    "keywords":       [],
}

data_lock = asyncio.Lock()
MAX_DAILY_PUBLIC_LINKS = 5

class DownloadCancelled(Exception):
    pass

# ─────────────────────────────────────────────
# Async-safe persistence helpers
# ─────────────────────────────────────────────
async def _sync_to_async(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

async def _read_json(path):
    def _inner():
        with open(path, "r") as f:
            return json.load(f)
    return await asyncio.to_thread(_inner)

async def _write_json(path, data):
    def _inner():
        with open(path, "w") as f:
            json.dump(data, f, indent=4)
    await asyncio.to_thread(_inner)

async def load_all_data():
    global USER_SESSIONS, AUTHORIZED_USERS, USER_SETTINGS, PROCESSED_HISTORY, DAILY_PUBLIC_USAGE
    async with data_lock:
        if USE_MONGO:
            try:
                s = await _sync_to_async(sessions_col.find_one, {"_id": "sessions"})
                USER_SESSIONS = s.get("data", {}) if s else {}
            except Exception as e: print(f"⚠️ Could not load sessions: {e}")
            try:
                a = await _sync_to_async(auth_col.find_one, {"_id": "auth"})
                AUTHORIZED_USERS = a.get("data", {}) if a else {}
            except Exception as e: print(f"⚠️ Could not load authorized users: {e}")
            try:
                st = await _sync_to_async(settings_col.find_one, {"_id": "settings"})
                USER_SETTINGS = st.get("data", {}) if st else {}
            except Exception as e: print(f"⚠️ Could not load settings: {e}")
            try:
                h = await _sync_to_async(history_col.find_one, {"_id": "history"})
                PROCESSED_HISTORY = h.get("data", {}) if h else {}
            except Exception as e: print(f"⚠️ Could not load history: {e}")
            try:
                d = await _sync_to_async(daily_usage_col.find_one, {"_id": "daily_usage"})
                DAILY_PUBLIC_USAGE = d.get("data", {}) if d else {}
            except Exception as e: print(f"⚠️ Could not load daily usage: {e}")
        else:
            files = {
                SESSION_FILE: "USER_SESSIONS",
                AUTH_FILE: "AUTHORIZED_USERS",
                SETTINGS_FILE: "USER_SETTINGS",
                HISTORY_FILE: "PROCESSED_HISTORY",
                DAILY_USAGE_FILE: "DAILY_PUBLIC_USAGE",
            }
            for path, name in files.items():
                if os.path.exists(path):
                    try:
                        globals()[name] = await _read_json(path)
                    except Exception as e: print(f"⚠️ Could not load {path}: {e}")

# Save functions — no lock inside (caller holds data_lock)
async def save_sessions():
    if USE_MONGO:
        await _sync_to_async(sessions_col.update_one,
            {"_id": "sessions"}, {"$set": {"data": USER_SESSIONS}}, upsert=True)
    else:
        await _write_json(SESSION_FILE, USER_SESSIONS)

async def save_authorized_users():
    if USE_MONGO:
        await _sync_to_async(auth_col.update_one,
            {"_id": "auth"}, {"$set": {"data": AUTHORIZED_USERS}}, upsert=True)
    else:
        await _write_json(AUTH_FILE, AUTHORIZED_USERS)

async def save_user_settings():
    if USE_MONGO:
        await _sync_to_async(settings_col.update_one,
            {"_id": "settings"}, {"$set": {"data": USER_SETTINGS}}, upsert=True)
    else:
        await _write_json(SETTINGS_FILE, USER_SETTINGS)

async def save_history():
    if USE_MONGO:
        await _sync_to_async(history_col.update_one,
            {"_id": "history"}, {"$set": {"data": PROCESSED_HISTORY}}, upsert=True)
    else:
        await _write_json(HISTORY_FILE, PROCESSED_HISTORY)

async def save_daily_usage():
    if USE_MONGO:
        await _sync_to_async(daily_usage_col.update_one,
            {"_id": "daily_usage"}, {"$set": {"data": DAILY_PUBLIC_USAGE}}, upsert=True)
    else:
        await _write_json(DAILY_USAGE_FILE, DAILY_PUBLIC_USAGE)

# ─────────────────────────────────────────────
# Daily limit check
# ─────────────────────────────────────────────
def check_and_update_daily_limit(user_id: int) -> bool:
    uid_str = str(user_id)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if uid_str not in DAILY_PUBLIC_USAGE:
        DAILY_PUBLIC_USAGE[uid_str] = {"date": today_str, "count": 1}
        return True
    entry = DAILY_PUBLIC_USAGE[uid_str]
    if entry["date"] != today_str:
        entry["date"] = today_str
        entry["count"] = 1
        return True
    else:
        if entry["count"] < MAX_DAILY_PUBLIC_LINKS:
            entry["count"] += 1
            return True
        return False

# ─────────────────────────────────────────────
# User config helpers
# ─────────────────────────────────────────────
def init_user_config(uid_str: str) -> dict:
    if uid_str not in USER_SETTINGS:
        USER_SETTINGS[uid_str] = {
            "caption":     "",
            "use_caption": False,
            "thumb":       "",
            "use_thumb":   False,
            "filters":     DEFAULT_FILTERS.copy(),
        }
    else:
        if "filters" not in USER_SETTINGS[uid_str]:
            USER_SETTINGS[uid_str]["filters"] = DEFAULT_FILTERS.copy()
        else:
            for k, v in DEFAULT_FILTERS.items():
                USER_SETTINGS[uid_str]["filters"].setdefault(k, v)
    return USER_SETTINGS[uid_str]

# ─────────────────────────────────────────────
# Keyboard builders
# ─────────────────────────────────────────────
def get_main_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖊️ CAPTION",   callback_data="menu_caption"),
            InlineKeyboardButton("🖼️ THUMBNAIL",  callback_data="menu_thumb"),
        ],
        [InlineKeyboardButton("🧹 FILTERS",        callback_data="menu_filters_page1")],
        [InlineKeyboardButton("🗑️ Reset All Configuration Profiles", callback_data="clear_all")],
    ])

def get_filters_page1_keyboard(uid_str: str) -> InlineKeyboardMarkup:
    cfg = init_user_config(uid_str)["filters"]
    def si(k): return "✅" if cfg.get(k, False) else "❌"
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
        [
            InlineKeyboardButton("◀️ back",   callback_data="menu_main"),
            InlineKeyboardButton("next ▶️",   callback_data="menu_filters_page2"),
        ],
    ])

def get_filters_page2_keyboard(uid_str: str) -> InlineKeyboardMarkup:
    cfg      = init_user_config(uid_str)["filters"]
    dup_st   = "✅" if cfg.get("skip_duplicate", True)  else "❌"
    sec_st   = "✅" if cfg.get("secure_message", False) else "❌"
    size_lbl = f"🔩 Size limit ({cfg.get('size_limit', 0)} MB)"
    ext_lbl  = f"💾 Extensions ({len(cfg.get('extensions', []))} active)"
    kw_lbl   = f"🕵️ Keywords ({len(cfg.get('keywords', []))} saved)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Skip duplicate",  callback_data="f2_toggle:skip_duplicate"), InlineKeyboardButton(dup_st, callback_data="f2_toggle:skip_duplicate")],
        [InlineKeyboardButton("🔒 Secure message", callback_data="f2_toggle:secure_message"), InlineKeyboardButton(sec_st, callback_data="f2_toggle:secure_message")],
        [InlineKeyboardButton(size_lbl, callback_data="f2_set:size_limit"), InlineKeyboardButton(ext_lbl, callback_data="f2_set:extensions")],
        [InlineKeyboardButton(kw_lbl,   callback_data="f2_set:keywords")],
        [InlineKeyboardButton("◀️ back", callback_data="menu_filters_page1")],
    ])

def _build_caption_menu(config: dict):
    status = "🟢 ON" if config["use_caption"] else "🔴 OFF"
    text   = f"📝 **Caption Configuration**\nCurrent: `{config['caption'] or 'None'}`"
    kb     = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Toggle Caption Mode: {status}", callback_data="toggle_cap_mode")],
        [InlineKeyboardButton("📝 Set New Caption Text",        callback_data="prompt_set_cap")],
        [InlineKeyboardButton("◀️ Return to Settings",          callback_data="menu_main")],
    ])
    return text, kb

def _build_thumb_menu(config: dict):
    status = "🟢 ON" if config["use_thumb"] else "🔴 OFF"
    text   = "🖼️ **Custom Thumbnail Panel**"
    kb     = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Toggle Custom Thumb: {status}", callback_data="toggle_thumb_mode")],
        [InlineKeyboardButton("🖼️ Register New Thumbnail",      callback_data="prompt_set_thumb")],
        [InlineKeyboardButton("◀️ Return to Settings",           callback_data="menu_main")],
    ])
    return text, kb

# ─────────────────────────────────────────────
# Admin commands
# ─────────────────────────────────────────────
@bot.on_message(filters.command(["adduser"]) & filters.user(ADMIN_ID))
async def add_subscriber(_bot: Client, m: Message) -> None:
    if len(m.command) < 3:
        await m.reply_text("Usage: `/adduser <user_id> <duration>` (e.g. `30d`, `12h`, `60m`)")
        return
    target_id, dur_str = m.command[1], m.command[2].lower()
    if not target_id.isdigit():
        return
    match = re.match(r"^(\d+)([mhd])$", dur_str)
    if not match:
        await m.reply_text("❌ Duration format: `<number><m|h|d>` (e.g. `7d`, `24h`)")
        return
    value, unit  = int(match.group(1)), match.group(2)
    delta        = value * 60 if unit == "m" else (value * 3600 if unit == "h" else value * 86400)
    async with data_lock:
        AUTHORIZED_USERS[str(target_id)] = time.time() + delta
        await save_authorized_users()
    await m.reply_text(f"✅ Subscriber added: `{target_id}` for `{m.command[2]}`")

@bot.on_message(filters.command(["remuser"]) & filters.user(ADMIN_ID))
async def remove_subscriber(_bot: Client, m: Message) -> None:
    if len(m.command) < 2:
        return
    async with data_lock:
        AUTHORIZED_USERS.pop(m.command[1], None)
        await save_authorized_users()
    await m.reply_text("🗑️ Target privileges revoked.")

# ─────────────────────────────────────────────
# Callback query handler
# ─────────────────────────────────────────────
@bot.on_callback_query()
async def handle_settings_callbacks(client: Client, cb: CallbackQuery) -> None:
    # BUG FIX: Answer immediately to stop UI button spinning before processing DB tasks
    try:
        await cb.answer()
    except:
        pass

    uid_str = str(cb.from_user.id)
    data    = cb.data
    config  = init_user_config(uid_str)

    if data == "menu_main":
        await cb.message.edit_text("⚙️ **Main Custom Output Settings Panel**",
                                   reply_markup=get_main_settings_keyboard())
    elif data == "menu_caption":
        txt, kb = _build_caption_menu(config)
        await cb.message.edit_text(txt, reply_markup=kb)
    elif data == "toggle_cap_mode":
        async with data_lock:
            config["use_caption"] = not config["use_caption"]
            await save_user_settings()
        txt, kb = _build_caption_menu(config)
        await cb.message.edit_text(txt, reply_markup=kb)
    elif data == "prompt_set_cap":
        USER_STATES[uid_str] = "SETTING_CAPTION"
        await cb.message.reply_text("📝 Send your custom caption string:")
    elif data == "menu_thumb":
        txt, kb = _build_thumb_menu(config)
        await cb.message.edit_text(txt, reply_markup=kb)
    elif data == "toggle_thumb_mode":
        async with data_lock:
            config["use_thumb"] = not config["use_thumb"]
            await save_user_settings()
        txt, kb = _build_thumb_menu(config)
        await cb.message.edit_text(txt, reply_markup=kb)
    elif data == "prompt_set_thumb":
        USER_STATES[uid_str] = "SETTING_THUMBNAIL"
        await cb.message.reply_text("🖼️ Send a photo to use as the thumbnail:")
    elif data == "menu_filters_page1":
        await cb.message.edit_text("⭐ **Content Filtering — Page 1**",
                                   reply_markup=get_filters_page1_keyboard(uid_str))
    elif data == "menu_filters_page2":
        await cb.message.edit_text("⚙️ **Advanced Filtering — Page 2**",
                                   reply_markup=get_filters_page2_keyboard(uid_str))
    elif data.startswith("f1_toggle:") or data.startswith("f2_toggle:"):
        key = data.split(":")[1]
        async with data_lock:
            config["filters"][key] = not config["filters"].get(key, False)
            await save_user_settings()
        kb = get_filters_page1_keyboard(uid_str) if "f1_" in data else get_filters_page2_keyboard(uid_str)
        await cb.message.edit_reply_markup(reply_markup=kb)
    elif data.startswith("f2_set:"):
        target = data.split(":")[1]
        if target == "size_limit":
            USER_STATES[uid_str] = "SETTING_SIZE_LIMIT"
            await cb.message.reply_text("🔩 Enter max file size in MB (send `0` for unlimited):")
        elif target == "extensions":
            USER_STATES[uid_str] = "SETTING_EXTENSIONS"
            await cb.message.reply_text("💾 Send allowed extensions separated by spaces (e.g. `mp4 pdf`).\nSend `clear` to reset.")
        elif target == "keywords":
            USER_STATES[uid_str] = "SETTING_KEYWORDS"
            await cb.message.reply_text("🕵️ Send keywords separated by commas.\nSend `clear` to remove all.")
    elif data == "clear_all":
        async with data_lock:
            USER_SETTINGS[uid_str] = {
                "caption":     "",
                "use_caption": False,
                "thumb":       "",
                "use_thumb":   False,
                "filters":     DEFAULT_FILTERS.copy(),
            }
            await save_user_settings()
        await cb.message.edit_text("⚙️ Configurations cleared.", reply_markup=get_main_settings_keyboard())

# ─────────────────────────────────────────────
# Content filter
# ─────────────────────────────────────────────
def passed_content_filters(uid_str: str, msg: Message, context: str) -> bool:
    cfg = init_user_config(uid_str)["filters"]
    if cfg.get("skip_duplicate", True) and context in PROCESSED_HISTORY.get(uid_str, []):
        return False
    msg_text = (msg.text or msg.caption or "").lower()
    keywords = cfg.get("keywords", [])
    if keywords and not any(kw.strip().lower() in msg_text for kw in keywords if kw.strip()):
        return False
    
    is_text_only = not any([msg.document, msg.video, msg.photo, msg.audio,
                            msg.voice, msg.animation, msg.sticker, msg.poll])
    if is_text_only and not cfg.get("text", True):       return False
    if msg.document  and not cfg.get("document", True):  return False
    if msg.video     and not cfg.get("video", True):     return False
    if msg.photo     and not cfg.get("photo", True):     return False
    if msg.audio     and not cfg.get("audio", True):     return False
    if msg.voice     and not cfg.get("voice", True):     return False
    if msg.animation and not cfg.get("animation", True): return False
    if msg.sticker   and not cfg.get("sticker", True):   return False
    if msg.poll      and not cfg.get("poll", True):      return False
    
    # BUG FIX: Ensure photo logic doesn't crash extension/size limit filters
    media = msg.document or msg.video or msg.audio or msg.voice or msg.animation or msg.photo
    if media:
        if cfg.get("size_limit", 0) > 0 and (getattr(media, "file_size", 0) / 1_048_576) > cfg["size_limit"]:
            return False
        exts = cfg.get("extensions", [])
        if exts and not msg.photo: # Photos don't have distinct extensions like docs do
            fname = getattr(media, "file_name", "").lower()
            if not any(fname.endswith(f".{e.lower()}") for e in exts):
                return False
    return True

async def record_history(uid_str: str, context: str) -> None:
    async with data_lock:
        PROCESSED_HISTORY.setdefault(uid_str, [])
        if context not in PROCESSED_HISTORY[uid_str]:
            PROCESSED_HISTORY[uid_str].append(context)
            await save_history()

# ─────────────────────────────────────────────
# User client pool
# ─────────────────────────────────────────────
async def get_user_client(uid: int, user_data: dict) -> Client:
    if uid in RUNNING_CLIENTS:
        c = RUNNING_CLIENTS[uid]
        if c.is_connected:
            return c
        try:
            await c.start()
            return c
        except:
            RUNNING_CLIENTS.pop(uid, None)
    c = Client(
        name=f"run_{uid}",
        api_id=user_data["api_id"],
        api_hash=user_data["api_hash"],
        session_string=user_data["session_string"],
        in_memory=True,
    )
    await c.start()
    RUNNING_CLIENTS[uid] = c
    return c

# ─────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────
def get_readable_size(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024.0:
            return f"{b:.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"

def get_readable_time(s: float) -> str:
    if s < 60:
        return f"{int(s)}s"
    m = s / 60
    if m < 60:
        return f"{int(m)}m {int(s % 60)}s"
    return f"{int(m // 60)}h {int(m % 60)}m"

async def progress_cb(current, total, status_msg, action_text, ctx, uid) -> None:
    if CANCEL_BATCH.get(uid, False):
        raise DownloadCancelled()
    if not total:
        return
    now = time.time()
    if now - ctx.get("last_edit", 0.0) >= 3.5 or current == total:
        ctx["last_edit"] = now
        elapsed = max(now - ctx["start_time"], 0.01)
        speed   = current / elapsed
        pct     = current * 100 / total
        eta     = (total - current) / speed if speed > 0 else 0
        filled  = int(20 * pct / 100)
        bar     = f"[{'█' * filled}{'░' * (20 - filled)}] {pct:.1f}%"
        panel   = (
            f"**{action_text}**\n\n"
            f"📊 {bar}\n"
            f"📁 **Size:** {get_readable_size(current)} / {get_readable_size(total)}\n"
            f"⚡ **Speed:** {get_readable_size(speed)}/s\n"
            f"⏳ **ETA:** {get_readable_time(eta)}"
        )
        try:
            await status_msg.edit_text(panel)
        except:
            pass

# ─────────────────────────────────────────────
# Public commands (no subscription filter)
# ─────────────────────────────────────────────
@bot.on_message(filters.command(["start"]))
async def cmd_start(_bot: Client, m: Message) -> None:
    await m.reply_text(
        "**Welcome!**\n\n"
        "🔑 /login    — Link your Telegram account (for private channels)\n"
        "🚪 /logout   — Unlink your account\n"
        "📊 /status   — Account & daily usage info\n"
        "⚙️ /settings — Configure filters & output\n"
        "❌ /cancel   — Abort ongoing tasks\n\n"
        f"ℹ️ You can extract **{MAX_DAILY_PUBLIC_LINKS} public links per day**. Private channels require a linked account."
    )

@bot.on_message(filters.command(["login"]) & filters.private)
async def cmd_login(_bot: Client, m: Message) -> None:
    str_uid = str(m.from_user.id)
    if str_uid in USER_SESSIONS:
        await m.reply_text("✅ Already logged in. Use /logout to disconnect first.")
        return
    USER_STATES[str_uid] = "WAITING_API_ID"
    await m.reply_text("🔑 Send your **API ID** (numeric, from my.telegram.org/apps):")

@bot.on_message(filters.command(["logout"]) & filters.private)
async def cmd_logout(_bot: Client, m: Message) -> None:
    uid     = m.from_user.id
    str_uid = str(uid)
    if str_uid not in USER_SESSIONS:
        await m.reply_text("❌ You are not currently logged in.")
        return
    if uid in RUNNING_CLIENTS:
        try: await RUNNING_CLIENTS[uid].stop()
        except: pass
        RUNNING_CLIENTS.pop(uid, None)
    async with data_lock:
        USER_SESSIONS.pop(str_uid, None)
        await save_sessions()
    ACTIVE_LOGINS.pop(uid, None)
    USER_STATES.pop(str_uid, None)
    await m.reply_text("✅ Session removed successfully.")

@bot.on_message(filters.command(["status"]) & filters.private)
async def cmd_status(_bot: Client, m: Message) -> None:
    uid     = m.from_user.id
    str_uid = str(uid)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = DAILY_PUBLIC_USAGE.get(str_uid)
    if entry and entry["date"] == today_str:
        daily_count = entry["count"]
    else:
        daily_count = 0
    remaining = max(0, MAX_DAILY_PUBLIC_LINKS - daily_count)
    usage_msg = f"📆 Public links today: {daily_count}/{MAX_DAILY_PUBLIC_LINKS} (resets at 00:00 UTC)"
    session_ok = "✅ Linked" if str_uid in USER_SESSIONS else "❌ Not linked"
    await m.reply_text(f"📊 **Account Status**\n\n{usage_msg}\n🔹 Session: {session_ok}")

@bot.on_message(filters.command(["settings"]) & filters.private)
async def cmd_settings(_bot: Client, m: Message) -> None:
    init_user_config(str(m.from_user.id))
    await m.reply_text("⚙️ **Main Custom Output Settings Panel**",
                       reply_markup=get_main_settings_keyboard())

@bot.on_message(filters.command(["cancel"]))
async def cmd_cancel(_bot: Client, m: Message) -> None:
    uid = m.from_user.id if m.from_user else m.chat.id
    USER_STATES.pop(str(uid), None)
    CANCEL_BATCH[uid] = True
    await m.reply_text("🛑 All sequences stopped.")

# ─────────────────────────────────────────────
# Conversation & link dispatcher
# ─────────────────────────────────────────────
@bot.on_message((filters.text | filters.caption | filters.photo))
async def handle_text_inputs(client: Client, message: Message) -> None:
    uid     = message.from_user.id if message.from_user else message.chat.id
    str_uid = str(uid)
    text    = (message.text or message.caption or "").strip()

    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0]
        if cmd in ("/start", "/login", "/logout", "/cancel", "/adduser", "/remuser", "/status", "/settings"):
            return

    # State machine
    if str_uid in USER_STATES and message.chat.type == pyrogram.enums.ChatType.PRIVATE:
        state  = USER_STATES[str_uid]
        config = init_user_config(str_uid)

        if state == "SETTING_CAPTION":
            if text:
                async with data_lock:
                    config["caption"]     = text
                    config["use_caption"] = True
                    await save_user_settings()
                USER_STATES.pop(str_uid, None)
                await message.reply_text("✅ Caption saved.")
            return
        elif state == "SETTING_THUMBNAIL":
            if message.photo:
                async with data_lock:
                    config["thumb"]    = message.photo.file_id
                    config["use_thumb"] = True
                    await save_user_settings()
                USER_STATES.pop(str_uid, None)
                await message.reply_text("✅ Thumbnail saved.")
            else:
                await message.reply_text("⚠️ Please send a photo image.")
            return
        elif state == "SETTING_SIZE_LIMIT":
            try:
                size = int(text)
                if size < 0:
                    raise ValueError
                async with data_lock:
                    config["filters"]["size_limit"] = size
                    await save_user_settings()
                USER_STATES.pop(str_uid, None)
                label = "unlimited" if size == 0 else f"{size} MB"
                await message.reply_text(f"✅ Size limit set: `{label}`.")
            except ValueError:
                await message.reply_text("❌ Please send a non-negative number (e.g. `50`).")
            return
        elif state == "SETTING_EXTENSIONS":
            if text.lower() == "clear":
                async with data_lock:
                    config["filters"]["extensions"] = []
                    await save_user_settings()
            else:
                async with data_lock:
                    config["filters"]["extensions"] = [
                        e.strip().lower().lstrip(".") for e in text.split() if e.strip()
                    ]
                    await save_user_settings()
            USER_STATES.pop(str_uid, None)
            await message.reply_text("✅ Extensions updated.")
            return
        elif state == "SETTING_KEYWORDS":
            if text.lower() == "clear":
                async with data_lock:
                    config["filters"]["keywords"] = []
                    await save_user_settings()
            else:
                async with data_lock:
                    config["filters"]["keywords"] = [
                        k.strip() for k in text.split(",") if k.strip()
                    ]
                    await save_user_settings()
            USER_STATES.pop(str_uid, None)
            await message.reply_text("✅ Keywords updated.")
            return

        # Login states
        if state == "WAITING_API_ID":
            if not text.isdigit():
                await message.reply_text("❌ API ID must be a number. Try again:")
                return
            ACTIVE_LOGINS[uid] = {"api_id": int(text)}
            USER_STATES[str_uid] = "WAITING_API_HASH"
            await message.reply_text("⚙️ Send your **API HASH**:")
            return
        elif state == "WAITING_API_HASH":
            if uid not in ACTIVE_LOGINS:
                USER_STATES.pop(str_uid, None)
                await message.reply_text("❌ Session expired. Use /login again.")
                return
            ACTIVE_LOGINS[uid]["api_hash"] = text
            USER_STATES[str_uid] = "WAITING_PHONE"
            await message.reply_text("📱 Send your **Phone Number** (with country code, e.g. +12345678900):")
            return
        elif state == "WAITING_PHONE":
            if uid not in ACTIVE_LOGINS:
                USER_STATES.pop(str_uid, None)
                await message.reply_text("❌ Session expired. Use /login again.")
                return
            ACTIVE_LOGINS[uid]["phone"] = text
            temp_client = None
            try:
                temp_client = Client(
                    name=f"login_{uid}",
                    api_id=ACTIVE_LOGINS[uid]["api_id"],
                    api_hash=ACTIVE_LOGINS[uid]["api_hash"],
                    in_memory=True,
                )
                await temp_client.connect()
                code_info = await temp_client.send_code(text)
                ACTIVE_LOGINS[uid]["client"]          = temp_client
                ACTIVE_LOGINS[uid]["phone_code_hash"] = code_info.phone_code_hash
                USER_STATES[str_uid] = "WAITING_OTP"
                await message.reply_text("📩 Send the **OTP code** (spaces are fine):")
            except Exception as exc:
                if temp_client:
                    try: await temp_client.disconnect()
                    except: pass
                ACTIVE_LOGINS.pop(uid, None)
                USER_STATES.pop(str_uid, None)
                await message.reply_text(f"❌ Connection error: `{exc}`")
            return
        elif state == "WAITING_OTP":
            login_data = ACTIVE_LOGINS.get(uid)
            if not login_data:
                USER_STATES.pop(str_uid, None)
                await message.reply_text("❌ Session expired. Use /login again.")
                return
            try:
                await login_data["client"].sign_in(
                    phone_number=login_data["phone"],
                    phone_code_hash=login_data["phone_code_hash"],
                    phone_code=text.replace(" ", ""),
                )
                session_str = await login_data["client"].export_session_string()
                async with data_lock:
                    USER_SESSIONS[str_uid] = {
                        "api_id":         login_data["api_id"],
                        "api_hash":       login_data["api_hash"],
                        "session_string": session_str,
                    }
                    await save_sessions()
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                await message.reply_text("🎉 **Login Successful!**")
            except SessionPasswordNeeded:
                USER_STATES[str_uid] = "WAITING_2FA"
                await message.reply_text("🔐 Enter your **2FA Password**:")
            except (PhoneCodeInvalid, PhoneCodeExpired) as exc:
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                await message.reply_text(f"❌ Invalid / expired OTP: `{exc}`\nUse /login to try again.")
            except Exception as exc:
                await message.reply_text(f"❌ Error: `{exc}`")
            return
        elif state == "WAITING_2FA":
            login_data = ACTIVE_LOGINS.get(uid)
            if not login_data:
                USER_STATES.pop(str_uid, None)
                await message.reply_text("❌ Session expired. Use /login again.")
                return
            try:
                await login_data["client"].check_password(password=text)
                session_str = await login_data["client"].export_session_string()
                async with data_lock:
                    USER_SESSIONS[str_uid] = {
                        "api_id":         login_data["api_id"],
                        "api_hash":       login_data["api_hash"],
                        "session_string": session_str,
                    }
                    await save_sessions()
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                await message.reply_text("🎉 **2FA Login Successful!**")
            except Exception as exc:
                await message.reply_text(f"❌ Error: `{exc}`")
            return

    if not text:
        return

    # Join links
    if "https://t.me/+" in text or "https://t.me/joinchat/" in text:
        if str_uid not in USER_SESSIONS:
            await message.reply_text("❌ You need to /login first to join chats.")
            return
        try:
            acc = await get_user_client(uid, USER_SESSIONS[str_uid])
            await acc.join_chat(text)
            await bot.send_message(message.chat.id, "✅ Joined chat.", reply_to_message_id=message.id)
        except Exception as exc:
            await bot.send_message(message.chat.id, f"❌ Join failed: `{exc}`")
        return

    CANCEL_BATCH[uid] = False

    range_match = re.match(r"(https://t\.me/(?:c/)?[^/\s]+)(?:/\d+)?/(\d+)\s*-\s*(\d+)$", text)
    if range_match:
        base  = range_match.group(1)
        start = int(range_match.group(2))
        end   = int(range_match.group(3))
        if end < start:
            return
        for msg_id in range(start, end + 1):
            if CANCEL_BATCH.get(uid, False):
                break
            await process_single_link(f"{base}/{msg_id}", message, uid=uid)
            await asyncio.sleep(2)
        return

    links = re.findall(r"https://t\.me/(?:c/)?[^/\s]+(?:\/\d+)?/\d+", text)
    for link in links:
        if CANCEL_BATCH.get(uid, False):
            break
        await process_single_link(link, message, uid=uid)
        await asyncio.sleep(2)

# ─────────────────────────────────────────────
# Core extraction loop
# ─────────────────────────────────────────────
async def process_single_link(link: str, original_msg: Message, uid: int = 0) -> None:
    parts    = link.split("/")
    msgid    = int(parts[-1])
    str_uid  = str(uid)
    user_cfg = init_user_config(str_uid)
    frules   = user_cfg["filters"]

    async def reply(txt: str):
        return await bot.send_message(original_msg.chat.id, txt, reply_to_message_id=original_msg.id)

    is_private = "https://t.me/c/" in link

    if not is_private:
        allowed = False
        async with data_lock:
            allowed = check_and_update_daily_limit(uid)
            if allowed:
                await save_daily_usage()
        if not allowed:
            await reply(f"❌ **Daily limit reached!**\nYou can only extract {MAX_DAILY_PUBLIC_LINKS} public links per day.\nLimit resets at midnight UTC.")
            return

    while True:
        if CANCEL_BATCH.get(uid, False):
            await reply("🛑 Extraction cancelled.")
            return

        file = thumb = pub_thumb = status_msg = None

        try:
            if is_private:
                if str_uid not in USER_SESSIONS:
                    await reply("❌ You need to /login to access private channels.")
                    return
                chatid   = int("-100" + parts[4])
                user_acc = await get_user_client(uid, USER_SESSIONS[str_uid])
                msg      = await user_acc.get_messages(chatid, msgid)
                if not msg:
                    break
                if not passed_content_filters(str_uid, msg, f"{chatid}_{msgid}"):
                    break

                if frules.get("forward_tag", False):
                    try:
                        await user_acc.forward_messages(original_msg.chat.id, chatid, msgid)
                        await record_history(str_uid, f"{chatid}_{msgid}")
                        break
                    except FloodWait as exc:
                        raise exc
                    except:
                        pass

                has_media      = any([msg.document, msg.video, msg.animation,
                                      msg.sticker, msg.voice, msg.audio, msg.photo])
                final_cap      = user_cfg["caption"] if user_cfg["use_caption"] else (msg.caption or "")
                final_entities = None if user_cfg["use_caption"] else (msg.caption_entities or msg.entities)

                if not has_media:
                    txt_out = user_cfg["caption"] if user_cfg["use_caption"] else (msg.text or msg.caption)
                    if txt_out:
                        await bot.send_message(original_msg.chat.id, txt_out,
                                               entities=final_entities,
                                               reply_to_message_id=original_msg.id)
                        await record_history(str_uid, f"{chatid}_{msgid}")
                    break

                status_msg = await reply("📥 Fetching content…")
                dl_ctx = {"start_time": time.time(), "last_edit": 0.0}
                up_ctx = {"start_time": time.time(), "last_edit": 0.0}

                file = await user_acc.download_media(
                    msg, progress=progress_cb,
                    progress_args=(status_msg, "📥 Downloading", dl_ctx, uid),
                )

                if user_cfg["use_thumb"] and user_cfg["thumb"]:
                    try: thumb = await bot.download_media(user_cfg["thumb"])
                    except: pass
                else:
                    holder = msg.document or msg.video
                    if holder and getattr(holder, "thumbs", None):
                        try: thumb = await user_acc.download_media(holder.thumbs[0].file_id)
                        except: pass

                if msg.document:
                    await bot.send_document(
                        original_msg.chat.id, file, thumb=thumb,
                        caption=final_cap, caption_entities=final_entities,
                        reply_to_message_id=original_msg.id,
                        progress=progress_cb,
                        progress_args=(status_msg, "📤 Uploading Document", up_ctx, uid),
                    )
                elif msg.video:
                    await bot.send_video(
                        original_msg.chat.id, file,
                        duration=msg.video.duration, width=msg.video.width, height=msg.video.height,
                        thumb=thumb, caption=final_cap, caption_entities=final_entities,
                        reply_to_message_id=original_msg.id,
                        progress=progress_cb,
                        progress_args=(status_msg, "📤 Uploading Video", up_ctx, uid),
                    )
                elif msg.photo:
                    await bot.send_photo(original_msg.chat.id, file,
                                         caption=final_cap, caption_entities=final_entities,
                                         reply_to_message_id=original_msg.id)
                elif msg.audio:
                    await bot.send_audio(original_msg.chat.id, file,
                                         caption=final_cap, caption_entities=final_entities,
                                         reply_to_message_id=original_msg.id)
                elif msg.voice:
                    await bot.send_voice(original_msg.chat.id, file,
                                         caption=final_cap, caption_entities=final_entities,
                                         reply_to_message_id=original_msg.id)
                elif msg.animation:
                    await bot.send_animation(original_msg.chat.id, file,
                                             caption=final_cap, caption_entities=final_entities,
                                             reply_to_message_id=original_msg.id)
                elif msg.sticker:
                    await bot.send_sticker(original_msg.chat.id, file,
                                           reply_to_message_id=original_msg.id)

                await record_history(str_uid, f"{chatid}_{msgid}")
                break

            else:   # public
                username = parts[-2]
                msg = await bot.get_messages(username, msgid)
                if not msg:
                    break
                if not passed_content_filters(str_uid, msg, f"{username}_{msgid}"):
                    break

                if frules.get("forward_tag", False):
                    await bot.forward_messages(original_msg.chat.id, username, msgid)
                    await record_history(str_uid, f"{username}_{msgid}")
                    break

                final_cap      = user_cfg["caption"] if user_cfg["use_caption"] else (msg.caption or "")
                final_entities = None if user_cfg["use_caption"] else (msg.caption_entities or msg.entities)

                if user_cfg["use_thumb"] and user_cfg["thumb"]:
                    try: pub_thumb = await bot.download_media(user_cfg["thumb"])
                    except: pass

                if msg.document:
                    if pub_thumb:
                        file = await bot.download_media(msg)
                        await bot.send_document(original_msg.chat.id, file, thumb=pub_thumb,
                                                caption=final_cap, caption_entities=final_entities,
                                                reply_to_message_id=original_msg.id)
                    else:
                        await bot.send_document(original_msg.chat.id, msg.document.file_id,
                                                caption=final_cap, caption_entities=final_entities,
                                                reply_to_message_id=original_msg.id)
                elif msg.video:
                    if pub_thumb:
                        file = await bot.download_media(msg)
                        await bot.send_video(original_msg.chat.id, file, thumb=pub_thumb,
                                             caption=final_cap, caption_entities=final_entities,
                                             reply_to_message_id=original_msg.id)
                    else:
                        await bot.send_video(original_msg.chat.id, msg.video.file_id,
                                             caption=final_cap, caption_entities=final_entities,
                                             reply_to_message_id=original_msg.id)
                elif msg.photo:
                    await bot.send_photo(original_msg.chat.id, msg.photo.file_id,
                                         caption=final_cap, caption_entities=final_entities,
                                         reply_to_message_id=original_msg.id)
                elif msg.audio:
                    await bot.send_audio(original_msg.chat.id, msg.audio.file_id,
                                         caption=final_cap, caption_entities=final_entities,
                                         reply_to_message_id=original_msg.id)
                elif msg.voice:
                    await bot.send_voice(original_msg.chat.id, msg.voice.file_id,
                                         caption=final_cap, reply_to_message_id=original_msg.id)
                elif msg.animation:
                    await bot.send_animation(original_msg.chat.id, msg.animation.file_id,
                                             caption=final_cap, caption_entities=final_entities,
                                             reply_to_message_id=original_msg.id)
                elif msg.sticker:
                    await bot.send_sticker(original_msg.chat.id, msg.sticker.file_id,
                                           reply_to_message_id=original_msg.id)
                elif msg.text:
                    send_text = user_cfg["caption"] if user_cfg["use_caption"] else msg.text
                    await bot.send_message(original_msg.chat.id, send_text,
                                           entities=final_entities, reply_to_message_id=original_msg.id)

                await record_history(str_uid, f"{username}_{msgid}")
                break

        except DownloadCancelled:
            await reply("🛑 Download cancelled.")
            break
        except FloodWait as exc:
            sleep_time = exc.value + 2
            await reply(f"⏳ **Rate Limit (FloodWait)**\nPausing `{sleep_time}s` then retrying…")
            await asyncio.sleep(sleep_time)
            continue
        except Exception as exc:
            await reply(f"⚠️ Extraction error: `{exc}`")
            break
        finally:
            for f in (file, thumb, pub_thumb):
                if f and os.path.exists(f):
                    try: os.remove(f)
                    except: pass
            if status_msg:
                try: await status_msg.delete()
                except: pass

# ─────────────────────────────────────────────
# Entry point (hard timeout on data loading)
# ─────────────────────────────────────────────
async def main():
    try:
        await asyncio.wait_for(load_all_data(), timeout=10)
    except asyncio.TimeoutError:
        print("⚠️ MongoDB loading timed out – starting with empty caches.")
    except Exception as e:
        print(f"❌ Data load error: {e}")

    try:
        await bot.start()
        print("✅ Bot is running…")
    except Exception as e:
        print(f"❌ FATAL — Bot failed to start: {e}")
        return

    # BUG FIX: Use idle() instead of Event().wait() for graceful shutdown
    await idle()
    await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
