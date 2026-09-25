# Get Status Extension for MESH-API

An automated robot status extension with **ROS 2 `/battery_state` Telemetry & Gemma Function Calling** for MESH-API.

## How it Works

```
1. User sends message: "What's the battery percentage?", "Send a report", "Status?"
                          │
                          ▼
2. Gemma receives prompt with tool definition:
   Tool: get_status()
                          │
                          ▼
3. Gemma analyzes user intent semantically. If battery/status is needed, Gemma issues tool call:
   get_status()
                          │
                          ▼
4. Extension retrieves live battery telemetry from ROS 2:
   - Queries `ros2 topic echo /battery_state --once` (or /tmp/robot_status.json)
   - Parses:
     • percentage: e.g. 0.0446 -> 4.5%
     • voltage: 10.72V
     • power_supply_status: discharging
     • cells: [3.57, 3.57, 3.57]
                          │
                          ▼
5. Telemetry dict is passed back to Gemma in Turn 2.
                          │
                          ▼
6. Gemma parses the data and formulates a natural, formatted response:
   "Robot battery is currently at 4.5% (10.72V, discharging). All 3 cells are balanced at 3.57V."
                          │
                          ▼
7. Response is transmitted back over the mesh!
```

## Features

- **Automatic ROS 2 Integration**: Reads `/battery_state` directly from ROS 2 Humble on your Innate MARS robot.
- **Background Polling & Caching**: Polls every 15 seconds in the background so Gemma receives the data in 0ms without waiting for Zenoh/DDS startup.
- **2-Turn Gemma Synthesis**: Gemma digests the raw percentage and voltage numbers and presents them conversationally.
- **Slash Commands**: `/status` and `/get_status` return instant status strings: `Battery: 4.5% (10.72V) [discharging] | Cells: [3.57, 3.57, 3.57]`.

## Configuration (`config.json`)

```json
{
  "enabled": true,
  "tool_name": "get_status",
  "tool_description": "Retrieve live robot telemetry, battery percentage, voltage, and diagnostics.",
  "ros_battery_topic": "/battery_state",
  "ros_source_command": "source /opt/ros/humble/setup.bash 2>/dev/null",
  "status_file": "/tmp/robot_status.json",
  "poll_interval_seconds": 15,
  "enable_commands": true
}
```
