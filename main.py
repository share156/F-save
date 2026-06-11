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
USER_STATES = {}       # Tracks login conversation states: user_id -> string
ACTIVE_LOGINS = {}     # Tracks temporary setup clients: user_id -> data dict
CANCEL_BATCH = {}      # Tracks cancel requests for queues: user_id -> bool

# Global live client instances to maintain peer cache & fast connections
RUNNING_CLIENTS = {}   # user_id -> active Client instance

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
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_count < 1024.0:
            return f"{bytes_count:.2f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.2f} PB"

def get_readable_time(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m {int(seconds % 60)}s"
    hours = minutes / 60
    return f"{int(hours)}h {int(minutes % 60)}m"

# --------------- Dynamic Visual Progress Callback ---------------
async def progress_cb(current, total, status_message, action_text, tracking_ctx, uid):
    if CANCEL_BATCH.get(uid, False):
        raise StopIteration("User initiated download cancellation pipeline.")

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
            f"⏳ **ETA:** {readable_eta}\n\n"
            f"🛑 Run /cancel to stop processing."
        )
        
        try:
            await status_message.edit_text(metrics_panel)
        except Exception:
            pass

# --------------- Utility Command Handlers ---------------
@bot.on_message(filters.command(["start"]) & (filters.private | filters.group | filters.channel))
async def start(bot: Client, m: Message):
    # Fallback to sender ID, handle channel postings dynamically via sender_chat
    user_id = m.from_user.id if m.from_user else m.chat.id
    uid = str(user_id)
    status = "✅ Connected" if uid in USER_SESSIONS else "❌ Not Logged In"
    
    await m.reply_text(
        f"**Welcome to the Restricted Content Saver Bot**\n\n"
        f"Your Login Status: {status}\n\n"
        "**Commands:**\n"
        "🔑 /login - Link your account securely\n"
        "🚪 /logout - Unlink your session details\n"
        "❌ /cancel - Abort ongoing tasks/logins anytime\n\n"
        "**Usage:**\n"
        "Send individual message links, or download multiple items utilizing range formats:\n"
        "`https://t.me/c/xxxx/100 - 200`"
    )

@bot.on_message(filters.command(["cancel"]) & (filters.private | filters.group | filters.channel))
async def cancel_login(bot: Client, m: Message):
    user_id = m.from_user.id if m.from_user else m.chat.id
    str_uid = str(user_id)
    
    cancelled_something = False

    if str_uid in USER_STATES:
        USER_STATES.pop(str_uid, None)
        if user_id in ACTIVE_LOGINS:
            try:
                await ACTIVE_LOGINS[user_id]["client"].disconnect()
            except: pass
            ACTIVE_LOGINS.pop(user_id, None)
        cancelled_something = True

    CANCEL_BATCH[user_id] = True
    cancelled_something = True

    if cancelled_something:
        await m.reply_text("🛑 **Cancel command executed.** Ongoing processing pipelines are being completely destroyed.")
    else:
        await m.reply_text("You do not have any operations currently running.")

@bot.on_message(filters.command(["logout"]) & (filters.private | filters.group | filters.channel))
async def logout_user(bot: Client, m: Message):
    user_id = m.from_user.id if m.from_user else m.chat.id
    str_uid = str(user_id)
    
    if user_id in RUNNING_CLIENTS:
        try: await RUNNING_CLIENTS[user_id].stop()
        except: pass
        RUNNING_CLIENTS.pop(user_id, None)

    if str_uid in USER_SESSIONS:
        USER_SESSIONS.pop(str_uid, None)
        save_sessions()
        await m.reply_text("🗑️ **Your session records have been wiped cleanly.**")
    else:
        await m.reply_text("You don't have an active login profile saved on this server.")

@bot.on_message(filters.command(["login"]) & (filters.private | filters.group | filters.channel))
async def login_start(bot: Client, m: Message):
    if not m.from_user:
        await m.reply_text("❌ Account authorization configuration can only be initiated by actual users, not channels.")
        return

    uid = m.from_user.id
    str_uid = str(uid)
    if str_uid in USER_SESSIONS:
        await m.reply_text("You are already logged in! Run /logout first.")
        return
    
    # Force user to complete the sensitive credentials input inside a secure private chat loop
    if m.chat.type != pyrogram.enums.ChatType.PRIVATE:
        try:
            await bot.send_message(
                uid, 
                "🔑 **Starting Secure Userbot Registration Sequence**\n\nStep 1: Please type and send your **API ID** right here."
            )
            USER_STATES[str_uid] = "WAITING_API_ID"
            ACTIVE_LOGINS[uid] = {}
            await m.reply_text("📩 I've sent the authorization steps to your Private Messages for security!")
        except Exception:
            await m.reply_text("❌ **Could not start configuration via PM.** Please start the bot in private chat first, then run /login.")
        return

    USER_STATES[str_uid] = "WAITING_API_ID"
    ACTIVE_LOGINS[uid] = {}
    await m.reply_text("🔑 **Starting Secure Userbot Registration Sequence**\n\nStep 1: Please type and send your **API ID**.")

