"""
LAN Chat - Offline peer-to-peer chat over a phone hotspot (no internet).
One device hosts (turns on hotspot), the other joins (connects to that
hotspot's WiFi). No manual IP entry - the app finds the host itself
using a UDP broadcast on the local network.

Architecture:
  - HOST: opens a TCP server socket + listens for UDP "discovery" pings
  - CLIENT: connects to hotspot WiFi, then broadcasts a UDP ping;
            whichever device replies "I'm the host, here's my IP"
            is used to open the TCP chat connection.
  - Messages are exchanged over the persistent TCP socket.
  - Every message is saved to a local SQLite file, so history survives
    app restarts even with zero connectivity.
"""

import json
import socket
import sqlite3
import threading
import time
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.properties import StringProperty, BooleanProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.graphics import Color, RoundedRectangle
from kivy.lang import Builder

DISCOVERY_PORT = 50000
CHAT_PORT = 50001
BROADCAST_MSG = b"LANCHAT_DISCOVER"
BROADCAST_REPLY_PREFIX = b"LANCHAT_HOST:"
DB_PATH = "lanchat_history.db"

# ---------------------------------------------------------------------
# Local storage
# ---------------------------------------------------------------------

class ChatStorage:
    """Saves every message to a local SQLite DB so history persists
    across app restarts, entirely offline."""

    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT,
                    text TEXT,
                    timestamp TEXT
                )"""
            )
            self.conn.commit()

    def save(self, sender, text, timestamp):
        with self.lock:
            self.conn.execute(
                "INSERT INTO messages (sender, text, timestamp) VALUES (?, ?, ?)",
                (sender, text, timestamp),
            )
            self.conn.commit()

    def load_all(self):
        with self.lock:
            cur = self.conn.execute(
                "SELECT sender, text, timestamp FROM messages ORDER BY id ASC"
            )
            return cur.fetchall()

    def clear(self):
        with self.lock:
            self.conn.execute("DELETE FROM messages")
            self.conn.commit()


# ---------------------------------------------------------------------
# Networking - runs on background threads, talks to UI via Kivy Clock
# ---------------------------------------------------------------------

class NetworkNode:
    """
    Handles both host and client roles.

    HOST role:
      - listens on CHAT_PORT for one incoming TCP connection
      - answers UDP discovery broadcasts with its own IP

    CLIENT role:
      - sends a UDP broadcast asking "who is the host?"
      - connects via TCP to whichever IP replies first
    """

    def __init__(self, on_message, on_status):
        self.on_message = on_message   # callback(sender, text, timestamp)
        self.on_status = on_status     # callback(status_string)
        self.sock = None               # active TCP connection (either role)
        self.server_sock = None        # TCP listening socket (host only)
        self.udp_sock = None
        self.running = False
        self.role = None               # "host" or "client"

    # ---- HOST ----

    def start_host(self):
        self.role = "host"
        self.running = True
        threading.Thread(target=self._host_udp_responder, daemon=True).start()
        threading.Thread(target=self._host_tcp_listener, daemon=True).start()

    def _host_udp_responder(self):
        try:
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_sock.bind(("", DISCOVERY_PORT))
            self._notify_status("Waiting for your friend to join...")
            while self.running:
                try:
                    data, addr = self.udp_sock.recvfrom(1024)
                    if data == BROADCAST_MSG:
                        my_ip = self._get_local_ip()
                        reply = BROADCAST_REPLY_PREFIX + my_ip.encode()
                        self.udp_sock.sendto(reply, addr)
                except OSError:
                    break
        except Exception as e:
            self._notify_status(f"Discovery error: {e}")

    def _host_tcp_listener(self):
        try:
            self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_sock.bind(("", CHAT_PORT))
            self.server_sock.listen(1)
            conn, addr = self.server_sock.accept()
            self.sock = conn
            self._notify_status("Connected!")
            self._listen_loop()
        except Exception as e:
            if self.running:
                self._notify_status(f"Connection error: {e}")

    # ---- CLIENT ----

    def start_client(self):
        self.role = "client"
        self.running = True
        threading.Thread(target=self._client_discover_and_connect, daemon=True).start()

    def _client_discover_and_connect(self):
        self._notify_status("Looking for host on this network...")
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp.settimeout(2)

            host_ip = None
            attempts = 0
            while self.running and host_ip is None and attempts < 30:
                attempts += 1
                try:
                    udp.sendto(BROADCAST_MSG, ("255.255.255.255", DISCOVERY_PORT))
                    data, addr = udp.recvfrom(1024)
                    if data.startswith(BROADCAST_REPLY_PREFIX):
                        host_ip = data[len(BROADCAST_REPLY_PREFIX):].decode()
                except socket.timeout:
                    continue

            udp.close()

            if not host_ip:
                self._notify_status(
                    "Couldn't find host. Make sure you're connected to "
                    "their hotspot WiFi and they tapped 'Host Chat'."
                )
                return

            self._notify_status(f"Found host at {host_ip}, connecting...")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((host_ip, CHAT_PORT))
            self._notify_status("Connected!")
            self._listen_loop()
        except Exception as e:
            self._notify_status(f"Connection error: {e}")

    # ---- shared ----

    def _listen_loop(self):
        buffer = b""
        while self.running:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self._notify_status("Friend disconnected.")
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line:
                        try:
                            msg = json.loads(line.decode())
                            self.on_message(msg["sender"], msg["text"], msg["timestamp"])
                        except (json.JSONDecodeError, KeyError):
                            pass
            except OSError:
                break

    def send(self, sender, text, timestamp):
        if not self.sock:
            return False
        payload = json.dumps(
            {"sender": sender, "text": text, "timestamp": timestamp}
        ).encode() + b"\n"
        try:
            self.sock.sendall(payload)
            return True
        except OSError:
            self._notify_status("Failed to send - connection lost.")
            return False

    def stop(self):
        self.running = False
        for s in (self.sock, self.server_sock, self.udp_sock):
            try:
                if s:
                    s.close()
            except OSError:
                pass

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"

    def _notify_status(self, text):
        Clock.schedule_once(lambda dt: self.on_status(text), 0)


# ---------------------------------------------------------------------
# UI - Instagram-DM-style bubbles
# ---------------------------------------------------------------------

KV = """
<RoundBox@BoxLayout>:
    bg_color: 1, 1, 1, 1
    radius: [dp(18)]
    canvas.before:
        Color:
            rgba: self.bg_color
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: self.radius

