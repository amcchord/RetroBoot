#!/usr/bin/env python3
"""A small fake ``nmcli`` for the AirPort NetworkManager backend tests.

State lives in the JSON file named by ``FAKE_NMCLI_STATE``::

    {"device": "wlan0", "radio": true,
     "aps": [{"ssid": "Home", "bssid": "aa:bb:cc:00:00:01", "chan": 6, "signal": 80,
              "security": "WPA2", "password": "secret123"}],
     "profiles": [{"name": "Home", "uuid": "...", "ssid": "Home", "psk": "...", "key_mgmt": "wpa-psk"}],
     "active": null,            # or {"uuid": ..., "bssid": ...}
     "hardware": {"vendor": "...", "product": "...", "driver": "..."}}   # optional

Only the invocations the backend and NetworkAdapter use are implemented;
anything else exits 2 so a test notices.  Every call is appended to
``calls`` (secrets are never in argv, so recording argv is safe).
"""
import json
import fcntl
import os
import sys
import uuid as uuidlib


def esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace(":", "\\:")


def load():
    path = os.environ["FAKE_NMCLI_STATE"]
    with open(path) as f:
        return path, json.load(f)


def save(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def keyfile(path):
    sec, out = None, {}
    for line in open(path).read().splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            sec = line[1:-1]
        elif "=" in line:
            k, v = line.split("=", 1)
            out[f"{sec}.{k}"] = v.replace("\\n", "\n").replace("\\\\", "\\")
    return out


def _run(argv):
    path, st = load()
    st.setdefault("calls", []).append(argv)
    dev = st.get("device")
    a = [x for x in argv if x not in ("-t",)]
    rc, out, err = 0, "", ""
    if a[:3] == ["-f", "DEVICE,TYPE,STATE", "device"]:
        if dev:
            out = f"{dev}:wifi:{'connected' if st.get('active') else 'disconnected'}\nlo:loopback:unmanaged\n"
    elif a[:2] == ["radio", "wifi"] and len(a) == 2:
        out = "enabled\n" if st.get("radio", True) else "disabled\n"
    elif a[:2] == ["radio", "wifi"]:
        st["radio"] = a[2] == "on"
        if not st["radio"]:
            st["active"] = None
    elif a[:4] == ["-f", "GENERAL.STATE", "device", "show"]:
        out = "GENERAL.STATE:100 (connected)\n" if st.get("active") else "GENERAL.STATE:30 (disconnected)\n"
    elif a[:4] == ["-f", "GENERAL.STATE,GENERAL.CONNECTION", "device", "show"]:
        act = st.get("active") or {}
        name = next((p["name"] for p in st.get("profiles", []) if p["uuid"] == act.get("uuid")), "--")
        out = ("GENERAL.STATE:100 (connected)\n" if act else "GENERAL.STATE:30 (disconnected)\n") + \
            f"GENERAL.CONNECTION:{name}\n"
    elif a[:4] == ["-f", "GENERAL.VENDOR,GENERAL.PRODUCT,GENERAL.DRIVER", "device", "show"]:
        hw = st.get("hardware") or {}
        out = (f"GENERAL.VENDOR:{hw.get('vendor', '--')}\nGENERAL.PRODUCT:{hw.get('product', '--')}\n"
               f"GENERAL.DRIVER:{hw.get('driver', '--')}\n")
    elif a[:3] == ["device", "wifi", "rescan"]:
        pass
    elif a[:5] == ["-f", "IN-USE,SSID,BSSID,SIGNAL,SECURITY,CHAN", "device", "wifi", "list"]:
        if st.get("radio", True):
            act = st.get("active") or {}
            for ap in st.get("aps", []):
                use = "*" if act.get("bssid") == ap["bssid"] else " "
                out += ":".join([use, esc(ap["ssid"]), esc(ap["bssid"].upper()), str(ap["signal"]),
                                 ap.get("security", "") or "--", str(ap["chan"])]) + "\n"
    elif a[:5] == ["-f", "IN-USE,SSID-HEX,BSSID,CHAN,SIGNAL,SECURITY", "device", "wifi", "list"]:
        if st.get("radio", True):
            act = st.get("active") or {}
            for ap in st.get("aps", []):
                use = "*" if act.get("bssid") == ap["bssid"] else " "
                out += ":".join([use, ap["ssid"].encode().hex().upper(), esc(ap["bssid"].upper()), str(ap["chan"]),
                                 str(ap["signal"]), ap.get("security", "") or "--"]) + "\n"
    elif a[:4] == ["-f", "NAME,UUID,TYPE,TIMESTAMP", "connection", "show"]:
        for p in st.get("profiles", []):
            out += f"{esc(p['name'])}:{p['uuid']}:802-11-wireless:{p.get('timestamp', 0)}\n"
        out += "Wired connection 1:00000000-0000-0000-0000-000000000001:802-3-ethernet:1791000000\n"
    elif a[:4] == ["-f", "NAME,UUID,TYPE", "connection", "show"]:
        for p in st.get("profiles", []):
            out += f"{esc(p['name'])}:{p['uuid']}:802-11-wireless\n"
        out += "Wired connection 1:00000000-0000-0000-0000-000000000001:802-3-ethernet\n"
    elif a[:2] == ["-g", "802-11-wireless.ssid,802-11-wireless-security.key-mgmt"] and a[2:5] == ["connection", "show", "uuid"]:
        p = next((p for p in st.get("profiles", []) if p["uuid"] == a[5]), None)
        if p is None:
            rc, err = 10, "Error: no such connection profile.\n"
        else:
            out = f"{p['ssid']}\n{p.get('key_mgmt', '')}\n"
    elif a[:2] == ["connection", "load"]:
        kf = keyfile(a[2])
        mode = os.stat(a[2]).st_mode & 0o777
        st.setdefault("profiles", []).append({"name": kf["connection.id"], "uuid": kf["connection.uuid"],
                                              "ssid": kf["wifi.ssid"], "psk": kf.get("wifi-security.psk"),
                                              "key_mgmt": kf.get("wifi-security.key-mgmt", ""),
                                              "hidden": kf.get("wifi.hidden") == "true", "file_mode": mode})
    elif a[:1] == ["--wait"] and a[2:4] == ["connection", "up"] and a[4] == "uuid":
        p = next((p for p in st.get("profiles", []) if p["uuid"] == a[5]), None)
        aps = [ap for ap in st.get("aps", []) if p and ap["ssid"] == p["ssid"]]
        if p is None:
            rc, err = 10, "Error: unknown connection.\n"
        elif not st.get("radio", True):
            rc, err = 4, "Error: Connection activation failed: radio is disabled.\n"
        elif not aps:
            rc, err = 4, f"Error: Connection activation failed: No network with SSID '{p['ssid']}' found.\n"
        else:
            ap = max(aps, key=lambda x: x["signal"])
            if ap.get("password") and ap["password"] != p.get("psk") and ap.get("sae_reject"):
                # WPA2/WPA3 transition networks: SAE rejection surfaces as ssid-not-found
                rc, err = 4, ("Error: Connection activation failed: The Wi-Fi network could not be found.\n")
            elif ap.get("password") and ap["password"] != p.get("psk"):
                rc, err = 4, ("Error: Connection activation failed: Secrets were required, but not provided.\n")
            else:
                st["active"] = {"uuid": p["uuid"], "bssid": ap["bssid"]}
    elif a[:3] == ["connection", "delete", "uuid"]:
        st["profiles"] = [p for p in st.get("profiles", []) if p["uuid"] != a[3]]
        if (st.get("active") or {}).get("uuid") == a[3]:
            st["active"] = None
    else:
        rc, err = 2, f"fake nmcli: unsupported {argv}\n"
    save(path, st)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


def main(argv):
    # Adapter polling and explicit user actions invoke separate processes.
    # Serialize the complete read/modify/replace transaction, including .tmp.
    with open(os.environ["FAKE_NMCLI_STATE"] + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _run(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
