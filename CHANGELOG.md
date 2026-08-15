# Changelog

## 0.1.0

Initial port of the monolith's Blender integration into a decoupled app.

- Tier-2 container on `lscr.io/linuxserver/blender:latest`, KasmVNC on 3010,
  `$AW_APP_DATA` → `/config` so preferences, add-ons and scenes persist.
- `Blender` window in the Apps grid (`managed_app` / `web`).
- `aw-blender` stdio MCP — the full 22-tool surface of PyPI `blender-mcp`
  1.8.0, reimplemented dependency-free because the gateway container that
  spawns app MCP servers installs nothing on an app's behalf.
  Reaches Blender by container name instead of the monolith's shared network
  namespace. No telemetry. `get_viewport_screenshot` returns bytes over the
  command channel instead of reading a path that only existed in the other
  container.
- `aw-blender` skill: add-on setup, tool surface, and the pipeline traps
  (fresh exec scope per call, getting files out, baking transforms before
  export).
