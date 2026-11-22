import argparse
import json
import sys

from . import hfsplus, prep


def main() -> int:
    if sys.argv[1:2] == ["usb-overdrive"]:
        from . import usboverdrive
        return usboverdrive.main(sys.argv[2:])
    if sys.argv[1:2] == ["ati"]:
        from . import ati
        return ati.main(sys.argv[2:])
    if sys.argv[1:2] == ["airport"]:
        from . import airport
        return airport.main(sys.argv[2:])
    if sys.argv[1:2] == ["68k"]:
        from . import prep68k
        return prep68k.main(sys.argv[2:])
    if sys.argv[1:2] == ["control-strip"]:
        from . import controlstrip
        return controlstrip.main(sys.argv[2:])
    ap = argparse.ArgumentParser(prog="rbguestprep", description="Prepare a stopped Mac OS 9 disk image for RetroBoot.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply", help="apply guest preparation (root; guest must be stopped)")
    a.add_argument("image")
    a.add_argument("--no-dhcp", action="store_true")
    a.add_argument("--keep-shutdown-warning", action="store_true")
    a.add_argument("--keep-guest-sleep", action="store_true")
    a.add_argument("--keep-software-update", action="store_true")
    a.add_argument("--keep-appletalk", action="store_true", help="leave AppleTalk's configuration alone")
    a.add_argument("--keep-base-station", action="store_true",
                   help="leave AirPort's Software Base Station (AirPort AP) where it is")
    a.add_argument("--network-port", choices=prep.NETWORK_PORTS, default="ethernet",
                   help="TCP/IP adapter for RetroBoot's DHCP configuration (default ethernet)")
    i = sub.add_parser("inspect", help="read-only report")
    i.add_argument("image")
    f = sub.add_parser("repair", help="fsck.hfsplus -fy the guest volume (root; guest stopped)")
    f.add_argument("image")
    c = sub.add_parser("cities", help="Date & Time's city list (read-only)")
    c.add_argument("image")
    args = ap.parse_args()
    if args.cmd == "cities":
        from . import macloc
        report = macloc.read_cities(args.image)
    elif args.cmd == "apply":
        report = prep.apply(args.image, dhcp=not args.no_dhcp, shutdown_warning_off=not args.keep_shutdown_warning,
                            sleep_never=not args.keep_guest_sleep,
                            software_update_off=not args.keep_software_update, network_port=args.network_port,
                            appletalk_off=not args.keep_appletalk,
                            base_station_follows_port=not args.keep_base_station)
    elif args.cmd == "repair":
        ok, out = prep.fsck(args.image, repair=True)
        report = {"ok": ok, "output": out.splitlines()[-3:]}
    else:
        with hfsplus.Volume(args.image) as vol:
            sysf = prep._system_folder(vol)
            report = {"volume_offset": vol.offset, "block_size": vol.block_size, "clean": vol.clean,
                      "attributes": hex(vol.attributes), "system_folder": sysf}
        _prefs, report["tcpip"] = prep._tcpip_state(args.image)
        report["airport_software"] = "present" if prep.airport_software_present(args.image) else "absent"
        report["ati_software"] = "present" if prep.ati_software_present(args.image) else "absent"
        from . import appletalk
        _prefs, report["appletalk"] = appletalk.state(args.image)
        from . import basestation
        report["airport_base_station"] = basestation.state(args.image)
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
