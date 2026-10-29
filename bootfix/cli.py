"""CLI: bootfix diagnose (en el live USB, como root) | bootfix analyze (offline)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import analyze as an
from . import plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bootfix")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diagnose", help="lee el disco en modo solo-lectura y propone un plan")
    d.add_argument("--disk", required=True, help="disco completo, p. ej. /dev/nvme0n1")
    d.add_argument("--firmware", choices=["uefi", "bios"], help="por defecto se detecta en el live")
    d.add_argument("--efibootmgr-file", help="salida guardada de efibootmgr -v (por defecto se lee la NVRAM)")
    d.add_argument("--snapshot-out", help="guarda la instantánea JSON")
    d.add_argument("--json", action="store_true")
    a2 = sub.add_parser("analyze", help="analiza una instantánea JSON ya guardada (sin root)")
    a2.add_argument("snapshot")
    a2.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "diagnose":
        if os.geteuid() != 0:
            print("bootfix diagnose necesita root para leer el disco (solo lectura).", file=sys.stderr)
            return 3
        from .collect import Collector
        snap = Collector(a.disk, a.firmware, a.efibootmgr_file).collect()
        if a.snapshot_out:
            with open(a.snapshot_out, "w") as fh:
                json.dump(snap, fh, indent=1)
    else:
        with open(a.snapshot) as fh:
            snap = json.load(fh)
    causes = an.analyze(snap)
    if a.json:
        print(json.dumps({"disk": snap.get("disk"), "causes": an.to_dict(causes)},
                         ensure_ascii=False, indent=2))
    else:
        print(plan.render(causes, snap))
    return 1 if any(c.severity == "error" for c in causes) else 0


if __name__ == "__main__":
    sys.exit(main())
