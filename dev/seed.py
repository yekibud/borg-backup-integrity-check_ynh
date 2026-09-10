#!/usr/bin/env python3
"""Build a cloud-init NoCloud seed ISO (volume label CIDATA) with pycdlib.

usage: seed.py OUT.iso --user-data FILE --meta-data FILE [--network-config FILE]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pycdlib


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--user-data", required=True)
    ap.add_argument("--meta-data", required=True)
    ap.add_argument("--network-config")
    ns = ap.parse_args()
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="CIDATA")
    files = {"user-data": ns.user_data, "meta-data": ns.meta_data}
    if ns.network_config:
        files["network-config"] = ns.network_config
    for name, src in files.items():
        data = Path(src).read_bytes()
        iso.add_fp(
            __import__("io").BytesIO(data),
            len(data),
            f"/{name.upper().replace('-', '_')[:8]}.;1",
            rr_name=name,
            joliet_path=f"/{name}",
        )
    iso.write(ns.out)
    iso.close()
    print(ns.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