<StartScreen>:
    BoxLayout:
        orientation: "vertical"
        padding: dp(30)
        spacing: dp(20)
        canvas.before:
            Color:
                rgba: 0.97, 0.97, 0.98, 1
            RoundedRectangle:
                pos: self.pos
                size: self.size

        Widget:
            size_hint_y: 0.15

        Label:
            text: "LAN Chat"
            font_size: dp(34)
            bold: True
            color: 0.1, 0.1, 0.1, 1
            size_hint_y: 0.12

        Label:
            text: "Chat with your friend over a hotspot.\\nNo internet needed."
            font_size: dp(15)
            color: 0.45, 0.45, 0.45, 1
            size_hint_y: 0.15
            halign: "center"

        Widget:
            size_hint_y: 0.08

        TextInput:
            id: name_input
            hint_text: "Your name"
            multiline: False
            size_hint_y: None
            height: dp(50)
            padding: [dp(15), dp(14)]
            background_normal: ""
            background_color: 1, 1, 1, 1
            foreground_color: 0.1, 0.1, 0.1, 1

        Button:
            text: "Host Chat  (turn on your hotspot first)"
            size_hint_y: None
            height: dp(56)
            background_normal: ""
            background_color: 0.0, 0.48, 1, 1
            color: 1, 1, 1, 1
            font_size: dp(16)
            bold: True
            on_release: app.begin_host(name_input.text)

        Button:
            text: "Join Chat  (connect to their hotspot WiFi first)"
            size_hint_y: None
            height: dp(56)
            background_normal: ""
            background_color: 0.85, 0.85, 0.87, 1
            color: 0.1, 0.1, 0.1, 1
            font_size: dp(16)
            bold: True
            on_release: app.begin_client(name_input.text)

        Label:
            id: status_label
            text: app.status_text
            font_size: dp(13)
            color: 0.5, 0.5, 0.5, 1
            size_hint_y: 0.2

        Widget:
            size_hint_y: 0.1

<ChatScreen>:
    BoxLayout:
        orientation: "vertical"
        canvas.before:
            Color:
                rgba: 0.97, 0.97, 0.98, 1
            RoundedRectangle:
                pos: self.pos
                size: self.size

        BoxLayout:
            size_hint_y: None
            height: dp(56)
            padding: [dp(15), 0]
            canvas.before:
                Color:
                    rgba: 1, 1, 1, 1
                RoundedRectangle:
                    pos: self.pos
                    size: self.size
            Label:
                text: app.status_text
                color: 0.1, 0.1, 0.1, 1
                font_size: dp(15)
                bold: True
                halign: "left"
                text_size: self.size

        ScrollView:
            id: scroll
            do_scroll_x: False
            BoxLayout:
                id: messages_box
                orientation: "vertical"
                size_hint_y: None
                height: self.minimum_height
                padding: [dp(10), dp(10)]
                spacing: dp(8)

        BoxLayout:
            size_hint_y: None
            height: dp(64)
            padding: [dp(10), dp(8)]
            spacing: dp(8)
            canvas.before:
                Color:
                    rgba: 1, 1, 1, 1
                RoundedRectangle:
                    pos: self.pos
                    size: self.size
            TextInput:
                id: msg_input
                hint_text: "Message..."
                multiline: False
                background_normal: ""
                background_color: 0.93, 0.93, 0.95, 1
                foreground_color: 0.1, 0.1, 0.1, 1
                padding: [dp(15), dp(12)]
                on_text_validate: app.send_message(self.text)
            Button:
                text: "Send"
                size_hint_x: None
                width: dp(70)
                background_normal: ""
                background_color: 0.0, 0.48, 1, 1
                color: 1, 1, 1, 1
                bold: True
                on_release: app.send_message(msg_input.text)
