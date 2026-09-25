"""Get Status extension for MESH-API.

Integrates robot battery and telemetry into the Gemma LLM workflow using tool/function calling.
When a user asks for battery, status, or diagnostics (e.g. "what's my battery percentage?"),
Gemma invokes the 'get_status' tool.
The extension queries the live ROS 2 topic /battery_state (e.g. on Innate MARS robot),
passes the parsed telemetry to Gemma, and Gemma formulates a natural, formatted response.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

from extensions.base_extension import BaseExtension


class GetStatusExtension(BaseExtension):
    """Extension providing 2-turn Gemma tool-calling with live ROS 2 /battery_state telemetry."""

    @property
    def name(self) -> str:
        return "Get Status"

    @property
    def version(self) -> str:
        return "1.3.0"

    # ── Registered Commands ──────────────────────────────────────────────
    @property
    def commands(self) -> dict:
        """Register optional slash commands for direct status access."""
        if self.config.get("enable_commands", True):
            return {
                "/status": "Get robot battery and telemetry report",
                "/get_status": "Get robot battery report (alias)",
            }
        return {}

    # ── Tool & Config Properties ─────────────────────────────────────────
    @property
    def tool_name(self) -> str:
        """Name of the tool exposed to Gemma."""
        return self.config.get("tool_name", "get_status")

    @property
    def tool_description(self) -> str:
        """Description of the tool provided to Gemma."""
        return self.config.get(
            "tool_description",
            "Retrieve live robot telemetry, battery percentage, voltage, and diagnostics."
        )

    @property
    def ros_battery_topic(self) -> str:
        """ROS 2 topic publishing BatteryState."""
        return self.config.get("ros_battery_topic", "/battery_state")

    @property
    def status_file(self) -> str:
        """Optional file where a dedicated ROS node might dump telemetry."""
        return self.config.get("status_file", "/tmp/robot_status.json")

    # ── ROS 2 Telemetry Ingestion ────────────────────────────────────────
    def _parse_battery_yaml(self, raw_text: str) -> dict | None:
        """Extract battery fields from 'ros2 topic echo /battery_state --once' output."""
        if not raw_text:
            return None

        # Extract percentage (e.g. percentage: 0.044607844203710556)
        m_pct = re.search(r"percentage:\s*([0-9.]+)", raw_text)
        if not m_pct:
            return None

        pct_raw = float(m_pct.group(1))
        # In ROS 2 BatteryState, percentage is 0.0 to 1.0 (or 0 to 100)
        if pct_raw <= 1.0:
            pct_val = round(pct_raw * 100, 1)
        else:
            pct_val = round(pct_raw, 1)

        # Extract voltage
        m_volt = re.search(r"voltage:\s*([0-9.]+)", raw_text)
        volt_val = round(float(m_volt.group(1)), 2) if m_volt else None

        # Extract power supply status
        m_status = re.search(r"power_supply_status:\s*([0-9]+)", raw_text)
        status_map = {
            1: "charging",
            2: "discharging",
            3: "not charging",
            4: "full",
        }
        status_val = status_map.get(int(m_status.group(1)), "discharging") if m_status else "discharging"

        # Extract cell voltages if available
        # Format in yaml:
        # cell_voltage:
        # - 3.573333263397217
        cells_match = re.search(r"cell_voltage:\s*\n((?:\s*-\s*[0-9.]+\n?)+)", raw_text)
        cells = []
        if cells_match:
            cells = [round(float(c), 2) for c in re.findall(r"-\s*([0-9.]+)", cells_match.group(1))]

        return {
            "battery_percentage": pct_val,
            "voltage": volt_val,
            "power_supply_status": status_val,
            "cells": cells if cells else None,
            "robot_state": "operational",
        }

    def _fetch_ros_battery(self) -> dict | None:
        """Fetch battery telemetry from /tmp/robot_status.json or by querying ROS 2 directly."""
        # 1. Try reading from status file first (fastest, 0ms)
        if os.path.exists(self.status_file):
            try:
                with open(self.status_file, "r") as f:
                    data = json.load(f)
                if data and "battery" in data or "battery_percentage" in data:
                    return data
            except Exception as e:
                self.log(f"Notice: could not read {self.status_file}: {e}")

        # 2. Query ROS 2 topic directly using subprocess
        source_cmd = self.config.get("ros_source_command", "source /opt/ros/humble/setup.bash 2>/dev/null")
        cmd = f"{source_cmd}; ros2 topic echo {self.ros_battery_topic} --once"

        try:
            res = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout:
                parsed = self._parse_battery_yaml(res.stdout)
                if parsed:
                    return parsed
            else:
                self.log(f"ros2 topic echo stderr: {res.stderr.strip()[:100]}")
        except subprocess.TimeoutExpired:
            self.log(f"Timeout querying {self.ros_battery_topic}")
        except Exception as exc:
            self.log(f"Error querying ROS 2: {exc}")

        return None

    def _poll_loop(self) -> None:
        """Background thread updating cached battery telemetry every poll interval."""
        interval = max(5, int(self.config.get("poll_interval_seconds", 15)))
        self.log(f"Battery polling thread started (interval: {interval}s, topic: {self.ros_battery_topic})")

        while not self._stop_event.is_set():
            data = self._fetch_ros_battery()
            if data:
                self._cached_battery_data = data
            self._stop_event.wait(interval)

        self.log("Battery polling thread stopped.")

    def get_robot_status_data(self, node_info: dict | None = None) -> dict:
        """Return the latest structured robot telemetry for Gemma."""
        # Return cached data if available
        if hasattr(self, "_cached_battery_data") and self._cached_battery_data:
            return self._cached_battery_data

        # Fallback to direct query
        fresh_data = self._fetch_ros_battery()
        if fresh_data:
            self._cached_battery_data = fresh_data
            return fresh_data

        # Safe default if ROS topic is unreachable
        return {
            "battery_percentage": 0,
            "voltage": 0.0,
            "power_supply_status": "offline/unknown",
            "robot_state": "connecting to ROS",
        }

    def format_status_fallback(self, data: dict) -> str:
        """Format the telemetry dictionary as a clean string for slash commands."""
        pct = data.get("battery_percentage", data.get("battery", "?"))
        volt = data.get("voltage")
        status = data.get("power_supply_status", data.get("status", "unknown"))

        msg = f"Battery: {pct}%"
        if volt:
            msg += f" ({volt}V)"
        if status:
            msg += f" [{status}]"

        cells = data.get("cells")
        if cells:
            msg += f" | Cells: {cells}"
        return msg

    # ── Lifecycle Hooks ──────────────────────────────────────────────────
    def on_load(self) -> None:
        """Set up AI tool wrapper and background battery polling thread."""
        self._patched_modules: list[tuple[object, object]] = []
        self._orig_ctx_fn = None
        self._cached_battery_data: dict | None = None
        self._stop_event = threading.Event()

        # Prime the cache on startup
        threading.Thread(target=self._prime_cache, daemon=True).start()

        # Start periodic poller
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        self.log(f"Get Status extension loaded (v{self.version}). Tool '{self.tool_name}' active.")
        self._install_ai_interceptor()

    def _prime_cache(self) -> None:
        """Perform an initial battery read shortly after launch."""
        time.sleep(1)
        data = self._fetch_ros_battery()
        if data:
            self._cached_battery_data = data
            pct = data.get("battery_percentage")
            volt = data.get("voltage")
            self.log(f"Initial battery reading captured: {pct}% ({volt}V)")

    def on_unload(self) -> None:
        """Clean up threads and interceptors on unload."""
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        self._remove_ai_interceptor()
        self.log("Get Status extension unloaded.")

    # ── AI Pipeline Interception ─────────────────────────────────────────
    def _install_ai_interceptor(self) -> None:
        """Hook into the core get_ai_response pipeline."""
        target_names = ["__main__", "mesh-api"]
        for mod_name in target_names:
            mod = sys.modules.get(mod_name)
            if mod and hasattr(mod, "get_ai_response"):
                orig_fn = getattr(mod, "get_ai_response")
                if not getattr(orig_fn, "_is_get_status_wrapper", False):
                    def make_wrapper(original_fn):
                        def wrapper(prompt, provider=None, endpoint=None):
                            if self.enabled:
                                return self._query_gemma_with_tools(prompt, original_fn)
                            return original_fn(prompt, provider=provider, endpoint=endpoint)
                        wrapper._is_get_status_wrapper = True
                        wrapper._original_fn = original_fn
                        return wrapper

                    wrapped_fn = make_wrapper(orig_fn)
                    setattr(mod, "get_ai_response", wrapped_fn)
                    self._patched_modules.append((mod, orig_fn))
                    self.log(f"Hooked status tool into {mod_name}.get_ai_response")

        if "get_ai_response" in self.app_context:
            orig_ctx_fn = self.app_context["get_ai_response"]
            if not getattr(orig_ctx_fn, "_is_get_status_wrapper", False):
                def make_ctx_wrapper(original_fn):
                    def wrapper(prompt, provider=None, endpoint=None):
                        if self.enabled:
                            return self._query_gemma_with_tools(prompt, original_fn)
                        return original_fn(prompt, provider=provider, endpoint=endpoint)
                    wrapper._is_get_status_wrapper = True
                    wrapper._original_fn = original_fn
                    return wrapper

                self._orig_ctx_fn = orig_ctx_fn
                self.app_context["get_ai_response"] = make_ctx_wrapper(orig_ctx_fn)

    def _remove_ai_interceptor(self) -> None:
        """Restore original get_ai_response implementations."""
        for mod, orig_fn in self._patched_modules:
            try:
                setattr(mod, "get_ai_response", orig_fn)
            except Exception as exc:
                self.log(f"⚠️ Failed to restore get_ai_response on {mod}: {exc}")
        self._patched_modules.clear()

        if self._orig_ctx_fn and "get_ai_response" in self.app_context:
            self.app_context["get_ai_response"] = self._orig_ctx_fn
            self._orig_ctx_fn = None

    # ── Gemma 2-Turn Tool Calling Execution ──────────────────────────────
    def _query_gemma_with_tools(self, prompt: str, orig_fn) -> str | None:
        """Handle 2-turn function calling with Gemma:
        
        Turn 1: Send prompt + get_status tool definition to Ollama /api/chat.
        Turn 2: If Gemma invokes get_status, collect live battery data,
                pass the JSON back to Gemma, and let Gemma formulate the final response.
        """
        main_cfg = self.app_context.get("config", {})
        provider = (main_cfg.get("ai_provider") or "ollama").lower()

        if provider != "ollama":
            return orig_fn(prompt)

        raw_url = main_cfg.get("ollama_url", "http://localhost:11434/api/generate")
        chat_url = self.config.get("ollama_chat_url") or raw_url.replace("/api/generate", "/api/chat")
        model = main_cfg.get("ollama_model", "gemma4:e2b-it-qat")
        timeout = int(main_cfg.get("ollama_timeout", 180))
        keep_alive = main_cfg.get("ollama_keep_alive", "10m")
        options = main_cfg.get("ollama_options", {})
        max_len = self.app_context.get("MAX_RESPONSE_LENGTH", 200)
        sanitize_fn = self.app_context.get("sanitize_model_output")

        base_system_prompt = main_cfg.get(
            "system_prompt",
            "You are a helpful assistant responding to mesh network chats. Keep replies concise."
        )
        system_instruction = (
            f"{base_system_prompt}\n"
            f"You have access to a tool named '{self.tool_name}'. "
            f"Call {self.tool_name} whenever the user asks for battery level, status, reports, "
            f"health, diagnostics, or power state. "
            f"When you receive the battery and telemetry data, parse it and formulate a clear, concise report for the user."
        )

        tools = [
            {
                "type": "function",
                "function": {
                    "name": self.tool_name,
                    "description": self.tool_description,
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }
        ]

        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt},
        ]

        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "keep_alive": keep_alive,
            "options": options,
        }

        try:
            # ── Turn 1: Send user message to Gemma with tools ────────────
            req = urllib.request.Request(
                chat_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            msg = data.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content", "").strip()

            # Check if Gemma invoked get_status natively
            invoked_natively = any(
                call.get("function", {}).get("name") == self.tool_name
                for call in tool_calls
            )

            # Check if Gemma invoked get_status via tag fallback
            tag_pattern = rf"(\[CALL:{re.escape(self.tool_name)}\]|<tool_call>{re.escape(self.tool_name)}.*?</tool_call>|\bcall:{re.escape(self.tool_name)}\b|\b{re.escape(self.tool_name)}\(\))"
            invoked_via_tag = bool(re.search(tag_pattern, content, re.IGNORECASE))

            if invoked_natively or invoked_via_tag:
                # ── Collect live robot battery telemetry ─────────────────
                status_dict = self.get_robot_status_data()
                status_json = json.dumps(status_dict)
                self.log(f"Gemma requested status. Telemetry: {status_json}")

                # ── Turn 2: Feed data back to Gemma to formulate response
                messages.append(msg)
                messages.append({
                    "role": "tool",
                    "content": status_json
                })

                payload["messages"] = messages
                payload.pop("tools", None)

                try:
                    req2 = urllib.request.Request(
                        chat_url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                        data2 = json.loads(resp2.read().decode("utf-8"))

                    turn2_content = data2.get("message", {}).get("content", "").strip()
                    if turn2_content:
                        clean_turn2 = sanitize_fn(turn2_content) if sanitize_fn else turn2_content
                        self.log(f"Gemma formatted status response: '{clean_turn2}'")
                        return clean_turn2[:max_len]
                except Exception as t2_err:
                    self.log(f"Turn 2 formatting error ({t2_err}), using formatted telemetry fallback")
                    return self.format_status_fallback(status_dict)

                # Fallback if Gemma returned empty text on Turn 2
                return self.format_status_fallback(status_dict)

            # Gemma decided no status check was needed (e.g. 'hello world')
            clean_content = sanitize_fn(content) if sanitize_fn else content
            return (clean_content if clean_content else "🤖 [No response]")[:max_len]

        except Exception as exc:
            self.log(f"⚠️ Ollama /api/chat failed ({exc}), falling back to core AI")

        # Fallback to original provider on error
        return orig_fn(prompt)

    # ── Command & Channel Hooks ──────────────────────────────────────────
    def handle_command(self, command: str, args: str, node_info: dict) -> str | None:
        """Handle slash commands like /status or /get_status."""
        if not self.enabled:
            return None
        cmd_lower = command.lower()
        if cmd_lower in ("/status", "/get_status"):
            data = self.get_robot_status_data(node_info)
            self.log(f"Handled command '{command}' from {node_info.get('shortname', '?')}")
            return self.format_status_fallback(data)
        return None

    def handle_channel_message(self, text: str, node_info: dict) -> str | None:
        """Handle plain-text traffic on an assigned agent channel."""
        if not self.enabled:
            return None
        return self._query_gemma_with_tools(text, lambda p: None)

    # ── MCP Tools (v0.7.0+) ──────────────────────────────────────────────
    def get_mcp_tools(self) -> list[dict]:
        """Expose get_status tool to external AI agents via MCP."""
        return [{
            "name": self.tool_name,
            "description": self.tool_description,
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        }]

    def call_mcp_tool(self, name: str, arguments: dict) -> str:
        """Handle execution of the get_status MCP tool."""
        if name == self.tool_name:
            return json.dumps(self.get_robot_status_data())
        return f"Unknown tool: {name}"
