"""
meshcore_core.py — Core-owned MeshCore radio manager for MESH-API v0.7.0.

Starting with v0.7.0 Beta, MeshCore is a **first-class radio** inside MESH-API,
on equal footing with Meshtastic — not a bridge plugin.  A user can run:

  * a Meshtastic radio only (classic behaviour, unchanged), or
  * a MeshCore radio only (standalone — no Meshtastic device required), or
  * one of each, with MESH-API acting as the man-in-the-middle so traffic,
    commands, the AI assistant, and every plugin work across both networks.

This module owns the MeshCore connection.  It wraps the asynchronous
``meshcore`` Python library (https://github.com/meshcore-dev/meshcore_py) in a
dedicated asyncio event loop running on a background thread, and exposes a
small **synchronous, thread-safe** surface that the synchronous MESH-API core
can call without ever blocking:

    mgr = MeshCoreManager(config, on_inbound=..., log=...)
    mgr.start()
    mgr.send_channel(0, "hello")
    mgr.send_dm("a1b2c3d4", "hi there")
    nodes = mgr.get_nodes()       # for the harmonized map
    status = mgr.get_status()     # for the web UI
    mgr.stop()

Inbound MeshCore messages are handed to the ``on_inbound`` callback using a
network-agnostic signature so the core can route them through the *same*
pipeline as Meshtastic messages (AI, slash commands, and all extension
hooks).  This is what lets a message that originates on MeshCore reach
Telegram, Discord, the AI provider, etc. (GitHub issue #59).

The library is optional: if it is not installed, the manager degrades
gracefully (``available`` is False) so MESH-API still runs.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from typing import Any, Callable, Optional

try:
    from meshcore import MeshCore, EventType  # type: ignore[import-untyped]
    MESHCORE_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    MESHCORE_AVAILABLE = False
    MeshCore = None  # type: ignore[assignment,misc]
    EventType = None  # type: ignore[assignment,misc]


# Inbound callback signature (kept network-agnostic on purpose):
#   on_inbound(network, sender_id, sender_name, text, is_direct, channel_idx, reply_target)
# where reply_target carries enough info to reply over MeshCore:
#   {"kind": "dm", "key": "<pubkey_prefix>"}  or  {"kind": "channel", "channel": <int>}
InboundCallback = Callable[[str, str, str, str, bool, Optional[int], dict], None]
LogCallback = Callable[[str], None]


class MeshCoreManager:
    """Manages a single MeshCore companion-radio connection for the core."""

    NETWORK = "meshcore"

    def __init__(
        self,
        config: dict,
        on_inbound: Optional[InboundCallback] = None,
        log: Optional[LogCallback] = None,
    ) -> None:
        self._config = config or {}
        self._on_inbound = on_inbound
        self._log_fn = log or (lambda m: print(m))

        self._mc = None  # MeshCore instance or None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._connected = False
        self._started = False

        # Cached device + contact state (refreshed in the bg loop, read by web UI)
        self._self_info: dict = {}
        self._device_info: dict = {}
        self._contacts: dict = {}
        self._contacts_lock = threading.Lock()
        self._channels: dict = {}  # idx -> {"name": str, "index": int}
        self._channels_lock = threading.Lock()
        self._last_rx_ts: float = 0.0
        self._last_advert: float = 0.0
        # v0.7.4.1: per-node "last heard" epochs (node id -> unix ts), updated
        # whenever we receive a DM/channel message from a contact. Combined with
        # each contact's advertisement time this gives MeshCore nodes the same
        # last-heard/beacon signal Meshtastic has (so "Previously Seen" works).
        self._node_last_seen: dict = {}
        self._node_last_seen_lock = threading.Lock()

        self._stats = {
            "rx": 0,            # messages received from MeshCore
            "tx": 0,            # messages sent to MeshCore
            "commands": 0,      # commands processed from MeshCore users
            "errors": 0,
            "reconnects": 0,
        }

        # v0.7.5.0: connection diagnostics so the WebUI/API can show *why* a radio
        # (esp. BLE) isn't connecting — previously this was only visible in the
        # server log. Tracks last error, attempt count, and (for BLE) a periodic
        # scan result with signal strength + bond hint.
        self._last_error: Optional[str] = None
        self._last_error_hint: Optional[str] = None
        self._last_error_ts: float = 0.0
        self._connect_attempts: int = 0
        self._last_connect_ts: float = 0.0
        self._ble_diag: dict = {
            "found": None,      # was the target device seen in the last scan?
            "rssi": None,       # signal strength (dBm) of the target device
            "name": None,       # advertised name of the matched device
            "address": None,    # matched BLE address
            "scanned_ts": None, # when the diagnostic scan last ran
        }
        self._ble_diag_lock = threading.Lock()
        self._last_ble_scan: float = 0.0
        self._ble_agent_proc = None       # bt-agent subprocess (Linux BLE pairing)
        self._ble_agent_warned = False     # only warn once if bt-agent is missing
        self._ble_pin_file = None          # temp pin file for bt-agent (cleaned up)

    # ── Public properties ────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True if the ``meshcore`` library is importable."""
        return MESHCORE_AVAILABLE

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled", False))

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background asyncio thread (idempotent)."""
        if self._started:
            return
        if not self.enabled:
            self._log("MeshCore radio is disabled in config; not starting.")
            return
        if not MESHCORE_AVAILABLE:
            self._log(
                "⚠️  MeshCore radio is enabled but the 'meshcore' package is not "
                "installed. Install it with:  pip install meshcore"
            )
            return
        self._started = True
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="meshcore-core",
            daemon=True,
        )
        self._thread.start()
        self._log("MeshCore radio manager started.")

    def stop(self) -> None:
        self._stopping.set()
        if self._loop and self._mc:
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=10)
        self._stop_ble_pairing_agent()
        self._started = False
        self._connected = False
        self._log("MeshCore radio manager stopped.")

    # ── Public send surface (thread-safe, synchronous) ───────────────

    def send_channel(self, channel: int, text: str) -> bool:
        """Send a message to a MeshCore channel. Returns True if scheduled."""
        if not self._can_send():
            return False
        asyncio.run_coroutine_threadsafe(
            self._send_channel_async(int(channel), text), self._loop  # type: ignore[arg-type]
        )
        return True

    def send_dm(self, key_prefix: str, text: str) -> bool:
        """Send a direct message to a MeshCore contact by public-key prefix."""
        if not self._can_send():
            return False
        asyncio.run_coroutine_threadsafe(
            self._send_dm_async(key_prefix, text), self._loop  # type: ignore[arg-type]
        )
        return True

    def broadcast(self, text: str) -> bool:
        """Broadcast to the public channel (0)."""
        return self.send_channel(0, text)

    # ── Public read surface (for web UI / harmonized map) ────────────

    def get_nodes(self) -> list:
        """Return MeshCore contacts in a UI-friendly, network-tagged shape."""
        nodes = []
        with self._contacts_lock:
            contacts = dict(self._contacts)
        with self._node_last_seen_lock:
            last_seen = dict(self._node_last_seen)
        for key, c in contacts.items():
            if not isinstance(c, dict):
                continue
            pub = c.get("public_key", key) or key
            name = c.get("adv_name") or c.get("name") or f"MC_{str(pub)[:6]}"
            lat = c.get("adv_lat")
            lon = c.get("adv_lon")
            nid = f"!mc-{str(pub)[:8]}"
            # MeshCore "last heard": newest of the node's advertisement time and
            # any message we've received from it. last_advert/lastmod are unix
            # epoch seconds parsed from the firmware contact record.
            #
            # IMPORTANT: MeshCore is NOT like Meshtastic. Nodes do not beacon
            # continuously; they flood an advert only occasionally and the
            # companion radio keeps a *persistent* contact book, so a contact
            # stays addressable indefinitely even if its advert is days old.
            # That means `last_advert` is NOT a reliable presence signal and must
            # not be used to decide a node is "gone". `last_msg` (message rx via
            # _node_last_seen) is the only true activity signal, so we surface it
            # separately for the UI's "Previously Seen" staleness logic.
            advert = c.get("last_advert") or c.get("lastmod") or 0
            try:
                advert = int(advert)
            except (TypeError, ValueError):
                advert = 0
            seen = last_seen.get(nid, 0)
            try:
                seen = int(seen)
            except (TypeError, ValueError):
                seen = 0
            last_heard = max(advert, seen) or None
            entry = {
                "id": nid,
                "pubkey": pub,
                "shortName": name,
                "longName": name,
                "network": self.NETWORK,
                "last_advert": advert or None,
                "last_heard": last_heard,
                "last_msg": seen or None,
            }
            try:
                if lat not in (None, 0) or lon not in (None, 0):
                    entry["lat"] = float(lat)
                    entry["lon"] = float(lon)
            except (TypeError, ValueError):
                pass
            nodes.append(entry)
        return nodes

    def get_status(self) -> dict:
        return {
            "available": MESHCORE_AVAILABLE,
            "enabled": self.enabled,
            "connected": self._connected,
            "connection_type": self._config.get("connection_type", "serial"),
            "self": {
                "name": self._self_info.get("name"),
                "public_key": self._self_info.get("public_key"),
                "lat": self._self_info.get("adv_lat"),
                "lon": self._self_info.get("adv_lon"),
            },
            "contacts": len(self._contacts),
            "channels": self.get_channels(),
            "last_rx_age_sec": (time.time() - self._last_rx_ts) if self._last_rx_ts else None,
            "stats": dict(self._stats),
            # v0.7.5.0: connection diagnostics for the WebUI (why isn't it connecting?)
            "diag": {
                "last_error": self._last_error,
                "last_error_hint": self._last_error_hint,
                "last_error_age_sec": (time.time() - self._last_error_ts) if self._last_error_ts else None,
                "connect_attempts": self._connect_attempts,
            },
            # BLE-only: is the target node visible, and how strong is the signal?
            "ble": (dict(self._ble_diag)
                    if str(self._config.get("connection_type", "serial")).lower() == "ble"
                    else None),
        }

    def get_channels(self) -> list:
        """Return MeshCore channels (group chats / private channels) for the UI."""
        with self._channels_lock:
            chans = dict(self._channels)
        out = []
        for idx in sorted(chans.keys()):
            c = chans[idx]
            name = (c.get("name") or "").strip() or (f"Public" if idx == 0 else f"Channel {idx}")
            out.append({"index": idx, "name": name})
        if not out:
            # Always offer the public channel even before a channel scan completes.
            out = [{"index": 0, "name": "Public"}]
        return out

    def get_device_info(self) -> dict:
        """Return MeshCore device model + firmware version (for update checks)."""
        di = self._device_info or {}
        si = self._self_info or {}
        return {
            "model": di.get("model") or si.get("model"),
            "firmware_version": (di.get("ver") or di.get("firmware_version")
                                  or di.get("fw_version") or si.get("firmware_version")),
            "manufacturer": di.get("manufacturer"),
        }

    def get_contacts(self) -> list:
        """Return MeshCore contacts (for DM targets) with full pubkeys."""
        with self._contacts_lock:
            contacts = dict(self._contacts)
        out = []
        for key, c in contacts.items():
            if not isinstance(c, dict):
                continue
            pub = str(c.get("public_key", key) or key)
            name = c.get("adv_name") or c.get("name") or f"MC_{pub[:6]}"
            out.append({
                "id": f"!mc-{pub[:8]}",
                "pubkey": pub,
                "name": name,
                "is_repeater": bool(c.get("type") == 2 or c.get("is_repeater")),
            })
        return out

    # ==================================================================
    #  Internal — asyncio event loop & connection management
    # ==================================================================

    def _log(self, msg: str) -> None:
        try:
            self._log_fn(f"[MeshCore] {msg}")
        except Exception:
            print(f"[MeshCore] {msg}")

    def _can_send(self) -> bool:
        return bool(self._mc and self._loop and self._connected)

    def _max_len(self) -> int:
        return int(self._config.get("max_message_length", 200))

    def _run_event_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"⚠️  event loop crashed: {exc}")
            self._log(traceback.format_exc())
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None

    async def _main_loop(self) -> None:
        """Connect (with bounded attempts + exponential backoff) and listen."""
        base_interval = int(self._config.get("reconnect_interval_sec", 15))
        backoff = base_interval
        max_backoff = max(base_interval, int(self._config.get("max_reconnect_interval_sec", 120)))

        conn_type = str(self._config.get("connection_type", "serial")).lower()
        while not self._stopping.is_set():
            try:
                self._connect_attempts += 1
                self._last_connect_ts = time.time()
                self._mc = await self._connect()
                if self._mc is None:
                    self._log(f"connection failed; retrying in {backoff}s…")
                    # For BLE, run a quick diagnostic scan while we're idle so the
                    # UI can show whether the node is even visible and how strong.
                    if conn_type == "ble":
                        await self._ble_diagnostic_scan()
                    await self._interruptible_sleep(backoff)
                    backoff = min(max_backoff, backoff * 2)
                    continue

                self._connected = True
                self._last_error = None
                self._last_error_hint = None
                backoff = base_interval  # reset backoff on a good connect
                self._log("✅ connected to MeshCore companion node.")

                await self._after_connect()
                await self._subscribe_events()
                try:
                    await self._mc.start_auto_message_fetching()
                except Exception as exc:
                    self._log(f"⚠️  could not start auto message fetching: {exc}")

                # Announce ourselves so other MeshCore nodes can discover us as a
                # contact (required before they can DM us — e.g. for ping/pong).
                await self._send_advert()
                self._last_advert = time.time()

                # Health-checked keep-alive loop.  Fixes silent dead links by
                # actively watching the library's connection flag.  Also re-adverts
                # on a configurable interval so we stay discoverable for DMs.
                advert_interval = int(self._config.get("advert_interval_sec", 1800))
                while not self._stopping.is_set() and self._is_link_alive():
                    await asyncio.sleep(1)
                    if advert_interval > 0 and (time.time() - self._last_advert) >= advert_interval:
                        await self._send_advert()
                        self._last_advert = time.time()

                self._log("connection lost.")
                self._record_error("connection lost after connecting"
                                   + (" (weak BLE signal drops the link)" if conn_type == "ble" else ""))
            except Exception as exc:
                self._log(f"⚠️  error: {exc}")
                self._record_error(str(exc) or repr(exc))
                self._stats["errors"] += 1
            finally:
                self._connected = False
                await self._teardown_connection()

            if not self._stopping.is_set():
                self._stats["reconnects"] += 1
                if conn_type == "ble":
                    await self._ble_diagnostic_scan()
                self._log(f"reconnecting in {backoff}s…")
                await self._interruptible_sleep(backoff)
                backoff = min(max_backoff, backoff * 2)

    def _is_link_alive(self) -> bool:
        if not self._mc:
            return False
        try:
            return bool(self._mc.is_connected)
        except Exception:
            return False

    def _record_error(self, err: str) -> None:
        """Store the last connection error plus a human-actionable hint so the UI
        can explain BLE/TCP/serial failures instead of leaving users to read logs."""
        self._last_error = str(err)
        self._last_error_ts = time.time()
        low = self._last_error.lower()
        hint = None
        conn = str(self._config.get("connection_type", "serial")).lower()
        if "authentication" in low or "auth failed" in low:
            hint = ("BLE passkey rejected — check ble_pin. The pairing PIN is verified by "
                    "the device; a wrong pin fails here.")
        elif "'nonetype' object has no attribute 'pair'" in low or ("pair" in low and "nonetype" in low):
            hint = ("BLE pairing needs a system BlueZ pairing agent to supply the pin. "
                    "MESH-API registers one automatically on Linux; if this persists, ensure "
                    "bluez is installed and the adapter is powered, or pre-bond the node.")
        elif "no meshcore device found" in low or "not found" in low or "not available" in low:
            hint = ("Target MeshCore node not seen in BLE scan — confirm it's powered, in BLE "
                    "range, advertising, and not already connected to a phone/app.")
        elif "no powered bluetooth" in low or "rf-kill" in low or "rfkill" in low:
            hint = ("No usable Bluetooth adapter — power it on / unblock rfkill "
                    "(`rfkill unblock bluetooth`) and ensure the bluetooth service is running.")
        elif "timed out" in low or "timeout" in low:
            if conn == "ble":
                hint = ("BLE connect timed out — usually weak signal. Move the node closer or "
                        "use a BLE adapter with a better antenna; check ble.rssi below.")
            elif conn == "tcp":
                hint = "TCP connect timed out — check tcp_host/tcp_port and that the node is reachable."
            else:
                hint = "Serial connect timed out — check serial_port and that the device is attached."
        elif "discover services" in low:
            hint = "Connected but GATT service discovery failed — usually an unstable/weak BLE link."
        self._last_error_hint = hint

    def _ensure_ble_pairing_agent(self, pin: str, addr: Optional[str]) -> None:
        """Make a BlueZ pairing agent available that answers pairing requests with
        ``pin``.

        Why this exists: the meshcore library calls ``BleakClient.pair()`` but does
        NOT hand the configured pin to BlueZ — BlueZ asks a registered *agent* for
        the passkey. With no agent, pairing fails with AuthenticationCanceled /
        "'NoneType' object has no attribute 'pair'". Historically users had to run
        ``bt-agent`` by hand. We now launch one automatically (Linux only, using the
        ``bt-agent`` from bluez-tools), so a configured ``ble_pin`` actually works.

        Best-effort and non-fatal: no-op on non-Linux, if already running, or if
        bt-agent isn't installed (we log one-time guidance in that case).
        """
        if platform.system() != "Linux":
            return
        # Already have a live agent we launched?
        if self._ble_agent_proc is not None and self._ble_agent_proc.poll() is None:
            return
        # Or a bt-agent already running system-wide (e.g. a user-managed systemd
        # service)? Don't launch a second one — two agents fight over pairing.
        try:
            if subprocess.run(["pgrep", "-x", "bt-agent"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return
        except Exception:
            pass
        btagent = shutil.which("bt-agent")
        if not btagent:
            if not self._ble_agent_warned:
                self._ble_agent_warned = True
                self._log("⚠️  BLE pairing helper 'bt-agent' not found. Install it "
                          "(`sudo apt install bluez-tools`) so the configured ble_pin can "
                          "authenticate, or pre-pair the node once with bluetoothctl.")
            return
        try:
            # Remove any previous pin file first so we never accumulate cleartext-PIN
            # temp files across reconnects/respawns.
            self._remove_ble_pin_file()
            # pin file: '<ADDR> <PIN>' for the target plus a wildcard fallback.
            fd, pinfile = tempfile.mkstemp(prefix="meshcore-btpin-")
            os.chmod(pinfile, 0o600)
            with os.fdopen(fd, "w") as f:
                if addr:
                    f.write(f"{addr} {pin}\n")
                f.write(f"* {pin}\n")
            self._ble_pin_file = pinfile
            self._ble_agent_proc = subprocess.Popen(
                [btagent, "-c", "KeyboardOnly", "-p", pinfile],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._log("registered BLE pairing agent (bt-agent) to supply ble_pin.")
        except Exception as exc:
            self._log(f"could not start BLE pairing agent: {exc}")
            self._ble_agent_proc = None
            self._remove_ble_pin_file()

    def _remove_ble_pin_file(self) -> None:
        f = getattr(self, "_ble_pin_file", None)
        if f:
            try:
                os.unlink(f)
            except OSError:
                pass
            self._ble_pin_file = None

    def _stop_ble_pairing_agent(self) -> None:
        proc = self._ble_agent_proc
        self._ble_agent_proc = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        # Never leave the cleartext-PIN file behind.
        self._remove_ble_pin_file()

    async def _ble_diagnostic_scan(self) -> None:
        """Best-effort BLE scan (when disconnected) to report whether the target
        node is visible and at what signal strength — the single most useful thing
        for diagnosing a BLE bring-up. Throttled and non-fatal."""
        if not MESHCORE_AVAILABLE:
            return
        now = time.time()
        if now - self._last_ble_scan < 25:
            return
        self._last_ble_scan = now
        try:
            from bleak import BleakScanner
        except Exception:
            return
        want_addr = (self._config.get("ble_address", "") or "").strip().lower()
        found = None
        try:
            devs = await BleakScanner.discover(timeout=6.0, return_adv=True)
            for addr, (d, adv) in devs.items():
                name = (getattr(adv, "local_name", None) or getattr(d, "name", None) or "")
                is_target = False
                if want_addr and str(addr).lower() == want_addr:
                    is_target = True
                elif not want_addr and name.startswith("MeshCore"):
                    is_target = True
                if is_target:
                    found = {"found": True, "rssi": getattr(adv, "rssi", None),
                             "name": name or None, "address": str(addr),
                             "scanned_ts": now}
                    break
        except Exception as exc:
            self._log(f"BLE diagnostic scan error: {exc}")
        with self._ble_diag_lock:
            self._ble_diag = found or {"found": False, "rssi": None, "name": None,
                                       "address": None, "scanned_ts": now}

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep that wakes early if a stop is requested."""
        end = time.time() + seconds
        while time.time() < end and not self._stopping.is_set():
            await asyncio.sleep(0.5)

    async def _connect(self):
        cfg = self._config
        conn_type = str(cfg.get("connection_type", "serial")).lower()
        auto_reconnect = bool(cfg.get("auto_reconnect", True))
        max_attempts = int(cfg.get("max_reconnect_attempts", 0))
        connect_timeout = self._num(cfg.get("connect_timeout_sec"), 20.0, float)

        try:
            if conn_type == "tcp":
                host = cfg.get("tcp_host", "192.168.1.100")
                port = self._num(cfg.get("tcp_port"), 5000, int)
                self._log(f"connecting via TCP {host}:{port}…")
                coro = MeshCore.create_tcp(
                    host, port,
                    auto_reconnect=auto_reconnect,
                    max_reconnect_attempts=(max_attempts if max_attempts > 0 else None),
                )
            elif conn_type == "ble":
                addr = cfg.get("ble_address", "") or None
                pin = cfg.get("ble_pin", "") or None
                # v0.7.5.0: ensure a system BlueZ pairing agent is present so the
                # meshcore lib's client.pair() has something to supply the passkey
                # (the lib does NOT feed the pin to BlueZ itself). No-op off-Linux
                # or when already handled; non-fatal.
                if pin:
                    self._ensure_ble_pairing_agent(str(pin), addr)
                self._log(f"connecting via BLE {addr or '(scan)'}…")
                if pin:
                    coro = MeshCore.create_ble(addr, pin=str(pin))
                else:
                    coro = MeshCore.create_ble(addr)
            else:
                serial_port = cfg.get("serial_port", "/dev/ttyUSB1")
                baud = self._num(cfg.get("serial_baud"), 115200, int)
                self._log(f"connecting via serial {serial_port} @ {baud} baud…")
                coro = MeshCore.create_serial(serial_port, baud)

            # Bound the connect so a wedged transport can never hang us forever.
            return await asyncio.wait_for(coro, timeout=connect_timeout)
        except asyncio.TimeoutError:
            self._log(f"⚠️  connect timed out after {connect_timeout}s.")
            self._record_error(f"connect timed out after {connect_timeout}s")
            return None
        except Exception as exc:
            self._log(f"⚠️  connection error: {exc}")
            self._record_error(str(exc) or repr(exc))
            return None

    @staticmethod
    def _num(val, default, cast):
        """Null/'' tolerant numeric coercion for config values (mirrors the core
        config sanitizer) so a blank/None field never crashes a connect."""
        try:
            if val is None or val == "":
                return default
            return cast(val)
        except (TypeError, ValueError):
            return default

    async def _send_advert(self) -> None:
        """Flood an advertisement so peers add us as a contact (enables DMs)."""
        if not self._mc:
            return
        if not self._config.get("send_adverts", True):
            return
        try:
            await self._mc.commands.send_advert(flood=True)
            self._log("sent advert (discoverable for DMs).")
        except Exception as exc:
            self._log(f"could not send advert: {exc}")

    async def _after_connect(self) -> None:
        """Pull self-info and the contact list once connected."""
        if not self._mc:
            return
        try:
            res = await self._mc.commands.send_appstart()
            if res is not None and not self._is_error(res):
                payload = getattr(res, "payload", None)
                if isinstance(payload, dict):
                    self._self_info = payload
        except Exception as exc:
            self._log(f"could not fetch self info: {exc}")
        # Device query gives firmware version + model (for the update checker).
        try:
            dq = await self._mc.commands.send_device_query()
            if dq is not None and not self._is_error(dq):
                p = getattr(dq, "payload", None)
                if isinstance(p, dict):
                    self._device_info = p
        except Exception as exc:
            self._log(f"could not fetch device info: {exc}")
        await self._refresh_contacts()
        await self._refresh_channels()

    async def _refresh_channels(self) -> None:
        """Read MeshCore channel configs (group chats / private channels)."""
        if not self._mc:
            return
        max_ch = int(self._config.get("max_channels", 8))
        found = {}
        for idx in range(max_ch):
            try:
                res = await self._mc.commands.get_channel(idx)
            except Exception:
                continue
            if res is None or self._is_error(res):
                continue
            payload = getattr(res, "payload", None)
            if isinstance(payload, dict):
                name = (payload.get("channel_name") or payload.get("name") or "").strip()
                # A channel with a secret/name configured is active; channel 0 is
                # always the public channel.
                has_secret = bool(payload.get("channel_secret") or payload.get("secret"))
                if idx == 0 or name or has_secret:
                    found[idx] = {"name": name, "index": idx}
        if found:
            with self._channels_lock:
                self._channels = found

    async def _refresh_contacts(self) -> None:
        if not self._mc:
            return
        try:
            res = await self._mc.commands.get_contacts()
            if res is not None and not self._is_error(res):
                payload = getattr(res, "payload", None)
                if isinstance(payload, dict):
                    with self._contacts_lock:
                        self._contacts = payload
        except Exception as exc:
            self._log(f"could not refresh contacts: {exc}")

    async def _subscribe_events(self) -> None:
        if not self._mc or EventType is None:
            return
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_message)
        self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_message)
        self._mc.subscribe(EventType.CONNECTED, self._on_connected)
        self._mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)
        # Keep the contact cache fresh as the mesh advertises new nodes.
        for evt_name in ("NEW_CONTACT", "ADVERTISEMENT"):
            evt = getattr(EventType, evt_name, None)
            if evt is not None:
                self._mc.subscribe(evt, self._on_contact_change)

    async def _teardown_connection(self) -> None:
        if not self._mc:
            return
        try:
            await self._mc.stop_auto_message_fetching()
        except Exception:
            pass
        try:
            await self._mc.disconnect()
        except Exception:
            pass
        self._mc = None

    async def _shutdown(self) -> None:
        await self._teardown_connection()

    # ── Inbound event handlers ───────────────────────────────────────

    async def _on_contact_message(self, event: Any) -> None:
        try:
            data = event.payload or {}
            text = data.get("text", "")
            prefix = (data.get("pubkey_prefix") or "").strip()[:12]
            name = self._resolve_name(prefix)
            sender_id = self._sender_id(prefix, name)
            self._stats["rx"] += 1
            self._last_rx_ts = time.time()
            self._mark_node_seen(sender_id)
            self._log(f"[DM] {name}: {text}")
            self._dispatch_inbound(
                sender_id=sender_id,
                sender_name=name,
                text=text,
                is_direct=True,
                channel_idx=None,
                reply_target={"kind": "dm", "key": prefix},
            )
        except Exception as exc:
            self._log(f"⚠️  error handling DM: {exc}")
            self._stats["errors"] += 1

    async def _on_channel_message(self, event: Any) -> None:
        try:
            data = event.payload or {}
            raw_text = data.get("text", "")
            channel = int(data.get("channel_idx", 0))
            prefix = (data.get("pubkey_prefix") or "").strip()[:12]
            # MeshCore channel broadcasts are shared-key with no per-sender
            # crypto identity, so the firmware embeds the sender name *inside*
            # the text as "SenderName: actual message". Parse it out so the
            # real message (e.g. a "/ai" command) is seen by the command/AI
            # router, and so the sender shows correctly instead of "unknown".
            name, text = self._split_channel_sender(raw_text, prefix)
            # Channel messages usually carry NO pubkey_prefix, so derive a
            # stable sender id from the parsed name (otherwise every channel
            # sender collides on "!mc-unknown").
            sender_id = self._sender_id(prefix, name)
            self._stats["rx"] += 1
            self._last_rx_ts = time.time()
            self._mark_node_seen(sender_id)
            self._log(f"[ch{channel}] {name}: {text}")
            self._dispatch_inbound(
                sender_id=sender_id,
                sender_name=name,
                text=text,
                is_direct=False,
                channel_idx=channel,
                reply_target={"kind": "channel", "channel": channel},
            )
        except Exception as exc:
            self._log(f"⚠️  error handling channel msg: {exc}")
            self._stats["errors"] += 1

    def _split_channel_sender(self, raw_text: str, prefix: str):
        """Split a MeshCore channel message of the form "Name: message".

        Returns (sender_name, message). Falls back to a contact-resolved name
        (or MC_<prefix>) and the raw text when no embedded name is present.
        """
        fallback = self._resolve_name(prefix)
        if not raw_text:
            return fallback, raw_text
        # Only treat a leading "Name: " as a sender tag when the name part is
        # short, has no leading slash (so we never eat a real "/cmd"), and the
        # separator is an early ": ".
        sep = raw_text.find(": ")
        if 0 < sep <= 32:
            candidate = raw_text[:sep].strip()
            if candidate and not candidate.startswith("/") and "\n" not in candidate:
                return candidate, raw_text[sep + 2:].lstrip()
        return fallback, raw_text

    async def _on_connected(self, event: Any) -> None:
        self._connected = True

    async def _on_disconnected(self, event: Any) -> None:
        self._connected = False
        self._log("⚠️  disconnected event received.")

    async def _on_contact_change(self, event: Any) -> None:
        await self._refresh_contacts()

    def _dispatch_inbound(self, **kwargs) -> None:
        if not self._on_inbound:
            return
        try:
            self._on_inbound(
                self.NETWORK,
                kwargs["sender_id"],
                kwargs["sender_name"],
                kwargs["text"],
                kwargs["is_direct"],
                kwargs["channel_idx"],
                kwargs["reply_target"],
            )
        except Exception as exc:
            self._log(f"⚠️  inbound handler error: {exc}")
            self._stats["errors"] += 1

    # ── Outbound async helpers ───────────────────────────────────────

    def _chunks(self, text: str) -> list:
        max_len = self._max_len()
        if not text:
            return []
        if len(text) <= max_len:
            return [text]
        out, remaining = [], text
        while remaining and len(out) < int(self._config.get("max_chunks", 5)):
            if len(remaining) <= max_len:
                out.append(remaining)
                break
            cut = remaining.rfind(" ", 0, max_len)
            if cut <= max_len // 2:
                cut = max_len
            out.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        return out

    async def _send_channel_async(self, channel: int, text: str) -> None:
        if not self._mc:
            return
        delay = float(self._config.get("chunk_delay_sec", 2))
        for chunk in self._chunks(text):
            try:
                res = await self._mc.commands.send_chan_msg(channel, chunk)
                self._stats["tx"] += 1
                if self._is_error(res):
                    self._log(f"⚠️  send_chan_msg error: {getattr(res, 'payload', '')}")
            except Exception as exc:
                self._log(f"⚠️  error sending to ch{channel}: {exc}")
                self._stats["errors"] += 1
                break
            await asyncio.sleep(delay)

    async def _send_dm_async(self, key_prefix: str, text: str) -> None:
        if not self._mc:
            return
        contact = await self._resolve_contact(key_prefix)
        if not contact:
            self._log(f"⚠️  no contact for prefix {key_prefix}; cannot DM "
                      "(the node may not have advertised to us yet).")
            return
        delay = float(self._config.get("chunk_delay_sec", 2))
        for chunk in self._chunks(text):
            try:
                res = await self._mc.commands.send_msg(contact, chunk)
                self._stats["tx"] += 1
                if self._is_error(res):
                    self._log(f"⚠️  send_msg error: {getattr(res, 'payload', '')}")
            except Exception as exc:
                self._log(f"⚠️  error sending DM: {exc}")
                self._stats["errors"] += 1
                break
            await asyncio.sleep(delay)

    async def _resolve_contact(self, key_prefix: str):
        """Find a contact by pubkey prefix, refreshing the device contact list
        once if the first lookup misses (the node may have just advertised)."""
        try:
            contact = self._mc.get_contact_by_key_prefix(key_prefix)
        except Exception:
            contact = None
        if contact:
            return contact
        # Miss: refresh contacts from the device and try again.
        await self._refresh_contacts()
        try:
            return self._mc.get_contact_by_key_prefix(key_prefix)
        except Exception:
            return None

    # ── Small utilities ──────────────────────────────────────────────

    @staticmethod
    def _is_error(result: Any) -> bool:
        if result is None or EventType is None:
            return False
        return getattr(result, "type", None) == EventType.ERROR

    def _resolve_name(self, prefix: str) -> str:
        try:
            if prefix:
                with self._contacts_lock:
                    for key, c in self._contacts.items():
                        if not isinstance(c, dict):
                            continue
                        pub = str(c.get("public_key", key) or key)
                        if pub.startswith(prefix) or str(key).startswith(prefix):
                            return c.get("adv_name") or c.get("name") or f"MC_{prefix[:6]}"
        except Exception:
            pass
        return f"MC_{prefix[:6]}" if prefix else "MC_unknown"

    def _sender_id(self, prefix: str, name: str) -> str:
        """Build a stable node id for a MeshCore sender.

        DMs carry a real ``pubkey_prefix`` (use it). Channel broadcasts are
        shared-key with no per-sender crypto id, so we derive a stable id from
        the parsed display name instead of collapsing everyone onto
        ``!mc-unknown``. Two distinct named senders get distinct ids.
        """
        if prefix and prefix.lower() != "unknown":
            return f"!mc-{prefix[:8]}"
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip()).strip("-").lower()
        if slug and not slug.startswith("mc-"):
            return f"!mc-{slug[:16]}"
        return f"!mc-{slug[:16]}" if slug else "!mc-unknown"

    def _mark_node_seen(self, node_id: str) -> None:
        """Record that we just heard from this MeshCore node id."""
        if not node_id:
            return
        with self._node_last_seen_lock:
            self._node_last_seen[node_id] = time.time()

