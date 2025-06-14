"""
LADA – Local Agent Driven Assistant  v0.2
"""
import webbrowser
from backend import create_app, socketio

app = create_app()

if __name__ == "__main__":
    webbrowser.open("http://127.0.0.1:5000")  # auto-open browser
    socketio.run(app, debug=True)
