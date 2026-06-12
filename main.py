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
import json
from datetime import datetime

# --------------- Configuration ---------------
bot_token = os.environ.get("TOKEN", "")
api_hash = os.environ.get("HASH", "")  
api_id = os.environ.get("ID", "")      

# ⚠️ REPLACE WITH YOUR ACTUAL NUMERIC TELEGRAM USER ID
ADMIN_ID = 123456789  

bot = Client("mybot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

# --------------- Data Persistence Layers ---------------
SESSION_FILE = "sessions.json"
AUTH_FILE = "authorized_users.json"
SETTINGS_FILE = "user_settings.json"
HISTORY_FILE = "processed_history.json"

USER_SESSIONS = {}
AUTHORIZED_USERS = {}  
USER_SETTINGS = {}     
PROCESSED_HISTORY = {} 

USER_STATES = {}       
ACTIVE_LOGINS = {}     
CANCEL_BATCH = {}      
RUNNING_CLIENTS = {}   

DEFAULT_FILTERS = {
    "forward_tag": False,
    "text": True,
    "document": True,
    "video": True,
    "photo": True,
    "audio": True,
    "voice": True,
    "animation": True,
    "sticker": True,
    "poll": True,
    "skip_duplicate": True,
    "secure_message": False,
    "size_limit": 0,        
    "extensions": [],       
    "keywords": []          
}

def load_data():
    global USER_SESSIONS, AUTHORIZED_USERS, USER_SETTINGS, PROCESSED_HISTORY
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f: USER_SESSIONS = json.load(f)
        except: USER_SESSIONS = {}
            
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r") as f: AUTHORIZED_USERS = json.load(f)
        except: AUTHORIZED_USERS = {}

    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f: USER_SETTINGS = json.load(f)
        except: USER_SETTINGS = {}

    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f: PROCESSED_HISTORY = json.load(f)
        except: PROCESSED_HISTORY = {}

def save_sessions():
    try:
        with open(SESSION_FILE, "w") as f: json.dump(USER_SESSIONS, f, indent=4)
    except Exception as e: print(f"Error saving sessions: {e}")

def save_authorized_users():
    try:
        with open(AUTH_FILE, "w") as f: json.dump(AUTHORIZED_USERS, f, indent=4)
    except Exception as e: print(f"Error saving authorized users: {e}")

def save_user_settings():
    try:
        with open(SETTINGS_FILE, "w") as f: json.dump(USER_SETTINGS, f, indent=4)
    except Exception as e: print(f"Error saving user settings: {e}")

def save_history():
    try:
        with open(HISTORY_FILE, "w") as f: json.dump(PROCESSED_HISTORY, f, indent=4)
    except Exception as e: print(f"Error saving history profile: {e}")

load_data()

# --------------- Subscription Protection Filter ---------------
async def is_subscribed(_, __, message: Message):
    if message.from_user and message.from_user.id == ADMIN_ID:
        return True
    
    user_id = message.from_user.id if message.from_user else message.chat.id
    str_uid = str(user_id)
    current_time = time.time()
    
    if str_uid in AUTHORIZED_USERS:
        expiry_time = AUTHORIZED_USERS[str_uid]
        if current_time < expiry_time:
            return True
        else:
            AUTHORIZED_USERS.pop(str_uid, None)
            save_authorized_users()
            if user_id in RUNNING_CLIENTS:
                try: await RUNNING_CLIENTS[user_id].stop()
                except: pass
                RUNNING_CLIENTS.pop(user_id, None)

    try:
        await message.reply_text("⚠️ **Access Denied / Expired**\nPlease contact the Administrator.")
    except:
        pass 
    return False

subscribed_only = filters.create(is_subscribed)


# --------------- Interactive Menu Keyboards UI Generators ---------------
def init_user_config(uid_str):
    if uid_str not in USER_SETTINGS:
        USER_SETTINGS[uid_str] = {
            "caption": "", "use_caption": False, 
            "thumb": "", "use_thumb": False,
            "filters": DEFAULT_FILTERS.copy()
        }
    else:
        if "filters" not in USER_SETTINGS[uid_str]:
            USER_SETTINGS[uid_str]["filters"] = DEFAULT_FILTERS.copy()
        else:
            for key, val in DEFAULT_FILTERS.items():
                if key not in USER_SETTINGS[uid_str]["filters"]:
                    USER_SETTINGS[uid_str]["filters"][key] = val
    save_user_settings()
    return USER_SETTINGS[uid_str]

def get_main_settings_keyboard():
    buttons = [
        [InlineKeyboardButton("🖊️ CAPTION", callback_data="menu_caption"), InlineKeyboardButton("🖼️ THUMBNAIL", callback_data="menu_thumb")],
        [InlineKeyboardButton("🧹 FILTERS", callback_data="menu_filters_page1")],
        [InlineKeyboardButton("🗑️ Reset All Configuration Profiles", callback_data="clear_all")]
    ]
    return InlineKeyboardMarkup(buttons)

def get_filters_page1_keyboard(uid_str):
    config = init_user_config(uid_str)["filters"]
    def status_icon(key): return "✅" if config.get(key, False) else "❌"
    buttons = [
        [InlineKeyboardButton("🏷️ Forward tag", callback_data="f1_toggle:forward_tag"), InlineKeyboardButton(status_icon("forward_tag"), callback_data="f1_toggle:forward_tag")],
        [InlineKeyboardButton("🖊️ Text", callback_data="f1_toggle:text"), InlineKeyboardButton(status_icon("text"), callback_data="f1_toggle:text")],
        [InlineKeyboardButton("📁 Document", callback_data="f1_toggle:document"), InlineKeyboardButton(status_icon("document"), callback_data="f1_toggle:document")],
        [InlineKeyboardButton("📦 Video", callback_data="f1_toggle:video"), InlineKeyboardButton(status_icon("video"), callback_data="f1_toggle:video")],
        [InlineKeyboardButton("📷 Photo", callback_data="f1_toggle:photo"), InlineKeyboardButton(status_icon("photo"), callback_data="f1_toggle:photo")],
        [InlineKeyboardButton("🎧 Audio", callback_data="f1_toggle:audio"), InlineKeyboardButton(status_icon("audio"), callback_data="f1_toggle:audio")],
        [InlineKeyboardButton("🎤 Voice", callback_data="f1_toggle:voice"), InlineKeyboardButton(status_icon("voice"), callback_data="f1_toggle:voice")],
        [InlineKeyboardButton("🎭 Animation", callback_data="f1_toggle:animation"), InlineKeyboardButton(status_icon("animation"), callback_data="f1_toggle:animation")],
        [InlineKeyboardButton("🃏 Sticker", callback_data="f1_toggle:sticker"), InlineKeyboardButton(status_icon("sticker"), callback_data="f1_toggle:sticker")],
        [InlineKeyboardButton("📊 Poll", callback_data="f1_toggle:poll"), InlineKeyboardButton(status_icon("poll"), callback_data="f1_toggle:poll")],
        [InlineKeyboardButton("◀️ back", callback_data="menu_main"), InlineKeyboardButton("next ▶️", callback_data="menu_filters_page2")]
    ]
    return InlineKeyboardMarkup(buttons)

def get_filters_page2_keyboard(uid_str):
    config = init_user_config(uid_str)["filters"]
    dup_status = "✅" if config.get("skip_duplicate", True) else "❌"
    sec_status = "✅" if config.get("secure_message", False) else "❌"
    size_lbl = f"🔩 size limit ({config.get('size_limit', 0)}MB)"
    ext_count = len(config.get("extensions", []))
    ext_lbl = f"💾 Extension ({ext_count} active)"
    kw_count = len(config.get("keywords", []))
    kw_lbl = f"🕵️ keywords ({kw_count} saved)"

    buttons = [
        [InlineKeyboardButton("▶️ Skip duplicate", callback_data="f2_toggle:skip_duplicate"), InlineKeyboardButton(dup_status, callback_data="f2_toggle:skip_duplicate")],
        [InlineKeyboardButton("🔒 Secure message", callback_data="f2_toggle:secure_message"), InlineKeyboardButton(sec_status, callback_data="f2_toggle:secure_message")],
        [InlineKeyboardButton(size_lbl, callback_data="f2_set:size_limit"), InlineKeyboardButton(ext_lbl, callback_data="f2_set:extensions")],
        [InlineKeyboardButton(kw_lbl, callback_data="f2_set:keywords")],
        [InlineKeyboardButton("◀️ back", callback_data="menu_filters_page1")]
    ]
    return InlineKeyboardMarkup(buttons)


# --------------- Admin-Only Management Commands ---------------
@bot.on_message(filters.command(["adduser"]) & filters.user(ADMIN_ID))
async def add_subscriber(bot: Client, m: Message):
    if len(m.command) < 3: return
    target_id, duration_str = m.command[1], m.command[2].lower()
    if not target_id.isdigit(): return
    match = re.match(r"^(\d+)([mhd])$", duration_str)
    if not match: return
    value, unit = int(match.group(1)), match.group(2)
    seconds_delta = value * 60 if unit == 'm' else (value * 3600 if unit == 'h' else value * 86400)
    AUTHORIZED_USERS[str(target_id)] = time.time() + seconds_delta
    save_authorized_users()
    await m.reply_text(f"✅ Subscriber Account Added: `{target_id}`")

@bot.on_message(filters.command(["remuser"]) & filters.user(ADMIN_ID))
async def remove_subscriber(bot: Client, m: Message):
    if len(m.command) < 2: return
    AUTHORIZED_USERS.pop(m.command[1], None)
    save_authorized_users()
    await m.reply_text("🗑️ Target privileges revoked.")


# --------------- Interactive Callback Queries Engine ---------------
@bot.on_callback_query()
async def handle_settings_callbacks(client: Client, cb: CallbackQuery):
    uid_str = str(cb.from_user.id)
    data = cb.data
    config = init_user_config(uid_str)
    
    if data == "menu_main":
        await cb.message.edit_text("⚙️ **Main Custom Output Settings Panel**", reply_markup=get_main_settings_keyboard())
    elif data == "menu_caption":
        cap_status = "🟢 ON" if config["use_caption"] else "🔴 OFF"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Toggle Caption Mode: {cap_status}", callback_data="toggle_cap_mode")],
            [InlineKeyboardButton("📝 Provide New Caption Text String", callback_data="prompt_set_cap")],
            [InlineKeyboardButton("◀️ Return to Settings", callback_data="menu_main")]
        ])
        await cb.message.edit_text(f"📝 **Caption Configuration**\nCurrent: `{config['caption'] or 'None'}`", reply_markup=kb)
    elif data == "toggle_cap_mode":
        config["use_caption"] = not config["use_caption"]
        save_user_settings()
        await handle_settings_callbacks(client, cb=CallbackQuery(id=cb.id, from_user=cb.from_user, message=cb.message, data="menu_caption"))
    elif data == "prompt_set_cap":
        USER_STATES[uid_str] = "SETTING_CAPTION"
        await cb.message.reply_text("📝 Send your custom caption string below:")
    elif data == "menu_thumb":
        thumb_status = "🟢 ON" if config["use_thumb"] else "🔴 OFF"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Toggle Custom Thumb: {thumb_status}", callback_data="toggle_thumb_mode")],
            [InlineKeyboardButton("🖼️ Register Custom Image", callback_data="prompt_set_thumb")],
            [InlineKeyboardButton("◀️ Return to Settings", callback_data="menu_main")]
        ])
        await cb.message.edit_text("🖼️ **Custom Thumbnail Panels**", reply_markup=kb)
    elif data == "toggle_thumb_mode":
        config["use_thumb"] = not config["use_thumb"]
        save_user_settings()
        await handle_settings_callbacks(client, cb=CallbackQuery(id=cb.id, from_user=cb.from_user, message=cb.message, data="menu_thumb"))
    elif data == "prompt_set_thumb":
        USER_STATES[uid_str] = "SETTING_THUMBNAIL"
        await cb.message.reply_text("🖼️ Send a photo file to use as the thumbnail:")
    elif data == "menu_filters_page1":
        await cb.message.edit_text("⭐ **Content Filtering (Page 1)**", reply_markup=get_filters_page1_keyboard(uid_str))
    elif data == "menu_filters_page2":
        await cb.message.edit_text("⚙️ **Advanced Filtering (Page 2)**", reply_markup=get_filters_page2_keyboard(uid_str))
    elif data.startswith("f1_toggle:"):
        key = data.split(":")[1]
        config["filters"][key] = not config["filters"].get(key, False)
        save_user_settings()
        await cb.message.edit_reply_markup(reply_markup=get_filters_page1_keyboard(uid_str))
    elif data.startswith("f2_toggle:"):
        key = data.split(":")[1]
        config["filters"][key] = not config["filters"].get(key, False)
        save_user_settings()
        await cb.message.edit_reply_markup(reply_markup=get_filters_page2_keyboard(uid_str))
    elif data.startswith("f2_set:"):
        target = data.split(":")[1]
        if target == "size_limit":
            USER_STATES[uid_str] = "SETTING_SIZE_LIMIT"
            await cb.message.reply_text("🔩 Enter max allowed file sizes in MB (Send `0` for unlimited):")
        elif target == "extensions":
            USER_STATES[uid_str] = "SETTING_EXTENSIONS"
            await cb.message.reply_text("💾 Send allowed extensions separated by spaces (e.g., `mp4 pdf`). Send `clear` to reset:")
        elif target == "keywords":
            USER_STATES[uid_str] = "SETTING_KEYWORDS"
            await cb.message.reply_text("🕵️ Send message keywords separated by commas. Send `clear` to remove rules:")
    elif data == "clear_all":
        USER_SETTINGS[uid_str] = {"caption": "", "use_caption": False, "thumb": "", "use_thumb": False, "filters": DEFAULT_FILTERS.copy()}
        save_user_settings()
        await cb.message.edit_text("⚙️ Configurations cleared.", reply_markup=get_main_settings_keyboard())
    
    try: await cb.answer()
    except: pass


