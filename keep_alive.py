from flask import Flask
from threading import Thread
import os

app = Flask(__name__)

@app.route('/')
def hello_world():
    return 'GreyMatters'

def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run, daemon=True)
    t.start()
