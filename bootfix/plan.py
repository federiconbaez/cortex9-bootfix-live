"""Plan de reparación explicado. Solo TEXTO: bootfix nunca ejecuta estos comandos."""

from __future__ import annotations

from .analyze import Cause

BACKUP = [
    "# 0. Copias antes de tocar nada (en un pendrive o en /root del live, NO en el disco roto)",
    "mkdir -p ~/bootfix-backup && cd ~/bootfix-backup",
    "sudo efibootmgr -v > nvram-antes.txt 2>/dev/null || true",
    "sudo dd if={disk} of=mbr-y-gpt.bin bs=1M count=1",
    "sudo sfdisk -d {disk} > tabla-particiones.sfdisk",
]


def _esp_backup(esp: str) -> list[str]:
    return [f"sudo mkdir -p /mnt/esp && sudo mount -o ro {esp} /mnt/esp && sudo tar -C /mnt/esp -czf esp.tar.gz . && sudo umount /mnt/esp"]


def steps_for(c: Cause, snap: dict) -> list[str]:
    ev = c.evidence
    disk = snap.get("disk", "/dev/sdX")
    if c.code == "uefi.no_boot_entry":
        esp = ev.get("esp", f"{disk}1")
        num = "".join(ch for ch in esp[len(disk):] if ch.isdigit()) or "1"
        return _esp_backup(esp) + [
            f"sudo efibootmgr --create --disk {disk} --part {num} --label 'Linux' --loader '\\EFI\\<distro>\\shimx64.efi'",
            "# (verificá con 'ls /mnt/esp/EFI' el nombre real de <distro> y si existe shimx64.efi o grubx64.efi)",
            "# reversible: sudo efibootmgr -b <NNNN> -B borra la entrada creada",
        ]
    if c.code == "uefi.loader_missing":
        return _esp_backup(ev.get("esp", "")) + [
            "# el cargador no está en la ESP: reinstalarlo desde el sistema (chroot)",
            "# montar root y ESP en /mnt, luego:",
            "for d in dev proc sys sys/firmware/efi/efivars run; do sudo mount --rbind /$d /mnt/$d; done",
            "sudo chroot /mnt grub-install --target=x86_64-efi --efi-directory=/boot/efi",
            "sudo chroot /mnt update-grub",
        ]
    if c.code == "bios.mbr_no_grub":
        return ["# reinstalar boot.img en el MBR (el backup de 1 MiB de arriba permite volver atrás)",
                "for d in dev proc sys run; do sudo mount --rbind /$d /mnt/$d; done",
                f"sudo chroot /mnt grub-install --target=i386-pc {disk}",
                f"# reversible: sudo dd if=mbr-y-gpt.bin of={disk} bs=446 count=1"]
    if c.code == "bios.no_bios_grub_partition":
        return ["# GPT + BIOS necesita una partición bios_grub de 1 MiB sin formato; crearla requiere espacio libre",
                "# fuera del alcance de este MVP: no se propone reparticionar automáticamente"]
    if c.code == "grub.cfg_missing":
        return ["# regenerar grub.cfg desde el sistema instalado",
                "for d in dev proc sys run; do sudo mount --rbind /$d /mnt/$d; done",
                "sudo chroot /mnt update-grub   # o grub2-mkconfig -o /boot/grub2/grub.cfg"]
    if c.code in ("grub.root_uuid_missing", "fstab.root_uuid_missing", "fstab.uuid_missing"):
        cand = [p for p in snap.get("partitions", []) if p.get("fstype") in ("ext4", "btrfs", "xfs")]
        hint = ", ".join(f"{p['path']}={p.get('uuid')}" for p in cand)
        f = "/mnt/etc/fstab" if c.code.startswith("fstab") else "/mnt/boot/grub/grub.cfg"
        return [f"sudo cp -a {f} {f}.bootfix-bak",
                f"# UUID buscado: {ev.get('uuid')}; sistemas de archivos presentes: {hint}",
                f"sudo sed -i 's/{ev.get('uuid')}/<UUID-correcto>/g' {f}",
                "# si es grub.cfg, mejor regenerarlo con update-grub dentro del chroot"]
    if c.code == "fstab.esp_missing":
        return ["sudo cp -a /mnt/etc/fstab /mnt/etc/fstab.bootfix-bak",
                f"echo 'UUID={ev.get('esp_uuid')} /boot/efi vfat umask=0077 0 1' | sudo tee -a /mnt/etc/fstab"]
    if c.code == "luks.uuid_mismatch":
        return [f"# UUID LUKS real: {', '.join(ev.get('present', []))}; pedido: {ev.get('requested')}",
                "sudo cp -a /mnt/boot/grub/grub.cfg /mnt/boot/grub/grub.cfg.bootfix-bak",
                "# corregir /etc/crypttab y /etc/default/grub del sistema y regenerar initramfs + grub.cfg:",
                "sudo cryptsetup open /dev/<luks> cryptroot && (montar root en /mnt)",
                "sudo chroot /mnt update-initramfs -u -k all && sudo chroot /mnt update-grub"]
    if c.code == "lvm.lv_missing":
        return ["sudo lvs -o vg_name,lv_name   # nombres reales",
                "# corregir root= en /etc/default/grub y /etc/fstab, luego update-grub en el chroot"]
    if c.code == "btrfs.subvol_missing":
        return [f"# subvolúmenes presentes: {ev.get('present')}; pedido: {ev.get('requested')}",
                "# corregir rootflags=subvol= en /etc/default/grub y regenerar grub.cfg"]
    if c.code == "grub.kernel_missing":
        return ["# reinstalar el kernel dentro del chroot (apt install --reinstall linux-image-...)"]
    return ["# sin receta automática para este caso"]


def render(causes: list[Cause], snap: dict) -> str:
    errs = [c for c in causes if c.severity == "error"]
    out = []
    if not errs:
        out.append("Diagnóstico: no se encontró una causa de no-arranque en lo que se pudo leer.")
    else:
        out.append(f"Causa probable: [{errs[0].code}] {errs[0].detail}")
        for c in errs[1:]:
            out.append(f"También: [{c.code}] {c.detail}")
    for c in causes:
        if c.severity != "error":
            out.append(f"Aviso: [{c.code}] {c.detail}")
    if errs:
        out.append("")
        out.append("Plan propuesto (NO se ejecutó nada; revisá cada paso):")
        out += [s.format(disk=snap.get("disk", "/dev/sdX")) for s in BACKUP]
        for i, c in enumerate(errs, 1):
            out.append(f"# {i}. {c.code}")
            out += steps_for(c, snap)
    return "\n".join(out)
