"""Native OS X power navigation must never confirm unknown UI."""
import asyncio
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from retroboot import power_osx
from retroboot.qmp import QMPError
from retroboot.supervisor import Supervisor


class Masks(unittest.TestCase):
    def test_sleep_reference_variant_is_restricted_to_exact_tiger_build(self):
        rgb = bytes(640 * 480 * 3)
        for profile, version, build in (("macosx-10.4", "10.4.11", "8S165"),
                                        ("macosx-10.4", "10.4.6", "8I128"),
                                        ("macosx-10.3", "10.3.9", "7W98")):
            with self.subTest(build=build), patch.object(power_osx, "_matches", side_effect=[True, False, True]) as matches:
                self.assertEqual(power_osx.state(profile, version, 640, 480, rgb, build=build), "apple-menu")
                shared = matches.call_args_list[0].args[-1]
                self.assertEqual(shared[1][0:2], power_osx.SHARED[1][0:2])
                expected = power_osx.SHARED[1][2]
                if build == "8S165":
                    expected = (expected, "71e0136e77a064924b2e13b48421b18b8af0f3b044adf1187d56b3e3a4216e97")
                self.assertEqual(shared[1][2], expected)
                self.assertEqual(shared[2], power_osx.SHARED[2])

    def test_unrecognized_sleep_label_does_not_authorize_a_menu(self):
        rgb = bytes(640 * 480 * 3)
        with patch.object(power_osx, "_matches", return_value=False) as matches:
            self.assertIsNone(power_osx.state("macosx-10.4", "10.4.11", 640, 480, rgb, build="8S165"))
            self.assertEqual(matches.call_count, 1)

    def test_post_wake_menu_reference_is_restricted_to_exact_tiger_build(self):
        rgb = bytes(640 * 480 * 3)
        cases = (
            ("macosx-10.4", "10.4.11", "8S165", power_osx.MENU_VARIANTS[("macosx-10.4", "10.4.11", "8S165")]),
            ("macosx-10.4", "10.4.6", "8I128", power_osx.MENU),
            ("macosx-10.3", "10.3.9", "7W98", power_osx.MENU),
        )
        for profile, version, build, menu in cases:
            with self.subTest(build=build), patch.object(power_osx, "_matches", side_effect=[True, False, True]) as matches:
                self.assertEqual(power_osx.state(profile, version, 640, 480, rgb, build=build), "apple-menu")
                self.assertEqual(matches.call_args.args[-1], (menu,))
        with patch.object(power_osx, "_matches") as matches:
            self.assertIsNone(power_osx.state("macosx-10.4", "10.4.11", 640, 480, rgb, build="unknown"))
            matches.assert_not_called()

    def test_one_rounding_sensitive_pixel_keeps_the_known_complete_shape(self):
        box = (0, 0, 3, 1)
        cases = (
            # Observed native ATI label-edge colors cross their threshold by one.
            ("dark", bytes((94, 95, 96, 255, 255, 255, 255, 255, 255)), b"\1\0\0"),
            ("blue", bytes((162, 189, 233, 255, 255, 255, 255, 255, 255)), b"\0\0\0"),
            ("light", bytes((220, 221, 221, 0, 0, 0, 0, 0, 0)), b"\1\0\0"),
        )
        for kind, rgb, known_mask in cases:
            with self.subTest(kind=kind):
                digest = hashlib.sha256(known_mask).hexdigest()
                self.assertTrue(power_osx._matches(3, 1, rgb, ((box, kind, digest),)))

    def test_rounding_does_not_accept_changed_shape_or_two_pixels(self):
        box = (0, 0, 3, 1)
        digest = hashlib.sha256(b"\1\0\0").hexdigest()
        checks = ((box, "dark", digest),)
        for rgb in (bytes((94, 95, 98, 255, 255, 255, 255, 255, 255)),
                    bytes((94, 95, 96, 95, 95, 95, 255, 255, 255)),
                    bytes((255, 255, 255, 0, 0, 0, 255, 255, 255))):
            self.assertFalse(power_osx._matches(3, 1, rgb, checks))

    def test_profile_and_exact_frame_length_fail_closed(self):
        rgb = bytes(640 * 480 * 3)
        for profile, version, data in (("macosx-10.3", "10.3.9", rgb),
                                       ("macosx-10.4", "10.4.11", rgb),
                                       ("macosx-10.4", "10.4.6", rgb[:-1])):
            self.assertIsNone(power_osx.state(profile, version, 640, 480, data))
        self.assertIsNone(power_osx.state("macosx-10.4", "10.4.6", 640, 480, rgb))

    def test_mask_requires_the_whole_measured_region(self):
        rgb = bytearray(b"\xc0\xc0\xc0" * (640 * 480))
        rgb[(10 * 640 + 10) * 3:(10 * 640 + 10) * 3 + 3] = b"\x00\x00\x00"
        box = (8, 8, 14, 14)
        expected = hashlib.sha256(power_osx._mask(640, 480, rgb, box, "dark")).hexdigest()
        checks = ((box, "dark", expected),)
        self.assertTrue(power_osx._matches(640, 480, rgb, checks))
        rgb[(11 * 640 + 11) * 3] = 0
        rgb[(11 * 640 + 11) * 3 + 1:(11 * 640 + 11) * 3 + 3] = b"\x00\x00"
        self.assertFalse(power_osx._matches(640, 480, rgb, checks))


