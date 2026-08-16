"""Stdio MCP server for the decoupled aw-app-blender app — the ``aw-blender``
upstream on this workspace's MCP gateway.

Ported from the monolith's ``mcp.json`` entry, which ran the PyPI
``blender-mcp`` package (``.venv/aw/bin/blender-mcp``, v1.8.0 at time of the
port) inside ``aw-sandbox``. Same tool surface, same wire protocol to the
BlenderMCP Blender add-on — reimplemented here on the standard library only.

**Why not just run the PyPI package.** The thing that spawns an app's stdio
MCP servers is the *gateway* container (``aw-app-mcp-gateway``): it scans
every installed app's root ``mcp.json`` and starts what it finds, with cwd
set to that app's dir under its read-only ``/opt/aw-workspace/apps`` mount.
That container is a plain ``python:3.12-slim`` and installs nothing on an
app's behalf — ``runtime.pip_requires`` (src/apps/runtime.py) installs into
the *workspace* process's environment, a different container entirely. So a
dependency on ``mcp[cli]`` + ``httpx`` would resolve in neither place. The
same reasoning produced aw-app-code-server's hand-rolled JSON-RPC loop; this
follows it.

**Wire protocol** (unchanged from upstream, this is the add-on's contract):
a plain TCP socket, one JSON object per command
(``{"type": ..., "params": {...}}``), one JSON object back
(``{"status": "success"|"error", "result"|"message": ...}``). No framing —
the reply is read until it parses as complete JSON, which is why send and
receive are held under one lock: responses are matched to commands purely by
order on the stream, so two overlapping calls hand each other's answers back
and the stream stays desynced until the timeout fires.

**Where Blender lives.** In the monolith the Blender container shared
``aw-sandbox``'s network namespace, so the add-on was on ``localhost:9876``.
Here it is a normal Tier-2 app container on the workspace's podman network,
resolvable by name — hence the ``BLENDER_HOST=aw-app-blender`` default in
this app's ``mcp.json``. Override with ``BLENDER_HOST`` / ``BLENDER_PORT``.

Run: ``python3 -m mcp_server.server`` (stdio).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import threading
from pathlib import Path

DEFAULT_HOST = os.environ.get("BLENDER_HOST", "aw-app-blender")
DEFAULT_PORT = int(os.environ.get("BLENDER_PORT", "9876"))

# Matches the add-on's own timeout. A long one is correct here: a single
# execute_code can be a multi-second bpy operation (import, bake, export).
SOCKET_TIMEOUT = 180.0

SERVER_NAME = "aw-blender"
SERVER_VERSION = "1.8.0"

# Where tools park files they produce, INSIDE the Blender container. This is
# the app's `$AW_APP_DATA` volume (aw-app.json), i.e. the same bytes the
# workspace sees under WORKSPACE_DATA_DIR — which is what makes returning a
# path instead of inline image data work at all.
CONTAINER_OUT_DIR = "/config/aw-out"

# The workspace-side path of that same volume. An agent session sees the
# workspace tree, so this is the path IT can open. Note this process (running
# in the gateway container) generally CANNOT — it doesn't mount the Blender
# app's data. That's fine: for the default path-returning mode nothing here
# ever touches the file, it only names it.
WORKSPACE_DATA_DIR = os.environ.get(
    "AW_BLENDER_DATA_DIR", "/opt/aw-workspace/.aw-workspace/data/blender"
)

_out_dir_ready = False
_file_counter = 0


class BlenderError(Exception):
    """An error the add-on reported, or a failure reaching it."""


class BlenderConnection:
    """One lazily-opened, serialized socket to the BlenderMCP add-on."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        if self.sock is not None:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(SOCKET_TIMEOUT)
            sock.connect((self.host, self.port))
        except OSError as exc:
            self.sock = None
            raise BlenderError(
                f"could not reach the BlenderMCP add-on at {self.host}:{self.port} "
                f"({exc}). Is the Blender app container running, and is the add-on's "
                f"'Connect to MCP server' button switched on in Blender's 3D-view "
                f"sidebar (N > BlenderMCP)? See this app's aw-blender skill."
            ) from exc
        self.sock = sock

    def disconnect(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _receive_full(self, sock: socket.socket) -> bytes:
        """Read until the accumulated bytes parse as one complete JSON object.

        The add-on sends no length prefix and no delimiter, so "the message is
        over" is only knowable by trying to parse it. Chunks are accumulated
        and re-parsed; a decode error means "not done yet", not "malformed".
        """
        chunks: list[bytes] = []
        sock.settimeout(SOCKET_TIMEOUT)
        while True:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                break
            except OSError as exc:
                raise BlenderError(f"connection to Blender lost: {exc}") from exc
            if not chunk:
                if not chunks:
                    raise BlenderError("connection closed before any data arrived")
                break
            chunks.append(chunk)
            data = b"".join(chunks)
            try:
                json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # incomplete — keep reading
            return data

        if not chunks:
            raise BlenderError("no data received from Blender")
        data = b"".join(chunks)
        try:
            json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BlenderError("incomplete JSON response from Blender") from exc
        return data

    def send_command(self, command_type: str, params: dict | None = None) -> dict:
        with self._lock:
            return self._send_locked(command_type, params)

    def _send_locked(self, command_type: str, params: dict | None) -> dict:
        self.connect()
        assert self.sock is not None
        payload = json.dumps({"type": command_type, "params": params or {}})
        try:
            self.sock.sendall(payload.encode("utf-8"))
            raw = self._receive_full(self.sock)
        except socket.timeout:
            # Don't reconnect here — just invalidate so the next call redials.
            self.disconnect()
            raise BlenderError(
                "timed out waiting for Blender. Try a smaller step. Note that a "
                "Blender started headless (`blender -b`) never executes these "
                "commands at all — it needs a GUI session, which is what this "
                "app's KasmVNC container provides."
            ) from None
        except OSError as exc:
            self.disconnect()
            raise BlenderError(f"connection to Blender lost: {exc}") from exc

        try:
            response = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.disconnect()
            raise BlenderError(f"invalid JSON response from Blender: {exc}") from exc

        if response.get("status") == "error":
            raise BlenderError(str(response.get("message", "unknown error from Blender")))
        return response.get("result", {}) or {}


_connection: BlenderConnection | None = None


def get_connection() -> BlenderConnection:
    """Process-wide connection, redialled once if a stale socket is detected."""
    global _connection
    if _connection is not None:
        try:
            _connection.send_command("get_polyhaven_status")
            return _connection
        except BlenderError:
            _connection.disconnect()
            _connection = None
    _connection = BlenderConnection(DEFAULT_HOST, DEFAULT_PORT)
    _connection.connect()
    return _connection


# ---- MCP plumbing -------------------------------------------------------


def _text(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _image(data: bytes, mime: str = "image/png") -> dict:
    return {
        "content": [
            {
                "type": "image",
                "data": base64.b64encode(data).decode("ascii"),
                "mimeType": mime,
            }
        ],
        "isError": False,
    }


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM_LIST = {"type": "array", "items": {"type": "number"}}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

TOOLS: list[dict] = [
    {
        "name": "get_scene_info",
        "description": "Get detailed information about the current Blender scene.",
        "inputSchema": _obj({}),
    },
    {
        "name": "get_object_info",
        "description": "Get detailed information about a specific object in the Blender scene.",
        "inputSchema": _obj(
            {"object_name": dict(_STR, description="Name of the object to inspect.")},
            ["object_name"],
        ),
    },
    {
        "name": "execute_blender_code",
        "description": (
            "Execute arbitrary Python (bpy) code inside Blender and return whatever "
            "it printed. Break work into small steps rather than one large script. "
            "Each call runs in a FRESH exec scope — a name bound in one call does not "
            "exist in the next, so anything you need later must be re-derived or "
            "stashed on bpy data."
        ),
        "inputSchema": _obj({"code": dict(_STR, description="Python code to run in Blender.")}, ["code"]),
    },
    {
        "name": "get_viewport_screenshot",
        "description": (
            "Capture the current Blender 3D viewport to a PNG on the shared "
            "workspace filesystem and return its path — no image data in the "
            "response, so it costs nothing to call when you just want the file. "
            "Set inline=true to get the image back in the response as well, for "
            "when you're actually iterating on what the camera sees. Requires a "
            "GUI Blender session (this app's container provides one)."
        ),
        "inputSchema": _obj(
            {
                "max_size": dict(_INT, description="Largest dimension in pixels. Default 800."),
                "inline": {
                    "type": "boolean",
                    "description": (
                        "Also return the image in the response. Default false — "
                        "an inline image costs context tokens on every call."
                    ),
                },
                "filename": dict(_STR, description="Optional output file name. Defaults to a unique one."),
            }
        ),
    },
    {
        "name": "get_polyhaven_status",
        "description": "Check whether the Poly Haven integration is enabled in the add-on's sidebar.",
        "inputSchema": _obj({}),
    },
    {
        "name": "get_polyhaven_categories",
        "description": "List Poly Haven categories for an asset type (hdris, textures, models, all).",
        "inputSchema": _obj({"asset_type": dict(_STR, description="hdris | textures | models | all. Default hdris.")}),
    },
    {
        "name": "search_polyhaven_assets",
        "description": "Search Poly Haven assets, optionally filtered by comma-separated categories.",
        "inputSchema": _obj(
            {
                "asset_type": dict(_STR, description="hdris | textures | models | all. Default all."),
                "categories": dict(_STR, description="Optional comma-separated category filter."),
            }
        ),
    },
    {
        "name": "download_polyhaven_asset",
        "description": "Download a Poly Haven asset and import it into the current scene.",
        "inputSchema": _obj(
            {
                "asset_id": dict(_STR, description="Poly Haven asset id."),
                "asset_type": dict(_STR, description="hdris | textures | models."),
                "resolution": dict(_STR, description="e.g. 1k, 2k, 4k. Default 1k."),
                "file_format": dict(_STR, description="Optional; sensible per-type default otherwise."),
            },
            ["asset_id", "asset_type"],
        ),
    },
    {
        "name": "set_texture",
        "description": "Apply a previously downloaded Poly Haven texture to an object.",
        "inputSchema": _obj(
            {
                "object_name": dict(_STR, description="Object to texture."),
                "texture_id": dict(_STR, description="Poly Haven texture id, already downloaded."),
            },
            ["object_name", "texture_id"],
        ),
    },
    {
        "name": "get_hyper3d_status",
        "description": "Check whether the Hyper3D Rodin integration is enabled in the add-on.",
        "inputSchema": _obj({}),
    },
    {
        "name": "get_sketchfab_status",
        "description": "Check whether the Sketchfab integration is enabled in the add-on.",
        "inputSchema": _obj({}),
    },
    {
        "name": "search_sketchfab_models",
        "description": "Search Sketchfab for downloadable models.",
        "inputSchema": _obj(
            {
                "query": dict(_STR, description="Search text."),
                "categories": dict(_STR, description="Optional comma-separated categories."),
                "count": dict(_INT, description="Max results. Default 20."),
                "downloadable": {"type": "boolean", "description": "Only downloadable models. Default true."},
            },
            ["query"],
        ),
    },
    {
        "name": "get_sketchfab_model_preview",
        "description": "Get preview/metadata for one Sketchfab model by uid.",
        "inputSchema": _obj({"uid": dict(_STR, description="Sketchfab model uid.")}, ["uid"]),
    },
    {
        "name": "download_sketchfab_model",
        "description": (
            "Download a Sketchfab model by uid and import it into the scene, "
            "normalized to a target size."
        ),
        "inputSchema": _obj(
            {
                "uid": dict(_STR, description="Sketchfab model uid."),
                "target_size": {"type": "number", "description": "Normalized size. Default 2.0."},
            },
            ["uid"],
        ),
    },
    {
        "name": "generate_hyper3d_model_via_text",
        "description": (
            "Generate a 3D asset from a text prompt via Hyper3D Rodin. Returns "
            "task_uuid/subscription_key (MAIN_SITE) or request_id (FAL_AI) to poll."
        ),
        "inputSchema": _obj(
            {
                "text_prompt": dict(_STR, description="English description of the asset."),
                "bbox_condition": dict(_NUM_LIST, description="Optional [L, W, H] ratio, 3 numbers."),
            },
            ["text_prompt"],
        ),
    },
    {
        "name": "generate_hyper3d_model_via_images",
        "description": (
            "Generate a 3D asset from images via Hyper3D Rodin. Give exactly one of "
            "input_image_paths (MAIN_SITE mode — absolute paths readable by THIS MCP "
            "process, not by Blender) or input_image_urls (FAL_AI mode)."
        ),
        "inputSchema": _obj(
            {
                "input_image_paths": dict(_STR_LIST, description="Absolute local paths."),
                "input_image_urls": dict(_STR_LIST, description="Image URLs."),
                "bbox_condition": dict(_NUM_LIST, description="Optional [L, W, H] ratio, 3 numbers."),
            }
        ),
    },
    {
        "name": "poll_rodin_job_status",
        "description": (
            "Poll a Hyper3D Rodin job. MAIN_SITE: pass subscription_key, done when every "
            "status is Done. FAL_AI: pass request_id, done when status is COMPLETED."
        ),
        "inputSchema": _obj({"subscription_key": _STR, "request_id": _STR}),
    },
    {
        "name": "import_generated_asset",
        "description": "Import a finished Hyper3D Rodin asset into the scene under `name`.",
        "inputSchema": _obj(
            {
                "name": dict(_STR, description="Name for the object in the scene."),
                "task_uuid": dict(_STR, description="MAIN_SITE mode."),
                "request_id": dict(_STR, description="FAL_AI mode."),
            },
            ["name"],
        ),
    },
    {
        "name": "get_hunyuan3d_status",
        "description": "Check whether the Hunyuan3D integration is enabled in the add-on.",
        "inputSchema": _obj({}),
    },
    {
        "name": "generate_hunyuan3d_model",
        "description": "Generate a 3D asset via Hunyuan3D from a text prompt or an image URL.",
        "inputSchema": _obj({"text_prompt": _STR, "input_image_url": _STR}),
    },
    {
        "name": "poll_hunyuan_job_status",
        "description": "Poll a Hunyuan3D job by job_id. Done when status is DONE.",
        "inputSchema": _obj({"job_id": _STR}, ["job_id"]),
    },
    {
        "name": "import_generated_asset_hunyuan",
        "description": "Import a finished Hunyuan3D asset (ZIP url) into the scene under `name`.",
        "inputSchema": _obj({"name": _STR, "zip_file_url": _STR}, ["name", "zip_file_url"]),
    },
]


# ---- tool implementations ----------------------------------------------


def _json(result: object) -> dict:
    return _text(json.dumps(result, indent=2, default=str))


def _require_polyhaven(conn: BlenderConnection) -> str | None:
    status = conn.send_command("get_polyhaven_status")
    if not status.get("enabled", False):
        return str(status.get("message", "Poly Haven integration is disabled."))
    return None


def _safe_name(filename: str | None) -> str | None:
    """Reduce a caller-supplied name to a single safe path segment."""
    if not filename:
        return None
    name = os.path.basename(str(filename)).strip()
    if not name or name in (".", ".."):
        return None
    if not name.lower().endswith(".png"):
        name += ".png"
    return name


def _ensure_out_dir(conn: BlenderConnection) -> None:
    global _out_dir_ready
    if _out_dir_ready:
        return
    conn.send_command("execute_code", {
        "code": f"import os\nos.makedirs({CONTAINER_OUT_DIR!r}, exist_ok=True)\n"
    })
    _out_dir_ready = True


def _screenshot(conn: BlenderConnection, max_size: int, inline: bool,
                filename: str | None) -> dict:
    """Capture the viewport into the shared app-data volume; return its path.

    Upstream writes the PNG to ``tempfile.gettempdir()`` and then opens that
    path locally, which only works when the MCP process and Blender share a
    filesystem — they don't (this runs in the gateway container). But the
    *agent* and Blender do: this app's ``$AW_APP_DATA`` volume is mounted at
    ``/config`` in the container and lives on the workspace tree, which every
    agent session can read. So the file is written there and the caller gets
    a path it can open on its own terms.

    Returning a path rather than inline image data is deliberate: an inline
    image costs context tokens on EVERY call, including the many where the
    caller only wanted the file. ``inline=True`` opts back in for the case
    where you're genuinely iterating on what the camera sees — that path
    still has to come back base64 over the command channel, because this
    process cannot read the volume itself.
    """
    _ensure_out_dir(conn)
    global _file_counter
    _file_counter += 1
    name = _safe_name(filename) or f"viewport-{os.getpid()}-{_file_counter}.png"
    container_path = f"{CONTAINER_OUT_DIR}/{name}"
    workspace_path = os.path.join(WORKSPACE_DATA_DIR, os.path.basename(CONTAINER_OUT_DIR), name)

    conn.send_command(
        "get_viewport_screenshot",
        {"max_size": max_size, "filepath": container_path, "format": "png"},
    )

    # Confirm it landed rather than handing back a path to nothing — the
    # add-on reports success even when there's no 3D viewport to capture.
    probe = conn.send_command("execute_code", {
        "code": (
            "import os\n"
            f"p = {container_path!r}\n"
            "print(os.path.getsize(p) if os.path.exists(p) else 0)\n"
        )
    })
    try:
        size = int(str(probe.get("result", "0")).strip() or 0)
    except ValueError:
        size = 0
    if not size:
        raise BlenderError(
            "the viewport screenshot was never written — the add-on needs a "
            "GUI session with a 3D viewport open"
        )

    if not inline:
        return _text(
            f"Viewport captured ({size} bytes).\n"
            f"Path (workspace): {workspace_path}\n"
            f"Path (inside Blender): {container_path}\n"
            "Open it with a file read if you need to look at it; pass "
            "inline=true to get the image back in the response instead."
        )

    result = conn.send_command("execute_code", {
        "code": (
            "import base64\n"
            f"print(base64.b64encode(open({container_path!r}, 'rb').read()).decode())\n"
        )
    })
    encoded = str(result.get("result", "")).strip()
    if not encoded:
        raise BlenderError(f"could not read the screenshot back from {container_path}")
    try:
        return _image(base64.b64decode(encoded))
    except (ValueError, TypeError) as exc:
        raise BlenderError(f"could not decode the screenshot: {exc}") from exc


def call_tool(name: str, args: dict) -> dict:
    conn = get_connection()

    if name == "get_scene_info":
        return _json(conn.send_command("get_scene_info"))

    if name == "get_object_info":
        return _json(conn.send_command("get_object_info", {"name": args.get("object_name", "")}))

    if name == "execute_blender_code":
        result = conn.send_command("execute_code", {"code": args.get("code", "")})
        return _text(f"Code executed successfully: {result.get('result', '')}")

    if name == "get_viewport_screenshot":
        return _screenshot(conn, int(args.get("max_size") or 800),
                           bool(args.get("inline", False)), args.get("filename"))

    if name in ("get_polyhaven_status", "get_hyper3d_status", "get_sketchfab_status",
                "get_hunyuan3d_status"):
        return _json(conn.send_command(name))

    if name == "get_polyhaven_categories":
        disabled = _require_polyhaven(conn)
        if disabled:
            return _text(disabled, is_error=True)
        return _json(conn.send_command(
            "get_polyhaven_categories", {"asset_type": args.get("asset_type") or "hdris"}))

    if name == "search_polyhaven_assets":
        disabled = _require_polyhaven(conn)
        if disabled:
            return _text(disabled, is_error=True)
        return _json(conn.send_command("search_polyhaven_assets", {
            "asset_type": args.get("asset_type") or "all",
            "categories": args.get("categories"),
        }))

    if name == "download_polyhaven_asset":
        return _json(conn.send_command("download_polyhaven_asset", {
            "asset_id": args.get("asset_id"),
            "asset_type": args.get("asset_type"),
            "resolution": args.get("resolution") or "1k",
            "file_format": args.get("file_format"),
        }))

    if name == "set_texture":
        return _json(conn.send_command("set_texture", {
            "object_name": args.get("object_name"),
            "texture_id": args.get("texture_id"),
        }))

    if name == "search_sketchfab_models":
        return _json(conn.send_command("search_sketchfab_models", {
            "query": args.get("query", ""),
            "categories": args.get("categories"),
            "count": int(args.get("count") or 20),
            "downloadable": bool(args.get("downloadable", True)),
        }))

    if name == "get_sketchfab_model_preview":
        return _json(conn.send_command("get_sketchfab_model_preview", {"uid": args.get("uid")}))

    if name == "download_sketchfab_model":
        return _json(conn.send_command("download_sketchfab_model", {
            "uid": args.get("uid"),
            "normalize_size": True,
            "target_size": float(args.get("target_size") or 2.0),
        }))

    if name == "generate_hyper3d_model_via_text":
        return _json(_rodin(conn, {
            "text_prompt": args.get("text_prompt"),
            "images": None,
            "bbox_condition": _bbox(args.get("bbox_condition")),
        }))

    if name == "generate_hyper3d_model_via_images":
        paths = args.get("input_image_paths")
        urls = args.get("input_image_urls")
        if paths and urls:
            return _text("Give only one of input_image_paths / input_image_urls.", is_error=True)
        if not paths and not urls:
            return _text("No image given.", is_error=True)
        if paths:
            missing = [p for p in paths if not os.path.exists(p)]
            if missing:
                # Worth stating plainly: these paths are resolved in the
                # gateway container this MCP runs in, not inside Blender.
                return _text(
                    "Not all image paths exist (as seen by the MCP process): "
                    + ", ".join(missing),
                    is_error=True,
                )
            images = []
            for path in paths:
                with open(path, "rb") as fh:
                    images.append((Path(path).suffix, base64.b64encode(fh.read()).decode("ascii")))
        else:
            images = list(urls)
        return _json(_rodin(conn, {
            "text_prompt": None,
            "images": images,
            "bbox_condition": _bbox(args.get("bbox_condition")),
        }))

    if name == "poll_rodin_job_status":
        kwargs: dict = {}
        if args.get("subscription_key"):
            kwargs["subscription_key"] = args["subscription_key"]
        elif args.get("request_id"):
            kwargs["request_id"] = args["request_id"]
        return _json(conn.send_command("poll_rodin_job_status", kwargs))

    if name == "import_generated_asset":
        kwargs = {"name": args.get("name")}
        if args.get("task_uuid"):
            kwargs["task_uuid"] = args["task_uuid"]
        elif args.get("request_id"):
            kwargs["request_id"] = args["request_id"]
        return _json(conn.send_command("import_generated_asset", kwargs))

    if name == "generate_hunyuan3d_model":
        result = conn.send_command("create_hunyuan_job", {
            "text_prompt": args.get("text_prompt"),
            "image": args.get("input_image_url"),
        })
        job_id = (result.get("Response") or {}).get("JobId")
        if job_id:
            return _json({"job_id": f"job_{job_id}"})
        return _json(result)

    if name == "poll_hunyuan_job_status":
        return _json(conn.send_command("poll_hunyuan_job_status", {"job_id": args.get("job_id")}))

    if name == "import_generated_asset_hunyuan":
        return _json(conn.send_command("import_generated_asset_hunyuan", {
            "name": args.get("name"),
            "zip_file_url": args.get("zip_file_url"),
        }))

    return _text(f"Unknown tool: {name}", is_error=True)


def _rodin(conn: BlenderConnection, params: dict) -> dict:
    result = conn.send_command("create_rodin_job", params)
    if result.get("submit_time"):
        return {
            "task_uuid": result.get("uuid"),
            "subscription_key": (result.get("jobs") or {}).get("subscription_key"),
        }
    return result


def _bbox(bbox: list | None) -> list[int] | None:
    """Normalize a [L, W, H] ratio to ints summing to ~100, as the add-on wants."""
    if bbox is None:
        return None
    if len(bbox) != 3:
        raise BlenderError("bbox_condition must have exactly 3 numbers ([L, W, H]).")
    largest = max(bbox)
    if largest <= 0:
        raise BlenderError("bbox_condition values must be positive.")
    return [int(float(v) / largest * 100) for v in bbox]


# ---- JSON-RPC loop ------------------------------------------------------


def handle_request(request: dict) -> dict | None:
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method.startswith("notifications/"):
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            result = call_tool(name, args)
        except BlenderError as exc:
            result = _text(f"{name} failed: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 — a tool error must not kill the server
            result = _text(f"{name} failed: {exc!r}", is_error=True)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(request)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
