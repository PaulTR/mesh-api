# Get Status Extension for MESH-API

An automated robot status extension with **Live On-Demand ROS 2 `/battery_state` Telemetry & Gemma Function Calling** for MESH-API.

## How it Works

There are **zero hardcoded values**: every time a report is requested, the extension runs a live query directly to ROS 2.

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
4. Extension executes a LIVE query directly to ROS 2:
   `source /opt/ros/humble/setup.bash; ros2 topic echo /battery_state --once`
   (or reads live /tmp/robot_status.json if a daemon node is writing it)
                          │
                          ▼
5. Real-time values are dynamically parsed from the live message:
   • percentage: dynamically parsed (e.g. 0.0446 -> 4.5%)
   • voltage: dynamically parsed (e.g. 10.72V)
   • power_supply_status: dynamically parsed (e.g. discharging)
   • cells: dynamically parsed individual cell voltages
                          │
                          ▼
6. Telemetry dict is passed back to Gemma in Turn 2.
                          │
                          ▼
7. Gemma parses the data and formulates a natural, formatted response:
   "Robot battery is currently at 4.5% (10.72V, discharging). All 3 cells are balanced at 3.57V."
                          │
                          ▼
8. Response is transmitted back over the mesh!
```

If ROS 2 is unreachable, the extension reports an explicit error dictionary rather than fabricating fake numbers, allowing Gemma to inform the user honestly that ROS 2 is unreachable.

## Configuration (`config.json`)

```json
{
  "enabled": true,
  "tool_name": "get_status",
  "tool_description": "Retrieve live robot telemetry, battery percentage, voltage, and diagnostics.",
  "ros_battery_topic": "/battery_state",
  "ros_source_command": "source /opt/ros/humble/setup.bash 2>/dev/null",
  "status_file": "/tmp/robot_status.json",
  "enable_commands": true
}
```
