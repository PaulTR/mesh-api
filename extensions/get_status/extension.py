"""Get Status extension for MESH-API.

Provides automated robot status updates over the mesh network.
Intercepts incoming status-related plain-text messages and responds with
a status update ("This is a status update" by default), while allowing
all other conversations (such as 'hello world') to pass through to the
Gemma AI model undisturbed.
"""

import re
import sys
from extensions.base_extension import BaseExtension


class GetStatusExtension(BaseExtension):
    """Extension providing robot status updates via plain-text keyword matching."""

    @property
    def name(self) -> str:
        return "Get Status"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ── Registered Commands ──────────────────────────────────────────────
    @property
    def commands(self) -> dict:
        """Register optional slash commands for status if enabled in config."""
        if self.config.get("enable_commands", True):
            return {
                "/status": "Get robot status update",
                "/get_status": "Get robot status update (alias)",
            }
        return {}

    # ── Status Message & Keyword Properties ──────────────────────────────
    @property
    def status_message(self) -> str:
        """Configured status response text."""
        return self.config.get("response_text", "This is a status update")

    @property
    def keywords(self) -> list[str]:
        """List of words/phrases that trigger a status update."""
        return self.config.get("keywords", ["status", "statuses"])

    def get_robot_status(self, node_info: dict | None = None) -> str:
        """Generate the status update text.

        Currently returns the configured response text ("This is a status update").
        Expand this method in the future to include dynamic robot telemetry
        (e.g., battery percentage, GPS position, ROS topics, hardware health,
        sensor readings, etc.).

        Parameters
        ----------
        node_info : dict, optional
            Information about the requesting mesh node (e.g. shortname, node_id).

        Returns
        -------
        str
            The status string to transmit back over the mesh.
        """
        # =====================================================================
        # TODO: Expand robot telemetry here as needed.
        # Example expansion:
        #   battery = self._read_battery_level()
        #   return f"{self.status_message} | Battery: {battery}%"
        # =====================================================================
        return self.status_message

    def _is_status_query(self, text: str) -> bool:
        """Check if incoming text contains any status-related keywords."""
        if not text:
            return False

        # Normalize text: lowercase and treat underscores as spaces
        clean_text = text.lower().replace("_", " ")

        for kw in self.keywords:
            cleaned_kw = kw.lower().strip().replace("_", " ")
            if not cleaned_kw:
                continue
            # Match whole-word boundary so "status" won't falsely match "statutory"
            pattern = r"\b" + re.escape(cleaned_kw) + r"\b"
            if re.search(pattern, clean_text):
                return True
        return False

    # ── Lifecycle Hooks ──────────────────────────────────────────────────
    def on_load(self) -> None:
        """Set up status keyword interceptor on load."""
        self._patched_modules: list[tuple[object, object]] = []
        self._orig_ctx_fn = None
        self.log(
            f"Get Status extension loaded (v{self.version}). "
            f"Keywords: {self.keywords}"
        )
        self._install_ai_interceptor()

    def on_unload(self) -> None:
        """Clean up interceptors on unload."""
        self._remove_ai_interceptor()
        self.log("Get Status extension unloaded.")

    # ── AI Pipeline Interception ─────────────────────────────────────────
    def _install_ai_interceptor(self) -> None:
        """Hook into the core get_ai_response pipeline.

        This ensures that when a plain-text status query (e.g. 'status' or
        'what is your status?') is sent directly to the robot, this extension
        responds immediately with the robot status update, preventing the Gemma
        model from being needlessly called while preserving normal AI chat
        for everything else (like 'hello world').
        """
        # Target modules where get_ai_response lives
        target_names = ["__main__", "mesh-api"]
        for mod_name in target_names:
            mod = sys.modules.get(mod_name)
            if mod and hasattr(mod, "get_ai_response"):
                orig_fn = getattr(mod, "get_ai_response")
                if not getattr(orig_fn, "_is_get_status_wrapper", False):
                    def make_wrapper(original_fn):
                        def wrapper(prompt, provider=None, endpoint=None):
                            if self.enabled and self.config.get("respond_to_direct", True):
                                if self._is_status_query(prompt):
                                    self.log(f"Intercepted status query: '{prompt}'")
                                    return self.get_robot_status()
                            return original_fn(prompt, provider=provider, endpoint=endpoint)
                        wrapper._is_get_status_wrapper = True
                        wrapper._original_fn = original_fn
                        return wrapper

                    wrapped_fn = make_wrapper(orig_fn)
                    setattr(mod, "get_ai_response", wrapped_fn)
                    self._patched_modules.append((mod, orig_fn))
                    self.log(f"Hooked status interceptor into {mod_name}.get_ai_response")

        # Also hook app_context's get_ai_response if present
        if "get_ai_response" in self.app_context:
            orig_ctx_fn = self.app_context["get_ai_response"]
            if not getattr(orig_ctx_fn, "_is_get_status_wrapper", False):
                def make_ctx_wrapper(original_fn):
                    def wrapper(prompt, provider=None, endpoint=None):
                        if self.enabled and self.config.get("respond_to_direct", True):
                            if self._is_status_query(prompt):
                                self.log(f"Intercepted status query via app_context: '{prompt}'")
                                return self.get_robot_status()
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

    # ── Command & Message Hooks ──────────────────────────────────────────
    def handle_command(self, command: str, args: str, node_info: dict) -> str | None:
        """Handle slash commands like /status or /get_status."""
        if not self.enabled:
            return None
        cmd_lower = command.lower()
        if cmd_lower in ("/status", "/get_status"):
            self.log(f"Handled command '{command}' from {node_info.get('shortname', '?')}")
            return self.get_robot_status(node_info)
        return None

    def handle_channel_message(self, text: str, node_info: dict) -> str | None:
        """Handle plain-text traffic on an assigned agent channel."""
        if not self.enabled:
            return None
        if self._is_status_query(text):
            self.log(f"Handled channel agent status query from {node_info.get('shortname', '?')}: '{text}'")
            return self.get_robot_status(node_info)
        return None

    def on_message(self, message: str, metadata: dict | None = None) -> None:
        """Observe inbound mesh messages and handle broadcast queries if needed."""
        if not self.enabled:
            return
        if not self._is_status_query(message):
            return

        metadata = metadata or {}
        is_direct = metadata.get("is_direct", False)

        # Direct messages are handled cleanly by the AI interceptor.
        # Slash commands are handled cleanly by handle_command.
        if is_direct or message.strip().startswith("/"):
            return

        # Check if the channel already has a channel agent assigned
        ch_idx = int(metadata.get("channel_idx") or 0)
        channel_agents = self.app_context.get("config", {}).get("channel_agents", {})
        if str(ch_idx) in channel_agents:
            # Channel agent route will handle it
            return

        # Reply to broadcast queries on channels when enabled
        if self.config.get("respond_to_broadcast", True):
            self.log(f"Replying to broadcast status query on channel {ch_idx}: '{message}'")
            self.send_to_mesh(self.get_robot_status(), channel_index=ch_idx)

    # ── MCP Tools (v0.7.0+) ──────────────────────────────────────────────
    def get_mcp_tools(self) -> list[dict]:
        """Expose get_status tool to external AI agents via MCP."""
        return [{
            "name": "get_status",
            "description": "Get the current robot status update.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        }]

    def call_mcp_tool(self, name: str, arguments: dict) -> str:
        """Handle execution of the get_status MCP tool."""
        if name == "get_status":
            return self.get_robot_status()
        return f"Unknown tool: {name}"
