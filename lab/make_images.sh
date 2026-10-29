#!/usr/bin/env bash
# Construye las imágenes de disco del laboratorio (requiere root: loop, LVM, LUKS, btrfs).
# Uso: sudo lab/make_images.sh OUTDIR
# Cada escenario deja OUTDIR/<nombre>.img y, si es UEFI, OUTDIR/<nombre>.nvram (salida simulada
# de `efibootmgr -v`: un disco en loop no tiene NVRAM propia).
set -euo pipefail
OUT=${1:?OUTDIR}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
STAGE="$OUT/.stage"

ESP_PARTUUID=0f0e0d0c-0000-4000-8000-0000000000e1
ESP_SERIAL=ABCD1234            # blkid -> ABCD-1234
ESP_UUID=ABCD-1234
ROOT_UUID=11111111-2222-4333-8444-555555555555
BOOT_UUID=22222222-3333-4444-8555-666666666666
BTRFS_UUID=33333333-4444-4555-8666-777777777777
LUKS_UUID=44444444-5555-4666-8777-888888888888
WRONG_UUID=deadbeef-0000-4000-8000-000000000bad
KVER=6.12.0-cx9
BOOT_IMG=${BOOT_IMG:-/usr/lib/grub/i386-pc/boot.img}

die() { echo "FALLA: $*" >&2; exit 1; }
for t in sgdisk sfdisk mkfs.ext4 mkfs.vfat mcopy mmd mkfs.btrfs btrfs cryptsetup pvcreate losetup udevadm; do
  command -v "$t" >/dev/null || die "falta $t"
done
[ -f "$BOOT_IMG" ] || die "falta $BOOT_IMG (grub-pc-bin)"

attach() { # img -> loop dev con particiones
  local dev; dev=$(losetup -fP --show "$1")
  udevadm settle || true
  for _ in $(seq 50); do [ -b "${dev}p1" ] && break; sleep 0.1; done
  [ -b "${dev}p1" ] || die "no aparecieron particiones de $dev"
  echo "$dev"
}

nvram_ok() {
  printf 'BootCurrent: 0001\nTimeout: 1 seconds\nBootOrder: 0001,0000\n'
  printf 'Boot0000* UiApp\tFvVol(7cb8bdc9-f8eb-4f34-aaea-3ee4af6516a1)/FvFile(462caa21-7614-4503-836e-8ab6f4662331)\n'
  printf 'Boot0001* debian\tHD(1,GPT,%s,0x800,0x20000)/File(\\EFI\\debian\\shimx64.efi)\n' "$ESP_PARTUUID"
}
nvram_no_entry() {
  printf 'BootCurrent: 0000\nTimeout: 1 seconds\nBootOrder: 0000\n'
  printf 'Boot0000* UiApp\tFvVol(7cb8bdc9-f8eb-4f34-aaea-3ee4af6516a1)/FvFile(462caa21-7614-4503-836e-8ab6f4662331)\n'
}

grub_cfg() { # search_uuid kernel_path cmdline
  cat <<G
set default=0
search --no-floppy --fs-uuid --set=root $1
menuentry 'Debian GNU/Linux' {
	search --no-floppy --fs-uuid --set=root $1
	linux	$2 $3
	initrd	${2/vmlinuz/initrd.img}
}
G
}

esp_tree() { # dir [sin_shim]
  mkdir -p "$1/EFI/debian"
  printf 'search.fs_uuid %s root\nset prefix=($root)/boot/grub\nconfigfile $prefix/grub.cfg\n' "$ROOT_UUID" > "$1/EFI/debian/grub.cfg"
  printf 'MZ-stub grubx64\n' > "$1/EFI/debian/grubx64.efi"
  [ "${2:-}" = sin_shim ] || printf 'MZ-stub shimx64\n' > "$1/EFI/debian/shimx64.efi"
}

