"""
Signal extension for MESH-API.

Provides bidirectional Signal ↔ Mesh integration via the signal-cli-rest-api
(https://github.com/bbernhard/signal-cli-rest-api):
- Outbound: sends mesh messages and AI responses to a Signal recipient
  (phone number or group ID).
- Inbound:  polls signal-cli-rest-api for new messages and routes them onto
  the mesh.
- Emergency: sends emergency alerts to the configured recipient.

Requires a running signal-cli-rest-api instance and a registered Signal
number.
"""

import threading
import time

try:
    import requests
except ImportError:
    requests = None

from extensions.base_extension import BaseExtension


class SignalExtension(BaseExtension):
    """Signal ↔ Mesh bridge extension."""

    # ------------------------------------------------------------------
    # Required properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Signal"

    @property
    def version(self) -> str:
        return "1.1.0"

    # ------------------------------------------------------------------
    # Config accessors
    # ------------------------------------------------------------------

    @property
    def api_url(self) -> str:
        url = self.config.get("signal_cli_rest_url", "http://localhost:8080")
        return url.rstrip("/") if url else ""

    @property
    def sender_number(self) -> str:
        return self.config.get("sender_number", "")

    @property
    def recipient(self) -> str:
        """Phone number or group ID to send to / receive from."""
        return self.config.get("recipient", "")

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
        """Optional mesh-channel filter. None (shipped default) = no filter:
        forward every channel and relay inbound on channel 0 (GitHub #59)."""
        val = self.config.get("inbound_channel_index")
        return int(val) if val is not None else None

    def _channel_ok(self, ch_idx) -> bool:
        """True when *ch_idx* passes the optional channel filter."""
        return self.inbound_channel_index is None or ch_idx == self.inbound_channel_index

    @property
    def poll_interval(self) -> int:
        return int(self.config.get("poll_interval_seconds", 5))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_load(self) -> None:
        self._poll_thread = None
        self._stop_event = threading.Event()

        status = []
        if self.sender_number:
            status.append(f"sender={self.sender_number}")
        if self.recipient:
            status.append(f"recipient={self.recipient}")
        if self.api_url:
            status.append(f"api={self.api_url}")

        self.log(f"Signal enabled. {', '.join(status) if status else 'No settings configured.'}")

        if self.api_url and self.sender_number and self.receive_enabled:
            self._poll_thread = threading.Thread(
                target=self._poll_signal,
                daemon=True,
                name="signal-poll",
            )
            self._poll_thread.start()
            self.log("Signal polling thread started.")

    def on_unload(self) -> None:
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        self.log("Signal extension unloaded.")

    # ------------------------------------------------------------------
    # Outbound: mesh → Signal
    # ------------------------------------------------------------------

    def send_message(self, message: str, metadata: dict | None = None) -> None:
        metadata = metadata or {}
        is_ai = metadata.get("is_ai_response", False)
        ch_idx = metadata.get("channel_idx")

        if self.send_all and not is_ai:
            if self._channel_ok(ch_idx):
                self._send_signal(message)
            return

        if self.send_ai and is_ai:
            if self._channel_ok(ch_idx):
                self._send_signal(message)

    def on_message(self, message: str, metadata: dict | None = None) -> None:
        if not self.send_all:
            return
        if message.startswith("[Signal:"):
            # Our own Signal->mesh relay coming back (MQTT/bridge echo) —
            # never forward it to Signal again.
            return
        metadata = metadata or {}
        ch_idx = metadata.get("channel_idx")
        if self._channel_ok(ch_idx):
            sender = metadata.get("sender_info", "Unknown")
            self._send_signal(f"{sender}: {message}")

    # ------------------------------------------------------------------
    # Emergency hook
    # ------------------------------------------------------------------

    def on_emergency(self, message: str, gps_coords: dict | None = None) -> None:
        if self.send_emergency:
            try:
                self._send_signal(f"🚨 EMERGENCY ALERT\n{message}")
                self.log("✅ Emergency alert sent via Signal.")
            except Exception as exc:
                self.log(f"⚠️ Signal emergency error: {exc}")

    # ------------------------------------------------------------------
    # Inbound: Signal → Mesh (polling signal-cli-rest-api)
    # ------------------------------------------------------------------

    def _poll_signal(self) -> None:
        time.sleep(5)

        while not self._stop_event.is_set():
            try:
                url = f"{self.api_url}/v1/receive/{self.sender_number}"
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200:
                    messages = resp.json()
                    if not isinstance(messages, list):
                        messages = []
                    for msg in messages:
                        envelope = msg.get("envelope", {})
                        data_msg = envelope.get("dataMessage")
                        if not data_msg:
                            continue
                        text = data_msg.get("message", "")
                        source = envelope.get("sourceName") or envelope.get("sourceNumber", "SignalUser")
                        if not text:
                            continue

                        formatted = f"[Signal:{source}] {text}"
                        log_fn = self.app_context.get("log_message")
                        if log_fn:
                            log_fn("Signal", formatted, direct=False,
                                   channel_idx=self.inbound_channel_index)
                        self.send_to_mesh(formatted,
                                          channel_index=self.inbound_channel_index)
                        self.log(f"Polled Signal message: {formatted}")
                else:
                    self.log(f"Signal API error: {resp.status_code}")
            except Exception as exc:
                self.log(f"Error polling Signal: {exc}")
            for _ in range(self.poll_interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_signal(self, text: str) -> None:
        """Send a message via the signal-cli-rest-api."""
        if not self.api_url or not self.sender_number or not self.recipient:
            return
        url = f"{self.api_url}/v2/send"
        payload = {
            "message": text,
            "number": self.sender_number,
            "recipients": [self.recipient],
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as exc:
            self.log(f"⚠️ Signal send error: {exc}")
