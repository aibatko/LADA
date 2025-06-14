from flask import Flask
from flask_socketio import SocketIO

socketio = SocketIO(cors_allowed_origins="*")


def create_app():
    app = Flask(__name__, static_folder="static", template_folder="templates")
    socketio.init_app(app)

    from .routes import register_routes
    register_routes(app)
    return app
