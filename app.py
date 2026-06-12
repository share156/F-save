import threading
import asyncio
from flask import Flask
import main  # Imports your main.py file

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Service is live and the bot is running!"

def run_bot():
    """Runs the asynchronous bot in a dedicated background thread."""
    # Create a new asyncio event loop for this specific background thread.
    # This is required for async libraries (like Pyrogram) to run safely outside the main thread.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        main.start_bot()
    except Exception as e:
        print(f"Bot encountered a fatal error: {e}")

# Initialize and start the background thread for the bot
bot_thread = threading.Thread(target=run_bot, daemon=True)
bot_thread.start()

# Local testing fallback (Render uses Gunicorn to run this instead)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
