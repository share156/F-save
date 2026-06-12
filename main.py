import pyrogram
from pyrogram import Client, filters
from pyrogram.errors import (
    UserAlreadyParticipant, InviteHashExpired, FloodWait,
    PeerIdInvalid, SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    ChannelPrivate, UserNotParticipant, ChatForbidden, RPCError
)
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import asyncio
import time
import os
import re
from pymongo import MongoClient

# --------------- Configuration ---------------
bot_token = os.environ.get("TOKEN", "")
api_hash = os.environ.get("HASH", "")
api_id = os.environ.get("ID", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
MONGO_URI = os.environ.get("MONGO_URI", "")

bot = Client("mybot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

# --------------- MongoDB Setup & Data Persistence ---------------
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable is not set!")

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_bot_db"]

# Collections
sessions_col = db["sessions"]
auth_col = db["authorized_users"]
settings_col = db["settings"]
history_col = db["processed_history"]

# Global dictionaries
USER_SESSIONS = {}
AUTHORIZED_USERS = {}
USER_SETTINGS = {}
PROCESSED_HISTORY = {}
USER_STATES = {}
ACTIVE_LOGINS = {}
CANCEL_BATCH = {}
RUNNING_CLIENTS = {}

DEFAULT_FILTERS = {
    "forward_tag": False, "text": True, "document": True, "video": True,
    "photo": True, "audio": True, "voice": True, "animation": True,
    "sticker": True, "poll": True, "skip_duplicate": True,
    "secure_message": False, "size_limit": 0, "extensions": [], "keywords": []
}

def load_data():
    global USER_SESSIONS, AUTHORIZED_USERS, USER_SETTINGS, PROCESSED_HISTORY
    s = sessions_col.find_one({"_id": "sessions"})
    USER_SESSIONS = s.get("data", {}) if s else {}
    a = auth_col.find_one({"_id": "auth"})
    AUTHORIZED_USERS = a.get("data", {}) if a else {}
    st = settings_col.find_one({"_id": "settings"})
    USER_SETTINGS = st.get("data", {}) if st else {}
    h = history_col.find_one({"_id": "history"})
    PROCESSED_HISTORY = h.get("data", {}) if h else {}

def save_all():
    sessions_col.update_one({"_id": "sessions"}, {"$set": {"data": USER_SESSIONS}}, upsert=True)
    auth_col.update_one({"_id": "auth"}, {"$set": {"data": AUTHORIZED_USERS}}, upsert=True)
    settings_col.update_one({"_id": "settings"}, {"$set": {"data": USER_SETTINGS}}, upsert=True)
    history_col.update_one({"_id": "history"}, {"$set": {"data": PROCESSED_HISTORY}}, upsert=True)

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
            save_all()
            if user_id in RUNNING_CLIENTS:
                try: await RUNNING_CLIENTS[user_id].stop()
                except: pass
                RUNNING_CLIENTS.pop(user_id, None)
    try: await message.reply_text("⚠️ **Access Denied / Expired**\nPlease contact the Administrator.")
    except: pass
    return False

subscribed_only = filters.create(is_subscribed)

# --------------- User Config Helper ---------------
def init_user_config(uid_str):
    if uid_str not in USER_SETTINGS:
        USER_SETTINGS[uid_str] = {
            "caption": "", "use_caption": False, "thumb": "", "use_thumb": False,
            "filters": DEFAULT_FILTERS.copy()
        }
    else:
        if "filters" not in USER_SETTINGS[uid_str]:
            USER_SETTINGS[uid_str]["filters"] = DEFAULT_FILTERS.copy()
        else:
            for key, val in DEFAULT_FILTERS.items():
                if key not in USER_SETTINGS[uid_str]["filters"]:
                    USER_SETTINGS[uid_str]["filters"][key] = val
    save_all()
    return USER_SETTINGS[uid_str]

# --------------- Interactive Menu Keyboards ---------------
def get_main_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖊️ CAPTION", callback_data="menu_caption"),
         InlineKeyboardButton("🖼️ THUMBNAIL", callback_data="menu_thumb")],
        [InlineKeyboardButton("🧹 FILTERS", callback_data="menu_filters_page1")],
        [InlineKeyboardButton("🗑️ Reset All Configuration Profiles", callback_data="clear_all")]
    ])

