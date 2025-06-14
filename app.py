"""
LADA – Local Agent Driven Assistant  v0.2
"""
import os
import webbrowser
from backend import create_app, socketio

app = create_app()

if __name__ == "__main__":
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        webbrowser.open("http://127.0.0.1:5000")  # auto-open browser once
    socketio.run(app, debug=True, use_reloader=False)
