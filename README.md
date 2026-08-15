# aw-app-blender

Blender 3D as an aw-workspace app: a real GUI Blender in a container, served
into the Apps grid over KasmVNC, plus the **`aw-blender` MCP** so an agent
session can inspect and drive the running scene.

Ported as-is from the `agentic-workspace` monolith's `aw-custom-blender`
docker service (`src/config/aw.json`) and its `aw-blender` MCP entry
(`src/config/mcp.json`).

## What you get

| Piece | Detail |
|---|---|
| Container | `lscr.io/linuxserver/blender:latest` (upstream image, not rebuilt here) |
| Window | Apps grid → **Blender** — the full desktop, `managed_app` / `web` |
| Persistence | `$AW_APP_DATA` → `/config`: preferences, add-ons, saved `.blend` files |
| MCP | `aw-blender`, 22 tools — scene info, arbitrary `bpy`, viewport screenshots, Poly Haven / Sketchfab / Hyper3D Rodin / Hunyuan3D |
| Skill | `aw-blender` — setup, tool surface, and the pipeline traps worth knowing |

## Setup

Installing the app gets you Blender in the browser. The **MCP** additionally
needs the BlenderMCP add-on installed and connected inside that Blender
session — a one-time GUI step, described in
[`skills/aw-blender/SKILL.md`](skills/aw-blender/SKILL.md). It persists in
`/config`, so it survives container recreates.

## How the MCP reaches Blender

The add-on listens on TCP `9876` inside the Blender container. In the
monolith the MCP process shared that container's network namespace
(`network_mode: container:aw-sandbox`), so it dialled `localhost`. Here the
container is a normal Tier-2 app on the workspace's podman network, so it is
reached by name — `BLENDER_HOST=aw-app-blender`, set in `mcp.json` and
overridable.

## Why the MCP is reimplemented rather than pip-installed

The monolith ran the PyPI `blender-mcp` package (v1.8.0). Here, the process
that spawns an app's stdio MCP servers is the **gateway container**
(`aw-app-mcp-gateway`): it scans each installed app's root `mcp.json` and
starts what it declares, with cwd under its read-only
`/opt/aw-workspace/apps` mount. That container is a plain `python:3.12-slim`
and installs nothing on an app's behalf — and `runtime.pip_requires` installs
into the *workspace* process's environment, which is a different container.
So `mcp[cli]` + `httpx` would be importable in neither.

`mcp_server/server.py` therefore speaks the add-on's socket protocol and MCP
stdio JSON-RPC on the standard library alone, the same shape
`aw-app-code-server` uses. Same tools, same wire format. Two deliberate
differences from upstream:

- **No telemetry.** Upstream phones home per tool call with prompt text and
  uploaded screenshots. Not carried over.
- **`get_viewport_screenshot` actually works across containers.** Upstream
  has the add-on write a PNG and then reads that path locally, which only
  works when both share a filesystem — they didn't in the monolith either.
  Here the bytes come back base64 over the command channel.

## Development

```bash
python3 -m pytest tests/ -q          # manifest + MCP protocol tests, no Blender needed
python3 -m mcp_server.server         # stdio MCP, for manual poking
```

Releases go through `aw-marketplace`'s shared `app-release.yml` (see
`.github/workflows/release.yml`) — push to `master` cuts a version from the
commit messages and opens the catalog sync PR.
