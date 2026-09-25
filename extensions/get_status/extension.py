"""Get Status extension for MESH-API.

Integrates robot status updates into the Gemma LLM workflow using tool/function calling.
When a user asks for a status or diagnostic report, Gemma invokes the 'get_status' tool.
The extension executes get_robot_status_data() to collect a telemetry dictionary
(e.g., {"battery": 30, "temperature": 21, "status": "operational"}), passes it back to
Gemma, and Gemma parses it into a natural, formatted response for the user.
"""

import json
import re
import sys
import urllib.request
import urllib.error

from extensions.base_extension import BaseExtension


class GetStatusExtension(BaseExtension):
    """Extension providing 2-turn Gemma tool-calling integration for robot telemetry."""

    @property
    def name(self) -> str:
        return "Get Status"

    @property
    def version(self) -> str:
        return "1.2.0"

    # ── Registered Commands ──────────────────────────────────────────────
    @property
    def commands(self) -> dict:
        """Register optional slash commands for direct status access."""
        if self.config.get("enable_commands", True):
            return {
                "/status": "Get robot status report",
                "/get_status": "Get robot status report (alias)",
            }
        return {}

    # ── Status Data Properties ───────────────────────────────────────────
    @property
    def tool_name(self) -> str:
        """Name of the tool exposed to Gemma."""
        return self.config.get("tool_name", "get_status")

    @property
    def tool_description(self) -> str:
        """Description of the tool provided to Gemma."""
        return self.config.get(
            "tool_description",
            "Retrieve live robot telemetry, battery level, temperature, and diagnostics."
        )

    def get_robot_status_data(self, node_info: dict | None = None) -> dict:
        """Collect and return structured robot telemetry.

        Returns a dictionary containing robot metrics such as battery percentage,
        temperature, operational status, etc.

        Expand this method to connect to your real robot sensors, ROS topics,
        power management boards, or GPS devices!

        Parameters
        ----------
        node_info : dict, optional
            Information about the requesting mesh node (e.g. shortname, node_id).

        Returns
        -------
        dict
            Structured telemetry data to feed back to Gemma.
        """
        # =====================================================================
        # TODO: Expand live robot telemetry here.
        # Example:
        #   return {
        #       "battery": self.read_battery_percent(),
        #       "temperature": self.read_core_temperature(),
        #       "motors": "nominal",
        #       "ros_nodes_active": 14,
        #   }
        # =====================================================================
        default_data = {
            "battery": 30,
            "temperature": 21,
            "status": "operational",
        }
        return self.config.get("status_data", default_data)

    def format_status_fallback(self, data: dict) -> str:
        """Format the telemetry dictionary as a clean string for slash commands or fallbacks."""
        parts = []
        if "battery" in data:
            parts.append(f"Battery: {data['battery']}%")
        if "temperature" in data:
            parts.append(f"Temp: {data['temperature']}°C")
        if "status" in data:
            parts.append(f"Status: {data['status']}")
        for k, v in data.items():
            if k not in ("battery", "temperature", "status"):
                parts.append(f"{k.capitalize()}: {v}")
        return " | ".join(parts) if parts else "Robot status nominal."

    # ── Lifecycle Hooks ──────────────────────────────────────────────────
    def on_load(self) -> None:
        """Set up AI tool wrapper on load."""
        self._patched_modules: list[tuple[object, object]] = []
        self._orig_ctx_fn = None
        self.log(
            f"Get Status extension loaded (v{self.version}). "
            f"Tool '{self.tool_name}' active."
        )
        self._install_ai_interceptor()

    def on_unload(self) -> None:
        """Clean up interceptors on unload."""
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
        Turn 2: If Gemma invokes get_status, execute get_robot_status_data(),
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
            f"Call {self.tool_name} whenever the user asks for status, reports, "
            f"health, diagnostics, telemetry, or battery state. "
            f"When you receive the status data, parse it and formulate a clear, concise report for the user."
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
                # ── Collect live robot telemetry dictionary ──────────────
                status_dict = self.get_robot_status_data()
                status_json = json.dumps(status_dict)
                self.log(f"Gemma requested status. Telemetry: {status_json}")

                # ── Turn 2: Feed data back to Gemma to formulate response
                messages.append(msg)
                messages.append({
                    "role": "tool",
                    "content": status_json
                })

                # In case model prefers explicit user role on Turn 2:
                payload["messages"] = messages
                # Remove tools in Turn 2 to encourage final text output
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
