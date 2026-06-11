import pyrogram
from pyrogram import Client, filters
from pyrogram.errors import (
    UserAlreadyParticipant, InviteHashExpired, FloodWait, 
    PeerIdInvalid, SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired
)
from pyrogram.types import Message

import time
import os
import threading
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

# --------------- Progress helpers ---------------
def progress_bar(percent):
    """Returns a visual progress bar string."""
    filled = int(20 * percent / 100)
    return f"[{'█' * filled}{'░' * (20 - filled)}] {percent:.1f}%"

def make_progress_callback(status_filename):
    """Writes download/upload percentage to a file (called by Pyrogram)."""
    def progress(current, total):
        with open(status_filename, "w") as f:
            f.write(f"{current * 100 / total:.1f}")
    return progress

# --------------- Real‑time status updater (runs in a thread) ---------------
def status_updater(status_file, status_message, total_message_count=None):
    """Reads percentages from the active status file and edits tracking text frames."""
    last_text = ""
    while True:
        if os.path.exists(status_file):
            with open(status_file, "r") as f:
                percent_str = f.read().strip()
            if percent_str:
                try:
                    percent = float(percent_str)
                    bar = progress_bar(percent)
                    if total_message_count:
                        new_text = f"{bar}\n📦 Processing message {total_message_count}"
                    else:
                        new_text = bar
                    if new_text != last_text:
                        bot.edit_message_text(
                            status_message.chat.id,
                            status_message.id,
                            new_text
                        )
                        last_text = new_text
                except:
                    pass
            time.sleep(2)
        else:
            time.sleep(1)