def get_filters_page1_keyboard(uid_str):
    config = init_user_config(uid_str)["filters"]
    def status_icon(key): return "✅" if config.get(key, False) else "❌"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷️ Forward tag", callback_data="f1_toggle:forward_tag"),
         InlineKeyboardButton(status_icon("forward_tag"), callback_data="f1_toggle:forward_tag")],
        [InlineKeyboardButton("🖊️ Text", callback_data="f1_toggle:text"),
         InlineKeyboardButton(status_icon("text"), callback_data="f1_toggle:text")],
        [InlineKeyboardButton("📁 Document", callback_data="f1_toggle:document"),
         InlineKeyboardButton(status_icon("document"), callback_data="f1_toggle:document")],
        [InlineKeyboardButton("📦 Video", callback_data="f1_toggle:video"),
         InlineKeyboardButton(status_icon("video"), callback_data="f1_toggle:video")],
        [InlineKeyboardButton("📷 Photo", callback_data="f1_toggle:photo"),
         InlineKeyboardButton(status_icon("photo"), callback_data="f1_toggle:photo")],
        [InlineKeyboardButton("◀️ back", callback_data="menu_main"),
         InlineKeyboardButton("next ▶️", callback_data="menu_filters_page2")]
    ])

def get_filters_page2_keyboard(uid_str):
    config = init_user_config(uid_str)["filters"]
    dup_status = "✅" if config.get("skip_duplicate", True) else "❌"
    size_lbl = f"🔩 size limit ({config.get('size_limit', 0)}MB)"
    ext_lbl = f"💾 Extension ({len(config.get('extensions', []))} active)"
    kw_lbl = f"🕵️ keywords ({len(config.get('keywords', []))} saved)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Skip duplicate", callback_data="f2_toggle:skip_duplicate"),
         InlineKeyboardButton(dup_status, callback_data="f2_toggle:skip_duplicate")],
        [InlineKeyboardButton(size_lbl, callback_data="f2_set:size_limit"),
         InlineKeyboardButton(ext_lbl, callback_data="f2_set:extensions")],
        [InlineKeyboardButton(kw_lbl, callback_data="f2_set:keywords")],
        [InlineKeyboardButton("◀️ back", callback_data="menu_filters_page1")]
    ])

# --------------- Base Commands ---------------
@bot.on_message(filters.command(["start"]) & subscribed_only)
async def start(bot: Client, m: Message):
    await m.reply_text("**Welcome!**\n\nCommands:\n🔑 /login - Link your profile\n⚙ /settings - Configure filters\n❌ /cancel - Abort tasks")

@bot.on_message(filters.command(["settings"]) & filters.private & subscribed_only)
async def open_settings(bot: Client, m: Message):
    init_user_config(str(m.from_user.id))
    await m.reply_text("⚙️ **Main Custom Output Settings Panel**", reply_markup=get_main_settings_keyboard())

@bot.on_message(filters.command(["cancel"]) & subscribed_only)
async def cancel_logic(bot: Client, m: Message):
    uid = m.from_user.id if m.from_user else m.chat.id
    if str(uid) in USER_STATES: USER_STATES.pop(str(uid), None)
    CANCEL_BATCH[uid] = True
    await m.reply_text("🛑 Sequences stopped.")

@bot.on_message(filters.command(["login"]) & filters.private & subscribed_only)
async def login_cmd(bot: Client, m: Message):
    USER_STATES[str(m.from_user.id)] = "WAITING_API_ID"
    await m.reply_text("🔑 **Login Process Started**\n\nPlease send your **API ID** (numbers only):")

# --------------- Admin Commands ---------------
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
    save_all()
    await m.reply_text(f"✅ Subscriber Account Added: `{target_id}`")

@bot.on_message(filters.command(["remuser"]) & filters.user(ADMIN_ID))
async def remove_subscriber(bot: Client, m: Message):
    if len(m.command) < 2: return
    AUTHORIZED_USERS.pop(m.command[1], None)
    save_all()
    await m.reply_text("🗑️ Target privileges revoked.")