mk_esp() { # dev dir
  mkfs.vfat -F 32 -i "$ESP_SERIAL" "$1" >/dev/null
  mcopy -s -i "$1" "$2"/* ::/
}

root_tree() { # dir fstab_root_uuid con_esp(1/0) grub(1/0) grub_uuid
  local d=$1
  mkdir -p "$d/etc" "$d/boot/grub"
  printf 'PRETTY_NAME="cortex9 bootfix lab"\n' > "$d/etc/os-release"
  { printf 'UUID=%s / ext4 errors=remount-ro 0 1\n' "$2"
    [ "$3" = 1 ] && printf 'UUID=%s /boot/efi vfat umask=0077 0 1\n' "$ESP_UUID"
    true; } > "$d/etc/fstab"
  printf 'kernel-stub\n' > "$d/boot/vmlinuz-$KVER"
  printf 'initrd-stub\n' > "$d/boot/initrd.img-$KVER"
  if [ "$4" = 1 ]; then
    grub_cfg "$5" "/boot/vmlinuz-$KVER" "root=UUID=$5 ro quiet" > "$d/boot/grub/grub.cfg"
  fi
}

gpt_uefi_2() { # img: ESP + root
  truncate -s 300M "$1"
  sgdisk -n1:2048:+64M -t1:ef00 -u1:$ESP_PARTUUID -n2:0:0 -t2:8304 "$1" >/dev/null
}
gpt_uefi_3() { # img: ESP + /boot + contenedor
  truncate -s 300M "$1"
  sgdisk -n1:2048:+64M -t1:ef00 -u1:$ESP_PARTUUID -n2:0:+64M -t2:8300 -n3:0:0 -t3:"$2" "$1" >/dev/null
}

uefi_ext4() { # name fstab_uuid con_esp grub grub_uuid esp_mode nvram_fn
  local name=$1 img="$OUT/$1.img" s="$STAGE/$1"
  rm -rf "$s"; mkdir -p "$s/esp" "$s/root"
  gpt_uefi_2 "$img"
  esp_tree "$s/esp" "$6"
  root_tree "$s/root" "$2" "$3" "$4" "$5"
  local dev; dev=$(attach "$img")
  mk_esp "${dev}p1" "$s/esp"
  mkfs.ext4 -q -U "$ROOT_UUID" -d "$s/root" "${dev}p2"
  losetup -d "$dev"
  "$7" > "$OUT/$name.nvram"
}

bios_ext4() { # name con_bootimg
  local name=$1 img="$OUT/$1.img" s="$STAGE/$1"
  rm -rf "$s"; mkdir -p "$s/root"
  truncate -s 300M "$img"
  printf 'label: dos\nstart=2048, type=83, bootable\n' | sfdisk -q "$img"
  [ "$2" = 1 ] && dd if="$BOOT_IMG" of="$img" bs=440 count=1 conv=notrunc status=none
  root_tree "$s/root" "$ROOT_UUID" 0 1 "$ROOT_UUID"
  local dev; dev=$(attach "$img")
  mkfs.ext4 -q -U "$ROOT_UUID" -d "$s/root" "${dev}p1"
  losetup -d "$dev"
}

boot_tree() { # dir cmdline
  mkdir -p "$1/grub"
  printf 'kernel-stub\n' > "$1/vmlinuz-$KVER"
  printf 'initrd-stub\n' > "$1/initrd.img-$KVER"
  grub_cfg "$BOOT_UUID" "/vmlinuz-$KVER" "$2" > "$1/grub/grub.cfg"
}

luks_case() { # name rd_luks_uuid
  local name=$1 img="$OUT/$1.img" s="$STAGE/$1"
  rm -rf "$s"; mkdir -p "$s/esp" "$s/boot"
  gpt_uefi_3 "$img" 8309
  esp_tree "$s/esp"
  boot_tree "$s/boot" "root=/dev/mapper/cryptroot ro rd.luks.uuid=luks-$2 quiet"
  local dev; dev=$(attach "$img")
  mk_esp "${dev}p1" "$s/esp"
  mkfs.ext4 -q -U "$BOOT_UUID" -d "$s/boot" "${dev}p2"
  printf 'cx9-lab-passphrase' > "$s/key"
  cryptsetup luksFormat -q --type luks2 --uuid "$LUKS_UUID" --pbkdf pbkdf2 \
    --pbkdf-force-iterations 1000 --key-file "$s/key" "${dev}p3"
  losetup -d "$dev"
  nvram_ok > "$OUT/$name.nvram"
}

lvm_case() { # name root_mapper
  local name=$1 img="$OUT/$1.img" s="$STAGE/$1"
  rm -rf "$s"; mkdir -p "$s/esp" "$s/boot" "$s/root/etc"
  gpt_uefi_3 "$img" 8e00
  esp_tree "$s/esp"
  boot_tree "$s/boot" "root=/dev/mapper/$2 ro quiet"
  printf '/dev/mapper/cx9vg-lvroot / ext4 errors=remount-ro 0 1\n' > "$s/root/etc/fstab"
  local dev; dev=$(attach "$img")
  mk_esp "${dev}p1" "$s/esp"
  mkfs.ext4 -q -U "$BOOT_UUID" -d "$s/boot" "${dev}p2"
  pvcreate -q --devices "${dev}p3" "${dev}p3"
  vgcreate -q --devices "${dev}p3" cx9vg "${dev}p3"
  lvcreate -q --devices "${dev}p3" -y -L 64M -n lvroot cx9vg
  mkfs.ext4 -q -d "$s/root" /dev/cx9vg/lvroot
  vgchange -q --devices "${dev}p3" -an cx9vg
  losetup -d "$dev"
  nvram_ok > "$OUT/$name.nvram"
}

btrfs_case() { # name subvol_pedido
  local name=$1 img="$OUT/$1.img" s="$STAGE/$1"
  rm -rf "$s"; mkdir -p "$s/esp" "$s/mnt"
  gpt_uefi_2 "$img"
  esp_tree "$s/esp"
  local dev; dev=$(attach "$img")
  mk_esp "${dev}p1" "$s/esp"
  mkfs.btrfs -q -f -U "$BTRFS_UUID" "${dev}p2"
  mount "${dev}p2" "$s/mnt"
  btrfs -q subvolume create "$s/mnt/@" >/dev/null
  mkdir -p "$s/mnt/@/etc" "$s/mnt/@/boot/grub"
  printf 'UUID=%s / btrfs subvol=@ 0 0\nUUID=%s /boot/efi vfat umask=0077 0 1\n' "$BTRFS_UUID" "$ESP_UUID" > "$s/mnt/@/etc/fstab"
  printf 'kernel-stub\n' > "$s/mnt/@/boot/vmlinuz-$KVER"
  printf 'initrd-stub\n' > "$s/mnt/@/boot/initrd.img-$KVER"
  grub_cfg "$BTRFS_UUID" "/@/boot/vmlinuz-$KVER" "root=UUID=$BTRFS_UUID ro rootflags=subvol=$2 quiet" > "$s/mnt/@/boot/grub/grub.cfg"
  umount "$s/mnt"
  losetup -d "$dev"
  nvram_ok > "$OUT/$name.nvram"
}

# --- controles sanos
uefi_ext4 c1_healthy_uefi   "$ROOT_UUID" 1 1 "$ROOT_UUID" con_shim nvram_ok
bios_ext4 c2_healthy_bios   1
luks_case c3_healthy_luks   "$LUKS_UUID"
# --- 10 rotas, una falla cada una
uefi_ext4 b01_fstab_root_uuid   "$WRONG_UUID" 1 1 "$ROOT_UUID" con_shim nvram_ok
uefi_ext4 b02_grub_root_uuid    "$ROOT_UUID"  1 1 "$WRONG_UUID" con_shim nvram_ok
uefi_ext4 b03_grub_cfg_missing  "$ROOT_UUID"  1 0 "$ROOT_UUID" con_shim nvram_ok
uefi_ext4 b04_nvram_no_entry    "$ROOT_UUID"  1 1 "$ROOT_UUID" con_shim nvram_no_entry
uefi_ext4 b05_esp_loader_missing "$ROOT_UUID" 1 1 "$ROOT_UUID" sin_shim nvram_ok
uefi_ext4 b06_esp_not_in_fstab  "$ROOT_UUID"  0 1 "$ROOT_UUID" con_shim nvram_ok
bios_ext4 b07_bios_mbr_no_grub  0
luks_case b08_luks_uuid_mismatch "$WRONG_UUID"
btrfs_case b09_btrfs_subvol_missing "@root"
lvm_case b10_lvm_lv_missing     cx9vg-root
rm -rf "$STAGE"
ls -la "$OUT"
