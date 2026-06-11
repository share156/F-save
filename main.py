import pyrogram
from pyrogram import Client, filters
from pyrogram.errors import (
    UserAlreadyParticipant, InviteHashExpired, FloodWait,
    PeerIdInvalid, SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    ChannelPrivate, UserNotParticipant, ChatForbidden,
    RPCError
)
from pyrogram.types import Message

import asyncio
import time
import os
import re
import json

# --------------- Configuration ---------------
bot_token = os.environ.get("TOKEN", "")
api_hash = os.environ.get("HASH", "")  # Main bot API hash
api_id = os.environ.get("ID", "")      # Main bot API ID

bot = Client("mybot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

# --------------- Local Session Storage ---------------
SESSION_FILE = "sessions.json"
USER_SESSIONS = {}
USER_STATES = {}     # Tracks login conversation states: user_id -> string
ACTIVE_LOGINS = {}   # Tracks temporary setup clients: user_id -> data dict

# Global live client instances to maintain peer cache & fast connections
RUNNING_CLIENTS = {} # user_id -> active Client instance

def load_sessions():
    global USER_SESSIONS
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                USER_SESSIONS = json.load(f)
        except Exception as e:
            print(f"Error loading sessions file: {e}")
            USER_SESSIONS = {}

def save_sessions():
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(USER_SESSIONS, f, indent=4)
    except Exception as e:
        print(f"Error saving sessions file: {e}")

# Load active storage databases at startup
load_sessions()

# --------------- Userbot Client Persistence Helper ---------------
async def get_user_client(uid, user_data):
    """Retrieves an existing running client or instantiates a reusable one to preserve peer cache."""
    if uid in RUNNING_CLIENTS:
        client = RUNNING_CLIENTS[uid]
        if client.is_connected:
            return client
        try:
            await client.start()
            return client
        except Exception:
            try: await client.stop()
            except: pass
            RUNNING_CLIENTS.pop(uid, None)

    # Spawn fresh in-memory persistent runner instance
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

# --------------- Helper Formatter Utilities ---------------
def get_readable_size(bytes_count):
    """Converts a raw integer of bytes into a human-readable metric string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_count < 1024.0:
            return f"{bytes_count:.2f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.2f} PB"

def get_readable_time(seconds):
    """Converts seconds into an intuitive duration string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m {int(seconds % 60)}s"
    hours = minutes / 60
    return f"{int(hours)}h {int(minutes % 60)}m"

# --------------- Dynamic Visual Progress Callback ---------------
async def progress_cb(current, total, status_message, action_text, tracking_ctx):
    if not total:
        return
        
    now = time.time()
    if now - tracking_ctx.get("last_edit", 0.0) >= 3.5 or current == total:
        tracking_ctx["last_edit"] = now
        
        elapsed_time = now - tracking_ctx["start_time"]
        if elapsed_time <= 0:
            elapsed_time = 0.01
            
        speed_bps = current / elapsed_time
        percent = current * 100 / total
        
        bytes_remaining = total - current
        eta_seconds = bytes_remaining / speed_bps if speed_bps > 0 else 0
        
        readable_current = get_readable_size(current)
        readable_total = get_readable_size(total)
        readable_speed = f"{get_readable_size(speed_bps)}/s"
        readable_eta = get_readable_time(eta_seconds) if current < total else "0s"
        
        filled = int(20 * percent / 100)
        bar = f"[{'█' * filled}{'░' * (20 - filled)}] {percent:.1f}%"
        
        metrics_panel = (
            f"**{action_text}**\n\n"
            f"📊 {bar}\n"
            f"📁 **Size:** {readable_current} / {readable_total}\n"
            f"⚡ **Speed:** {readable_speed}\n"
            f"⏳ **ETA:** {readable_eta}"
        )
        
        try:
            await status_message.edit_text(metrics_panel)
        except Exception:
            pass

# --------------- Utility Command Handlers ---------------
@bot.on_message(filters.command(["start"]) & filters.private)
async def start(bot: Client, m: Message):
    uid = str(m.from_user.id)
    status = "✅ Connected" if uid in USER_SESSIONS else "❌ Not Logged In"
    
    await m.reply_text(
        f"**Welcome to the Restricted Content Saver Bot**\n\n"
        f"Your Login Status: {status}\n\n"
        "**Commands:**\n"
        "🔑 /login - Link your account securely using your own API credentials\n"
        "🚪 /logout - Unlink your session details from the bot application\n"
        "❌ /cancel - Abort your ongoing login sequence anytime\n\n"
        "**Usage:**\n"
        "Send individual message links, or download multiple items utilizing range formats:\n"
        "`https://t.me/c/xxxx/100 - 200`"
    )

@bot.on_message(filters.command(["cancel"]) & filters.private)
async def cancel_login(bot: Client, m: Message):
    uid = m.from_user.id
    str_uid = str(uid)
    
    if str_uid in USER_STATES:
        USER_STATES.pop(str_uid, None)
        if uid in ACTIVE_LOGINS:
            try:
                await ACTIVE_LOGINS[uid]["client"].disconnect()
            except: pass
            ACTIVE_LOGINS.pop(uid, None)
        await m.reply_text("✅ Ongoing setup operations have been successfully canceled.")
    else:
        await m.reply_text("You do not have any active registration flows currently running.")

@bot.on_message(filters.command(["logout"]) & filters.private)
async def logout_user(bot: Client, m: Message):
    uid = m.from_user.id
    str_uid = str(uid)
    
    if uid in RUNNING_CLIENTS:
        try: await RUNNING_CLIENTS[uid].stop()
        except: pass
        RUNNING_CLIENTS.pop(uid, None)

    if str_uid in USER_SESSIONS:
        USER_SESSIONS.pop(str_uid, None)
        save_sessions()
        await m.reply_text("🗑️ **Your session records have been wiped cleanly.** You will now need to login again to parse private links.")
    else:
        await m.reply_text("You don't have an active login profile saved on this server.")

@bot.on_message(filters.command(["login"]) & filters.private)
async def login_start(bot: Client, m: Message):
    str_uid = str(m.from_user.id)
    if str_uid in USER_SESSIONS:
        await m.reply_text("You are already logged in! Run /logout first if you need to reconfigure accounts.")
        return
    
    USER_STATES[str_uid] = "WAITING_API_ID"
    ACTIVE_LOGINS[m.from_user.id] = {}
    await m.reply_text("🔑 **Starting Secure Userbot Registration Sequence**\n\nStep 1: Please type and send your **API ID**.\n(Obtain your credentials safely via https://my.telegram.org)")

# --------------- Conversation Engine & Main Process Filter ---------------
@bot.on_message(filters.text & filters.private)
async def handle_text_inputs(client: Client, message: Message):
    uid = message.from_user.id
    str_uid = str(uid)
    text = message.text.strip()

    if str_uid in USER_STATES:
        state = USER_STATES[str_uid]
        
        if state == "WAITING_API_ID":
            if not text.isdigit():
                await message.reply_text("❌ Invalid API ID. It must consist solely of numbers. Try sending it again:")
                return
            ACTIVE_LOGINS[uid]["api_id"] = int(text)
            USER_STATES[str_uid] = "WAITING_API_HASH"
            await message.reply_text("⚙️ **Step 2:** Received API ID. Now send your **API HASH** key string:")
            return

        elif state == "WAITING_API_HASH":
            if len(text) < 10:
                await message.reply_text("❌ Invalid API HASH key structure detected. Please review and send again:")
                return
            ACTIVE_LOGINS[uid]["api_hash"] = text
            USER_STATES[str_uid] = "WAITING_PHONE"
            await message.reply_text("📱 **Step 3:** Received API Hash. Now send your **Phone Number** with country code identifier prefix.\nExample: `+1234567890`")
            return

        elif state == "WAITING_PHONE":
            if not text.startswith("+"):
                await message.reply_text("❌ Phone number format syntax missing country identifiers. Must begin with `+`. Re-send:")
                return
            
            await message.reply_text("⏳ Generating secure authentication environment connection instance...")
            ACTIVE_LOGINS[uid]["phone"] = text
            
            try:
                temp_client = Client(
                    name=f"login_{uid}",
                    api_id=ACTIVE_LOGINS[uid]["api_id"],
                    api_hash=ACTIVE_LOGINS[uid]["api_hash"],
                    in_memory=True
                )
                await temp_client.connect()
                code_info = await temp_client.send_code(text)
                
                ACTIVE_LOGINS[uid]["client"] = temp_client
                ACTIVE_LOGINS[uid]["phone_code_hash"] = code_info.phone_code_hash
                
                USER_STATES[str_uid] = "WAITING_OTP"
                await message.reply_text("📩 **Step 4:** Code dispatched successfully! Please enter the **OTP Code** sent to your official Telegram app.\n\n*Format optimization Tip:* If code reads `12 345`, reply directly with compressed characters `12345` without blank spaces.")
            except Exception as error_msg:
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                await message.reply_text(f"❌ Failed connecting client instance: `{error_msg}`\n\nRun /login to retry configuration steps cleanly.")
            return

        elif state == "WAITING_OTP":
            clean_otp = text.replace(" ", "")
            login_data = ACTIVE_LOGINS[uid]
            temp_client = login_data["client"]
            
            try:
                await temp_client.sign_in(
                    phone_number=login_data["phone"],
                    phone_code_hash=login_data["phone_code_hash"],
                    phone_code=clean_otp
                )
                
                session_str = await temp_client.export_session_string()
                USER_SESSIONS[str_uid] = {
                    "api_id": login_data["api_id"],
                    "api_hash": login_data["api_hash"],
                    "session_string": session_str
                }
                save_sessions()
                
                try: await temp_client.disconnect()
                except: pass
                
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                await message.reply_text("🎉 **Login Successful!** Your profile is securely stored. Private links will process directly via your linked credentials.")
                
            except SessionPasswordNeeded:
                USER_STATES[str_uid] = "WAITING_2FA"
                await message.reply_text("🔐 **Step 5 (2FA Active):** Account Cloud Two-Factor authentication requested. Please provide your profile password string below:")
            except (PhoneCodeInvalid, PhoneCodeExpired):
                await message.reply_text("❌ The OTP input was incorrect or expired. Please check carefully and re-send code:")
            except Exception as e:
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                await message.reply_text(f"❌ Unexpected authentication error: `{e}`\nRun /login to reset configuration attempts.")
            return

        elif state == "WAITING_2FA":
            login_data = ACTIVE_LOGINS[uid]
            temp_client = login_data["client"]
            
            try:
                await temp_client.check_password(password=text)
                session_str = await temp_client.export_session_string()
                
                USER_SESSIONS[str_uid] = {
                    "api_id": login_data["api_id"],
                    "api_hash": login_data["api_hash"],
                    "session_string": session_str
                }
                save_sessions()
                
                try: await temp_client.disconnect()
                except: pass
                
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                await message.reply_text("🎉 **Login Successful!** Your 2FA authentication verified. Private content parsers are fully functional.")
            except Exception as error_2fa:
                await message.reply_text(f"❌ 2FA Password Verification Failed: `{error_2fa}`\nPlease enter your password again:")
            return

    # --- Link Parser Logic Blocks ---
    if "https://t.me/+" in text or "https://t.me/joinchat/" in text:
        if str_uid not in USER_SESSIONS:
            await message.reply_text("❌ **You must login first** to make your linked profile join targeted channels. Run /login.")
            return
        
        user_data = USER_SESSIONS[str_uid]
        try:
            user_acc = await get_user_client(uid, user_data)
            await user_acc.join_chat(text)
            await bot.send_message(message.chat.id, "**Successfully joined private chat via your account!**", reply_to_message_id=message.id)
        except UserAlreadyParticipant:
            await bot.send_message(message.chat.id, "**Already a member of this chat group.**", reply_to_message_id=message.id)
        except InviteHashExpired:
            await bot.send_message(message.chat.id, "**The private invitation URL has expired.**", reply_to_message_id=message.id)
        except Exception as e:
            await bot.send_message(message.chat.id, f"⚠️ Error joining chat: `{e}`", reply_to_message_id=message.id)
        return

    range_match = re.match(r'(https://t\.me/(?:c/)?[^/\s]+)(?:/\d+)?/(\d+)\s*-\s*(\d+)$', text)
    if range_match:
        base_link = range_match.group(1)
        start_id = int(range_match.group(2))
        end_id = int(range_match.group(3))

        if end_id < start_id:
            await bot.send_message(message.chat.id, "End ID must be larger than start ID.")
            return

        total = end_id - start_id + 1
        await bot.send_message(message.chat.id, f"Processing batch queue extraction: {start_id} → {end_id} ({total} links)")

        for msg_id in range(start_id, end_id + 1):
            link = f"{base_link}/{msg_id}"
            await process_single_link(link, message, current=msg_id - start_id + 1, total=total)
            await asyncio.sleep(2)
        return

    links = re.findall(r'https://t\.me/(?:c/)?[^/\s]+(?:\/\d+)?/\d+', text)
    if not links:
        return

    total = len(links)
    await bot.send_message(message.chat.id, f"Found {total} link(s). Initializing parsing queue...", reply_to_message_id=message.id)

    for i, link in enumerate(links, start=1):
        await process_single_link(link, message, current=i, total=total)
        await asyncio.sleep(2)

# --------------- Engine Loop Processor (Individual Media Tasks) ---------------
async def process_single_link(link, original_msg, current=0, total=0):
    datas = link.split("/")
    msgid = int(datas[-1])
    uid = original_msg.from_user.id
    str_uid = str(uid)

    async def reply(text):
        return await bot.send_message(original_msg.chat.id, text, reply_to_message_id=original_msg.id)

    while True:
        # --- Route 1: Target Link Points to Private Chat Configuration Content ---
        if "https://t.me/c/" in link:
            if str_uid not in USER_SESSIONS:
                await reply("❌ **You have not configured your account parameters yet.**\nUse /login first.")
                return

            chatid = int("-100" + datas[4])
            user_data = USER_SESSIONS[str_uid]
            
            try:
                user_acc = await get_user_client(uid, user_data)
                if not user_acc:
                    await reply("❌ Failed to start your session.")
                    return
            except Exception as e:
                await reply(f"❌ Failed to fetch user runner session connection: `{e}`")
                return
            
            try:
                msg = await user_acc.get_messages(chatid, msgid)
                if msg is None:
                    raise ValueError("message missing")
            except FloodWait as e:
                await asyncio.sleep(e.value)
                continue
            except (RPCError, ValueError) as e:
                err_text = str(e).lower()
                if any(keyword in err_text for keyword in [
                    "peer id invalid", "peer_id_invalid", "channel_private", "user_not_participant",
                    "chat_forbidden", "invalid", "not a member", "no such process", "message missing"
                ]):
                    status_msg_peer = await reply("🔍 Resolving chat authorization access hashes...")
                    found = False
                    try:
                        async for dialog in user_acc.get_dialogs():
                            if dialog.chat.id == chatid:
                                found = True
                                break
                    except Exception as d_err:
                        try: await status_msg_peer.delete()
                        except: pass
                        await reply(f"⚠️ Failed parsing account dialog directory: `{d_err}`")
                        return

                    try: await status_msg_peer.delete()
                    except: pass

                    if found:
                        try:
                            msg = await user_acc.get_messages(chatid, msgid)
                        except Exception as retry_err:
                            await reply(f"❌ Chat verified but message collection failed: `{retry_err}`")
                            return
                    else:
                        await reply("❌ **Your logged‑in account is not a member of this private chat.**")
                        return
                else:
                    await reply(f"⚠️ Telegram API error: `{e}`")
                    return
            except Exception as e:
                await reply(f"⚠️ Unexpected error while fetching message: `{e}`")
                return

            if msg is None:
                await reply(f"❌ Target item data was missing or not found on server: {link}")
                return

            # Check if there is any actual physical file attached to download
            has_downloadable_media = any([
                msg.document, msg.video, msg.animation, 
                msg.sticker, msg.voice, msg.audio, msg.photo
            ])

            # --- DYNAMIC CRASH RECOVERY FALLBACK ---
            if not has_downloadable_media:
                text_to_send = msg.text or msg.caption
                if text_to_send:
                    await bot.send_message(
                        original_msg.chat.id, 
                        text_to_send, 
                        entities=msg.entities or msg.caption_entities, 
                        reply_to_message_id=original_msg.id
                    )
                else:
                    # If it completely fails to identify media but you know a video is there, 
                    # we trigger an aggressive system-level message copy fallback directly using the userbot string.
                    fallback_notice = await reply("🔄 Dynamic media payload missing. Attempting deep structural bypass...")
                    try:
                        await user_acc.forward_messages(original_msg.chat.id, chatid, msgid)
                        await fallback_notice.delete()
                    except Exception as f_err:
                        await fallback_notice.edit_text(
                            f"❌ Protected/Empty Link Bypass Failed.\n"
                            f"Reason: This link is highly protected, restricted from saving, or empty structural metadata."
                        )
                break

            # --- Processing Downloader Visualizations ---
            status_msg = await reply("⬇️ Connecting to target user server storage files...")
            
            download_ctx = {"start_time": time.time(), "last_edit": 0.0}
            upload_ctx = {"start_time": time.time(), "last_edit": 0.0}

            try:
                file = await user_acc.download_media(
                    msg, 
                    progress=progress_cb, 
                    progress_args=(status_msg, f"📥 Downloading message {current}/{total}", download_ctx)
                )

                thumb = None
                if msg.document and msg.document.thumbs:
                    try: thumb = await user_acc.download_media(msg.document.thumbs[0].file_id)
                    except: pass
                elif msg.video and msg.video.thumbs:
                    try: thumb = await user_acc.download_media(msg.video.thumbs[0].file_id)
                    except: pass
                elif msg.audio and msg.audio.thumbs:
                    try: thumb = await user_acc.download_media(msg.audio.thumbs[0].file_id)
                    except: pass

                upload_ctx["start_time"] = time.time()
                
                if msg.document:
                    await bot.send_document(original_msg.chat.id, file, thumb=thumb,
                                      caption=msg.caption, caption_entities=msg.caption_entities,
                                      reply_to_message_id=original_msg.id, progress=progress_cb,
                                      progress_args=(status_msg, f"📤 Uploading message {current}/{total}", upload_ctx))
                elif msg.video:
                    await bot.send_video(original_msg.chat.id, file, duration=msg.video.duration,
                                   width=msg.video.width, height=msg.video.height, thumb=thumb,
                                   caption=msg.caption, caption_entities=msg.caption_entities,
                                   reply_to_message_id=original_msg.id, progress=progress_cb,
                                   progress_args=(status_msg, f"📤 Uploading message {current}/{total}", upload_ctx))
                elif msg.animation:
                    await bot.send_animation(original_msg.chat.id, file, reply_to_message_id=original_msg.id)
                elif msg.sticker:
                    await bot.send_sticker(original_msg.chat.id, file, reply_to_message_id=original_msg.id)
                elif msg.voice:
                    await bot.send_voice(original_msg.chat.id, file, caption=msg.caption, reply_to_message_id=original_msg.id)
                elif msg.audio:
                    await bot.send_audio(original_msg.chat.id, file, caption=msg.caption,
                                   caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)
                elif msg.photo:
                    await bot.send_photo(original_msg.chat.id, file, caption=msg.caption,
                                   caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)

                if file and os.path.exists(file): os.remove(file)
                if thumb and os.path.exists(thumb): os.remove(thumb)
                
                try: await status_msg.delete()
                except: pass
                break 

            except FloodWait as e:
                await asyncio.sleep(e.value)
                continue
            except Exception as e:
                await reply(f"⚠️ Processing pipeline failed on link: {link} – Trace: `{e}`")
                break

        # --- Route 2: Target Link Points to Public Chat Settings ---
        else:
            username = datas[-2]
            try:
                msg = await bot.get_messages(username, msgid)
                if msg is None:
                    await reply(f"❌ Message not found: {link}")
                    return

                if msg.document:
                    await bot.send_document(original_msg.chat.id, msg.document.file_id,
                                      caption=msg.caption, caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)
                elif msg.video:
                    await bot.send_video(original_msg.chat.id, msg.video.file_id,
                                   caption=msg.caption, caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)
                elif msg.animation:
                    await bot.send_animation(original_msg.chat.id, msg.animation.file_id, reply_to_message_id=original_msg.id)
                elif msg.sticker:
                    await bot.send_sticker(original_msg.chat.id, msg.sticker.file_id, reply_to_message_id=original_msg.id)
                elif msg.voice:
                    await bot.send_voice(original_msg.chat.id, msg.voice.file_id,
                                   caption=msg.caption, caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)
                elif msg.audio:
                    await bot.send_audio(original_msg.chat.id, msg.audio.file_id,
                                   caption=msg.caption, caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)
                elif msg.text:
                    await bot.send_message(original_msg.chat.id, msg.text, entities=msg.entities, reply_to_message_id=original_msg.id)
                elif msg.photo:
                    await bot.send_photo(original_msg.chat.id, msg.photo.file_id,
                                   caption=msg.caption, caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)

                break

            except FloodWait as e:
                await asyncio.sleep(e.value)
                continue 
            except Exception as e:
                await reply(f"⚠️ Public parsing operation error: {link} – `{e}`")
                break 

# --------------- Start Application Execution ---------------
bot.run()
