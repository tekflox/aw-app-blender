---
name: aw-blender
description: Drive this workspace's Blender (the aw-app-blender container — a real GUI Blender served over KasmVNC in the browser) from an agent session, via the aw-blender MCP. Covers the one-time BlenderMCP add-on setup the tools need, the full tool surface (scene inspection, arbitrary bpy execution, viewport screenshots, Poly Haven / Sketchfab / Hyper3D / Hunyuan3D asset providers), and the pipeline lessons that cost real time before — getting files out of the container, baking transforms before export, and the fresh-exec-scope trap. Use whenever asked to build, inspect, fix or export a 3D scene, import a model from Sketchfab/Poly Haven, generate an asset from text or a photo, or prepare a mesh for Roblox.
---

# aw-blender — driving Blender from an agent session

Blender runs as a Tier-2 app container (`aw-app-blender`, image
`lscr.io/linuxserver/blender:latest`). Two ways in:

| | |
|---|---|
| **The window** | Apps grid → **Blender**. A real Blender GUI over KasmVNC. |
| **The MCP** | `aw-blender` on the gateway → `aw__blender__*` / `mcp__aw-gateway__aw_blender__*` depending on prefix. |

The MCP does **not** talk to the image. It talks to the **BlenderMCP add-on
running inside the Blender session**, over a TCP socket on port `9876`. No
Blender open with the add-on's server switched on → every tool fails with a
connection error. That is the single most common cause of "the Blender tools
are broken".

## One-time setup (per fresh container data volume)

The app's `/config` is a persistent volume (`$AW_APP_DATA`), so this survives
container recreates — but it does **not** survive deleting the app's data.

1. Open the Blender window from the Apps grid, wait for the desktop.
2. Install the BlenderMCP add-on: download `addon.py` from
   [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp), then in
   Blender → **Edit → Preferences → Add-ons → Install…** → pick the file →
   tick **Interface: Blender MCP**.
3. In the 3D viewport press **N** to open the sidebar → **BlenderMCP** tab →
   **Connect to MCP server**. Optionally tick the Poly Haven / Sketchfab /
   Hyper3D / Hunyuan3D integrations there — the matching `get_*_status` tools
   report whether each is on.
4. Verify from an agent session with `get_scene_info`.

**Do not run Blender headless (`blender -b`) for this.** The add-on's
command handler is driven off the GUI event loop; headless accepts the socket
and then never executes anything, so every call dies on the 180s timeout.
This app's container exists precisely to provide a GUI session.

## Tool surface

Core:

- **`get_scene_info`** — always start here. Objects, collections, the lot.
- **`get_object_info(object_name)`** — one object's transform, mesh stats, materials.
- **`execute_blender_code(code)`** — arbitrary `bpy`. Returns whatever the code *printed*.
- **`get_viewport_screenshot(max_size=800, inline=false, filename=…)`** —
  writes a PNG into the shared volume and returns its **path**. Pass
  `inline=true` only when you actually need to look at the render this turn;
  an inline image costs context tokens on every single call.

Asset providers (each gated on its toggle in the add-on sidebar — check with
`get_polyhaven_status` / `get_sketchfab_status` / `get_hyper3d_status` /
`get_hunyuan3d_status` before assuming a failure is a bug):

- Poly Haven — `get_polyhaven_categories`, `search_polyhaven_assets`,
  `download_polyhaven_asset`, `set_texture`
- Sketchfab — `search_sketchfab_models`, `get_sketchfab_model_preview`,
  `download_sketchfab_model`
- Hyper3D Rodin — `generate_hyper3d_model_via_text`,
  `generate_hyper3d_model_via_images`, `poll_rodin_job_status`,
  `import_generated_asset`
- Hunyuan3D — `generate_hunyuan3d_model`, `poll_hunyuan_job_status`,
  `import_generated_asset_hunyuan`

For text/image → 3D, **[[aw-tripo3d]]** is usually the cheaper option than
Hyper3D Rodin (pay-per-use vs Rodin's US$120/mo Business plan for API
access); generate there, then import the `.glb` here with
`bpy.ops.import_scene.gltf` via `execute_blender_code`.

