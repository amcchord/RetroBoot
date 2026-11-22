"""Qualified native OS X graphics and explicit hardware EGL selection.

Classic graphics retain their own package, probe and default EGL selection.
An OS X result must identify the same hardware render node that QEMU will use.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

FEATURE = "macosx-ati-r100-1"
EGL_FEATURE = "ati-egl-render-node-1"
COHERENT_FEATURE = "macosx-ati-coherent-vram-1"
I8_FEATURE = "macosx-ati-i8-byte-order-1"
YUV_VIEW_FEATURE = "macosx-ati-yuv16-view-1"
YUV_RAM_FEATURE = "macosx-ati-yuv-ram-byte-order-1"
DEPTH_MEMORY_FEATURE = "macosx-ati-depth-memory-1"
SELECTIVE_2D_FEATURE = "macosx-ati-selective-2d-1"
TEXTURE_PAGE_FEATURE = "macosx-ati-texture-page-coherence-1"
RAM_SOURCE_FEATURE = "macosx-ati-ram-source-sync-1"
_host_gl: dict | None = None


def qualified(machine, package: Path) -> bool:
    from . import graphics, osx
    if machine is None or not osx.package_capable(package):
        return False
    identity = (machine.guest_os, machine.meta.get("os_build"))
    if identity not in {("macosx-10.3", "7W98"), ("macosx-10.4", "8S165")}:
        return False
    required = {FEATURE, EGL_FEATURE, COHERENT_FEATURE, I8_FEATURE} | osx.SLEEP_FEATURES
    return required.issubset(graphics.package_features(package))


def set_host_gl(info: dict | None) -> None:
    global _host_gl
    _host_gl = info


def host_gl() -> dict | None:
    return _host_gl


def node_path(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"/dev/dri/renderD[0-9]+", value) is not None


def hardware_ready(info: dict | None = None) -> bool:
    from .graphics import is_software
    info = host_gl() if info is None else info
    return bool(info and info.get("ok") is True and info.get("software") is False
                and node_path(info.get("render_node")) and isinstance(info.get("renderer"), str)
                and info["renderer"]
                and not is_software(info["renderer"])
                and "software rasterizer" not in info["renderer"].lower())


def why_not(machine, package: Path) -> str | None:
    if not qualified(machine, package):
        return "osx"
    if host_gl() is None:
        return "checking"
    return None if hardware_ready() else "host-gl"


# Same device-platform, no-config ES3 contract as the dedicated runtime loader.
# Runs in an isolated child as the eventual guest user; never loads in hostd.
PROBE = r'''
import ctypes as C, json, sys
def fail(reason):
    print(json.dumps({"ok":False,"reason":reason}));sys.exit(0)
try: egl=C.CDLL("libEGL.so.1")
except OSError: fail("no-libegl")
try: gl=C.CDLL("libGLESv2.so.2")
except OSError: fail("no-libgles")
P=C.c_void_p; I=C.c_int; U=C.c_uint
egl.eglGetProcAddress.argtypes=[C.c_char_p];egl.eglGetProcAddress.restype=P
def function(name,result,args):
    address=egl.eglGetProcAddress(name.encode())
    if not address: fail("egl-device-api")
    return C.CFUNCTYPE(result,*args)(address)
egl.eglQueryString.argtypes=[P,I];egl.eglQueryString.restype=C.c_char_p
extensions=set((egl.eglQueryString(None,0x3055) or b"").decode().split())
if "EGL_EXT_platform_device" not in extensions or not (
    "EGL_EXT_device_base" in extensions or
    {"EGL_EXT_device_enumeration","EGL_EXT_device_query"}.issubset(extensions)):
    fail("egl-device-api")
devices=function("eglQueryDevicesEXT",U,[I,P,P])
device_string=function("eglQueryDeviceStringEXT",C.c_char_p,[P,I])
display_attribute=function("eglQueryDisplayAttribEXT",U,[P,I,P])
get_display=function("eglGetPlatformDisplayEXT",P,[U,P,P])
count=I()
if not devices(0,None,C.byref(count)) or not 0<count.value<=64:fail("egl-device-count")
capacity=count.value;listed=(P*capacity)()
if not devices(capacity,listed,C.byref(count)) or not 0<count.value<=capacity:fail("egl-device-count")
selected=[]
for device in listed[:count.value]:
    ext=set((device_string(device,0x3055) or b"").decode().split())
    if "EGL_EXT_device_drm_render_node" not in ext:continue
    node=device_string(device,0x3377)
    if node and node.decode()==sys.argv[1]:
        if "EGL_MESA_device_software" in ext:fail("software-only")
        selected.append(device)
if len(selected)!=1:fail("egl-device-match")
dpy=get_display(0x313F,selected[0],None)
egl.eglInitialize.argtypes=[P,P,P];egl.eglInitialize.restype=U
if not dpy or not egl.eglInitialize(dpy,None,None):fail("egl-init")
actual=C.c_ssize_t()
if not display_attribute(dpy,0x322C,C.byref(actual)) or actual.value!=selected[0]:fail("egl-device-identity")
ext=set((egl.eglQueryString(dpy,0x3055) or b"").decode().split())
if not {"EGL_KHR_surfaceless_context","EGL_KHR_no_config_context"}.issubset(ext):fail("gles-context")
egl.eglBindAPI.argtypes=[U];egl.eglBindAPI.restype=U
if not egl.eglBindAPI(0x30A0):fail("gles-context")
attrs=(I*5)(0x3098,3,0x30FB,0,0x3038)
egl.eglCreateContext.argtypes=[P,P,P,P];egl.eglCreateContext.restype=P
ctx=egl.eglCreateContext(dpy,None,None,attrs)
egl.eglMakeCurrent.argtypes=[P,P,P,P];egl.eglMakeCurrent.restype=U
if not ctx or not egl.eglMakeCurrent(dpy,None,None,ctx):fail("gles-context")
gl.glGetString.argtypes=[U];gl.glGetString.restype=C.c_char_p
r=gl.glGetString(0x1F01);v=gl.glGetString(0x1F02)
if not r or not v:fail("gles-context")
r=r.decode();v=v.decode()
if any(x in r.lower() for x in ("llvmpipe","softpipe","swrast","lavapipe","software rasterizer")):fail("software-only")
print(json.dumps({"ok":True,"render_node":sys.argv[1],"renderer":r,"version":v,"software":False}))
'''


def probe_host_gl(run_as: str | None = None, dev: str = "/dev/dri", timeout: float = 5) -> dict:
    from .graphics import GUEST_GROUPS, render_nodes
    nodes = [node for node in render_nodes(dev) if node_path(node)]
    info = {"ok": False, "reason": "no-render-node", "render_nodes": nodes,
            "checked_at": time.time(), "attempts": []}
    identity = {}
    if run_as and os.geteuid() == 0:
        import grp
        import pwd
        try:
            user = pwd.getpwnam(run_as)
            groups = []
            for name in GUEST_GROUPS:
                try:
                    groups.append(grp.getgrnam(name).gr_gid)
                except KeyError:
                    pass
            identity = {"user": user.pw_uid, "group": user.pw_gid, "extra_groups": groups}
        except KeyError:
            return {**info, "reason": "guest-user"}
    deadline = time.monotonic() + timeout
    for node in nodes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            info["reason"] = "probe-timeout"
            break
        try:
            result = subprocess.run([sys.executable, "-I", "-c", PROBE, node],
                                    capture_output=True, text=True, timeout=min(5, remaining),
                                    # Match launcher's clean environment: parent loader
                                    # overrides and software-rendering policies are omitted.
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, **identity)
            got = json.loads(result.stdout.strip().splitlines()[-1])
            if result.returncode or not isinstance(got, dict):
                raise ValueError("probe failed")
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            got = {"ok": False, "reason": "probe-failed"}
        info["attempts"].append({"node": node, "reason": got.get("reason")})
        if hardware_ready(got) and got["render_node"] == node:
            return {**info, **got, "reason": None}
        info["reason"] = got.get("reason") or "egl-device-identity"
    return info
