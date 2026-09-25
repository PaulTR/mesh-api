"""Wave extension for MESH-API.

Enables the Innate MARS robot to physically wave its arm when receiving greetings
and reply conversationally through the Gemma LLM.

Executes the real Innate OS physical skill (innate-os/wave) non-blockingly via
the innate CLI or Python handle, and integrates seamlessly into Gemma 2-turn
function calling.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

from extensions.base_extension import BaseExtension

GREETING_PATTERN = re.compile(
    r"\b(hello|hi|hey|howdy|greetings|welcome|good\s+(morning|afternoon|evening)|yo\b|sup\b|wave|salutations|bonjour|hola)\b",
    re.IGNORECASE,
)


class WaveExtension(BaseExtension):
    """Extension providing physical arm wave actuation for Innate MARS robot on greetings."""

    def __init__(self, extension_dir: str, app_context: dict):
        super().__init__(extension_dir, app_context)
        self._last_wave_time = 0.0
        self._wave_lock = threading.Lock()
        self._patched_modules: list[tuple[object, object]] = []
        self._orig_ctx_fn = None

    @property
    def name(self) -> str:
        return "Wave"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ── Registered Commands ──────────────────────────────────────────────
    @property
    def commands(self) -> dict:
        """Register slash commands for direct wave trigger."""
        if self.config.get("enable_commands", True):
            return {
                "/wave": "Physically wave the robot arm and greet back",
            }
        return {}

    # ── Tool & Config Properties ─────────────────────────────────────────
    @property
    def tool_name(self) -> str:
        return self.config.get("tool_name", "wave")

    @property
    def tool_description(self) -> str:
        return self.config.get(
            "tool_description",
            "Physically wave the robot's arm to greet someone. Call this whenever the user "
            "greets you (hello, hi, hey, greetings, welcome) or asks you to wave.",
        )

    @property
    def skill_id(self) -> str:
        """Innate OS skill identifier (default: innate-os/wave)."""
        return self.config.get("skill_id", "innate-os/wave")

    @property
    def wave_command(self) -> str:
        """Shell command to run the wave skill."""
        return self.config.get("wave_command", "innate skill run innate-os/wave")

    @property
    def cooldown_seconds(self) -> float:
        """Minimum seconds between physical arm actuations to protect servo motors."""
        return float(self.config.get("cooldown_seconds", 6.0))

    @property
    def trigger_on_greetings(self) -> bool:
        """Whether to trigger physical wave on incoming greetings."""
        return bool(self.config.get("trigger_on_greetings", True))

    @property
    def fallback_reply(self) -> str:
        return self.config.get("fallback_reply", "*Waves arm* Hello! It's great to hear from you.")

    def log(self, message: str) -> None:
        """Log to console and core logger."""
        print(f"[ext:{self.name}] {message}", flush=True)
        super().log(message)

    # ── Physical Skill Execution ─────────────────────────────────────────
    def _run_physical_wave(self) -> None:
        """Execute the real wave skill on Innate OS in background thread."""
        self.log(f"Starting physical wave actuation for skill '{self.skill_id}'...")

        # 1. Try Python API handle if innate_skills is installed in environment
        try:
            from innate_skills.wave import Wave  # type: ignore

            wave_skill = Wave()
            if hasattr(wave_skill, "execute"):
                self.log("Invoking Innate wave skill via Python API handle...")
                wave_skill.execute()
                self.log("Innate wave physical motion completed via Python handle.")
                return
        except ImportError:
            pass
        except Exception as exc:
            self.log(f"Notice: Python handle invocation failed ({exc}), falling back to CLI")

        # 2. Try Innate CLI execution
        commands_to_try = []

        # Direct executable invocation if in PATH
        if shutil.which("innate"):
            commands_to_try.append(["innate", "skill", "run", self.skill_id])

        # Sourced shell invocation (handles robot user environment, aliases, and ROS setup)
        shell_script = (
            "source ~/.zshrc 2>/dev/null || source ~/.bashrc 2>/dev/null; "
            f"{self.wave_command}"
        )
        commands_to_try.append(["bash", "-c", shell_script])

        timeout_sec = int(self.config.get("wave_timeout_seconds", 15))

        for cmd in commands_to_try:
            cmd_display = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            self.log(f"Executing wave command ({timeout_sec}s timeout): {cmd_display}...")
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                )
                output = (res.stdout or "") + "\n" + (res.stderr or "")
                if res.returncode == 0:
                    self.log(f"✅ Physical wave skill completed successfully (code 0).")
                    return
                else:
                    self.log(f"⚠️ Wave command exited with code {res.returncode}. Snippet: {output.strip()[:140]}")
            except subprocess.TimeoutExpired:
                self.log(f"⚠️ Timeout ({timeout_sec}s) executing wave skill '{self.skill_id}'")
                return
            except Exception as exc:
                self.log(f"⚠️ Error executing wave skill: {exc}")

    def trigger_wave(self, reason: str = "greeting") -> dict:
        """Trigger physical wave skill non-blockingly with motor cooldown check."""
        with self._wave_lock:
            now = time.time()
            elapsed = now - self._last_wave_time
            if elapsed < self.cooldown_seconds:
                remaining = round(self.cooldown_seconds - elapsed, 1)
                self.log(f"Wave cooldown active ({remaining}s remaining). Skipping physical actuation.")
                return {
                    "status": "cooldown",
                    "action": "wave",
                    "skill": self.skill_id,
                    "message": "The robot recently waved its arm.",
                }
            self._last_wave_time = now

        self.log(f"Triggering physical wave arm motion (reason: {reason})")
        # Launch physical movement asynchronously so radio message is never delayed
        actuation_thread = threading.Thread(
            target=self._run_physical_wave,
            name="InnateWaveSkillThread",
            daemon=True,
        )
        actuation_thread.start()

        return {
            "status": "success",
            "action": "waving arm",
            "skill": self.skill_id,
            "message": "The robot is physically waving its arm to greet the user.",
        }

    # ── Lifecycle Hooks ──────────────────────────────────────────────────
    def on_load(self) -> None:
        """Register wave tool in shared Gemma tool registry and install interceptor."""
        self._register_in_tool_registry()
        self._install_ai_interceptor()
        self.log(f"Wave extension loaded (v{self.version}). Ready to wave on greetings ({self.skill_id}).")

    def on_unload(self) -> None:
        """Unregister tool and clean up interceptor."""
        self._unregister_from_tool_registry()
        self._remove_ai_interceptor()
        self.log("Wave extension unloaded.")

    # ── Shared Gemma Tool Registry ───────────────────────────────────────
    def _register_in_tool_registry(self) -> None:
        """Register the wave tool into app_context['gemma_tool_registry']."""
        registry = self.app_context.setdefault("gemma_tool_registry", {})
        registry[self.tool_name] = {
            "name": self.tool_name,
            "extension": self,
            "tool_def": {
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
            },
            "system_instruction": (
                f"You have access to a tool named '{self.tool_name}'. "
                f"Call {self.tool_name} whenever the user greets you (e.g. hello, hi, hey, greetings, welcome) or asks you to wave. "
                f"When the wave action completes, formulate a warm, friendly, concise greeting acknowledging the wave."
            ),
            "handler": lambda args: self.trigger_wave(reason="gemma_tool"),
            "format_fallback": lambda res: self.fallback_reply,
            "is_greeting": lambda text: bool(GREETING_PATTERN.search(text)),
        }

    def _unregister_from_tool_registry(self) -> None:
        """Remove the wave tool from the shared registry."""
        registry = self.app_context.get("gemma_tool_registry", {})
        registry.pop(self.tool_name, None)

    # ── AI Pipeline Interception ─────────────────────────────────────────
    def _install_ai_interceptor(self) -> None:
        """Hook into get_ai_response pipeline with multi-tool aggregation."""
        # Check if already installed
        if self.app_context.get("gemma_tool_interceptor_installed"):
            return

        target_names = ["__main__", "mesh-api"]
        for mod_name in target_names:
            mod = sys.modules.get(mod_name)
            if mod and hasattr(mod, "get_ai_response"):
                orig_fn = getattr(mod, "get_ai_response")
                if not getattr(orig_fn, "_is_gemma_tool_wrapper", False):
                    def make_wrapper(original_fn):
                        def wrapper(prompt, provider=None, endpoint=None):
                            return self._query_gemma_with_tools(prompt, original_fn, provider=provider, endpoint=endpoint)
                        wrapper._is_gemma_tool_wrapper = True
                        wrapper._original_fn = original_fn
                        return wrapper

                    wrapped_fn = make_wrapper(orig_fn)
                    setattr(mod, "get_ai_response", wrapped_fn)
                    self._patched_modules.append((mod, orig_fn))
                    self.log(f"Hooked multi-tool dispatch into {mod_name}.get_ai_response")

        if "get_ai_response" in self.app_context:
            orig_ctx_fn = self.app_context["get_ai_response"]
            if not getattr(orig_ctx_fn, "_is_gemma_tool_wrapper", False):
                def make_ctx_wrapper(original_fn):
                    def wrapper(prompt, provider=None, endpoint=None):
                        return self._query_gemma_with_tools(prompt, original_fn, provider=provider, endpoint=endpoint)
                    wrapper._is_gemma_tool_wrapper = True
                    wrapper._original_fn = original_fn
                    return wrapper

                self._orig_ctx_fn = orig_ctx_fn
                self.app_context["get_ai_response"] = make_ctx_wrapper(orig_ctx_fn)

        self.app_context["gemma_tool_interceptor_installed"] = True

    def _remove_ai_interceptor(self) -> None:
        """Restore original get_ai_response if no tools remain."""
        registry = self.app_context.get("gemma_tool_registry", {})
        # If other extensions (like get_status) still have active tools, keep interceptor active
        active_tools = {k: v for k, v in registry.items() if getattr(v.get("extension"), "enabled", False)}
        if active_tools:
            return

        for mod, orig_fn in self._patched_modules:
            try:
                setattr(mod, "get_ai_response", orig_fn)
            except Exception as exc:
                self.log(f"⚠️ Failed to restore get_ai_response on {mod}: {exc}")
        self._patched_modules.clear()

        if self._orig_ctx_fn and "get_ai_response" in self.app_context:
            self.app_context["get_ai_response"] = self._orig_ctx_fn
            self._orig_ctx_fn = None

        self.app_context["gemma_tool_interceptor_installed"] = False

    # ── Gemma 2-Turn Multi-Tool Calling Engine ────────────────────────────
    def _query_gemma_with_tools(self, prompt: str, orig_fn, provider=None, endpoint=None) -> str | None:
        """Handle 2-turn function calling with Gemma supporting all registered tools.
        
        Discovers tools across all loaded extensions (get_status, wave, etc.)
        and feeds them to Ollama /api/chat.
        """
        registry = self.app_context.get("gemma_tool_registry", {})
        # Filter to currently enabled extensions
        active_tools = {
            k: v for k, v in registry.items()
            if getattr(v.get("extension"), "enabled", True)
        }

        # If no tools registered or enabled, bypass directly to original provider
        if not active_tools:
            return orig_fn(prompt, provider=provider, endpoint=endpoint)

        main_cfg = self.app_context.get("config", {})
        prov = (provider or main_cfg.get("ai_provider") or "ollama").lower()
        if prov != "ollama" or endpoint:
            # If prompt is a greeting and wave is enabled, trigger physical wave before falling back
            if self.enabled and self.trigger_on_greetings and GREETING_PATTERN.search(prompt):
                self.trigger_wave(reason="non_ollama_greeting")
            return orig_fn(prompt, provider=provider, endpoint=endpoint)

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

        instructions = [base_system_prompt]
        for tname, tinfo in active_tools.items():
            if "system_instruction" in tinfo:
                instructions.append(tinfo["system_instruction"])
        system_instruction = "\n".join(instructions)

        tools_payload = [tinfo["tool_def"] for tinfo in active_tools.values()]

        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt},
        ]

        payload = {
            "model": model,
            "messages": messages,
            "tools": tools_payload,
            "stream": False,
            "keep_alive": keep_alive,
            "options": options,
        }

        try:
            # ── Turn 1: Send user message with all active tools ───────────
            req = urllib.request.Request(
                chat_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            msg = data.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content", "").strip()

            # Identify which tools Gemma called
            called_tools = []
            for call in tool_calls:
                fn_name = call.get("function", {}).get("name")
                if fn_name in active_tools:
                    called_tools.append((fn_name, call.get("function", {}).get("arguments") or {}))

            # Tag fallback check (e.g. [CALL:wave] or call:get_status)
            for tname in active_tools:
                if not any(t[0] == tname for t in called_tools):
                    tag_pattern = rf"(\[CALL:{re.escape(tname)}\]|<tool_call>{re.escape(tname)}.*?</tool_call>|\bcall:{re.escape(tname)}\b|\b{re.escape(tname)}\(\))"
                    if re.search(tag_pattern, content, re.IGNORECASE):
                        called_tools.append((tname, {}))

            # Greeting safety check: if prompt is clearly a greeting and wave wasn't invoked, trigger wave
            if self.enabled and self.trigger_on_greetings and GREETING_PATTERN.search(prompt):
                if not any(t[0] == self.tool_name for t in called_tools):
                    self.trigger_wave(reason="greeting_observer_fallback")

            if called_tools:
                # ── Execute all invoked tools ─────────────────────────────
                messages.append(msg)
                last_result = None
                last_tname = None

                for tname, targs in called_tools:
                    tinfo = active_tools[tname]
                    self.log(f"Executing tool '{tname}' invoked by Gemma...")
                    res = tinfo["handler"](targs)
                    last_result = res
                    last_tname = tname
                    res_str = json.dumps(res) if isinstance(res, (dict, list)) else str(res)
                    messages.append({
                        "role": "tool",
                        "content": res_str,
                    })

                # ── Turn 2: Feed tool outputs back to Gemma to format ─────
                payload["messages"] = messages
                payload.pop("tools", None)

                try:
                    req2 = urllib.request.Request(
                        chat_url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                        data2 = json.loads(resp2.read().decode("utf-8"))

                    turn2_content = data2.get("message", {}).get("content", "").strip()
                    if turn2_content:
                        clean_turn2 = sanitize_fn(turn2_content) if sanitize_fn else turn2_content
                        self.log(f"Gemma Turn 2 response: '{clean_turn2}'")
                        return clean_turn2[:max_len]
                except Exception as t2_err:
                    self.log(f"Turn 2 formatting error ({t2_err}), using tool fallback")
                    if last_tname and last_tname in active_tools:
                        return active_tools[last_tname]["format_fallback"](last_result)

                if last_tname and last_tname in active_tools:
                    return active_tools[last_tname]["format_fallback"](last_result)

            # Gemma did not invoke any tools; return standard conversational response
            clean_content = sanitize_fn(content) if sanitize_fn else content
            return (clean_content if clean_content else "🤖 [No response]")[:max_len]

        except Exception as exc:
            self.log(f"⚠️ Ollama /api/chat failed ({exc}), falling back to core AI")

        # Fallback to original provider
        if self.enabled and self.trigger_on_greetings and GREETING_PATTERN.search(prompt):
            self.trigger_wave(reason="error_fallback")
            return self.fallback_reply

        return orig_fn(prompt, provider=provider, endpoint=endpoint)

    # ── Command & Message Hooks ──────────────────────────────────────────
    def handle_command(self, command: str, args: str, node_info: dict) -> str | None:
        """Handle slash command /wave."""
        if not self.enabled:
            return None
        cmd_lower = command.lower()
        if cmd_lower == "/wave":
            res = self.trigger_wave(reason="slash_command")
            sender = node_info.get("shortname", "friend")
            self.log(f"Handled /wave command from {sender}")
            return f"*Waves arm* Hello {sender}! Innate MARS robot at your service."
        return None

    def handle_channel_message(self, text: str, node_info: dict) -> str | None:
        """Handle messages on an assigned agent channel."""
        if not self.enabled:
            return None
        return self._query_gemma_with_tools(text, lambda p, **kw: None)

    def on_message(self, message: str, metadata: dict | None = None) -> None:
        """Observe inbound mesh messages and trigger wave on greetings."""
        if not self.enabled or not self.trigger_on_greetings:
            return
        if not message:
            return
        if GREETING_PATTERN.search(message):
            # Check if this was a broadcast where AI won't reply; wave to greet people in room
            is_direct = metadata.get("is_direct", False) if metadata else False
            if not is_direct:
                sender = metadata.get("sender_info", "mesh") if metadata else "mesh"
                self.log(f"Greeting observed on mesh from {sender}. Initiating wave actuation.")
                self.trigger_wave(reason="mesh_broadcast_greeting")

    # ── MCP Tools (v0.7.0+) ──────────────────────────────────────────────
    def get_mcp_tools(self) -> list[dict]:
        """Expose wave tool to external AI agents via MCP."""
        return [{
            "name": self.tool_name,
            "description": self.tool_description,
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        }]

    def call_mcp_tool(self, name: str, arguments: dict) -> str:
        """Handle execution of the wave MCP tool."""
        if name == self.tool_name:
            res = self.trigger_wave(reason="mcp_tool")
            return json.dumps(res)
        return f"Unknown tool: {name}"