## The three traps that have actually cost time

### 1. Every `execute_blender_code` call is a fresh exec scope

A name bound in one call does not exist in the next. Anything you need later
must be re-derived, or stashed somewhere that persists (`bpy.data`, a scene
custom property, a file). This is why a background thread started in one call
can't be shut down from a later one — do the whole start/use/stop cycle
inside a single call, or accept it lingers.

### 2. Getting files *out* of Blender's container — write them to `/config`

**Have Blender write to `/config/…`, not `/tmp`.** That directory is this
app's `$AW_APP_DATA` volume, which lives on the workspace tree — so the file
appears, same bytes, at:

```
inside Blender:   /config/aw-out/hero.fbx
from a session:   /opt/aw-workspace/.aw-workspace/data/blender/aw-out/hero.fbx
```

Anything written to `/tmp` inside the container, by contrast, is invisible
everywhere else and dies with the container. So:

```python
import os
os.makedirs('/config/aw-out', exist_ok=True)
bpy.ops.export_scene.fbx(filepath='/config/aw-out/hero.fbx', use_selection=True)
```

…then just read the workspace path. `get_viewport_screenshot` works exactly
this way, which is why it returns a path instead of image bytes.

The container writes as PUID 1000 with a normal umask, so files land
group/world-readable — an agent session reads them without any permission
dance. Writing *into* that tree from the session side is the direction that
needs care.

> Older notes describe serving files out with an ad-hoc `http.server` inside
> `execute_blender_code` and `curl`-ing `aw-app-blender:<port>`. That was the
> monolith's workaround for having no shared volume at all. It still works,
> but it is no longer necessary — prefer `/config`.

### 3. Exporting a mesh: bake world transforms first

Objects that aren't co-located at the origin export wrong unless their world
matrix is baked in. The recipe that worked (and produced a Roblox-accepted
FBX):

```python
for o in selected_meshes:
    o.data.transform(o.matrix_world)
    o.matrix_world.identity()
bpy.ops.object.join()
bpy.ops.export_scene.fbx(use_selection=True, object_types={'MESH'},
                         mesh_smooth_type='FACE', path_mode='COPY',
                         embed_textures=True)
```

Roblox-specific follow-ups: the Assets API **rejects a mesh with multiple
material slots** — run `bpy.ops.mesh.separate(type='MATERIAL')` after the
join and export the pieces in one FBX. See [[aw-roblox]] for the upload side.

### Bonus: duplicate imports are the usual cause of "two figures"

A Sketchfab/FBX import can land twice, and one import can itself carry two
sibling `Armature` branches with identical mesh parts. Diagnose by listing
`bpy.data.objects`, filtering on the import name, then walking
`.parent`/`.children` to find every `Sketchfab_model*` root and comparing
vertex counts and mesh-data names. Don't assume a `.001` suffix is the only
duplication pattern — Blender's global name namespace produces
same-named-but-different-numbered duplicates across unrelated import
sessions too.

## When something fails

1. `aw-workspace-cli apps blender` — is the container even running?
   `aw-workspace-cli start blender` if not.
2. Open the window — is Blender up, and is the sidebar's **Connect to MCP
   server** still on? It does not auto-reconnect after a Blender restart.
3. Timeout on a big operation → break it into smaller
   `execute_blender_code` steps. The socket timeout is 180s per command.
4. `aw-workspace-cli doctor` — the failure is often somewhere else entirely.

## What did NOT come across from the monolith

- The monolith ran the PyPI `blender-mcp` package inside `aw-sandbox`, which
  shared the Blender container's network namespace, so the add-on was on
  `localhost:9876`. Here the MCP is a dependency-free reimplementation in
  this app's own repo (`mcp_server/server.py`) and reaches Blender by
  container name — `BLENDER_HOST=aw-app-blender`. Same tools, same wire
  protocol.
- Upstream's telemetry (which phoned home per tool call, with prompt text
  and screenshots) is not in this port at all.
- `get_viewport_screenshot` upstream read the PNG off the local filesystem
  and so never worked across containers. Here it writes into the shared
  `/config` volume and returns the path — the caller decides whether the
  image is worth pulling into context (`inline=true`).
