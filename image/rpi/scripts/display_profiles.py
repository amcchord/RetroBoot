#!/usr/bin/env python3
"""Stage the exact display presets used by hostd onto a Raspberry Pi boot tree."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hostd"))
from retroboot.rpidisplay import DisplayError, MODELS, stage  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("stage")
    s.add_argument("--bootfs", type=Path, required=True)
    s.add_argument("--model", choices=MODELS, required=True)
    s.add_argument("--overlay-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        stage(args.bootfs, args.model, args.overlay_dir)
    except (OSError, DisplayError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
