# Get Status Extension for MESH-API

An automated robot status extension with **2-turn Gemma LLM Function Calling** for MESH-API.

## How the Workflow Works

```
1. User sends message: "Send a report", "What is your status?", "How are systems holding up?"
                          │
                          ▼
2. Gemma receives prompt with tool definition:
   Tool: get_status()
                          │
                          ▼
3. Gemma analyzes user intent semantically. If status is needed, Gemma issues tool call:
   get_status()
                          │
                          ▼
4. Extension executes get_robot_status_data():
   Returns telemetry dict: {"battery": 30, "temperature": 21, "status": "operational"}
                          │
                          ▼
5. Telemetry dict is passed back to Gemma in Turn 2.
                          │
                          ▼
6. Gemma parses the data and formulates a natural, conversational response:
   "Battery is at 30% and temperature is 21°C. All systems are operational."
                          │
                          ▼
7. Response is transmitted back over the mesh!
```

If the user sends general chatter (e.g., `"hello world"`), Gemma determines no tool is needed and responds directly without calling `get_status`.

## Configuration (`config.json`)

```json
{
  "enabled": true,
  "tool_name": "get_status",
  "tool_description": "Retrieve live robot telemetry, battery level, temperature, and diagnostics.",
  "status_data": {
    "battery": 30,
    "temperature": 21,
    "status": "operational"
  },
  "enable_commands": true
}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `true` | Enable or disable the extension |
| `tool_name` | string | `"get_status"` | Name of the tool exposed to Gemma |
| `tool_description` | string | `"Retrieve live robot telemetry..."` | Prompt hint guiding Gemma when to call the tool |
| `status_data` | dict | `{"battery": 30, ...}` | Default mock telemetry data |
| `enable_commands` | boolean | `true` | Register `/status` and `/get_status` slash commands |

## Connecting to Real Robot Hardware

To feed real sensor readings to Gemma, edit [get_robot_status_data](file:///Users/paul/Documents/code/mesh-api/extensions/get_status/extension.py#L52-L84) in [extensions/get_status/extension.py](file:///Users/paul/Documents/code/mesh-api/extensions/get_status/extension.py):

```python
def get_robot_status_data(self, node_info: dict | None = None) -> dict:
    # Example reading from your robot's hardware / ROS:
    return {
        "battery": self.read_battery_percent(),
        "temperature": self.read_core_temperature(),
        "motors": "nominal",
        "ros_nodes_active": 14,
        "gps": {"lat": 37.7749, "lon": -122.4194}
    }
```
Gemma receives this entire dictionary and dynamically highlights the metrics in its response to the user.
