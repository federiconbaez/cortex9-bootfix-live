"""Recolección SOLO-LECTURA de un disco: tabla, blkid, MBR, archivos de arranque, LVM, NVRAM.

Garantías que este módulo intenta dar (y que lab/measure.py mide con sha256 y con los
contadores de escritura del kernel):
- marca el disco y sus particiones como solo-lectura en el kernel (blockdev --setro)
  mientras dura la lectura, y restaura el estado previo al terminar;
- monta con opciones que no reproducen journal (ext*: noload, xfs: norecovery,
  btrfs: rescue=nologreplay) y siempre `ro`;
- nunca desbloquea LUKS ni activa LVM (lvs --readonly lee metadatos sin activar).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

MOUNT_OPTS = {
    "ext2": ["ro,noload", "ro"], "ext3": ["ro,noload"], "ext4": ["ro,noload"],
    "vfat": ["ro"], "xfs": ["ro,norecovery"],
    "btrfs": ["ro,rescue=nologreplay,subvolid=5", "ro,nologreplay,subvolid=5"],
}
CANDIDATE_FILES = ["etc/fstab", "etc/crypttab", "boot/grub/grub.cfg", "boot/grub2/grub.cfg",
                   "grub/grub.cfg", "grub2/grub.cfg", "EFI/debian/grub.cfg", "EFI/ubuntu/grub.cfg"]
LIST_DIRS = ["boot", "EFI", "efi", "grub", "grub2", ""]
MAX_LIST = 5000


class Collector:
    def __init__(self, disk: str, firmware: str | None = None, nvram_file: str | None = None,
                 workdir: str | None = None):
        self.disk = disk
        self.firmware = firmware
        self.nvram_file = nvram_file
        self.workdir = workdir or "/run"
        self.log: list[str] = []
        self._ro_restore: list[str] = []
        self._mounts: list[str] = []

    def sh(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        self.log.append(" ".join(args))
        cp = subprocess.run(list(args), capture_output=True, text=True)
        if check and cp.returncode != 0:
            raise RuntimeError(f"{' '.join(args)}: {cp.stderr.strip()}")
        return cp

    # --- protección
    def _setro(self, dev: str) -> None:
        cp = self.sh("blockdev", "--getro", dev)
        if cp.returncode == 0 and cp.stdout.strip() == "0":
            if self.sh("blockdev", "--setro", dev).returncode == 0:
                self._ro_restore.append(dev)

    def _restore(self) -> None:
        for m in reversed(self._mounts):
            self.sh("umount", m)
            try:
                os.rmdir(m)
            except OSError:
                pass
        self._mounts.clear()
        for dev in reversed(self._ro_restore):
            self.sh("blockdev", "--setrw", dev)
        self._ro_restore.clear()

    # --- lectura
    def partitions(self) -> tuple[str | None, list[dict]]:
        cp = self.sh("sfdisk", "-J", self.disk)
        if cp.returncode != 0:
            return None, []
        pt = json.loads(cp.stdout)["partitiontable"]
        parts = []
        for i, p in enumerate(pt.get("partitions", []), 1):
            node = p["node"]
            num = int("".join(ch for ch in node[len(self.disk):] if ch.isdigit()) or i)
            parts.append({"path": node, "number": num,
                          "parttype": str(p.get("type", "")).lower(),
                          "partuuid": str(p.get("uuid", "")).lower() or None})
        return pt.get("label"), parts

    def blkid(self, dev: str) -> dict:
        cp = self.sh("blkid", "-p", "-o", "export", dev)
        out = {}
        for line in cp.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        return out

    def read_mbr(self) -> bytes:
        with open(self.disk, "rb") as fh:
            return fh.read(440)

    def _mount(self, dev: str, fstype: str) -> str | None:
        base = tempfile.mkdtemp(prefix="bootfix-", dir=self.workdir)
        for opts in MOUNT_OPTS.get(fstype, []):
            if self.sh("mount", "-t", fstype, "-o", opts, dev, base).returncode == 0:
                self._mounts.append(base)
                return base
        os.rmdir(base)
        return None

    @staticmethod
    def _listing(root: str, prefixes: list[str]) -> list[str]:
        out: list[str] = []
        for pre in prefixes:
            for d in LIST_DIRS:
                top = os.path.join(root, pre, d) if d else os.path.join(root, pre)
                if not os.path.isdir(top):
                    continue
                if not d:
                    for name in os.listdir(top):
                        if os.path.isfile(os.path.join(top, name)):
                            out.append(os.path.relpath(os.path.join(top, name), root))
                    continue
                for dp, _dn, fn in os.walk(top):
                    for name in fn:
                        out.append(os.path.relpath(os.path.join(dp, name), root))
                        if len(out) > MAX_LIST:
                            return out
        return sorted(set(out))

    @staticmethod
    def _read_files(root: str, prefixes: list[str]) -> dict[str, str]:
        files = {}
        for pre in prefixes:
            for rel in CANDIDATE_FILES:
                p = os.path.join(root, pre, rel)
                if os.path.isfile(p):
                    with open(p, errors="replace") as fh:
                        files[os.path.relpath(p, root)] = fh.read(1_000_000)
        return files

    def _btrfs_subvols(self, mnt: str) -> list[str]:
        cp = self.sh("btrfs", "subvolume", "list", mnt)
        subs = []
        for line in cp.stdout.splitlines():
            if " path " in line:
                subs.append(line.split(" path ", 1)[1].strip())
        return subs

    def _lvm(self, dev: str) -> list[dict]:
        if shutil.which("lvs") is None:
            return []
        cp = self.sh("lvs", "--readonly", "--reportformat", "json", "-o", "vg_name,lv_name",
                     "--devices", dev)
        if cp.returncode != 0:
            return []
        out = []
        for rep in json.loads(cp.stdout).get("report", []):
            for lv in rep.get("lv", []):
                out.append({"vg": lv["vg_name"], "lv": lv["lv_name"], "pv": dev})
        return out

    def collect(self) -> dict:
        fw = self.firmware or ("uefi" if os.path.isdir("/sys/firmware/efi") else "bios")
        snap: dict = {"disk": self.disk, "firmware": fw, "partitions": [], "filesystems": {},
                      "lvm": [], "nvram_text": None}
        try:
            self._setro(self.disk)
            table, parts = self.partitions()
            snap["table"] = table
            snap["mbr_has_grub"] = b"GRUB" in self.read_mbr()
            for p in parts:
                self._setro(p["path"])
                b = self.blkid(p["path"])
                p["fstype"] = b.get("TYPE")
                p["uuid"] = (b.get("UUID") or "").lower() or None
                p["label"] = b.get("LABEL")
                snap["partitions"].append(p)
                if p["fstype"] in MOUNT_OPTS:
                    mnt = self._mount(p["path"], p["fstype"])
                    if mnt is None:
                        snap["filesystems"][p["path"]] = {"mounted": False, "files": {}, "listing": []}
                        continue
                    prefixes = [""]
                    subs = []
                    if p["fstype"] == "btrfs":
                        subs = self._btrfs_subvols(mnt)
                        prefixes += subs
                    snap["filesystems"][p["path"]] = {
                        "mounted": True, "subvols": subs,
                        "files": self._read_files(mnt, prefixes),
                        "listing": self._listing(mnt, prefixes)}
                elif p["fstype"] == "LVM2_member":
                    snap["lvm"] += self._lvm(p["path"])
            if self.nvram_file:
                with open(self.nvram_file) as fh:
                    snap["nvram_text"] = fh.read()
                snap["nvram_source"] = f"archivo {self.nvram_file}"
            elif fw == "uefi" and shutil.which("efibootmgr"):
                snap["nvram_text"] = self.sh("efibootmgr", "-v").stdout
                snap["nvram_source"] = "efibootmgr -v"
        finally:
            self._restore()
        snap["commands_run"] = self.log
        return snap