class Navigation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sup = Supervisor.__new__(Supervisor)
        self.sup.session = "native-test"
        self.sup.machine = SimpleNamespace(guest_os="macosx-10.4", meta={"os_version": "10.4.6", "os_build": "8I128"})
        self.sup.qmp = SimpleNamespace(send_key=AsyncMock(), key_event=AsyncMock(), execute=AsyncMock(side_effect=[[{"current": True, "absolute": True}], {}]))
        self.sup.log = SimpleNamespace(event=Mock())
        self.sup.restart_by_shutdown = None
        self.sup._session_alive = Mock(return_value=True)
        self.sup._osx_power_state = AsyncMock(return_value=None)
        self.sup._osx_wait_power_state = AsyncMock(side_effect=["apple-menu", "shutdown-selected",
                                                               "shutdown-confirmation"])

    async def run_ui(self, **kwargs):
        with patch("retroboot.supervisor.asyncio.sleep", new=AsyncMock()):
            return await self.sup._osx_shutdown_ui("test", **kwargs)

    def keys(self):
        return [call.args for call in self.sup.qmp.send_key.await_args_list]

    async def test_two_returns_require_selected_menu_then_confirmation(self):
        self.assertTrue(await self.run_ui(restart_by_shutdown=True))
        self.assertEqual(self.keys(), [("down",), ("end",), ("up",), ("ret",), ("ret",)])
        self.assertEqual([c.args for c in self.sup.qmp.key_event.await_args_list],
                         [("ctrl", True), ("f2", True), ("f2", False), ("ctrl", False)])
        self.assertEqual(self.sup.restart_by_shutdown["session"], "native-test")
        self.assertIsNone(self.sup.power_failure)

    async def test_relative_pointer_never_receives_automatic_navigation(self):
        self.sup.qmp.execute.side_effect = [[{"current": True, "absolute": False}]]
        self.assertFalse(await self.run_ui())
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.sup.qmp.execute.await_count, 1)

    async def test_pointer_moves_without_clicking_before_menu_navigation(self):
        self.assertTrue(await self.run_ui())
        calls = self.sup.qmp.execute.await_args_list
        self.assertEqual(calls[0].args, ("query-mice",))
        self.assertEqual(calls[1].args[0], "input-send-event")
        self.assertEqual({e["type"] for e in calls[1].args[1]["events"]}, {"abs"})

    async def test_unknown_menu_never_receives_return(self):
        self.sup._osx_wait_power_state.side_effect = [None]
        self.assertFalse(await self.run_ui(restart_by_shutdown=True))
        self.assertEqual(self.keys(), [("down",)])
        self.assertIsNone(self.sup.restart_by_shutdown)

    async def test_unrecognized_selection_never_receives_return(self):
        self.sup._osx_wait_power_state.side_effect = ["apple-menu", None]
        self.assertFalse(await self.run_ui())
        self.assertNotIn(("ret",), self.keys())

    async def test_unrecognized_alert_is_never_confirmed(self):
        self.sup._osx_wait_power_state.side_effect = ["apple-menu", "shutdown-selected", None]
        self.assertFalse(await self.run_ui())
        self.assertEqual(self.keys().count(("ret",)), 1)

    async def test_power_button_leaves_native_confirmation_to_the_user(self):
        self.assertTrue(await self.run_ui(confirm=False))
        self.assertEqual(self.keys().count(("ret",)), 1)
        self.assertIsNone(self.sup.restart_by_shutdown)

    async def test_unknown_version_has_no_input(self):
        self.sup.machine.meta["os_version"] = "10.3.9"
        self.assertFalse(await self.run_ui())
        self.assertEqual(self.keys(), [])

    async def test_panther_uses_its_own_measured_reference(self):
        self.sup.machine.guest_os = "macosx-10.3"
        self.sup.machine.meta.update(os_version="10.3.9", os_build="7W98")
        self.assertTrue(await self.run_ui())
        self.assertEqual(self.sup.log.event.call_args.kwargs["reference"], "panther-7W98-en")
        self.assertIsNone(power_osx.reference("macosx-10.3", "10.3.8"))

    async def test_final_tiger_update_has_its_own_measured_reference(self):
        self.sup.machine.meta.update(os_version="10.4.11", os_build="8S165")
        self.assertTrue(await self.run_ui())
        self.assertEqual(self.sup.log.event.call_args.kwargs["reference"], "tiger-8S165-en")

    async def test_adopted_content_does_not_wait_for_a_past_content_event(self):
        self.sup.machine.osx = True
        self.sup.content_seen = True
        self.sup.content_event = asyncio.Event()
        self.sup.paused = False
        self.sup.state = "running"
        self.assertTrue(await Supervisor.wait_ready(self.sup, None))
        self.assertFalse(self.sup.content_event.is_set())

    async def test_session_change_stops_navigation(self):
        self.sup._session_alive.side_effect = [True, False]
        self.assertFalse(await self.run_ui())
        self.assertEqual(self.keys(), [])

    async def test_failed_final_send_clears_restart_intent(self):
        self.sup._osx_power_state.return_value = "shutdown-confirmation"
        self.sup._osx_wait_power_state.side_effect = ["shutdown-confirmation"]
        self.sup.qmp.send_key.side_effect = QMPError("disconnected")
        with self.assertRaises(QMPError):
            await self.run_ui(restart_by_shutdown=True)
        self.assertIsNone(self.sup.restart_by_shutdown)

    async def test_jaguar_and_graphite_keyboard_fallback_verifies_menu_and_modal(self):
        # Baseline 640x480 desktop, menu opened box, menu selection moved, and centered modal box.
        w, h = 640, 480
        base = bytearray([210] * (w * h * 3))

        def paint(buf, x0, y0, x1, y1, r, g, b):
            out = bytearray(buf)
            for y in range(y0, y1):
                for x in range(x0, x1):
                    off = (y * w + x) * 3
                    out[off:off + 3] = bytes((r, g, b))
            return bytes(out)

        menu_open = paint(base, 12, 22, 160, 290, 245, 245, 245)
        menu_sel = paint(menu_open, 14, 244, 156, 266, 130, 140, 150)  # Graphite highlight
        modal = paint(base, 150, 150, 490, 290, 240, 240, 240)
        self.assertTrue(power_osx.detect_menu_opened(w, h, bytes(base), menu_open))
        self.assertTrue(power_osx.detect_menu_selection_moved(w, h, menu_open, menu_sel))
        self.assertTrue(power_osx.detect_confirmation_dialog(w, h, bytes(base), modal))
        self.assertFalse(power_osx.detect_menu_opened(w, h, bytes(base), bytes(base)))

        # Jaguar 10.2.8 (usb-mouse -> absolute=False) succeeds via verified visual diff fallback (#45).
        self.sup.machine = SimpleNamespace(guest_os="macosx-10.2", meta={"os_version": "10.2.8", "os_build": "6R73"})
        self.sup.qmp = SimpleNamespace(send_key=AsyncMock(), key_event=AsyncMock(),
                                       execute=AsyncMock(return_value=[{"current": True, "absolute": False}]))
        self.sup.dump_frame = AsyncMock(side_effect=[
            (w, h, bytes(base)),
            (w, h, menu_open),
            (w, h, menu_sel),
            (w, h, modal),
        ])
        self.assertTrue(await self.run_ui())
        self.assertEqual(self.keys(), [("down",), ("end",), ("up",), ("ret",), ("ret",)])
        self.assertIsNone(self.sup.power_failure)

        # Unchanged frames fail closed without pressing Return.
        self.sup.dump_frame = AsyncMock(side_effect=[(w, h, bytes(base)), (w, h, bytes(base))])
        self.sup.qmp.send_key.reset_mock()
        self.assertFalse(await self.run_ui())
        self.assertNotIn(("ret",), self.keys())

    async def test_ui_wait_requires_consecutive_positive_frames(self):
        self.sup._osx_power_state.side_effect = ["shutdown-selected", None, "shutdown-selected", "shutdown-selected"]
        with patch("retroboot.supervisor.asyncio.sleep", new=AsyncMock()):
            result = await Supervisor._osx_wait_power_state(self.sup, "native-test", {"shutdown-selected"})
        self.assertEqual(result, "shutdown-selected")
        self.assertEqual(self.sup._osx_power_state.await_count, 4)
