# Changelog

## 0.3.0

- `get_viewport_screenshot` now writes into `/config/aw-out/` and returns the
  **path** (both the in-container one and the workspace-side one) instead of
  inline image data. An inline image cost context tokens on every call, even
  when the caller only wanted the file. `inline=true` opts back in for when
  you're iterating on a render; `filename` names the output.
- Verifies the capture actually landed before handing back a path, so a
  Blender session with no 3D viewport reports an error rather than a path to
  nothing.
- Skill/README: `/config` is the shared `$AW_APP_DATA` volume, so **any** file
  Blender writes there is readable from the workspace tree. The old
  `http.server` file-extraction workaround is documented as unnecessary.

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