# --------------- Conversation Engine & Main Process Filter ---------------
@bot.on_message((filters.text | filters.caption) & (filters.private | filters.group | filters.channel))
async def handle_text_inputs(client: Client, message: Message):
    # Safely unpack text or media description payload strings
    text = (message.text or message.caption or "").strip()
    if not text:
        return

    # Check and clean group command mentions seamlessly
    if text.startswith("/"):
        # If it's a known command being processed inside a group/channel, strip clean text logic so handlers trigger smoothly
        cmd_part = text.split()[0].split("@")[0]
        if cmd_part in ["/start", "/login", "/logout", "/cancel"]:
            return

    user_id = message.from_user.id if message.from_user else message.chat.id
    str_uid = str(user_id)

    # 1. Evaluate conversation state machines (Only process within Secure Private Dialogues)
    if str_uid in USER_STATES and message.chat.type == pyrogram.enums.ChatType.PRIVATE:
        state = USER_STATES[str_uid]
        
        if state == "WAITING_API_ID":
            if not text.isdigit():
                await message.reply_text("❌ Invalid API ID. Try sending it again:")
                return
            ACTIVE_LOGINS[user_id]["api_id"] = int(text)
            USER_STATES[str_uid] = "WAITING_API_HASH"
            await message.reply_text("⚙️ **Step 2:** Received API ID. Now send your **API HASH** key string:")
            return

        elif state == "WAITING_API_HASH":
            if len(text) < 10:
                await message.reply_text("❌ Invalid API HASH key structure detected. Please review and send again:")
                return
            ACTIVE_LOGINS[user_id]["api_hash"] = text
            USER_STATES[str_uid] = "WAITING_PHONE"
            await message.reply_text("📱 **Step 3:** Received API Hash. Now send your **Phone Number**:")
            return

        elif state == "WAITING_PHONE":
            if not text.startswith("+"):
                await message.reply_text("❌ Phone number format syntax missing country identifiers. Re-send:")
                return
            
            await message.reply_text("⏳ Generating secure authentication environment connection instance...")
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
                
                ACTIVE_LOGINS[user_id]["client"] = temp_client
                ACTIVE_LOGINS[user_id]["phone_code_hash"] = code_info.phone_code_hash
                
                USER_STATES[str_uid] = "WAITING_OTP"
                await message.reply_text("📩 **Step 4:** Code dispatched successfully! Please enter the **OTP Code**:")
            except Exception as error_msg:
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(user_id, None)
                await message.reply_text(f"❌ Failed connecting client instance: `{error_msg}`")
            return

        elif state == "WAITING_OTP":
            clean_otp = text.replace(" ", "")
            login_data = ACTIVE_LOGINS[user_id]
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
                ACTIVE_LOGINS.pop(user_id, None)
                await message.reply_text("🎉 **Login Successful!**")
                
            except SessionPasswordNeeded:
                USER_STATES[str_uid] = "WAITING_2FA"
                await message.reply_text("🔐 **Step 5 (2FA Active):** Please provide your profile password string below:")
            except (PhoneCodeInvalid, PhoneCodeExpired):
                await message.reply_text("❌ The OTP input was incorrect or expired. Re-send code:")
            except Exception as e:
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(user_id, None)
                await message.reply_text(f"❌ Unexpected authentication error: `{e}`")
            return

        elif state == "WAITING_2FA":
            login_data = ACTIVE_LOGINS[user_id]
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
                ACTIVE_LOGINS.pop(user_id, None)
                await message.reply_text("🎉 **Login Successful!**")
            except Exception as error_2fa:
                await message.reply_text(f"❌ 2FA Password Verification Failed: `{error_2fa}`")
            return

    # --- Link Parser Logic Blocks ---
    if "https://t.me/+" in text or "https://t.me/joinchat/" in text:
        if str_uid not in USER_SESSIONS:
            await message.reply_text("❌ **You must login first**")
            return
        
        user_data = USER_SESSIONS[str_uid]
        try:
            user_acc = await get_user_client(user_id, user_data)
            await user_acc.join_chat(text)
            await bot.send_message(message.chat.id, "**Successfully joined private chat!**", reply_to_message_id=message.id)
        except UserAlreadyParticipant:
            await bot.send_message(message.chat.id, "**Already a member of this chat group.**", reply_to_message_id=message.id)
        except Exception as e:
            await bot.send_message(message.chat.id, f"⚠️ Error joining chat: `{e}`", reply_to_message_id=message.id)
        return

    CANCEL_BATCH[user_id] = False

    range_match = re.match(r'(https://t\.me/(?:c/)?[^/\s]+)(?:/\d+)?/(\d+)\s*-\s*(\d+)$', text)
    if range_match:
        base_link = range_match.group(1)
        start_id = int(range_match.group(2))
        end_id = int(range_match.group(3))

        if end_id < start_id:
            await bot.send_message(message.chat.id, "End ID must be larger than start ID.", reply_to_message_id=message.id)
            return

        total = end_id - start_id + 1
        await bot.send_message(message.chat.id, f"Processing batch queue extraction: {start_id} → {end_id} ({total} links)", reply_to_message_id=message.id)

        for msg_id in range(start_id, end_id + 1):
            if CANCEL_BATCH.get(user_id, False):
                break
            link = f"{base_link}/{msg_id}"
            await process_single_link(link, message, current=msg_id - start_id + 1, total=total, uid=user_id)
            await asyncio.sleep(2)
        
        if CANCEL_BATCH.get(user_id, False):
            await bot.send_message(message.chat.id, "❌ **Batch sequence cancelled completely.**", reply_to_message_id=message.id)
        return

    links = re.findall(r'https://t\.me/(?:c/)?[^/\s]+(?:\/\d+)?/\d+', text)
    if not links:
        return

    total = len(links)
    await bot.send_message(message.chat.id, f"Found {total} link(s). Initializing parsing queue...", reply_to_message_id=message.id)

    for i, link in enumerate(links, start=1):
        if CANCEL_BATCH.get(user_id, False):
            break
        await process_single_link(link, message, current=i, total=total, uid=user_id)
        await asyncio.sleep(2)
        
    if CANCEL_BATCH.get(user_id, False):
        await bot.send_message(message.chat.id, "❌ **Bulk sequence queue processing stopped.**", reply_to_message_id=message.id)

