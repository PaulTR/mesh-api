"""
Telegram extension for MESH-API.

Provides bidirectional Telegram ↔ Mesh integration:
- Outbound: sends mesh messages and AI responses to a Telegram chat via
  the Bot API (sendMessage).
- Inbound:  polls Telegram for new messages using getUpdates and routes
  them onto the mesh.
- Emergency: posts emergency alerts to the configured chat.

Requires a Telegram Bot Token from @BotFather and the numeric chat_id
of the target group / user.
"""

import threading
import time

try:
    import requests
except ImportError:
    requests = None

from extensions.base_extension import BaseExtension


class TelegramExtension(BaseExtension):
    """Telegram ↔ Mesh bridge extension."""

    # ------------------------------------------------------------------
    # Required properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Telegram"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ------------------------------------------------------------------
    # Config accessors
    # ------------------------------------------------------------------

    @property
    def bot_token(self) -> str:
        return self.config.get("bot_token", "")

    @property
    def chat_id(self) -> str:
        return str(self.config.get("chat_id", ""))

    @property
    def send_emergency(self) -> bool:
        return bool(self.config.get("send_emergency", False))

    @property
    def send_ai(self) -> bool:
        return bool(self.config.get("send_ai", False))

    @property
    def send_all(self) -> bool:
        return bool(self.config.get("send_all", False))

    @property
    def receive_enabled(self) -> bool:
        return bool(self.config.get("receive_enabled", True))

    @property
    def inbound_channel_index(self):
        val = self.config.get("inbound_channel_index")
        return int(val) if val is not None else None

    @property
    def poll_interval(self) -> int:
        return int(self.config.get("poll_interval_seconds", 5))

    @property
    def allow_commands(self) -> bool:
        """v0.7.5.0 (GitHub #59): when true, a Telegram message that starts with
        '/' is routed through the core command pipeline and the reply is sent back
        to Telegram — so /ai, /whereami, etc. work from Telegram. Ordinary chat is
        still NOT auto-answered by the AI (echo-loop protection stays intact).
        Off by default."""
        return bool(self.config.get("allow_commands", False))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_load(self) -> None:
        self._poll_thread = None
        self._stop_event = threading.Event()
        self._last_update_id = 0

        status = []
        if self.bot_token:
            status.append("bot_token=set")
        if self.chat_id:
            status.append(f"chat_id={self.chat_id}")

        self.log(f"Telegram enabled. {', '.join(status) if status else 'No settings configured.'}")

        if self.bot_token and self.chat_id and self.receive_enabled:
            self._poll_thread = threading.Thread(
                target=self._poll_telegram,
                daemon=True,
                name="telegram-poll",
            )
            self._poll_thread.start()
            self.log("Telegram polling thread started.")

    def on_unload(self) -> None:
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        self.log("Telegram extension unloaded.")

    # ------------------------------------------------------------------
    # Outbound: mesh → Telegram
    # ------------------------------------------------------------------

    def send_message(self, message: str, metadata: dict | None = None) -> None:
        metadata = metadata or {}
        is_ai = metadata.get("is_ai_response", False)
        ch_idx = metadata.get("channel_idx")

        if self.send_all and not is_ai:
            if self.inbound_channel_index is not None and ch_idx == self.inbound_channel_index:
                self._send_telegram(message)
            return

        if self.send_ai and is_ai:
            if self.inbound_channel_index is not None and ch_idx == self.inbound_channel_index:
                self._send_telegram(message)

    def on_message(self, message: str, metadata: dict | None = None) -> None:
        if not self.send_all:
            return
        metadata = metadata or {}
        ch_idx = metadata.get("channel_idx")
        if self.inbound_channel_index is not None and ch_idx == self.inbound_channel_index:
            sender = metadata.get("sender_info", "Unknown")
            self._send_telegram(f"<b>{sender}</b>: {message}")

    # ------------------------------------------------------------------
    # Emergency hook
    # ------------------------------------------------------------------

    def on_emergency(self, message: str, gps_coords: dict | None = None) -> None:
        if self.send_emergency:
            try:
                self._send_telegram(f"🚨 <b>EMERGENCY ALERT</b>\n{message}")
                self.log("✅ Emergency alert posted to Telegram.")
            except Exception as exc:
                self.log(f"⚠️ Telegram emergency error: {exc}")

    # ------------------------------------------------------------------
    # Inbound: Telegram → Mesh (long-poll via getUpdates)
    # ------------------------------------------------------------------

    def _poll_telegram(self) -> None:
        time.sleep(5)
        base = f"https://api.telegram.org/bot{self.bot_token}"

        while not self._stop_event.is_set():
            try:
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": self.poll_interval,
                    "allowed_updates": '["message"]',
                }
                resp = requests.get(f"{base}/getUpdates", params=params,
                                    timeout=self.poll_interval + 5)
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        self._last_update_id = update["update_id"]
                        msg = update.get("message")
                        if not msg:
                            continue
                        # Only accept messages from the configured chat
                        msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                        if msg_chat_id != self.chat_id:
                            continue
                        text = msg.get("text", "")
                        if not text:
                            continue
                        user = msg.get("from", {})
                        username = user.get("username") or user.get("first_name", "TGUser")
                        # v0.7.5.0 (#59): run slash-commands from Telegram through the
                        # core command pipeline and reply back to Telegram, instead of
                        # relaying them to the mesh as literal chat. Opt-in.
                        if self.allow_commands and text.strip().startswith("/"):
                            self._handle_tg_command(text.strip(), username)
                            continue
                        formatted = f"[TG:{username}] {text}"
                        log_fn = self.app_context.get("log_message")
                        if log_fn:
                            log_fn("Telegram", formatted, direct=False,
                                   channel_idx=self.inbound_channel_index)
                        if self.inbound_channel_index is not None:
                            self.send_to_mesh(formatted,
                                              channel_index=self.inbound_channel_index)
                        self.log(f"Polled TG message: {formatted}")
                else:
                    self.log(f"Telegram API error: {data}")
            except Exception as exc:
                self.log(f"Error polling Telegram: {exc}")
                time.sleep(5)

    # ------------------------------------------------------------------
    # Commands from Telegram (opt-in) — GitHub #59
    # ------------------------------------------------------------------

    def _handle_tg_command(self, text: str, username: str) -> None:
        """Route a Telegram slash-command through the core and reply to Telegram.

        - '/ai <question>' (and '/ask', '/bot') query the AI directly — a Telegram
          convenience, since the mesh AI alias is randomized to avoid RF collisions.
        - Any other '/cmd' is dispatched to the core command handler exactly as a
          mesh user's command would be.
        The reply goes back only to Telegram; the command is not broadcast to the mesh.
        """
        ac = self.app_context or {}
        cmd = text.split()[0].lower()
        arg = text[len(text.split()[0]):].strip()
        resp = None
        try:
            if cmd in ("/ai", "/ask", "/bot"):
                get_ai = ac.get("get_ai_response")
                if not get_ai:
                    resp = "AI is not available."
                elif not arg:
                    resp = "Usage: /ai <your question>"
                else:
                    resp = get_ai(arg)
            else:
                handle_command = ac.get("handle_command")
                if handle_command:
                    resp = handle_command(cmd, text, f"tg-{username}")
        except Exception as exc:
            self.log(f"⚠️ Telegram command error ({cmd}): {exc}")
            resp = None
        if resp:
            max_len = ac.get("MAX_RESPONSE_LENGTH", 2000)
            self._send_telegram(str(resp)[:max_len])
            self.log(f"TG command {cmd} handled -> replied to Telegram.")
        else:
            self._send_telegram(f"🤖 Unknown or empty command: {cmd}")
            self.log(f"TG command {cmd}: no response.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_telegram(self, text: str) -> None:
        """Send a message via the Telegram Bot API."""
        if not self.bot_token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
            }, timeout=15)
        except Exception as exc:
            self.log(f"⚠️ Telegram send error: {exc}")
