import os
from flask import Flask
from flask_socketio import SocketIO

socketio = SocketIO(cors_allowed_origins="*")


def create_app():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    app = Flask(
        __name__,
        static_folder=os.path.join(base_dir, "static"),
        template_folder=os.path.join(base_dir, "templates"),
    )
    socketio.init_app(app)

    from .routes import register_routes
    register_routes(app)
    return app
