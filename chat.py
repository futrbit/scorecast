# chat.py
from flask_socketio import SocketIO, emit
from flask import session
from datetime import datetime

socketio = SocketIO()

def init_chat(app):
    socketio.init_app(app)

    @socketio.on('send_message')
    def handle_message(data):
        username = session.get('username', 'Anonymous')
        message = data['message']
        if message.strip():
            emit('receive_message', {
                'username': username,
                'message': message,
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }, broadcast=True)