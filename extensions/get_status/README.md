# Get Status Extension for MESH-API

An automated robot status extension for MESH-API.

## Overview

This extension intercepts incoming messages containing status-related keywords (such as `status`, `statuses`, `what is your status?`, `robot status`) and responds with `"This is a status update"`.

All other conversations (such as `hello world`, general queries, etc.) pass through directly to your Gemma AI model without interference.

## Features

- **Keyword Interception:** Plain-text detection using word-boundary matching so words like `status` match while unrelated words like `statutory` or `apparatus` are ignored.
- **AI Pass-Through:** Skips calling the LLM (Gemma) when status is requested, saving robot compute, power, and radio bandwidth.
- **Multi-Radio Compatible:** Works across Meshtastic and MeshCore.
- **Optional Slash Commands:** Supports `/status` and `/get_status`.
- **Channel Agent Support:** Implements `handle_channel_message` for dedicated robot status channels.
- **MCP Tool:** Auto-exposes `get_status` tool for AI agents and the MCP server.

## Configuration (`config.json`)

```json
{
  "enabled": true,
  "response_text": "This is a status update",
  "keywords": [
    "status",
    "statuses"
  ],
  "respond_to_direct": true,
  "respond_to_broadcast": true,
  "enable_commands": true
}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `true` | Enable or disable the extension |
| `response_text` | string | `"This is a status update"` | The status message returned to the user |
| `keywords` | list[str] | `["status", "statuses"]` | Words or phrases that trigger the status response |
| `respond_to_direct` | boolean | `true` | Intercept direct messages sent to the robot |
| `respond_to_broadcast` | boolean | `true` | Reply to status queries received on public/group channels |
| `enable_commands` | boolean | `true` | Register `/status` and `/get_status` slash commands |

## Expanding Robot Telemetry

To add live robot data (such as battery percentage, ROS topics, GPS, or uptime), edit `get_robot_status()` in [`extension.py`](extension.py):

```python
def get_robot_status(self, node_info: dict | None = None) -> str:
    # Example: query your robot's battery or sensor API
    # battery = read_battery_level()
    # return f"{self.status_message} | Battery: {battery}%"
    return self.status_message
```
