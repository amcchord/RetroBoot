"""Speakers or headphones only (Gemini PDA), through the image's own hook.

The Gemini's speakers are driven from the MT6351 codec's headphone outputs through
two amplifiers on SoC GPIOs 243 and 244; the 3.5 mm jack is wired to the same
outputs and there is no jack detection.  The Gemini image runs
``rb-gemini-audio watch`` (retroboot-gemini-audio.service): it sets the playback
route at boot and switches the amplifiers on only while a playback stream runs
and the saved output is "speaker" (with no stream the lines float and hiss), and
``rb-gemini-audio speaker|headphone`` saves the output and applies it at once
(``/var/lib/retroboot/gemini/audio-output``).  hostd does not drive the GPIOs or
the route itself; it only offers the choice (the classic panel's Display & Sound
window) and reports it.  hostd.json ``gemini_audio_cmd`` names the hook (empty:
the board profile's; "none": no choice offered).  Nothing happens on other
boards, or when the hook is not installed.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .util import run as run_cmd

if TYPE_CHECKING:
    from .hostd import Hostd

log = logging.getLogger("retroboot.speakers")

OUTPUTS = ("speaker", "headphones")
HOOK_WORDS = {"speaker": "speaker", "headphones": "headphone"}


class SpeakerSwitch:
    def __init__(self, app: "Hostd", runner=None):
        self.app = app
        self.run = runner or run_cmd
        cfg = app.cfg
        prof = app.platform.profile
        hook = str(getattr(cfg, "gemini_audio_cmd", "") or prof.get("audio_hook", "") or "")
        self.hook = None if hook.lower() in ("", "none") else hook
        # host tools are off in tests unless a runner is given
        tools = runner is not None or getattr(cfg, "allow_network_tools", True)
        present = bool(self.hook) and (runner is not None or os.access(self.hook, os.X_OK))
        self.enabled = bool(prof.get("audio_hook")) and tools and present
        self.state_path = app.paths.data / prof.get("audio_output_state", "gemini/audio-output")
        self.errors: list[str] = []

    @property
    def output(self) -> str:
        """The saved output (the hook's state file): "speaker" unless it says headphone."""
        try:
            word = self.state_path.read_text().strip()
        except OSError:
            word = ""
        return "headphones" if word == "headphone" else "speaker"

    async def set_output(self, output: str, origin: str = "panel") -> bool:
        if not self.enabled or output not in OUTPUTS:
            return False
        rc, _, err = await self.run([self.hook, HOOK_WORDS[output]], timeout=10)
        ok = rc == 0
        if not ok:
            msg = (err or "").strip()[:160] or f"exit {rc}"
            self.errors = (self.errors + [msg])[-4:]
            log.warning("speaker output switch: %s", msg)
        self.app.lifecycle.event("audio_output_switch", output=output, origin=origin, ok=ok)
        return ok

    def public(self) -> dict | None:
        if not self.enabled:
            return None
        out = {"output": self.output, "hook": self.hook}
        if self.errors:
            out["errors"] = list(self.errors)
        return out
