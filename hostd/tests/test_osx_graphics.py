"""A hardware result must identify the exact device used by native OS X."""
import json
from types import SimpleNamespace
from unittest import mock

import helpers
from retroboot import graphics, osx_graphics


def test_probe_skips_software_and_selects_the_matching_hardware_node():
    nodes = ["/dev/dri/renderD128", "/dev/dri/renderD129"]
    results = [SimpleNamespace(returncode=0, stdout=json.dumps({"ok": False, "reason": "software-only"})),
               SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "render_node": nodes[1],
                               "renderer": "virgl AMD gfx1150", "version": "OpenGL ES 3.2", "software": False}))]
    with mock.patch.object(graphics, "render_nodes", return_value=nodes), \
            mock.patch.object(osx_graphics.subprocess, "run", side_effect=results) as run:
        result = osx_graphics.probe_host_gl()
    assert osx_graphics.hardware_ready(result)
    assert result["render_node"] == nodes[1]
    assert [call.args[0][-1] for call in run.call_args_list] == nodes
    # Launcher and preflight both drop parent graphics-loader overrides.
    assert all(call.kwargs["env"] == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
               for call in run.call_args_list)


def test_probe_rejects_a_success_for_another_node():
    got = {"ok": True, "render_node": "/dev/dri/renderD129", "renderer": "AMD",
           "version": "OpenGL ES 3.2", "software": False}
    with mock.patch.object(graphics, "render_nodes", return_value=["/dev/dri/renderD128"]), \
            mock.patch.object(osx_graphics.subprocess, "run",
                              return_value=SimpleNamespace(returncode=0, stdout=json.dumps(got))):
        result = osx_graphics.probe_host_gl()
    assert not result["ok"] and result["reason"] == "egl-device-identity"


def test_probe_deadline_bounds_all_devices_together():
    with mock.patch.object(graphics, "render_nodes", return_value=["/dev/dri/renderD128"]), \
            mock.patch.object(osx_graphics.time, "monotonic", side_effect=[10, 16]), \
            mock.patch.object(osx_graphics.subprocess, "run") as run:
        result = osx_graphics.probe_host_gl(timeout=5)
    assert result["reason"] == "probe-timeout"
    run.assert_not_called()


def test_hardware_requires_explicit_nonsoftware_identity():
    baseline = {"ok": True, "render_node": "/dev/dri/renderD128", "renderer": "AMD", "software": False}
    assert osx_graphics.hardware_ready(baseline)
    for change in ({"software": True}, {"software": None}, {"renderer": "Software Rasterizer"},
                   {"renderer": "llvmpipe"}, {"render_node": "../../renderD128"}, {"render_node": None}):
        assert not osx_graphics.hardware_ready({**baseline, **change})
