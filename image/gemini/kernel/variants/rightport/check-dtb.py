#!/usr/bin/env python3
"""Check the rightport DTB (build-kernel.sh hook).

Usage: check-dtb.py DTB (reads the flattened tree itself)

- The right port's FUSB301A (i2c0 0x25) is bound to the RetroBoot right-port policy
  with vbus-gpios = GPIO94 and switch-gpios = GPIO72.
- No gpio-hog and no other node drives GPIO94 or GPIO72; the &pio pinctrl hog holds
  both low (GPIO mode, output-low) from pin-controller probe.
- The lane mux hogs stay (GPIO70 high, GPIO71 low: USB position), and the left port is
  unchanged: its FUSB301A on i2c1, the BQ25896 and the GPIO107 boost-gate hog.
"""
import struct
import sys

I2C0, I2C1, PIO = "i2c@11007000", "i2c@11008000", "pinctrl@10005000"
VBUS, SWITCH = 94, 72


def parse(blob):
    """FDT binary -> nested dict {"props": {name: bytes}, "nodes": {name: ...}}."""
    magic, _total, off_struct, off_strings = struct.unpack(">4I", blob[:16])
    if magic != 0xd00dfeed:
        sys.exit("not an FDT")
    strings = blob[off_strings:]
    pos = off_struct
    stack = []
    root = None
    while True:
        (tok,) = struct.unpack(">I", blob[pos:pos + 4])
        pos += 4
        if tok == 1:
            end = blob.index(b"\0", pos)
            name = blob[pos:end].decode() or "/"
            pos = (end + 4) & ~3
            node = {"props": {}, "nodes": {}}
            if stack:
                stack[-1]["nodes"][name] = node
            else:
                root = node
            stack.append(node)
        elif tok == 2:
            stack.pop()
        elif tok == 3:
            ln, nameoff = struct.unpack(">II", blob[pos:pos + 8])
            pos += 8
            pname = strings[nameoff:strings.index(b"\0", nameoff)].decode()
            stack[-1]["props"][pname] = blob[pos:pos + ln]
            pos = (pos + ln + 3) & ~3
        elif tok == 4:
            continue
        elif tok == 9:
            return root
        else:
            sys.exit(f"bad FDT token {tok}")


def u32s(b):
    return list(struct.unpack(f">{len(b) // 4}I", b)) if b and len(b) % 4 == 0 else []


def strs(b):
    return [x.decode() for x in b.rstrip(b"\0").split(b"\0")] if b else []


def walk(node, path="/"):
    yield path, node
    for name, child in node["nodes"].items():
        yield from walk(child, path.rstrip("/") + "/" + name)


def find(root, name):
    hits = [(p, n) for p, n in walk(root) if p.rsplit("/", 1)[-1] == name]
    if len(hits) != 1:
        sys.exit(f"FAIL: {len(hits)} nodes named {name}")
    return hits[0][1]


def okay(node):
    return strs(node["props"].get("status", b"okay\0")) in (["okay"], ["ok"])


def main():
    root = parse(open(sys.argv[1], "rb").read())
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    pio = find(root, PIO)
    pio_ph = u32s(pio["props"].get("phandle", b""))
    check(len(pio_ph) == 1, "pio has no phandle")
    pio_ph = pio_ph[0] if pio_ph else None

    # the right port's controller and its GPIOs
    right = find(root, I2C0)["nodes"].get("typec@25")
    check(right is not None, "no typec@25 on i2c0")
    if right:
        p = right["props"]
        check(strs(p.get("compatible")) == ["retroboot,gemini-right-port"],
              f"i2c0 typec@25 compatible {strs(p.get('compatible'))}")
        check(u32s(p.get("reg")) == [0x25], "i2c0 typec@25 reg")
        check(okay(right), "i2c0 typec@25 not okay")
        check(u32s(p.get("vbus-gpios")) == [pio_ph, VBUS, 0], f"vbus-gpios {u32s(p.get('vbus-gpios'))}")
        check(u32s(p.get("switch-gpios")) == [pio_ph, SWITCH, 0],
              f"switch-gpios {u32s(p.get('switch-gpios'))}")

    # nothing else drives GPIO94/72; the hogs left are the lane mux and the left boost gate
    hogs = {}
    for path, node in walk(root):
        p = node["props"]
        if "gpio-hog" in p:
            g = u32s(p.get("gpios"))
            lvl = "high" if "output-high" in p else "low" if "output-low" in p else "input"
            hogs[g[0] if g else None] = lvl
            check(not g or g[0] not in (VBUS, SWITCH), f"gpio-hog on GPIO{g[0]} at {path}")
        for name, val in p.items():
            if node is right or not (name.endswith("-gpios") or name.endswith("-gpio") or name == "gpios"):
                continue
            v = u32s(val)
            for i in range(0, len(v) - 1):
                if v[i] == pio_ph and v[i + 1] in (VBUS, SWITCH) and "gpio-hog" not in p:
                    check(False, f"{path} {name} uses GPIO{v[i + 1]}")
    check(hogs.get(70) == "high", f"GPIO70 lane-mux hog {hogs.get(70)}")
    check(hogs.get(71) == "low", f"GPIO71 lane-mux hog {hogs.get(71)}")
    check(hogs.get(107) == "high", f"GPIO107 (left-port boost gate) hog {hogs.get(107)}")

    # the pinctrl hog: GPIO94/72 in GPIO mode, output low
    check(strs(pio["props"].get("pinctrl-names")) == ["default"], "pio pinctrl-names")
    state = u32s(pio["props"].get("pinctrl-0"))
    check(len(state) == 1, f"pio pinctrl-0 {state}")
    low = set()
    for _path, node in walk(pio):
        if state and u32s(node["props"].get("phandle")) == state[:1]:
            for sub in node["nodes"].values():
                sp = sub["props"]
                check("output-high" not in sp, "pinctrl hog has output-high")
                if "output-low" in sp:
                    for pm in u32s(sp.get("pinmux")):
                        check(pm & 0xff == 0, f"pinmux 0x{pm:x} not GPIO mode")
                        low.add(pm >> 8)
    check(low == {VBUS, SWITCH}, f"pinctrl hog drives {sorted(low)} low, want 72 and 94")

    # the left port is unchanged
    i2c1 = find(root, I2C1)
    left = i2c1["nodes"].get("typec@25")
    check(left is not None and strs(left["props"].get("compatible")) == ["onsemi,fusb301a"]
          and okay(left), "left typec@25 on i2c1 changed")
    bq = find(root, I2C0)["nodes"].get("charger@6b")
    check(bq is not None and strs(bq["props"].get("compatible")) == ["ti,bq25896"] and okay(bq),
          "BQ25896 charger@6b on i2c0 changed")
    musb = find(root, "usb@11200000")
    check(okay(musb) and strs(musb["props"].get("dr_mode")) == ["host"], "right-port MUSB changed")

    if fails:
        for f in fails:
            print("FAIL:", f)
        sys.exit(1)
    print(f"rightport DTB: i2c0 typec@25 retroboot,gemini-right-port vbus GPIO{VBUS} "
          f"switch GPIO{SWITCH}; no hog or other user of them; pinctrl hog low; "
          f"lane mux 70 high / 71 low; left port, BQ25896, GPIO107 unchanged")


if __name__ == "__main__":
    main()