# --------------- Interactive Callback Queries Engine ---------------
@bot.on_callback_query()
async def handle_settings_callbacks(client: Client, cb: CallbackQuery):
    try: await cb.answer()
    except: pass

    uid_str = str(cb.from_user.id)
    data = cb.data
    config = init_user_config(uid_str)

    if data == "menu_main":
        await cb.message.edit_text("⚙️ **Main Custom Output Settings Panel**", reply_markup=get_main_settings_keyboard())
    elif data == "menu_caption":
        cap_status = "🟢 ON" if config["use_caption"] else "🔴 OFF"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Toggle Caption Mode: {cap_status}", callback_data="toggle_cap_mode")],
            [InlineKeyboardButton("📝 Provide New Caption Text", callback_data="prompt_set_cap")],
            [InlineKeyboardButton("◀️ Return to Settings", callback_data="menu_main")]
        ])
        await cb.message.edit_text(f"📝 **Caption Configuration**\nCurrent: `{config['caption'] or 'None'}`", reply_markup=kb)
    elif data == "toggle_cap_mode":
        config["use_caption"] = not config["use_caption"]
        save_all()
        await handle_settings_callbacks(client, CallbackQuery(id=cb.id, from_user=cb.from_user, message=cb.message, data="menu_caption"))
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
        save_all()
        await handle_settings_callbacks(client, CallbackQuery(id=cb.id, from_user=cb.from_user, message=cb.message, data="menu_thumb"))
    elif data == "prompt_set_thumb":
        USER_STATES[uid_str] = "SETTING_THUMBNAIL"
        await cb.message.reply_text("🖼️ Send a photo file to use as the thumbnail:")
    elif data == "menu_filters_page1":
        await cb.message.edit_text("⭐ **Content Filtering (Page 1)**", reply_markup=get_filters_page1_keyboard(uid_str))
    elif data == "menu_filters_page2":
        await cb.message.edit_text("⚙️ **Advanced Filtering (Page 2)**", reply_markup=get_filters_page2_keyboard(uid_str))
    elif data.startswith("f1_toggle:") or data.startswith("f2_toggle:"):
        key = data.split(":")[1]
        config["filters"][key] = not config["filters"].get(key, False)
        save_all()
        kb = get_filters_page1_keyboard(uid_str) if "f1_" in data else get_filters_page2_keyboard(uid_str)
        await cb.message.edit_reply_markup(reply_markup=kb)
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
        save_all()
        await cb.message.edit_text("⚙️ Configurations cleared.", reply_markup=get_main_settings_keyboard())

# --------------- Content Extraction Helpers ---------------
def passed_content_filters(uid_str, msg: Message, chat_link_context: str) -> bool:
    config = init_user_config(uid_str)["filters"]
    if config.get("skip_duplicate", True):
        if chat_link_context in PROCESSED_HISTORY.get(uid_str, []): return False

    msg_text = (msg.text or msg.caption or "").lower()
    keywords = config.get("keywords", [])
    if keywords:
        if not any(kw.strip().lower() in msg_text for kw in keywords if kw.strip()): return False

    is_text_only = not any([msg.document, msg.video, msg.photo, msg.audio, msg.voice, msg.animation, msg.sticker, msg.poll])
    if is_text_only and not config.get("text", True): return False
    if msg.document and not config.get("document", True): return False
    if msg.video and not config.get("video", True): return False
    if msg.photo and not config.get("photo", True): return False

    media_obj = msg.document or msg.video or msg.audio or msg.voice or msg.animation
    if media_obj:
        if config.get("size_limit", 0) > 0:
            if (getattr(media_obj, "file_size", 0) / (1024 * 1024)) > config.get("size_limit", 0): return False
        if config.get("extensions", []):
            file_name = getattr(media_obj, "file_name", "").lower()
            if not any(file_name.endswith(f".{ext.lower()}") for ext in config.get("extensions", [])): return False
    return True

def record_processed_history(uid_str, chat_link_context):
    if uid_str not in PROCESSED_HISTORY: PROCESSED_HISTORY[uid_str] = []
    if chat_link_context not in PROCESSED_HISTORY[uid_str]:
        PROCESSED_HISTORY[uid_str].append(chat_link_context)
        save_all()

async def get_user_client(uid, user_data):
    if uid in RUNNING_CLIENTS:
        client = RUNNING_CLIENTS[uid]
        if client.is_connected: return client
        try: await client.start(); return client
        except: RUNNING_CLIENTS.pop(uid, None)
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

async def progress_cb(current, total, status_message, action_text, tracking_ctx, uid):
    if CANCEL_BATCH.get(uid, False): raise StopIteration()
    now = time.time()
    if now - tracking_ctx.get("last_edit", 0.0) >= 3.5 or current == total:
        tracking_ctx["last_edit"] = now
        speed = current / (now - tracking_ctx["start_time"] or 0.01)
        percent = current * 100 / total if total else 0
        filled = int(20 * percent / 100)
        try:
            await status_message.edit_text(
                f"**{action_text}**\n\n📊 [{'█' * filled}{'░' * (20 - filled)}] {percent:.1f}%\n⚡ Speed: {speed/1024/1024:.2f} MB/s"
            )
        except: pass