# --------------- Content Extraction Validation Filter Core Engine ---------------
def passed_content_filters(uid_str, msg: Message, chat_link_context: str) -> bool:
    config = init_user_config(uid_str)["filters"]
    
    if config.get("skip_duplicate", True):
        uid_hist = PROCESSED_HISTORY.get(uid_str, [])
        if chat_link_context in uid_hist: return False

    msg_text = (msg.text or msg.caption or "").lower()
    keywords = config.get("keywords", [])
    if keywords:
        matched_kw = any(kw.strip().lower() in msg_text for kw in keywords if kw.strip())
        if not matched_kw: return False

    is_text_only = not any([msg.document, msg.video, msg.photo, msg.audio, msg.voice, msg.animation, msg.sticker, msg.poll])
    if is_text_only and not config.get("text", True): return False
    if msg.document and not config.get("document", True): return False
    if msg.video and not config.get("video", True): return False
    if msg.photo and not config.get("photo", True): return False
    if msg.audio and not config.get("audio", True): return False
    if msg.voice and not config.get("voice", True): return False
    if msg.animation and not config.get("animation", True): return False
    if msg.sticker and not config.get("sticker", True): return False
    if msg.poll and not config.get("poll", True): return False

    media_obj = msg.document or msg.video or msg.audio or msg.voice or msg.animation
    if media_obj:
        size_limit_mb = config.get("size_limit", 0)
        if size_limit_mb > 0:
            if (getattr(media_obj, "file_size", 0) / (1024 * 1024)) > size_limit_mb: return False
        
        allowed_exts = config.get("extensions", [])
        if allowed_exts:
            file_name = getattr(media_obj, "file_name", "").lower()
            if not any(file_name.endswith(f".{ext.lower()}") for ext in allowed_exts): return False

    return True

