"""Parsers puros: grub.cfg, fstab, crypttab, efibootmgr -v."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class GrubEntry:
    title: str
    search_uuids: list[str] = field(default_factory=list)
    kernel: str | None = None
    cmdline: dict[str, str] = field(default_factory=dict)


def _cmdline(args: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            out.setdefault(k, v)
        else:
            out.setdefault(a, "")
    return out


def parse_grub_cfg(text: str) -> list[GrubEntry]:
    """Entradas menuentry en orden; si no hay menuentry, una entrada implícita con lo global."""
    entries: list[GrubEntry] = []
    glob = GrubEntry(title="(global)")
    cur = glob
    depth = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"""menuentry\s+(['"])(.*?)\1""", line)
        if m and line.endswith("{"):
            cur = GrubEntry(title=m.group(2), search_uuids=list(glob.search_uuids))
            entries.append(cur)
            depth += 1
            continue
        if line.startswith("submenu") and line.endswith("{"):
            depth += 1
            continue
        if line == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                cur = glob
            continue
        toks = line.split()
        if toks[0] == "search.fs_uuid" and len(toks) >= 2:
            cur.search_uuids.append(toks[1])
        elif toks[0] == "search":
            if "--fs-uuid" in toks or "-u" in toks:
                cand = [t for t in toks[1:] if not t.startswith("-")]
                if cand:
                    cur.search_uuids.append(cand[-1])
        elif toks[0] in ("linux", "linuxefi", "linux16") and len(toks) >= 2:
            cur.kernel = toks[1]
            cur.cmdline = _cmdline(toks[2:])
    if not entries and (glob.kernel or glob.search_uuids):
        entries.append(glob)
    return entries


@dataclass
class FstabEntry:
    spec: str
    mountpoint: str
    fstype: str
    options: str


def parse_fstab(text: str) -> list[FstabEntry]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        out.append(FstabEntry(parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else "defaults"))
    return out


def parse_crypttab(text: str) -> list[tuple[str, str]]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            parts = line.split()
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
    return out


@dataclass
class BootEntry:
    num: str
    label: str
    active: bool
    partuuid: str | None
    part: int | None
    path: str | None


@dataclass
class Nvram:
    boot_current: str | None
    boot_order: list[str]
    entries: list[BootEntry]


def parse_efibootmgr(text: str) -> Nvram:
    cur = None
    order: list[str] = []
    entries: list[BootEntry] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("BootCurrent:"):
            cur = line.split(":", 1)[1].strip()
        elif line.startswith("BootOrder:"):
            order = [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()]
        else:
            m = re.match(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+(.*)$", line)
            if not m:
                continue
            rest = m.group(3)
            hd = re.search(r"HD\((\d+),GPT,([0-9a-fA-F-]{36})[^)]*\)", rest)
            fp = re.search(r"File\(([^)]*)\)", rest)
            label = re.split(r"\s{2,}|\t|\s(?=HD\()|\s(?=VenHw\()|\s(?=PciRoot\()", rest, maxsplit=1)[0].strip()
            entries.append(BootEntry(num=m.group(1).upper(), label=label, active=m.group(2) == "*",
                                     partuuid=hd.group(2).lower() if hd else None,
                                     part=int(hd.group(1)) if hd else None,
                                     path=fp.group(1) if fp else None))
    return Nvram(cur, [o.upper() for o in order], entries)
