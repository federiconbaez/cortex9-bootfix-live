"""Análisis puro de una instantánea (dict JSON producido por collect.py).

No toca discos: recibe datos ya leídos y devuelve causas ordenadas por etapa de
arranque (firmware -> cargador -> grub.cfg -> kernel/cmdline -> fstab).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import parse

ESP_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
BIOS_GRUB_GUID = "21686148-6449-6e6f-744e-656564454649"
READABLE = ("ext2", "ext3", "ext4", "vfat", "btrfs", "xfs")
STAGES = ["firmware", "loader", "grubcfg", "cmdline", "fstab"]


@dataclass
class Cause:
    code: str
    stage: str
    severity: str  # error | warning | info
    detail: str
    evidence: dict = field(default_factory=dict)


def _ci_exists(listing: set[str], path: str) -> bool:
    p = path.replace("\\", "/").lstrip("/").lower()
    return p in {x.lower() for x in listing}


def _lvm_names(lvm: list[dict]) -> set[str]:
    names = set()
    for x in lvm:
        vg, lv = x["vg"], x["lv"]
        names.add(f"{vg.replace('-', '--')}-{lv.replace('-', '--')}")
        names.add(f"{vg}/{lv}")
    return names


def analyze(snap: dict) -> list[Cause]:
    causes: list[Cause] = []
    parts = snap.get("partitions", [])
    fss = snap.get("filesystems", {})
    fw = snap.get("firmware")
    fs_uuids = {p["uuid"].lower() for p in parts if p.get("uuid") and p.get("fstype") in READABLE}
    luks = {p["uuid"].lower(): p for p in parts if p.get("fstype") == "crypto_LUKS" and p.get("uuid")}
    lvm = snap.get("lvm", [])
    has_lvm_pv = any(p.get("fstype") == "LVM2_member" for p in parts)
    opaque = bool(luks) or has_lvm_pv  # contenedores cuyo contenido no se lee en modo diagnóstico

    def add(code, stage, sev, detail, **ev):
        causes.append(Cause(code, stage, sev, detail, ev))

    # --- firmware / cargador
    esps = [p for p in parts if (p.get("parttype") or "").lower() == ESP_GUID]
    if fw == "uefi":
        if not esps:
            add("uefi.no_esp", "firmware", "error", "firmware UEFI pero el disco no tiene partición ESP")
        else:
            esp_ids = {(p.get("partuuid") or "").lower(): p for p in esps}
            nv = parse.parse_efibootmgr(snap.get("nvram_text") or "")
            mine = [e for e in nv.entries if e.partuuid and e.partuuid in esp_ids]
            esp = esps[0]
            listing = set(fss.get(esp["path"], {}).get("listing", []))
            fallback = _ci_exists(listing, "EFI/BOOT/BOOTX64.EFI")
            if not mine:
                if fallback:
                    add("uefi.fallback_only", "firmware", "warning",
                        "no hay entrada NVRAM para esta ESP; arrancaría solo por \\EFI\\BOOT\\BOOTX64.EFI")
                else:
                    add("uefi.no_boot_entry", "firmware", "error",
                        "ninguna entrada NVRAM apunta a la ESP de este disco y no hay cargador de respaldo",
                        esp=esp["path"], esp_partuuid=esp.get("partuuid"))
            else:
                ok = [e for e in mine if e.path and _ci_exists(listing, e.path)]
                if not ok:
                    e = mine[0]
                    add("uefi.loader_missing", "loader", "error",
                        f"la entrada Boot{e.num} '{e.label}' apunta a {e.path}, que no existe en la ESP",
                        entry=e.num, path=e.path, esp=esp["path"])
                elif not any(e.num in nv.boot_order for e in ok):
                    add("uefi.not_in_bootorder", "firmware", "warning",
                        "la entrada válida no está en BootOrder")
    elif fw == "bios":
        if snap.get("table") == "gpt" and not any(
                (p.get("parttype") or "").lower() == BIOS_GRUB_GUID for p in parts):
            add("bios.no_bios_grub_partition", "loader", "error",
                "disco GPT arrancado en BIOS sin partición bios_grub para core.img")
        if not snap.get("mbr_has_grub"):
            add("bios.mbr_no_grub", "firmware", "error",
                "el código de arranque del MBR no contiene GRUB (boot.img)")

    # --- grub.cfg
    cfg_loc = None
    for path, fs in fss.items():
        for rel, text in fs.get("files", {}).items():
            low = rel.lower()
            if low.startswith("efi/"):
                continue  # stub de la ESP: se analiza aparte
            if low.endswith("grub/grub.cfg") or low.endswith("grub2/grub.cfg"):
                cfg_loc = (path, rel, text)
                break
        if cfg_loc:
            break
    if cfg_loc is None:
        add("grub.cfg_missing", "grubcfg", "error",
            "no se encontró grub.cfg en ninguna partición legible")
    else:
        fs_path, rel, text = cfg_loc
        entries = parse.parse_grub_cfg(text)
        if not entries:
            add("grub.cfg_no_entries", "grubcfg", "error", f"{rel} no tiene menuentry con linux")
        else:
            e = entries[0]
            for u in e.search_uuids:
                if u.lower() not in fs_uuids:
                    add("grub.root_uuid_missing", "grubcfg", "error",
                        f"grub.cfg busca --fs-uuid {u}, que no existe en el disco",
                        uuid=u, grub_cfg=f"{fs_path}:{rel}")
                    break
            cl = e.cmdline
            root = cl.get("root", "")
            luks_req = []
            if "rd.luks.uuid" in cl:
                luks_req.append(cl["rd.luks.uuid"].removeprefix("luks-"))
            if cl.get("cryptdevice", "").startswith("UUID="):
                luks_req.append(cl["cryptdevice"][5:].split(":", 1)[0])
            for u in luks_req:
                if u.lower() not in luks:
                    add("luks.uuid_mismatch", "cmdline", "error",
                        f"el kernel pide desbloquear LUKS {u}, pero el contenedor LUKS del disco es "
                        f"{', '.join(luks) or 'ninguno'}", requested=u, present=sorted(luks))
            if root.startswith("UUID="):
                u = root[5:].lower()
                if u not in fs_uuids and not opaque:
                    add("grub.root_uuid_missing", "cmdline", "error",
                        f"root=UUID={u} no existe en el disco", uuid=u, grub_cfg=f"{fs_path}:{rel}")
            elif root.startswith("/dev/mapper/") and lvm:
                name = root[len("/dev/mapper/"):]
                if name not in _lvm_names(lvm) and not luks:
                    add("lvm.lv_missing", "cmdline", "error",
                        f"root={root} no corresponde a ningún LV ({', '.join(sorted(_lvm_names(lvm)))})",
                        requested=name)
            sub = None
            for opt in cl.get("rootflags", "").split(","):
                if opt.startswith("subvol="):
                    sub = opt[len("subvol="):].strip("/")
            if sub is not None:
                subs = set()
                for f in fss.values():
                    subs.update(s.strip("/") for s in f.get("subvols", []))
                if sub not in subs:
                    add("btrfs.subvol_missing", "cmdline", "error",
                        f"rootflags=subvol={sub} pero los subvolúmenes son {sorted(subs)}",
                        requested=sub, present=sorted(subs))
            if e.kernel:
                listing = set(fss.get(fs_path, {}).get("listing", []))
                if not _ci_exists(listing, e.kernel):
                    add("grub.kernel_missing", "cmdline", "error",
                        f"el kernel {e.kernel} no está en {fs_path}", kernel=e.kernel)

    # --- fstab
    fstab_loc = None
    for path, fs in fss.items():
        for rel, text in fs.get("files", {}).items():
            if rel.endswith("etc/fstab"):
                fstab_loc = (path, rel, text)
                break
        if fstab_loc:
            break
    if fstab_loc:
        _, rel, text = fstab_loc
        entries = parse.parse_fstab(text)
        for fe in entries:
            if fe.spec.startswith("UUID="):
                u = fe.spec[5:].lower()
                if u not in fs_uuids:
                    code = "fstab.root_uuid_missing" if fe.mountpoint == "/" else "fstab.uuid_missing"
                    add(code, "fstab", "error",
                        f"fstab monta {fe.mountpoint} desde UUID={u}, que no existe en el disco",
                        mountpoint=fe.mountpoint, uuid=u)
        if fw == "uefi" and esps and not any(fe.mountpoint in ("/boot/efi", "/efi", "/boot")
                                             and fe.fstype == "vfat" for fe in entries):
            add("fstab.esp_missing", "fstab", "error",
                "fstab no monta la ESP: grub-install/update-grub no actualizarán el cargador",
                esp=esps[0]["path"], esp_uuid=esps[0].get("uuid"))

    causes.sort(key=lambda c: (c.severity != "error", STAGES.index(c.stage)))
    return causes


def to_dict(causes: list[Cause]) -> list[dict]:
    return [asdict(c) for c in causes]