def record_processed_history(uid_str, chat_link_context):
    if uid_str not in PROCESSED_HISTORY: PROCESSED_HISTORY[uid_str] = []
    if chat_link_context not in PROCESSED_HISTORY[uid_str]:
        PROCESSED_HISTORY[uid_str].append(chat_link_context)
        save_history()


# --------------- Account Persistence Runners Configuration Helpers ---------------
async def get_user_client(uid, user_data):
    if uid in RUNNING_CLIENTS:
        client = RUNNING_CLIENTS[uid]
        if client.is_connected: return client
        try: await client.start(); return client
        except: RUNNING_CLIENTS.pop(uid, None)
    client = Client(name=f"run_{uid}", api_id=user_data["api_id"], api_hash=user_data["api_hash"], session_string=user_data["session_string"], in_memory=True)
    await client.start()
    RUNNING_CLIENTS[uid] = client
    return client

def get_readable_size(bytes_count):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_count < 1024.0: return f"{bytes_count:.2f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.2f} PB"

def get_readable_time(seconds):
    if seconds < 60: return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60: return f"{int(minutes)}m {int(seconds % 60)}s"
    hours = minutes / 60
    return f"{int(hours)}h {int(minutes % 60)}m"

async def progress_cb(current, total, status_message, action_text, tracking_ctx, uid):
    if CANCEL_BATCH.get(uid, False): raise StopIteration()
    if not total: return
    now = time.time()
    if now - tracking_ctx.get("last_edit", 0.0) >= 3.5 or current == total:
        tracking_ctx["last_edit"] = now
        elapsed_time = now - tracking_ctx["start_time"] or 0.01
        speed_bps = current / elapsed_time
        percent = current * 100 / total
        eta_seconds = (total - current) / speed_bps if speed_bps > 0 else 0
        filled = int(20 * percent / 100)
        bar = f"[{'█' * filled}{'░' * (20 - filled)}] {percent:.1f}%"
        metrics_panel = f"**{action_text}**\n\n📊 {bar}\n📁 **Size:** {get_readable_size(current)} / {get_readable_size(total)}\n⚡ **Speed:** {get_readable_size(speed_bps)}/s\n⏳ **ETA:** {get_readable_time(eta_seconds)}"
        try: await status_message.edit_text(metrics_panel)
        except: pass