# --------------- Utility Command Handlers ---------------
@bot.on_message(filters.command(["start"]))
def start(bot: Client, m: Message):
    uid = str(m.from_user.id)
    status = "✅ Connected" if uid in USER_SESSIONS else "❌ Not Logged In"
    
    m.reply_text(
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

@bot.on_message(filters.command(["cancel"]))
def cancel_login(bot: Client, m: Message):
    uid = m.from_user.id
    str_uid = str(uid)
    
    if str_uid in USER_STATES:
        USER_STATES.pop(str_uid, None)
        if uid in ACTIVE_LOGINS:
            try:
                ACTIVE_LOGINS[uid]["client"].disconnect()
            except: pass
            ACTIVE_LOGINS.pop(uid, None)
        m.reply_text("✅ Ongoing setup operations have been successfully canceled.")
    else:
        m.reply_text("You do not have any active registration flows currently running.")

@bot.on_message(filters.command(["logout"]))
def logout_user(bot: Client, m: Message):
    str_uid = str(m.from_user.id)
    if str_uid in USER_SESSIONS:
        USER_SESSIONS.pop(str_uid, None)
        save_sessions()
        m.reply_text("🗑️ **Your session records have been wiped cleanly.** You will now need to login again to parse private links.")
    else:
        m.reply_text("You don't have an active login profile saved on this server.")

@bot.on_message(filters.command(["login"]))
def login_start(bot: Client, m: Message):
    str_uid = str(m.from_user.id)
    if str_uid in USER_SESSIONS:
        m.reply_text("You are already logged in! Run /logout first if you need to reconfigure accounts.")
        return
    
    USER_STATES[str_uid] = "WAITING_API_ID"
    ACTIVE_LOGINS[m.from_user.id] = {}
    m.reply_text("🔑 **Starting Secure Userbot Registration Sequence**\n\nStep 1: Please type and send your **API ID**.\n(Obtain your credentials safely via https://my.telegram.org)")

# --------------- Conversation Engine & Main Process Filter ---------------
@bot.on_message(filters.text & filters.private)
def handle_text_inputs(client: Client, message: Message):
    uid = message.from_user.id
    str_uid = str(uid)
    text = message.text.strip()

    # --- Check Login State Machine Flow First ---
    if str_uid in USER_STATES:
        state = USER_STATES[str_uid]
        
        if state == "WAITING_API_ID":
            if not text.isdigit():
                message.reply_text("❌ Invalid API ID. It must consist solely of numbers. Try sending it again:")
                return
            ACTIVE_LOGINS[uid]["api_id"] = int(text)
            USER_STATES[str_uid] = "WAITING_API_HASH"
            message.reply_text("⚙️ **Step 2:** Received API ID. Now send your **API HASH** key string:")
            return

        elif state == "WAITING_API_HASH":
            if len(text) < 10:
                message.reply_text("❌ Invalid API HASH key structure detected. Please review and send again:")
                return
            ACTIVE_LOGINS[uid]["api_hash"] = text
            USER_STATES[str_uid] = "WAITING_PHONE"
            message.reply_text("📱 **Step 3:** Received API Hash. Now send your **Phone Number** with country code identifier prefix.\nExample: `+1234567890`")
            return

        elif state == "WAITING_PHONE":
            if not text.startswith("+"):
                message.reply_text("❌ Phone number format syntax missing country identifiers. Must begin with `+`. Re-send:")
                return
            
            message.reply_text("⏳ Generating secure authentication environment connection instance...")
            ACTIVE_LOGINS[uid]["phone"] = text
            
            try:
                # Spawn dynamic in-memory authentication client running on user's target API credentials
                temp_client = Client(
                    name=f"login_{uid}",
                    api_id=ACTIVE_LOGINS[uid]["api_id"],
                    api_hash=ACTIVE_LOGINS[uid]["api_hash"],
                    in_memory=True
                )
                temp_client.connect()
                code_info = temp_client.send_code(text)
                
                ACTIVE_LOGINS[uid]["client"] = temp_client
                ACTIVE_LOGINS[uid]["phone_code_hash"] = code_info.phone_code_hash
                
                USER_STATES[str_uid] = "WAITING_OTP"
                message.reply_text("📩 **Step 4:** Code dispatched successfully! Please enter the **OTP Code** sent to your official Telegram app.\n\n*Format optimization Tip:* If code reads `12 345`, reply directly with compressed characters `12345` without blank spaces.")
            except Exception as error_msg:
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                message.reply_text(f"❌ Failed connecting client instance: `{error_msg}`\n\nRun /login to retry configuration steps cleanly.")
            return

        elif state == "WAITING_OTP":
            clean_otp = text.replace(" ", "")
            login_data = ACTIVE_LOGINS[uid]
            temp_client = login_data["client"]
            
            try:
                temp_client.sign_in(
                    phone_number=login_data["phone"],
                    phone_code_hash=login_data["phone_code_hash"],
                    phone_code=clean_otp
                )
                
                # Sign in complete without 2FA
                session_str = temp_client.export_session_string()
                USER_SESSIONS[str_uid] = {
                    "api_id": login_data["api_id"],
                    "api_hash": login_data["api_hash"],
                    "session_string": session_str
                }
                save_sessions()
                
                try: temp_client.disconnect()
                except: pass
                
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                message.reply_text("🎉 **Login Successful!** Your profile is securely stored. Private links will process directly via your linked credentials.")
                
            except SessionPasswordNeeded:
                USER_STATES[str_uid] = "WAITING_2FA"
                message.reply_text("🔐 **Step 5 (2FA Active):** Account Cloud Two-Factor authentication requested. Please provide your profile password string below:")
            except (PhoneCodeInvalid, PhoneCodeExpired):
                message.reply_text("❌ The OTP input was incorrect or expired. Please check carefully and re-send code:")
            except Exception as e:
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                message.reply_text(f"❌ Unexpected authentication error: `{e}`\nRun /login to reset configuration attempts.")
            return

        elif state == "WAITING_2FA":
            login_data = ACTIVE_LOGINS[uid]
            temp_client = login_data["client"]
            
            try:
                temp_client.check_password(password=text)
                session_str = temp_client.export_session_string()
                
                USER_SESSIONS[str_uid] = {
                    "api_id": login_data["api_id"],
                    "api_hash": login_data["api_hash"],
                    "session_string": session_str
                }
                save_sessions()
                
                try: temp_client.disconnect()
                except: pass
                
                USER_STATES.pop(str_uid, None)
                ACTIVE_LOGINS.pop(uid, None)
                message.reply_text("🎉 **Login Successful!** Your 2FA authentication verified. Private content parsers are fully functional.")
            except Exception as error_2fa:
                message.reply_text(f"❌ 2FA Password Verification Failed: `{error_2fa}`\nPlease enter your password again:")
            return

    # --- Link Parser Logic Blocks (Normal Non-State Text Checks) ---
    # 1. Join private invitation codes
    if "https://t.me/+" in text or "https://t.me/joinchat/" in text:
        if str_uid not in USER_SESSIONS:
            message.reply_text("❌ **You must login first** to make your linked profile join targeted channels. Run /login.")
            return
        
        user_data = USER_SESSIONS[str_uid]
        user_acc = Client(f"run_{uid}", api_id=user_data["api_id"], api_hash=user_data["api_hash"], session_string=user_data["session_string"])
        try:
            with user_acc:
                user_acc.join_chat(text)
            bot.send_message(message.chat.id, "**Successfully joined private chat via your account!**", reply_to_message_id=message.id)
        except UserAlreadyParticipant:
            bot.send_message(message.chat.id, "**Already a member of this chat group.**", reply_to_message_id=message.id)
        except InviteHashExpired:
            bot.send_message(message.chat.id, "**The private invitation URL has expired.**", reply_to_message_id=message.id)
        except Exception as e:
            bot.send_message(message.chat.id, f"⚠️ Error joining chat: `{e}`", reply_to_message_id=message.id)
        return

    # 2. Sequential ranges handling
    range_match = re.match(r'(https://t\.me/(?:c/)?[^/\s]+)/(\d+)\s*-\s*(\d+)$', text)
    if range_match:
        base_link = range_match.group(1)
        start_id = int(range_match.group(2))
        end_id = int(range_match.group(3))

        if end_id < start_id:
            bot.send_message(message.chat.id, "End ID must be larger than start ID.")
            return

        total = end_id - start_id + 1
        bot.send_message(message.chat.id, f"Processing batch queue extraction: {start_id} → {end_id} ({total} links)")

        for msg_id in range(start_id, end_id + 1):
            link = f"{base_link}/{msg_id}"
            process_single_link(link, message, current=msg_id - start_id + 1, total=total)
            time.sleep(2)
        return

    # 3. Direct Bulk Link Detections
    links = re.findall(r'https://t\.me/(?:c/)?[^/\s]+/\d+', text)
    if not links:
        return

    total = len(links)
    bot.send_message(message.chat.id, f"Found {total} link(s). Initializing parsing queue...", reply_to_message_id=message.id)

    for i, link in enumerate(links, start=1):
        process_single_link(link, message, current=i, total=total)
        time.sleep(2)

# --------------- Engine Loop Processor (Individual Media Tasks) ---------------
def process_single_link(link, original_msg, current=0, total=0):
    datas = link.split("/")
    msgid = int(datas[-1])
    uid = original_msg.from_user.id
    str_uid = str(uid)

    def reply(text):
        return bot.send_message(original_msg.chat.id, text, reply_to_message_id=original_msg.id)

    while True:
        # --- Route 1: Target Link Points to Private Chat Configuration Content ---
        if "https://t.me/c/" in link:
            if str_uid not in USER_SESSIONS:
                reply("❌ **You have not configured your account parameters yet.**\nUse /login first to handle tracking operations on private chat URLs safely via your own session.")
                return

            chatid = int("-100" + datas[-2])
            user_data = USER_SESSIONS[str_uid]
            
            # Instantiate user's account client dynamically for processing private downloads safely
            user_acc = Client(
                name=f"run_{uid}",
                api_id=user_data["api_id"],
                api_hash=user_data["api_hash"],
                session_string=user_data["session_string"]
            )
            
            try:
                with user_acc:
                    try:
                        user_acc.get_chat(chatid)
                    except PeerIdInvalid:
                        reply("❌ **Your logged in userbot account is not a member of this private chat group.**\nPlease join the chat first using your profile link.")
                        return

                    msg = user_acc.get_messages(chatid, msgid)
                
                if msg is None:
                    reply(f"❌ Target item data was missing or not found: {link}")
                    return

                if "text" in str(msg) and not msg.media:
                    reply(msg.text)
                    return 

                # --- Processing Downloader Visualizations ---
                sid = f"{original_msg.id}_{current}"
                down_file = f"{sid}downstatus.txt"
                up_file = f"{sid}upstatus.txt"

                status_msg = reply("⬇️ Connecting to target user server storage files...")
                tracker = threading.Thread(target=status_updater, args=(down_file, status_msg, f"{current}/{total}"), daemon=True)
                tracker.start()

                # Download media using user's custom client
                with user_acc:
                    file = user_acc.download_media(msg, progress=make_progress_callback(down_file))
                
                if os.path.exists(down_file):
                    os.remove(down_file)

                up_tracker = threading.Thread(target=status_updater, args=(up_file, status_msg, f"{current}/{total}"), daemon=True)
                up_tracker.start()

                thumb = None
                with user_acc:
                    if "Document" in str(msg) and msg.document.thumbs:
                        try: thumb = user_acc.download_media(msg.document.thumbs[0].file_id)
                        except: pass
                    elif "Video" in str(msg) and msg.video.thumbs:
                        try: thumb = user_acc.download_media(msg.video.thumbs[0].file_id)
                        except: pass
                    elif "Audio" in str(msg) and msg.audio.thumbs:
                        try: thumb = user_acc.download_media(msg.audio.thumbs[0].file_id)
                        except: pass

                # Forward cleanly into target bot chat output allocations
                if "Document" in str(msg):
                    bot.send_document(original_msg.chat.id, file, thumb=thumb,
                                      caption=msg.caption, caption_entities=msg.caption_entities,
                                      reply_to_message_id=original_msg.id, progress=make_progress_callback(up_file))
                elif "Video" in str(msg):
                    bot.send_video(original_msg.chat.id, file, duration=msg.video.duration,
                                   width=msg.video.width, height=msg.video.height, thumb=thumb,
                                   caption=msg.caption, caption_entities=msg.caption_entities,
                                   reply_to_message_id=original_msg.id, progress=make_progress_callback(up_file))
                elif "Animation" in str(msg):
                    bot.send_animation(original_msg.chat.id, file, reply_to_message_id=original_msg.id)
                elif "Sticker" in str(msg):
                    bot.send_sticker(original_msg.chat.id, file, reply_to_message_id=original_msg.id)
                elif "Voice" in str(msg):
                    bot.send_voice(original_msg.chat.id, file, caption=msg.caption, reply_to_message_id=original_msg.id)
                elif "Audio" in str(msg):
                    bot.send_audio(original_msg.chat.id, file, caption=msg.caption,
                                   caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)
                elif "Photo" in str(msg):
                    bot.send_photo(original_msg.chat.id, file, caption=msg.caption,
                                   caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)

                # Post processing system scrubbing sweeps
                if os.path.exists(file): os.remove(file)
                if thumb and os.path.exists(thumb): os.remove(thumb)
                if os.path.exists(up_file): os.remove(up_file)
                
                try: bot.delete_messages(original_msg.chat.id, [status_msg.id])
                except: pass

                break # Completed processing without crashing loops

            except FloodWait as e:
                time.sleep(e.value)
                reply(f"⚠️ Flood wait limits triggered – retrying same item index after waiting {e.value}s")
                continue
            except Exception as e:
                reply(f"⚠️ Extraction block error on link: {link} – Trace: `{e}`")
                break

        # --- Route 2: Target Link Points to Public Chat Settings ---
        else:
            username = datas[-2]
            try:
                msg = bot.get_messages(username, msgid)
                if msg is None:
                    reply(f"❌ Message not found: {link}")
                    return

                if "Document" in str(msg):
                    bot.send_document(original_msg.chat.id, msg.document.file_id,
                                      caption=msg.caption, caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)
                elif "Video" in str(msg):
                    bot.send_video(original_msg.chat.id, msg.video.file_id,
                                   caption=msg.caption, caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)
                elif "Animation" in str(msg):
                    bot.send_animation(original_msg.chat.id, msg.animation.file_id, reply_to_message_id=original_msg.id)
                elif "Sticker" in str(msg):
                    bot.send_sticker(original_msg.chat.id, msg.sticker.file_id, reply_to_message_id=original_msg.id)
                elif "Voice" in str(msg):
                    bot.send_voice(original_msg.chat.id, msg.voice.file_id,
                                   caption=msg.caption, caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)
                elif "Audio" in str(msg):
                    bot.send_audio(original_msg.chat.id, msg.audio.file_id,
                                   caption=msg.caption, caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)
                elif "text" in str(msg):
                    bot.send_message(original_msg.chat.id, msg.text, entities=msg.entities, reply_to_message_id=original_msg.id)
                elif "Photo" in str(msg):
                    bot.send_photo(original_msg.chat.id, msg.photo.file_id,
                                   caption=msg.caption, caption_entities=msg.caption_entities, reply_to_message_id=original_msg.id)

                break

            except FloodWait as e:
                time.sleep(e.value)
                reply(f"⚠️ Flood wait – resuming extraction task in {e.value}s")
                continue 
            except Exception as e:
                reply(f"⚠️ Public parsing operation error: {link} – `{e}`")
                break 

# --------------- Start Application Execution ---------------
bot.run()

