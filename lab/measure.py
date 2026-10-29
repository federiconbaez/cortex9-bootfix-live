"""Medición pre-registrada (lab/prereg.json). Corre como root. Sale != 0 si no se cumple un umbral.

Uso: sudo python3 -m lab.measure IMGDIR [--out results.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sectors_written(dev: str) -> dict[str, int]:
    """Campo 7 de /sys/block/<dev>/stat (sectores escritos) para el disco y sus particiones."""
    name = os.path.basename(dev)
    base = f"/sys/block/{name}"
    out = {}
    for d in [base] + [os.path.join(base, x) for x in sorted(os.listdir(base)) if x.startswith(name + "p")]:
        with open(os.path.join(d, "stat")) as fh:
            out[os.path.basename(d)] = int(fh.read().split()[6])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("imgdir")
    ap.add_argument("--out")
    a = ap.parse_args()
    if os.geteuid() != 0:
        print("FALLA: la medición necesita root (loop devices)")
        return 2
    with open(os.path.join(HERE, "prereg.json"), "rb") as fh:
        raw = fh.read()
    prereg = json.loads(raw)
    rows = []
    for sc in prereg["scenarios"]:
        img = os.path.join(a.imgdir, sc["name"] + ".img")
        nv = os.path.join(a.imgdir, sc["name"] + ".nvram")
        before = sha256(img)
        dev = subprocess.run(["losetup", "-fP", "--show", img], capture_output=True, text=True,
                             check=True).stdout.strip()
        subprocess.run(["udevadm", "settle"], check=False)
        time.sleep(0.5)
        w0 = sectors_written(dev)
        cmd = [sys.executable, "-m", "bootfix", "diagnose", "--disk", dev, "--firmware", sc["firmware"],
               "--json", "--snapshot-out", os.path.join(a.imgdir, sc["name"] + ".snapshot.json")]
        if os.path.exists(nv):
            cmd += ["--efibootmgr-file", nv]
        t0 = time.monotonic()
        cp = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        ms = (time.monotonic() - t0) * 1000
        subprocess.run(["sync"], check=False)
        w1 = sectors_written(dev)
        subprocess.run(["losetup", "-d", dev], check=True)
        after = sha256(img)
        try:
            causes = json.loads(cp.stdout)["causes"]
        except (json.JSONDecodeError, KeyError):
            print(cp.stdout[-2000:], cp.stderr[-2000:])
            causes = None
        errs = [c["code"] for c in (causes or []) if c["severity"] == "error"]
        top = errs[0] if errs else "none"
        correct = causes is not None and top == sc["expect"]
        delta = {k: w1.get(k, 0) - w0.get(k, 0) for k in w1}
        row = {"name": sc["name"], "expect": sc["expect"], "top": top, "errors": errs,
               "correct": correct, "sha256_before": before, "sha256_after": after,
               "sha256_identical": before == after, "sectors_written": sum(delta.values()),
               "sectors_written_by_dev": delta, "ms": round(ms, 1), "rc": cp.returncode}
        rows.append(row)
        print(f"{sc['name']:26s} esperado={sc['expect']:24s} top={top:24s} "
              f"{'OK' if correct else 'NO'} sha256={'igual' if row['sha256_identical'] else 'DISTINTO'} "
              f"sectores_escritos={row['sectors_written']} {row['ms']} ms")
    # control positivo del instrumento: un montaje rw de verdad TIENE que verse en sha256 y en
    # /sys/block/*/stat; si no, "0 bytes escritos" no probaría nada.
    import shutil
    import tempfile
    src = os.path.join(a.imgdir, prereg["scenarios"][0]["name"] + ".img")
    ctl_img = os.path.join(a.imgdir, "instrument-control.img")
    shutil.copyfile(src, ctl_img)
    before = sha256(ctl_img)
    dev = subprocess.run(["losetup", "-fP", "--show", ctl_img], capture_output=True, text=True,
                         check=True).stdout.strip()
    subprocess.run(["udevadm", "settle"], check=False)
    time.sleep(0.5)
    w0 = sectors_written(dev)
    mnt = tempfile.mkdtemp(prefix="ctl-", dir=a.imgdir)
    subprocess.run(["mount", f"{dev}p2", mnt], check=True)
    with open(os.path.join(mnt, "escritura-de-control"), "w") as fh:
        fh.write("x\n")
    subprocess.run(["umount", mnt], check=True)
    subprocess.run(["sync"], check=False)
    w1 = sectors_written(dev)
    subprocess.run(["losetup", "-d", dev], check=True)
    after = sha256(ctl_img)
    os.rmdir(mnt)
    os.remove(ctl_img)
    ctl_sectors = sum(w1.get(k, 0) - w0.get(k, 0) for k in w1)
    instrument = {"sha256_changed": before != after, "sectors_written": ctl_sectors}
    print(f"control del instrumento (mount rw + 1 archivo): sha256 {'cambió' if before != after else 'IGUAL'}, "
          f"sectores escritos {ctl_sectors}")

    broken = [r for r in rows if r["expect"] != "none"]
    ctl = [r for r in rows if r["expect"] == "none"]
    summary = {
        "diagnosis_correct_broken": f"{sum(r['correct'] for r in broken)}/{len(broken)}",
        "controls_without_error": f"{sum(r['correct'] for r in ctl)}/{len(ctl)}",
        "sha256_identical": f"{sum(r['sha256_identical'] for r in rows)}/{len(rows)}",
        "sectors_written_total": sum(r["sectors_written"] for r in rows),
        "instrument_control": instrument,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if a.out:
        with open(a.out, "w") as fh:
            json.dump({"prereg_sha256": hashlib.sha256(raw).hexdigest(), "summary": summary,
                       "rows": rows, "kernel": os.uname().release}, fh, ensure_ascii=False, indent=2)
    fails = []
    if sum(r["correct"] for r in broken) < 9:
        fails.append("diagnosis_correct_broken")
    if sum(r["correct"] for r in ctl) != len(ctl):
        fails.append("controls_without_error")
    if not all(r["sha256_identical"] for r in rows):
        fails.append("sha256_identical")
    if summary["sectors_written_total"] != 0:
        fails.append("sectors_written")
    if not (instrument["sha256_changed"] and instrument["sectors_written"] > 0):
        fails.append("instrument_control (medición de escrituras insensible)")
    if fails:
        print("UMBRAL NO CUMPLIDO:", ", ".join(fails))
        return 1
    print("todos los umbrales cumplidos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
