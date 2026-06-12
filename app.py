import threading
from flask import Flask
# Import your bot's startup function from main.py
from main import start_bot 

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

if __name__ == "__main__":
    # This keeps Gunicorn from blocking the bot
    pass

# Start the bot in a separate thread before Gunicorn takes over
threading.Thread(target=start_bot, daemon=True).start()
