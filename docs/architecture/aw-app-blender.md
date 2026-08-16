---
repo: architecture
path: docs/architecture/aw-app-blender.md
source: generated
edited: false
checksum: sha256:c2a5e85df14a1e269074ae55a5c86d92807bb0779690c42a59963f2af7a42838
---
# Blender

- **repo**: aw-app-blender
- **layer**: app-container
- **technologies**: docker
- **health** (derived): planned

Blender 3D in the browser (linuxserver/blender over KasmVNC), with a persistent /config so scenes, preferences and add-ons survive container recreates — plus the aw-blender MCP, which drives the running Blender from an agent session (scene inspection, arbitrary bpy execution, viewport screenshots, Poly Haven / Sketchfab / Hyper3D / Hunyuan3D asset providers). Ported as-is from the agentic-workspace monolith's aw-custom-blender docker service + aw-blender MCP.

## Connections
_none_

## MCP tools
_none exposed_

## Requirements
_none documented_