# --------------- Engine Loop Processor (Individual Media Tasks) ---------------
async def process_single_link(link, original_msg, current=0, total=0, uid=0):
    datas = link.split("/")
    msgid = int(datas[-1])
    str_uid = str(uid)

    async def reply(text):
        return await bot.send_message(original_msg.chat.id, text, reply_to_message_id=original_msg.id)

    while True:
        if CANCEL_BATCH.get(uid, False):
            return

        # --- Route 1: Target Link Points to Private Chat Configuration Content ---
        if "https://t.me/c/" in link:
            if str_uid not in USER_SESSIONS:
                await reply("❌ **You have not configured your account parameters yet.**")
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
                            if CANCEL_BATCH.get(uid, False): break
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

                    if CANCEL_BATCH.get(uid, False): return

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

            has_downloadable_media = any([
                msg.document, msg.video, msg.animation, 
                msg.sticker, msg.voice, msg.audio, msg.photo
            ])

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
                    fallback_notice = await reply("🔄 Dynamic media payload missing. Attempting deep structural bypass...")
                    try:
                        await user_acc.forward_messages(original_msg.chat.id, chatid, msgid)
                        await fallback_notice.delete()
                    except Exception as f_err:
                        await fallback_notice.edit_text(
                            f"❌ Protected/Empty Link Bypass Failed."
                        )
                break

            if CANCEL_BATCH.get(uid, False): return

            status_msg = await reply("⬇️ Connecting to target user server storage files...")
            
            download_ctx = {"start_time": time.time(), "last_edit": 0.0}
            upload_ctx = {"start_time": time.time(), "last_edit": 0.0}

            file = None
            try:
                file = await user_acc.download_media(
                    msg, 
                    progress=progress_cb, 
                    progress_args=(status_msg, f"📥 Downloading message {current}/{total}", download_ctx, uid)
                )

                if CANCEL_BATCH.get(uid, False):
                    raise StopIteration("Cancelled manually")

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

                if CANCEL_BATCH.get(uid, False):
                    raise StopIteration("Cancelled manually")

                upload_ctx["start_time"] = time.time()
                
                if msg.document:
                    await bot.send_document(original_msg.chat.id, file, thumb=thumb,
                                      caption=msg.caption, caption_entities=msg.caption_entities,
                                      reply_to_message_id=original_msg.id, progress=progress_cb,
                                      progress_args=(status_msg, f"📤 Uploading message {current}/{total}", upload_ctx, uid))
                elif msg.video:
                    await bot.send_video(original_msg.chat.id, file, duration=msg.video.duration,
                                   width=msg.video.width, height=msg.video.height, thumb=thumb,
                                   caption=msg.caption, caption_entities=msg.caption_entities,
                                   reply_to_message_id=original_msg.id, progress=progress_cb,
                                   progress_args=(status_msg, f"📤 Uploading message {current}/{total}", upload_ctx, uid))
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

            except (StopIteration, asyncio.CancelledError):
                if file and os.path.exists(file): os.remove(file)
                try: await status_msg.delete()
                except: pass
                return

            except FloodWait as e:
                await asyncio.sleep(e.value)
                continue
            except Exception as e:
                if file and os.path.exists(file): os.remove(file)
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
