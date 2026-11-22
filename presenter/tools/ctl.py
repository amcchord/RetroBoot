#!/usr/bin/env python3
"""Tiny client for the presenter control socket (ARCHITECTURE.md section 4).

  ctl.py status
  ctl.py set_scene scene=panel background=checkerboard
  ctl.py ui_show doc=@/path/doc.json
  ctl.py display_config scaling=fit 'guest_size=[1024,768]'
  ctl.py screenshot path=/tmp/shot.png what=composed
  ctl.py --listen hello                 # then keep printing events
  ctl.py --wait-event guest_revealed --timeout 120 hello

Values are parsed as JSON when possible, else used as strings; @FILE loads a
JSON file. Several commands can be chained with ' -- '. Note: connecting
replaces any existing hostd connection (only one client is allowed).
"""
import argparse
import json
import socket
import sys
import time


def parse_value(v):
    if v.startswith('@'):
        with open(v[1:]) as f:
            return json.load(f)
    try:
        return json.loads(v)
    except ValueError:
        return v


def build(words, ident):
    if len(words) == 1 and words[0].startswith('{'):
        req = json.loads(words[0])
    else:
        req = {'cmd': words[0]}
        for w in words[1:]:
            k, _, v = w.partition('=')
            req[k] = parse_value(v)
    req.setdefault('id', ident)
    return req


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--socket', default='/run/retroboot/presenter.sock')
    ap.add_argument('--listen', action='store_true', help='keep printing events after the replies')
    ap.add_argument('--wait-event', help='exit 0 when this event arrives')
    ap.add_argument('--timeout', type=float, default=10.0)
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('words', nargs='+')
    a = ap.parse_args()
    groups, cur = [], []
    for w in a.words:
        if w == '--':
            if cur:
                groups.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        groups.append(cur)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(a.socket)
    pending = set()
    for i, g in enumerate(groups, 1):
        req = build(g, i)
        pending.add(req['id'])
        s.sendall((json.dumps(req) + '\n').encode())
    buf = b''
    deadline = time.monotonic() + a.timeout
    rc = 0
    while True:
        if not pending and not a.listen and not a.wait_event:
            break
        left = deadline - time.monotonic()
        if left <= 0 and not a.listen:
            print('timeout', file=sys.stderr)
            return 3
        s.settimeout(None if a.listen and not a.wait_event else max(0.05, left))
        try:
            chunk = s.recv(1 << 20)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            msg = json.loads(line)
            if not a.quiet:
                print(json.dumps(msg), flush=True)
            if 'id' in msg and msg['id'] in pending:
                pending.discard(msg['id'])
                if not msg.get('ok'):
                    rc = 1
            if a.wait_event and msg.get('event') == a.wait_event:
                return rc
    return rc


if __name__ == '__main__':
    sys.exit(main())