# --------------- Conversation Engine & State Machine ---------------
@bot.on_message((filters.text | filters.caption | filters.photo) & subscribed_only)
async def handle_text_inputs(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    str_uid = str(user_id)
    text = (message.text or message.caption or "").strip()

    if text.startswith("/"): return

    # State routing
    if str_uid in USER_STATES and message.chat.type == pyrogram.enums.ChatType.PRIVATE:
        state = USER_STATES[str_uid]
        config = init_user_config(str_uid)

        if state == "SETTING_CAPTION":
            config["caption"], config["use_caption"] = text, True
            save_all(); USER_STATES.pop(str_uid, None)
            await message.reply_text("✅ Custom caption saved."); return

        elif state == "SETTING_THUMBNAIL":
            if message.photo:
                config["thumb"], config["use_thumb"] = message.photo.file_id, True
                save_all(); USER_STATES.pop(str_uid, None)
                await message.reply_text("✅ Thumbnail saved.")
            return

        elif state == "SETTING_SIZE_LIMIT":
            if text.isdigit():
                config["filters"]["size_limit"] = int(text)
                save_all(); USER_STATES.pop(str_uid, None)
                await message.reply_text(f"✅ Max limit: `{text} MB`")
            return

        elif state == "SETTING_EXTENSIONS" or state == "SETTING_KEYWORDS":
            if text.lower() == "clear":
                config["filters"][state.split("_")[1].lower()] = []
            else:
                config["filters"][state.split("_")[1].lower()] = (
                    text.replace(".", "").split() if "EXT" in state else [k.strip() for k in text.split(",") if k.strip()]
                )
            save_all(); USER_STATES.pop(str_uid, None)
            await message.reply_text("✅ Rules updated."); return

        # Login steps
        elif state == "WAITING_API_ID":
            if not text.isdigit(): await message.reply_text("❌ API ID must be numbers."); return
            ACTIVE_LOGINS[user_id] = {"api_id": int(text)}
            USER_STATES[str_uid] = "WAITING_API_HASH"
            await message.reply_text("⚙️ Send your **API HASH**:"); return

        elif state == "WAITING_API_HASH":
            ACTIVE_LOGINS[user_id]["api_hash"] = text
            USER_STATES[str_uid] = "WAITING_PHONE"
            await message.reply_text("📱 Send your **Phone Number** (with country code):"); return

        elif state == "WAITING_PHONE":
            ACTIVE_LOGINS[user_id]["phone"] = text
            try:
                temp_client = Client(
                    name=f"login_{user_id}",
                    api_id=ACTIVE_LOGINS[user_id]["api_id"],
                    api_hash=ACTIVE_LOGINS[user_id]["api_hash"],
                    in_memory=True
                )
                await temp_client.connect()
                code_info = await temp_client.send_code(text)
                ACTIVE_LOGINS[user_id]["client"], ACTIVE_LOGINS[user_id]["phone_code_hash"] = temp_client, code_info.phone_code_hash
                USER_STATES[str_uid] = "WAITING_OTP"
                await message.reply_text("📩 Send the **OTP Code**:"); return
            except Exception as e:
                USER_STATES.pop(str_uid, None)
                await message.reply_text(f"❌ Error: `{e}`"); return

        elif state == "WAITING_OTP":
            login_data = ACTIVE_LOGINS[user_id]
            try:
                await login_data["client"].sign_in(
                    phone_number=login_data["phone"],
                    phone_code_hash=login_data["phone_code_hash"],
                    phone_code=text.replace(" ", "")
                )
                USER_SESSIONS[str_uid] = {
                    "api_id": login_data["api_id"],
                    "api_hash": login_data["api_hash"],
                    "session_string": await login_data["client"].export_session_string()
                }
                save_all(); USER_STATES.pop(str_uid, None)
                await message.reply_text("🎉 **Login Successful!**")
            except SessionPasswordNeeded:
                USER_STATES[str_uid] = "WAITING_2FA"
                await message.reply_text("🔐 Enter your **2FA Password**:")
            except Exception as e:
                await message.reply_text(f"❌ Error: `{e}`")
            return

        elif state == "WAITING_2FA":
            try:
                await ACTIVE_LOGINS[user_id]["client"].check_password(password=text)
                USER_SESSIONS[str_uid] = {
                    "api_id": ACTIVE_LOGINS[user_id]["api_id"],
                    "api_hash": ACTIVE_LOGINS[user_id]["api_hash"],
                    "session_string": await ACTIVE_LOGINS[user_id]["client"].export_session_string()
                }
                save_all(); USER_STATES.pop(str_uid, None)
                await message.reply_text("🎉 **2FA Login Successful!**")
            except Exception as e:
                await message.reply_text(f"❌ Error: `{e}`")
            return

    # Batch link processing
    if not text: return
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

# --------------- Engine Loop Processor ---------------
async def process_single_link(link, original_msg, uid=0):
    datas = link.split("/")
    msgid = int(datas[-1])
    str_uid = str(uid)
    user_config = init_user_config(str_uid)

    async def reply(text): return await bot.send_message(original_msg.chat.id, text, reply_to_message_id=original_msg.id)

    while True:
        if CANCEL_BATCH.get(uid, False): return
        file = thumb = public_thumb = status_msg = None

        try:
            # PRIVATE CHATS
            if "https://t.me/c/" in link:
                if str_uid not in USER_SESSIONS:
                    await reply("⚠️ You must `/login` to process private chat links."); return
                chatid = int("-100" + datas[4])
                user_acc = await get_user_client(uid, USER_SESSIONS[str_uid])
                msg = await user_acc.get_messages(chatid, msgid)
                if not msg: break
                if not passed_content_filters(str_uid, msg, f"{chatid}_{msgid}"): break

                has_media = any([msg.document, msg.video, msg.animation, msg.sticker, msg.voice, msg.audio, msg.photo])
                final_cap = user_config["caption"] if user_config["use_caption"] else (msg.caption or "")

                if not has_media:
                    txt = user_config["caption"] if user_config["use_caption"] else (msg.text or msg.caption)
                    if txt: await bot.send_message(original_msg.chat.id, txt, reply_to_message_id=original_msg.id)
                    record_processed_history(str_uid, f"{chatid}_{msgid}")
                    break

                status_msg = await reply("📥 Fetching encrypted storage payloads...")
                ctx = {"start_time": time.time()}
                file = await user_acc.download_media(msg, progress=progress_cb, progress_args=(status_msg, "📥 Downloading", ctx, uid))

                if user_config["use_thumb"] and user_config["thumb"]:
                    try: thumb = await bot.download_media(user_config["thumb"])
                    except: pass

                if msg.document:
                    await bot.send_document(original_msg.chat.id, file, thumb=thumb, caption=final_cap, reply_to_message_id=original_msg.id, progress=progress_cb, progress_args=(status_msg, "📤 Uploading Document", {"start_time": time.time()}, uid))
                elif msg.video:
                    await bot.send_video(original_msg.chat.id, file, thumb=thumb, caption=final_cap, reply_to_message_id=original_msg.id, progress=progress_cb, progress_args=(status_msg, "📤 Uploading Video File", {"start_time": time.time()}, uid))
                elif msg.photo:
                    await bot.send_photo(original_msg.chat.id, file, caption=final_cap, reply_to_message_id=original_msg.id)

                record_processed_history(str_uid, f"{chatid}_{msgid}")
                break

            # PUBLIC CHATS
            else:
                username = datas[-2]
                msg = await bot.get_messages(username, msgid)
                if not msg: break
                if not passed_content_filters(str_uid, msg, f"{username}_{msgid}"): break

                final_cap = user_config["caption"] if user_config["use_caption"] else (msg.caption or "")

                if msg.document:
                    await bot.send_document(original_msg.chat.id, msg.document.file_id, caption=final_cap, reply_to_message_id=original_msg.id)
                elif msg.video:
                    await bot.send_video(original_msg.chat.id, msg.video.file_id, caption=final_cap, reply_to_message_id=original_msg.id)
                elif msg.photo:
                    await bot.send_photo(original_msg.chat.id, msg.photo.file_id, caption=final_cap, reply_to_message_id=original_msg.id)
                elif msg.text:
                    await bot.send_message(original_msg.chat.id, user_config["caption"] if user_config["use_caption"] else msg.text, reply_to_message_id=original_msg.id)

                record_processed_history(str_uid, f"{username}_{msgid}")
                break

        except FloodWait as e:
            await reply(f"⏳ **Rate Limit (FloodWait)**\nPausing for `{e.value + 2}s` and retrying...")
            await asyncio.sleep(e.value + 2)
            continue
        except Exception as e:
            await reply(f"⚠️ Error: `{e}`")
            break
        finally:
            for f in [file, thumb, public_thumb]:
                if f and os.path.exists(f):
                    try: os.remove(f)
                    except: pass
            if status_msg:
                try: await status_msg.delete()
                except: pass

if __name__ == "__main__":
    try:
        from keep_alive import keep_alive
        keep_alive()
    except ImportError:
        pass
    bot.run()
