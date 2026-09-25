# Wave Extension for MESH-API

The **Wave** extension enables the Innate MARS robot to physically wave its arm whenever it receives a greeting over the mesh network (or via direct slash command), and replies conversationally using the Gemma LLM.

---

## Features

- **Embodied Greeting Interaction**: Triggers the physical wave ROS skill on Innate OS (`innate-os/wave`) whenever the robot receives a greeting (e.g., *"hello"*, *"hi"*, *"hey"*, *"greetings"*, *"wave at me"*).
- **Gemma 2-Turn Tool Calling**: Exposes the `wave` tool natively to the Gemma LLM. Gemma decides to invoke `wave` on greetings, the robot executes the physical motion, and Gemma formulates a warm, natural reply (e.g. `"*Waves arm* Hello there! How can I help you today?"`).
- **Shared Multi-Tool Architecture**: Seamlessly interoperates with `get_status` and any other tool-enabled extensions. Gemma can call `wave`, `get_status`, or both within a single conversation turn.
- **Non-Blocking Arm Actuation**: Executes the physical skill in an asynchronous daemon thread so radio transmissions and mesh routing are never delayed.
- **Servo Motor Protection & Cooldown**: Configurable cooldown (default 6 seconds) prevents physical hardware and servo thrashing if multiple greetings arrive in rapid succession.
- **Observer Hook**: Automatically detects greetings in broadcast mesh traffic, allowing the embodied robot to wave at people in the room even if public mesh channel AI responses are disabled.
- **Direct Slash Command**: `/wave` triggers the physical gesture immediately and returns a friendly acknowledgment.
- **MCP Tool Exposure (v0.7.0+)**: Duck-typed `get_mcp_tools()` and `call_mcp_tool()` allow external MCP clients to trigger wave gestures.

---

## Configuration (`config.json`)

```json
{
  "enabled": true,
  "tool_name": "wave",
  "tool_description": "Physically wave the robot's arm to greet someone. Call this whenever the user greets you (hello, hi, hey, greetings, welcome) or asks you to wave.",
  "skill_id": "innate-os/wave",
  "wave_command": "innate skill run innate-os/wave",
  "cooldown_seconds": 6,
  "enable_commands": true,
  "trigger_on_greetings": true,
  "fallback_reply": "*Waves arm* Hello! It's great to hear from you."
}
```

### Configuration Options

| Option | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `true` | Enable or disable the extension. |
| `tool_name` | `str` | `"wave"` | Name of the function exposed to Gemma and MCP clients. |
| `tool_description` | `str` | *(see above)* | System description explaining to Gemma when to invoke the wave gesture. |
| `skill_id` | `str` | `"innate-os/wave"` | Identifier of the skill registered in Innate OS. |
| `wave_command` | `str` | `"innate skill run innate-os/wave"` | Shell command to execute the wave skill. |
| `cooldown_seconds` | `float` | `6` | Minimum seconds between consecutive physical actuations. |
| `enable_commands` | `bool` | `true` | Registers the `/wave` slash command. |
| `trigger_on_greetings`| `bool` | `true` | Enables automatic physical wave on incoming greetings. |
| `fallback_reply` | `str` | `"*Waves arm* Hello!..."` | Reply used if Gemma is unreachable or in non-LLM fallback mode. |

---

## Innate OS Execution

The extension attempts the following methods in order to execute the wave gesture on the Innate MARS robot:
1. **Python API Handle**: Directly executes `from innate_skills.wave import Wave; Wave().execute()`.
2. **Innate CLI (`innate`)**: Directly executes `innate skill run <skill_id>`.
3. **Sourced Shell**: Sources `~/.zshrc` / `~/.bashrc` to inherit robot environment variables and runs `wave_command`.
