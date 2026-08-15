"""Manifest + mcp.json invariants.

These are the things that fail *silently* if they drift: an app whose
mcp.json points at a cwd the gateway doesn't mount registers zero tools and
says nothing, and a skill listed in `contributes.skills` that isn't in the
repo just quietly never reaches an agent.
"""

from __future__ import annotations

import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def manifest() -> dict:
    with open(os.path.join(ROOT, "aw-app.json")) as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def mcp() -> dict:
    with open(os.path.join(ROOT, "mcp.json")) as fh:
        return json.load(fh)


def test_identity(manifest):
    assert manifest["id"] == "blender"
    assert manifest["tier"] == "container"
    assert manifest["manifest_version"] == 1


def test_runtime_ports_agree_with_the_images_custom_port(manifest):
    # linuxserver's KasmVNC images serve on $CUSTOM_PORT, not a fixed 3000 —
    # if these two ever disagree the window loads a blank frame.
    runtime = manifest["runtime"]
    assert str(runtime["port"]) == runtime["env"]["CUSTOM_PORT"]


def test_config_volume_is_declared(manifest):
    targets = [v["target"] for v in manifest["runtime"]["volumes"]]
    assert "/config" in targets, "without this, Blender resets on every recreate"


def test_permissions_cover_what_the_manifest_asks_for(manifest):
    perms = set(manifest["permissions"])
    assert "containers:manage" in perms
    assert "fs:workspace-data" in perms, "$AW_APP_DATA volume needs it"


def test_window_is_namespaced_under_the_app_id(manifest):
    for window in manifest["contributes"]["windows"]:
        assert window["id"].startswith("blender.")


def test_contributed_skills_exist_on_disk(manifest):
    for skill in manifest["contributes"]["skills"]:
        assert os.path.isfile(os.path.join(ROOT, skill["path"])), skill["path"]


def test_mcp_server_name_and_cwd(mcp):
    spec = mcp["mcpServers"]["aw-blender"]
    assert spec["type"] == "stdio"
    # The gateway mounts $AW_APPS_ROOT read-only at /opt/aw-workspace/apps and
    # spawns children there; a cwd anywhere else means the module never imports.
    assert spec["cwd"] == "/opt/aw-workspace/apps/blender"


def test_mcp_points_at_the_app_container_by_name(mcp):
    env = mcp["mcpServers"]["aw-blender"]["env"]
    assert env["BLENDER_HOST"] == "aw-app-blender"
    assert env["BLENDER_PORT"] == "9876"
