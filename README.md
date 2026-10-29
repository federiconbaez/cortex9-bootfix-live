# cortex9-bootfix-live

Diagnóstico **solo-lectura** de una máquina Linux que no arranca (`grub rescue`,
`unknown filesystem`, sin entrada UEFI), pensado para correr desde un live USB. Lee la tabla de
particiones, `blkid`, el MBR, la ESP, `grub.cfg`, `fstab`, cabeceras LUKS, metadatos LVM y
subvolúmenes Btrfs **sin escribir en el disco**, ordena la causa probable por etapa de arranque
y propone un plan de reparación explicado, con copias de seguridad primero.
**bootfix no ejecuta ninguna reparación**: el plan es texto para que una persona lo revise.

Estado: **MVP de un día** (el script de diagnóstico). Licencia Apache-2.0.

```sh
sudo python3 -m bootfix diagnose --disk /dev/nvme0n1            # en el live USB
sudo python3 -m bootfix diagnose --disk /dev/sda --json --snapshot-out snap.json
python3 -m bootfix analyze snap.json                              # offline, sin root
```

## Cómo evita escribir

- `blockdev --setro` sobre el disco mientras lee (las particiones lo heredan) y restaura el estado previo al salir.
- Monta siempre `ro`, con `noload` (ext*), `norecovery` (xfs) y `rescue=nologreplay` (btrfs) para no reproducir journals.
- Nunca desbloquea LUKS ni activa LVM: compara el UUID de la cabecera LUKS y lee LVM con `lvs --readonly`.
- Cada comando que corre queda en `commands_run` de la instantánea ([ejemplo](results/snapshots/b10_lvm_lv_missing.snapshot.json)).

## Causas que detecta

`uefi.no_esp`, `uefi.no_boot_entry`, `uefi.fallback_only` (aviso), `uefi.loader_missing`,
`uefi.not_in_bootorder` (aviso), `bios.mbr_no_grub`, `bios.no_bios_grub_partition`,
`grub.cfg_missing`, `grub.cfg_no_entries`, `grub.root_uuid_missing`, `grub.kernel_missing`,
`luks.uuid_mismatch`, `lvm.lv_missing`, `btrfs.subvol_missing`, `fstab.root_uuid_missing`,
`fstab.uuid_missing`, `fstab.esp_missing`.

## Resultados (medidos)

Pre-registro: [`lab/prereg.json`](lab/prereg.json), commit `679967e`, empujado antes de construir
o medir cualquier imagen. Imágenes: [`lab/make_images.sh`](lab/make_images.sh). Medición:
`sudo python3 -m lab.measure imgs`. Corre en GitHub Actions (ubuntu-24.04, kernel
7.0.0-1017-azure) con sudo; en la box no se puede (sin nodos de partición de loop).

Cada imagen se adjunta con `losetup -fP` **en lectura-escritura**: la protección tiene que
venir de bootfix. Se mide sha256 del archivo antes y después, y los sectores escritos
(campo 7 de `/sys/block/<dev>/stat`) del loop y de cada partición durante `bootfix diagnose`.

| Métrica | Umbral | Run [40885390487](https://github.com/federiconbaez/cortex9-bootfix-live/actions/runs/40885390487) (main, pre-registro) | Run [40886085693](https://github.com/federiconbaez/cortex9-bootfix-live/actions/runs/40886085693) (PR #1) |
|---|---|---|---|
| Causa probable correcta en imágenes rotas | ≥ 9/10 | **10/10** | **10/10** |
| Controles sanos sin ningún error | 3/3 | **3/3** | **3/3** |
| sha256 idéntico antes/después | 13/13 | **13/13** | **13/13** |
| Sectores escritos durante el diagnóstico | 0 | **0** | **0** |
| Control positivo del instrumento: montaje rw + 1 archivo | sha256 cambia y sectores > 0 | (no existía) | sha256 cambió, **320** sectores |

Diagnóstico de 65 a 112 ms por imagen en el CI. Detalle por imagen:
[`results/ci-run-40886085693.json`](results/ci-run-40886085693.json) e instantáneas en
[`results/snapshots/`](results/snapshots/).

Las 10 rotas (una falla cada una): UUID de root en fstab inexistente, UUID en `grub.cfg`
inexistente, `grub.cfg` ausente, NVRAM sin entrada para la ESP, entrada NVRAM a un
`shimx64.efi` que no está, ESP fuera de fstab, MBR sin GRUB (BIOS), `rd.luks.uuid` distinto de
la cabecera LUKS2, `rootflags=subvol=` inexistente en Btrfs, `root=/dev/mapper/` que no es un
LV. Controles: UEFI ext4, BIOS MBR con `boot.img` de GRUB, UEFI con `/boot` + LUKS2.

**Caveat "por construcción":** 10/10 y 3/3 son sobre imágenes que armó el mismo autor que el
analizador, con una sola falla por imagen y nombres de causa definidos de antemano. Prueba que
cada chequeo funciona contra discos reales en loop, no la cobertura de fallas del mundo real.

**Cambio posterior a la primera medición:** el control positivo del instrumento se agregó en
el PR #1, después del run 40885390487. No cambió escenarios ni umbrales.

## Alcance: qué NO está verificado

- **Arrancar hasta login tras aplicar el plan** (≥ 8/10 en la idea original): **no medido**. Falta
  la matriz QEMU/OVMF; el plan de reparación no se ejecutó nunca.
- **NVRAM simulada:** un loop no tiene NVRAM; se pasa un archivo con salida de `efibootmgr -v`.
  La lectura real con `efibootmgr -v` en un live UEFI no se probó.
- **Journal sucio:** ninguna imagen tiene journal pendiente, así que no se probó que `noload` /
  `rescue=nologreplay` eviten escrituras en ese caso.
- Root cifrado o en LVM: su contenido (fstab, crypttab) no se lee en modo diagnóstico.
- Discos reales (NVMe/SATA), live USB de distribuciones concretas, Secure Boot, dual-boot con
  Windows, RAID/mdadm, XFS, ZFS y `grub.cfg` de Fedora/BLS: sin probar.
- Comparación con Boot-Repair: no se hizo.
- Imágenes de 300 MB con kernels e initrd falsos (archivos de texto).

## Tests

`python3 -m tests.run`: 28 tests sin root (parsers, analizador, plan, CLI). Un test salteado
hace fallar el job. El recolector se ejercita como root en el job `lab-readonly`.

## Tamaño honesto

Producto completo estimado en 4 semanas de una persona; la matriz de VMs que arrancan después
de reparar es la mayor parte y no está en este MVP.
