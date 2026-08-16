"""Tests for the aw-blender stdio MCP server.

No Blender anywhere near these — the add-on is replaced by a fake socket
that speaks the same one-JSON-object-in / one-JSON-object-out protocol, so
what's under test is exactly the part this app owns: the framing, the
command mapping, and the error paths.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_server import server  # noqa: E402


class FakeSocket:
    """Records what was sent; replies with whatever the test queued."""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.sent: list[dict] = []
        self._pending = b""

    def settimeout(self, _t):  # noqa: D102
        pass

    def sendall(self, payload: bytes) -> None:
        self.sent.append(json.loads(payload.decode()))
        reply = self.replies.pop(0) if self.replies else {"status": "success", "result": {}}
        self._pending = json.dumps(reply).encode()

    def recv(self, _size: int) -> bytes:
        # Deliberately dribbled out in two chunks: the protocol has no length
        # prefix, so "message complete" is decided by re-parsing, and a
        # single-chunk fake would never exercise that.
        if not self._pending:
            return b""
        half = max(1, len(self._pending) // 2)
        chunk, self._pending = self._pending[:half], self._pending[half:]
        return chunk

    def close(self):  # noqa: D102
        pass


@pytest.fixture
def conn(monkeypatch):
    """A BlenderConnection wired to a FakeSocket, installed as the global one."""

    def _make(replies: list[dict]) -> FakeSocket:
        sock = FakeSocket(replies)
        c = server.BlenderConnection("fake", 9876)
        c.sock = sock
        monkeypatch.setattr(server, "get_connection", lambda: c)
        return sock

    yield _make
    server._connection = None
    server._out_dir_ready = False
    server._file_counter = 0


def _call(name: str, args: dict | None = None) -> dict:
    return server.handle_request({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    })["result"]


def test_initialize_advertises_tools():
    result = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "aw-blender"


def test_notifications_get_no_response():
    assert server.handle_request({"method": "notifications/initialized"}) is None


def test_every_advertised_tool_has_a_schema_and_is_dispatchable():
    listed = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in listed["result"]["tools"]]
    assert len(names) == len(set(names)), "duplicate tool names"
    for tool in listed["result"]["tools"]:
        assert tool["description"].strip()
        assert tool["inputSchema"]["type"] == "object"
    # The whole monolith surface, so a dropped tool fails loudly here.
    assert "execute_blender_code" in names
    assert "get_scene_info" in names
    assert len(names) == 22


def test_unknown_tool_is_an_error_not_a_crash(conn):
    conn([])
    assert _call("nope")["isError"] is True


def test_get_scene_info_round_trips(conn):
    sock = conn([{"status": "success", "result": {"objects": ["Cube"]}}])
    out = _call("get_scene_info")
    assert sock.sent[0] == {"type": "get_scene_info", "params": {}}
    assert json.loads(out["content"][0]["text"]) == {"objects": ["Cube"]}


def test_object_info_maps_object_name_to_the_addons_name_param(conn):
    sock = conn([{"status": "success", "result": {}}])
    _call("get_object_info", {"object_name": "Cube"})
    assert sock.sent[0]["params"] == {"name": "Cube"}


def test_execute_code_uses_the_addons_execute_code_command(conn):
    sock = conn([{"status": "success", "result": {"result": "42"}}])
    out = _call("execute_blender_code", {"code": "print(42)"})
    assert sock.sent[0]["type"] == "execute_code"
    assert "42" in out["content"][0]["text"]


def test_addon_error_becomes_an_error_result_not_an_exception(conn):
    conn([{"status": "error", "message": "no such object"}])
    out = _call("get_scene_info")
    assert out["isError"] is True
    assert "no such object" in out["content"][0]["text"]


def test_screenshot_returns_a_workspace_path_and_no_image_by_default(conn):
    sock = conn([
        {"status": "success", "result": {}},                    # makedirs
        {"status": "success", "result": {}},                    # capture
        {"status": "success", "result": {"result": "1234"}},    # size probe
    ])
    out = _call("get_viewport_screenshot", {"max_size": 400, "filename": "shot"})
    # The whole point of the default: no image content, so calling this costs
    # the caller nothing in context when it only wanted the file.
    assert out["content"][0]["type"] == "text"
    assert not any(c["type"] == "image" for c in out["content"])
    assert "/config/aw-out/shot.png" in out["content"][0]["text"]
    assert f"{server.WORKSPACE_DATA_DIR}/aw-out/shot.png" in out["content"][0]["text"]
    # ...and it wrote into the shared volume, not the container's own /tmp.
    capture = next(m for m in sock.sent if m["type"] == "get_viewport_screenshot")
    assert capture["params"]["filepath"] == "/config/aw-out/shot.png"
    assert capture["params"]["max_size"] == 400
    assert len(sock.sent) == 3, "must not fetch the bytes when inline is off"


def test_screenshot_inline_opt_in_also_returns_the_image(conn):
    import base64 as b64
    png = b"\x89PNG\r\n\x1a\nfake"
    sock = conn([
        {"status": "success", "result": {}},                                       # makedirs
        {"status": "success", "result": {}},                                       # capture
        {"status": "success", "result": {"result": "9"}},                          # size probe
        {"status": "success", "result": {"result": b64.b64encode(png).decode()}},  # read back
    ])
    out = _call("get_viewport_screenshot", {"inline": True})
    assert out["content"][0]["type"] == "image"
    assert b64.b64decode(out["content"][0]["data"]) == png
    assert len(sock.sent) == 4


def test_screenshot_that_never_landed_is_an_error_not_a_dangling_path(conn):
    conn([
        {"status": "success", "result": {}},                  # makedirs
        {"status": "success", "result": {}},                  # capture "succeeds"
        {"status": "success", "result": {"result": "0"}},     # ...but nothing on disk
    ])
    out = _call("get_viewport_screenshot")
    assert out["isError"] is True
    assert "GUI" in out["content"][0]["text"]


def test_screenshot_filenames_do_not_escape_the_output_dir(conn):
    sock = conn([
        {"status": "success", "result": {}},
        {"status": "success", "result": {}},
        {"status": "success", "result": {"result": "10"}},
    ])
    _call("get_viewport_screenshot", {"filename": "../../etc/passwd"})
    capture = next(m for m in sock.sent if m["type"] == "get_viewport_screenshot")
    assert capture["params"]["filepath"] == "/config/aw-out/passwd.png"


def test_the_output_dir_is_only_created_once_per_process(conn):
    sock = conn([{"status": "success", "result": {"result": "5"}}] * 10)
    _call("get_viewport_screenshot")
    _call("get_viewport_screenshot")
    makedirs = [m for m in sock.sent if m["type"] == "execute_code" and "makedirs" in m["params"]["code"]]
    assert len(makedirs) == 1


def test_successive_screenshots_do_not_clobber_each_other(conn):
    sock = conn([{"status": "success", "result": {"result": "5"}}] * 10)
    _call("get_viewport_screenshot")
    _call("get_viewport_screenshot")
    paths = [m["params"]["filepath"] for m in sock.sent if m["type"] == "get_viewport_screenshot"]
    assert len(paths) == 2 and paths[0] != paths[1]


def test_polyhaven_tools_refuse_early_when_the_integration_is_off(conn):
    sock = conn([{"status": "success", "result": {"enabled": False, "message": "PolyHaven is off"}}])
    out = _call("search_polyhaven_assets", {"asset_type": "hdris"})
    assert out["isError"] is True
    assert "off" in out["content"][0]["text"]
    assert len(sock.sent) == 1, "must not issue the search once the gate says no"


def test_rodin_text_job_returns_the_polling_handles(conn):
    conn([{
        "status": "success",
        "result": {"submit_time": "now", "uuid": "u-1", "jobs": {"subscription_key": "sk-1"}},
    }])
    out = json.loads(_call("generate_hyper3d_model_via_text", {"text_prompt": "a chair"})["content"][0]["text"])
    assert out == {"task_uuid": "u-1", "subscription_key": "sk-1"}


def test_bbox_condition_is_normalized_to_ints_relative_to_the_largest(conn):
    sock = conn([{"status": "success", "result": {}}])
    _call("generate_hyper3d_model_via_text", {"text_prompt": "x", "bbox_condition": [1, 2, 4]})
    assert sock.sent[0]["params"]["bbox_condition"] == [25, 50, 100]


def test_hyper3d_images_rejects_both_paths_and_urls(conn):
    conn([])
    out = _call("generate_hyper3d_model_via_images",
                {"input_image_paths": ["/a.png"], "input_image_urls": ["http://x/a.png"]})
    assert out["isError"] is True


def test_hyper3d_images_reports_missing_paths_instead_of_crashing(conn):
    # Upstream v1.8.0 crashed here: the URL-validation branch iterated
    # input_image_paths, which is None in URL mode. Guarded in this port.
    conn([])
    out = _call("generate_hyper3d_model_via_images", {"input_image_paths": ["/definitely/not/here.png"]})
    assert out["isError"] is True
    assert "not/here.png" in out["content"][0]["text"]


def test_hunyuan_job_id_is_prefixed(conn):
    conn([{"status": "success", "result": {"Response": {"JobId": "7"}}}])
    out = json.loads(_call("generate_hunyuan3d_model", {"text_prompt": "x"})["content"][0]["text"])
    assert out == {"job_id": "job_7"}


def test_connection_failure_is_reported_with_the_addon_hint(monkeypatch):
    server._connection = None
    c = server.BlenderConnection("nowhere.invalid", 9)
    monkeypatch.setattr(server, "get_connection", lambda: c.connect())
    out = _call("get_scene_info")
    assert out["isError"] is True
    assert "add-on" in out["content"][0]["text"]