"""


class MessageBubble(BoxLayout):
    """A single chat bubble, styled like Instagram DMs -
    right/blue for me, left/gray for them."""

    def __init__(self, sender, text, timestamp, is_me, **kwargs):
        super().__init__(orientation="vertical", size_hint_y=None, **kwargs)
        self.padding = (dp(12), dp(8))
        self.size_hint_x = 0.75
        self.pos_hint = {"right": 1} if is_me else {"x": 0}

        bubble_color = (0.0, 0.48, 1, 1) if is_me else (0.90, 0.90, 0.92, 1)
        text_color = (1, 1, 1, 1) if is_me else (0.1, 0.1, 0.1, 1)

        with self.canvas.before:
            Color(*bubble_color)
            self.rect = RoundedRectangle(radius=[dp(16)])
        self.bind(pos=self._update_rect, size=self._update_rect)

        label = Label(
            text=text,
            color=text_color,
            font_size=dp(15),
            size_hint_y=None,
            halign="left",
            valign="middle",
        )
        label.bind(
            width=lambda inst, w: setattr(inst, "text_size", (w, None)),
            texture_size=lambda inst, ts: setattr(label, "height", ts[1]),
        )

        time_label = Label(
            text=timestamp,
            color=(text_color[0], text_color[1], text_color[2], 0.7),
            font_size=dp(10),
            size_hint_y=None,
            height=dp(14),
            halign="left" if not is_me else "right",
        )
        time_label.bind(width=lambda inst, w: setattr(inst, "text_size", (w, dp(14))))

        self.add_widget(label)
        self.add_widget(time_label)
        self.bind(minimum_height=self._set_height)

    def _set_height(self, instance, value):
        self.height = value + dp(16)

    def _update_rect(self, *args):
        self.rect.pos = self.pos
        self.rect.size = self.size


class StartScreen(Screen):
    pass


class ChatScreen(Screen):
    pass


class LANChatApp(App):
    status_text = StringProperty("Enter your name to begin")
    my_name = StringProperty("Me")

    def build(self):
        self.storage = ChatStorage()
        self.node = None
        Builder.load_string(KV)
        self.sm = ScreenManager(transition=SlideTransition())
        self.sm.add_widget(StartScreen(name="start"))
        self.sm.add_widget(ChatScreen(name="chat"))
        return self.sm

    # ---- role setup ----

    def begin_host(self, name):
        self.my_name = name.strip() or "Me"
        self.status_text = "Starting hotspot host..."
        self.node = NetworkNode(self._on_message_received, self._on_status_update)
        self.node.start_host()
        self._go_to_chat()

    def begin_client(self, name):
        self.my_name = name.strip() or "Me"
        self.status_text = "Searching for host..."
        self.node = NetworkNode(self._on_message_received, self._on_status_update)
        self.node.start_client()
        self._go_to_chat()

    def _go_to_chat(self):
        self.sm.current = "chat"
        self._load_history()

    def _load_history(self):
        chat_screen = self.sm.get_screen("chat")
        box = chat_screen.ids.messages_box
        box.clear_widgets()
        for sender, text, timestamp in self.storage.load_all():
            is_me = sender == self.my_name
            box.add_widget(MessageBubble(sender, text, timestamp, is_me))

    # ---- messaging ----

    def send_message(self, text):
        text = text.strip()
        if not text or not self.node:
            return
        timestamp = datetime.now().strftime("%H:%M")
        sent = self.node.send(self.my_name, text, timestamp)
        if sent:
            self.storage.save(self.my_name, text, timestamp)
            self._add_bubble(self.my_name, text, timestamp, is_me=True)
            chat_screen = self.sm.get_screen("chat")
            chat_screen.ids.msg_input.text = ""

    def _on_message_received(self, sender, text, timestamp):
        def update(dt):
            self.storage.save(sender, text, timestamp)
            self._add_bubble(sender, text, timestamp, is_me=False)
        Clock.schedule_once(update, 0)

    def _add_bubble(self, sender, text, timestamp, is_me):
        chat_screen = self.sm.get_screen("chat")
        box = chat_screen.ids.messages_box
        box.add_widget(MessageBubble(sender, text, timestamp, is_me))
        scroll = chat_screen.ids.scroll
        Clock.schedule_once(lambda dt: setattr(scroll, "scroll_y", 0), 0.1)

    def _on_status_update(self, text):
        self.status_text = text

    def on_stop(self):
        if self.node:
            self.node.stop()


if __name__ == "__main__":
    Window.clearcolor = (0.97, 0.97, 0.98, 1)
    LANChatApp().run()
