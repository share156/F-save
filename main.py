import pyrogram
from pyrogram import Client, filters
from pyrogram.errors import UserAlreadyParticipant, InviteHashExpired, FloodWait, PeerIdInvalid
from pyrogram.types import Message

import time
import os
import threading
import re

# --------------- Configuration ---------------
bot_token = os.environ.get("TOKEN", "")
api_hash = os.environ.get("HASH", "")
api_id = os.environ.get("ID", "")
ss = os.environ.get("STRING", "")

bot = Client("mybot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)
acc = Client("myacc", api_id=api_id, api_hash=api_hash, session_string=ss)

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
    """
    Reads the percentage from the status file every 2 seconds and
    updates the message with a progress bar.
    """
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

# --------------- Start command ---------------
@bot.on_message(filters.command(["start"]))
async def start(bot: Client, m: Message):
    await m.reply_text(
        "**I am a simple save restricted bot.**\n\n"
        "Send one or more message links, or a range like:\n"
        "`https://t.me/channel/100 - 200`\n\n"
        "For private chats, send the invite link first."
    )

# --------------- Bulk command (informational) ---------------
@bot.on_message(filters.command(["bulk"]))
async def bulk_info(bot: Client, m: Message):
    await m.reply_text(
        "Send multiple links in one message (one per line) or a range like:\n"
        "`https://t.me/c/xxx/100 - 200`"
    )

# --------------- Main handler ---------------
@bot.on_message(filters.text)
def save(client: Client, message: Message):
    text = message.text.strip()

    # 1. Join private chat
    if "https://t.me/+" in text or "https://t.me/joinchat/" in text:
        try:
            with acc:
                acc.join_chat(text)
            bot.send_message(message.chat.id, "**Successfully joined the chat**", reply_to_message_id=message.id)
        except UserAlreadyParticipant:
            bot.send_message(message.chat.id, "**Already a member**", reply_to_message_id=message.id)
        except InviteHashExpired:
            bot.send_message(message.chat.id, "**Invite link has expired.**", reply_to_message_id=message.id)
        return

    # 2. Range detection
    range_match = re.match(
        r'(https://t\.me/(?:c/)?[^/\s]+)/(\d+)\s*-\s*(\d+)$', text
    )
    if range_match:
        base_link = range_match.group(1)
        start_id = int(range_match.group(2))
        end_id = int(range_match.group(3))

        if end_id < start_id:
            bot.send_message(message.chat.id, "End ID must be larger than start ID.")
            return

        total = end_id - start_id + 1
        bot.send_message(message.chat.id, f"Processing range: {start_id} → {end_id} ({total} messages)")

        for msg_id in range(start_id, end_id + 1):
            link = f"{base_link}/{msg_id}"
            process_single_link(link, message, current=msg_id - start_id + 1, total=total)
            time.sleep(2)
        return

    # 3. Multiple links (bulk)
    links = re.findall(r'https://t\.me/(?:c/)?[^/\s]+/\d+', text)
    if not links:
        return

    total = len(links)
    bot.send_message(message.chat.id, f"Found {total} link(s). Processing…", reply_to_message_id=message.id)

    for i, link in enumerate(links, start=1):
        process_single_link(link, message, current=i, total=total)
        time.sleep(2)

# --------------- Process one message link ---------------
def process_single_link(link, original_msg, current=0, total=0):
    datas = link.split("/")
    msgid = int(datas[-1])

    def reply(text):
        bot.send_message(original_msg.chat.id, text, reply_to_message_id=original_msg.id)

    # --- Private chat ---
    if "https://t.me/c/" in link:
        chatid = int("-100" + datas[-2])
        try:
            with acc:
                try:
                    acc.get_chat(chatid)
                except PeerIdInvalid:
                    reply("❌ **Your session account is not a member of this private chat.**\n"
                          "Send the correct invite link first.")
                    return

                msg = acc.get_messages(chatid, msgid)
            if msg is None:
                reply(f"❌ Message not found: {link}")
                return

            if "text" in str(msg):
                reply(msg.text)
                return

            # --- Download & Upload with real‑time progress bar ---
            sid = f"{original_msg.id}_{current}"
            down_file = f"{sid}downstatus.txt"
            up_file = f"{sid}upstatus.txt"

            # Create a single status message that will be updated throughout
            status_msg = reply("⬇️ Preparing download...")
            tracker = threading.Thread(target=status_updater, args=(down_file, status_msg, f"{current}/{total}"), daemon=True)
            tracker.start()

            file = acc.download_media(msg, progress=make_progress_callback(down_file))
            os.remove(down_file)

            # Switch to upload progress
            up_tracker = threading.Thread(target=status_updater, args=(up_file, status_msg, f"{current}/{total}"), daemon=True)
            up_tracker.start()

            thumb = None
            if "Document" in str(msg) and msg.document.thumbs:
                try:
                    with acc:
                        thumb = acc.download_media(msg.document.thumbs[0].file_id)
                except: pass
            elif "Video" in str(msg) and msg.video.thumbs:
                try:
                    with acc:
                        thumb = acc.download_media(msg.video.thumbs[0].file_id)
                except: pass
            elif "Audio" in str(msg) and msg.audio.thumbs:
                try:
                    with acc:
                        thumb = acc.download_media(msg.audio.thumbs[0].file_id)
                except: pass

            if "Document" in str(msg):
                bot.send_document(original_msg.chat.id, file, thumb=thumb,
                                  caption=msg.caption, caption_entities=msg.caption_entities,
                                  reply_to_message_id=original_msg.id,
                                  progress=make_progress_callback(up_file))
            elif "Video" in str(msg):
                bot.send_video(original_msg.chat.id, file, duration=msg.video.duration,
                               width=msg.video.width, height=msg.video.height, thumb=thumb,
                               caption=msg.caption, caption_entities=msg.caption_entities,
                               reply_to_message_id=original_msg.id,
                               progress=make_progress_callback(up_file))
            elif "Animation" in str(msg):
                bot.send_animation(original_msg.chat.id, file, reply_to_message_id=original_msg.id)
            elif "Sticker" in str(msg):
                bot.send_sticker(original_msg.chat.id, file, reply_to_message_id=original_msg.id)
            elif "Voice" in str(msg):
                bot.send_voice(original_msg.chat.id, file, caption=msg.caption,
                               reply_to_message_id=original_msg.id)
            elif "Audio" in str(msg):
                bot.send_audio(original_msg.chat.id, file, caption=msg.caption,
                               caption_entities=msg.caption_entities,
                               reply_to_message_id=original_msg.id)
            elif "Photo" in str(msg):
                bot.send_photo(original_msg.chat.id, file, caption=msg.caption,
                               caption_entities=msg.caption_entities,
                               reply_to_message_id=original_msg.id)

            # Cleanup
            os.remove(file)
            if os.path.exists(up_file):
                os.remove(up_file)
            try:
                bot.delete_messages(original_msg.chat.id, [status_msg.id])
            except:
                pass

        except FloodWait as e:
            time.sleep(e.x)
            reply(f"⚠️ Flood wait – retrying after {e.x}s")
        except Exception as e:
            reply(f"⚠️ Failed: {link} – {e}")

    # --- Public chat ---
    else:
        username = datas[-2]
        try:
            msg = bot.get_messages(username, msgid)
            if msg is None:
                reply(f"❌ Message not found: {link}")
                return

            # Public chats use file IDs directly (no download progress needed)
            if "Document" in str(msg):
                bot.send_document(original_msg.chat.id, msg.document.file_id,
                                  caption=msg.caption, caption_entities=msg.caption_entities,
                                  reply_to_message_id=original_msg.id)
            elif "Video" in str(msg):
                bot.send_video(original_msg.chat.id, msg.video.file_id,
                               caption=msg.caption, caption_entities=msg.caption_entities,
                               reply_to_message_id=original_msg.id)
            elif "Animation" in str(msg):
                bot.send_animation(original_msg.chat.id, msg.animation.file_id,
                                   reply_to_message_id=original_msg.id)
            elif "Sticker" in str(msg):
                bot.send_sticker(original_msg.chat.id, msg.sticker.file_id,
                                 reply_to_message_id=original_msg.id)
            elif "Voice" in str(msg):
                bot.send_voice(original_msg.chat.id, msg.voice.file_id,
                               caption=msg.caption, caption_entities=msg.caption_entities,
                               reply_to_message_id=original_msg.id)
            elif "Audio" in str(msg):
                bot.send_audio(original_msg.chat.id, msg.audio.file_id,
                               caption=msg.caption, caption_entities=msg.caption_entities,
                               reply_to_message_id=original_msg.id)
            elif "text" in str(msg):
                bot.send_message(original_msg.chat.id, msg.text, entities=msg.entities,
                                 reply_to_message_id=original_msg.id)
            elif "Photo" in str(msg):
                bot.send_photo(original_msg.chat.id, msg.photo.file_id,
                               caption=msg.caption, caption_entities=msg.caption_entities,
                               reply_to_message_id=original_msg.id)

        except FloodWait as e:
            time.sleep(e.x)
            reply(f"⚠️ Flood wait – retrying after {e.x}s")
        except Exception as e:
            reply(f"⚠️ Failed: {link} – {e}")

# --------------- Run ---------------
bot.run()