# --------------- Base Commands Routers ---------------
@bot.on_message(filters.command(["start"]) & subscribed_only)
async def start(bot: Client, m: Message):
    await m.reply_text("**Welcome!**\n\nCommands:\n🔑 /login - Link your profile\n⚙ /settings - Configure filters & custom outputs\n❌ /cancel - Abort tasks")

@bot.on_message(filters.command(["settings"]) & filters.private & subscribed_only)
async def open_settings(bot: Client, m: Message):
    uid_str = str(m.from_user.id)
    init_user_config(uid_str)
    await m.reply_text("⚙️ **Main Custom Output Settings Panel**", reply_markup=get_main_settings_keyboard())

@bot.on_message(filters.command(["cancel"]) & subscribed_only)
async def cancel_login(bot: Client, m: Message):
    user_id = m.from_user.id if m.from_user else m.chat.id
    if str(user_id) in USER_STATES: USER_STATES.pop(str(user_id), None)
    CANCEL_BATCH[user_id] = True
    await m.reply_text("🛑 Sequences stopped.")


# --------------- Conversation Engine & Main Process Filter (Protected) ---------------
@bot.on_message((filters.text | filters.caption | filters.photo) & subscribed_only)
async def handle_text_inputs(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    str_uid = str(user_id)
    text = (message.text or message.caption or "").strip()

    if text.startswith("/"):
        cmd_part = text.split()[0].split("@")[0]
        if cmd_part in ["/start", "/login", "/logout", "/cancel", "/adduser", "/remuser", "/status", "/settings"]: return

    # 1. State Handlers
    if str_uid in USER_STATES and message.chat.type == pyrogram.enums.ChatType.PRIVATE:
        state = USER_STATES[str_uid]
        config = init_user_config(str_uid)
        
        if state == "SETTING_CAPTION":
            if text:
                config["caption"], config["use_caption"] = text, True
                save_user_settings(); USER_STATES.pop(str_uid, None)
                await message.reply_text("✅ Custom caption saved.")
            return
        elif state == "SETTING_THUMBNAIL":
            if message.photo:
                config["thumb"], config["use_thumb"] = message.photo.file_id, True
                save_user_settings(); USER_STATES.pop(str_uid, None)
                await message.reply_text("✅ Thumbnail saved.")
            return
        elif state == "SETTING_SIZE_LIMIT":
            if text.isdigit():
                config["filters"]["size_limit"] = int(text)
                save_user_settings(); USER_STATES.pop(str_uid, None)
                await message.reply_text(f"✅ Max limit: `{text} MB`")
            return
        elif state == "SETTING_EXTENSIONS":
            if text.lower() == "clear": config["filters"]["extensions"] = []
            else: config["filters"]["extensions"] = text.replace(".", "").split()
            save_user_settings(); USER_STATES.pop(str_uid, None)
            await message.reply_text("✅ Extensions updated.")
            return
        elif state == "SETTING_KEYWORDS":
            if text.lower() == "clear": config["filters"]["keywords"] = []
            else: config["filters"]["keywords"] = [k.strip() for k in text.split(",") if k.strip()]
            save_user_settings(); USER_STATES.pop(str_uid, None)
            await message.reply_text("✅ Keywords updated.")
            return

        # Login Steps...
        if state == "WAITING_API_ID":
            ACTIVE_LOGINS[user_id] = {"api_id": int(text)}
            USER_STATES[str_uid] = "WAITING_API_HASH"
            await message.reply_text("⚙️ Send your **API HASH**:")
            return
        elif state == "WAITING_API_HASH":
            ACTIVE_LOGINS[user_id]["api_hash"] = text
            USER_STATES[str_uid] = "WAITING_PHONE"
            await message.reply_text("📱 Send your **Phone Number**:")
            return
        elif state == "WAITING_PHONE":
            ACTIVE_LOGINS[user_id]["phone"] = text
            try:
                temp_client = Client(name=f"login_{user_id}", api_id=ACTIVE_LOGINS[user_id]["api_id"], api_hash=ACTIVE_LOGINS[user_id]["api_hash"], in_memory=True)
                await temp_client.connect()
                code_info = await temp_client.send_code(text)
                ACTIVE_LOGINS[user_id]["client"], ACTIVE_LOGINS[user_id]["phone_code_hash"] = temp_client, code_info.phone_code_hash
                USER_STATES[str_uid] = "WAITING_OTP"
                await message.reply_text("📩 Send the **OTP Code**:")
            except Exception as e:
                USER_STATES.pop(str_uid, None)
                await message.reply_text(f"❌ Error: `{e}`")
            return
        elif state == "WAITING_OTP":
            login_data = ACTIVE_LOGINS[user_id]
            try:
                await login_data["client"].sign_in(phone_number=login_data["phone"], phone_code_hash=login_data["phone_code_hash"], phone_code=text.replace(" ", ""))
                session_str = await login_data["client"].export_session_string()
                USER_SESSIONS[str_uid] = {"api_id": login_data["api_id"], "api_hash": login_data["api_hash"], "session_string": session_str}
                save_sessions(); USER_STATES.pop(str_uid, None)
                await message.reply_text("🎉 **Login Successful!**")
            except SessionPasswordNeeded:
                USER_STATES[str_uid] = "WAITING_2FA"
                await message.reply_text("🔐 Enter your **2FA Password**:")
            except Exception as e: await message.reply_text(f"❌ Error: `{e}`")
            return
        elif state == "WAITING_2FA":
            try:
                await ACTIVE_LOGINS[user_id]["client"].check_password(password=text)
                session_str = await ACTIVE_LOGINS[user_id]["client"].export_session_string()
                USER_SESSIONS[str_uid] = {"api_id": ACTIVE_LOGINS[user_id]["api_id"], "api_hash": ACTIVE_LOGINS[user_id]["api_hash"], "session_string": session_str}
                save_sessions(); USER_STATES.pop(str_uid, None)
                await message.reply_text("🎉 **2FA Login Successful!**")
            except Exception as e: await message.reply_text(f"❌ Error: `{e}`")
            return

    if not text: return

    # --- Batch Processor Routers ---
    if "https://t.me/+" in text or "https://t.me/joinchat/" in text:
        if str_uid not in USER_SESSIONS: return
        try:
            user_acc = await get_user_client(user_id, USER_SESSIONS[str_uid])
            await user_acc.join_chat(text)
            await bot.send_message(message.chat.id, "✅ Joined chat.", reply_to_message_id=message.id)
        except Exception as e: await bot.send_message(message.chat.id, f"❌ Join Failed: `{e}`")
        return

    CANCEL_BATCH[user_id] = False
    range_match = re.match(r'(https://t\.me/(?:c/)?[^/\s]+)(?:/\d+)?/(\d+)\s*-\s*(\d+)$', text)
    if range_match:
        base_link, start_id, end_id = range_match.group(1), int(range_match.group(2)), int(range_match.group(3))
        if end_id < start_id: return
        for msg_id in range(start_id, end_id + 1):
            if CANCEL_BATCH.get(user_id, False): break
            await process_single_link(f"{base_link}/{msg_id}", message, uid=user_id)
            await asyncio.sleep(2)
        return

    links = re.findall(r'https://t\.me/(?:c/)?[^/\s]+(?:\/\d+)?/\d+', text)
    for link in links:
        if CANCEL_BATCH.get(user_id, False): break
        await process_single_link(link, message, uid=user_id)
        await asyncio.sleep(2)


# --------------- Engine Loop Processor (Robust FloodWait Resilient) ---------------
async def process_single_link(link, original_msg, uid=0):
    datas = link.split("/")
    msgid = int(datas[-1])
    str_uid = str(uid)
    user_config = init_user_config(str_uid)
    filter_rules = user_config["filters"]

    async def reply(text):
        return await bot.send_message(original_msg.chat.id, text, reply_to_message_id=original_msg.id)

    # 🛑 THE RETRY LOOP: Prevents message skipping when hit by API rate limits
    while True:
        if CANCEL_BATCH.get(uid, False): return

        file = None
        thumb = None
        public_thumb = None
        status_msg = None

        try:
            # --- ROUTE 1: PRIVATE CHAT HANDLING ---
            if "https://t.me/c/" in link:
                if str_uid not in USER_SESSIONS: return
                chatid = int("-100" + datas[4])
                
                user_acc = await get_user_client(uid, USER_SESSIONS[str_uid])
                msg = await user_acc.get_messages(chatid, msgid)
                if not msg: break

                if not passed_content_filters(str_uid, msg, f"{chatid}_{msgid}"): break

                if filter_rules.get("forward_tag", False):
                    try:
                        await user_acc.forward_messages(original_msg.chat.id, chatid, msgid)
                        record_processed_history(str_uid, f"{chatid}_{msgid}")
                        break
                    except FloodWait as e:
                        raise e # Handled by the outer except block
                    except:
                        pass # Fallback to manual download/upload if restricted

                has_media = any([msg.document, msg.video, msg.animation, msg.sticker, msg.voice, msg.audio, msg.photo])
                final_caption = user_config["caption"] if user_config["use_caption"] else (msg.caption or "")
                final_entities = None if user_config["use_caption"] else (msg.caption_entities or msg.entities)

                if not has_media:
                    text_to_send = user_config["caption"] if user_config["use_caption"] else (msg.text or msg.caption)
                    if text_to_send:
                        await bot.send_message(original_msg.chat.id, text_to_send, entities=final_entities, reply_to_message_id=original_msg.id)
                        record_processed_history(str_uid, f"{chatid}_{msgid}")
                    break

                status_msg = await reply("📥 Fetching encrypted storage payloads...")
                download_ctx, upload_ctx = {"start_time": time.time()}, {"start_time": time.time()}

                file = await user_acc.download_media(msg, progress=progress_cb, progress_args=(status_msg, "📥 Processing Download", download_ctx, uid))
                
                if user_config["use_thumb"] and user_config["thumb"]:
                    try: thumb = await bot.download_media(user_config["thumb"])
                    except: pass
                else:
                    target_thumb_holder = msg.document or msg.video
                    if target_thumb_holder and target_thumb_holder.thumbs:
                        try: thumb = await user_acc.download_media(target_thumb_holder.thumbs[0].file_id)
                        except: pass

                if msg.document:
                    await bot.send_document(original_msg.chat.id, file, thumb=thumb, caption=final_caption, caption_entities=final_entities, reply_to_message_id=original_msg.id, progress=progress_cb, progress_args=(status_msg, "📤 Uploading Document", upload_ctx, uid))
                elif msg.video:
                    await bot.send_video(original_msg.chat.id, file, duration=msg.video.duration, width=msg.video.width, height=msg.video.height, thumb=thumb, caption=final_caption, caption_entities=final_entities, reply_to_message_id=original_msg.id, progress=progress_cb, progress_args=(status_msg, "📤 Uploading Video File", upload_ctx, uid))
                elif msg.photo:
                    await bot.send_photo(original_msg.chat.id, file, caption=final_caption, caption_entities=final_entities, reply_to_message_id=original_msg.id)
                elif msg.animation:
                    await bot.send_animation(original_msg.chat.id, file, reply_to_message_id=original_msg.id)
                elif msg.sticker:
                    await bot.send_sticker(original_msg.chat.id, file, reply_to_message_id=original_msg.id)
                elif msg.voice:
                    await bot.send_voice(original_msg.chat.id, file, caption=final_caption, reply_to_message_id=original_msg.id)
                elif msg.audio:
                    await bot.send_audio(original_msg.chat.id, file, caption=final_caption, caption_entities=final_entities, reply_to_message_id=original_msg.id)

                record_processed_history(str_uid, f"{chatid}_{msgid}")
                break # Job done, escape the retry loop

            # --- ROUTE 2: PUBLIC CHAT HANDLING ---
            else:
                username = datas[-2]
                msg = await bot.get_messages(username, msgid)
                if not msg: break
                
                if not passed_content_filters(str_uid, msg, f"{username}_{msgid}"): break

                if filter_rules.get("forward_tag", False):
                    await bot.forward_messages(original_msg.chat.id, username, msgid)
                    record_processed_history(str_uid, f"{username}_{msgid}")
                    break

                final_caption = user_config["caption"] if user_config["use_caption"] else (msg.caption or "")
                final_entities = None if user_config["use_caption"] else (msg.caption_entities or msg.entities)

                if user_config["use_thumb"] and user_config["thumb"]:
                    try: public_thumb = await bot.download_media(user_config["thumb"])
                    except: pass

                if msg.document:
                    if public_thumb:
                        file = await bot.download_media(msg)
                        await bot.send_document(original_msg.chat.id, file, thumb=public_thumb, caption=final_caption, caption_entities=final_entities, reply_to_message_id=original_msg.id)
                    else:
                        await bot.send_document(original_msg.chat.id, msg.document.file_id, caption=final_caption, caption_entities=final_entities, reply_to_message_id=original_msg.id)
                elif msg.video:
                    if public_thumb:
                        file = await bot.download_media(msg)
                        await bot.send_video(original_msg.chat.id, file, thumb=public_thumb, caption=final_caption, caption_entities=final_entities, reply_to_message_id=original_msg.id)
                    else:
                        await bot.send_video(original_msg.chat.id, msg.video.file_id, caption=final_caption, caption_entities=final_entities, reply_to_message_id=original_msg.id)
                elif msg.photo:
                    await bot.send_photo(original_msg.chat.id, msg.photo.file_id, caption=final_caption, caption_entities=final_entities, reply_to_message_id=original_msg.id)
                elif msg.text:
                    await bot.send_message(original_msg.chat.id, user_config["caption"] if user_config["use_caption"] else msg.text, entities=final_entities, reply_to_message_id=original_msg.id)
                elif msg.animation:
                    await bot.send_animation(original_msg.chat.id, msg.animation.file_id, reply_to_message_id=original_msg.id)
                elif msg.sticker:
                    await bot.send_sticker(original_msg.chat.id, msg.sticker.file_id, reply_to_message_id=original_msg.id)
                elif msg.voice:
                    await bot.send_voice(original_msg.chat.id, msg.voice.file_id, caption=final_caption, reply_to_message_id=original_msg.id)
                elif msg.audio:
                    await bot.send_audio(original_msg.chat.id, msg.audio.file_id, caption=final_caption, reply_to_message_id=original_msg.id)

                record_processed_history(str_uid, f"{username}_{msgid}")
                break # Job done, escape the retry loop

        except FloodWait as e:
            # Catch the limit error, wait the required time, and CONTINUES the loop to retry
            sleep_time = e.value + 2
            await reply(f"⏳ **Rate Limit Executed (FloodWait)**\nTelegram is restricting bot speed. Pausing for `{sleep_time}s` and automatically retrying this message...")
            await asyncio.sleep(sleep_time)
            continue 

        except Exception as e:
            await reply(f"⚠️ Unrecoverable Extraction Error: `{e}`")
            break
            
        finally:
            # Housekeeping to ensure disk space doesn't clutter on a retry/failure
            if file and os.path.exists(file): 
                try: os.remove(file)
                except: pass
            if thumb and os.path.exists(thumb): 
                try: os.remove(thumb)
                except: pass
            if public_thumb and os.path.exists(public_thumb): 
                try: os.remove(public_thumb)
                except: pass
            if status_msg:
                try: await status_msg.delete()
                except: pass


# --------------- Start Application Execution ---------------
if __name__ == "__main__":
    bot.run()
