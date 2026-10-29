# cortex9-bootfix-live

Diagnóstico **solo-lectura** de una máquina Linux que no arranca (GRUB/UEFI/BIOS), pensado
para correr desde un live USB: lee tabla de particiones, `blkid`, MBR, ESP, `grub.cfg`,
`fstab`, contenedores LUKS/LVM y subvolúmenes Btrfs sin escribir en el disco, ordena la causa
probable y propone un plan de reparación explicado con copias de seguridad primero.
**No ejecuta ninguna reparación.**

Estado: MVP de un día. La medición está pre-registrada en [`lab/prereg.json`](lab/prereg.json);
los resultados se agregan en un PR posterior.

Licencia: Apache-2.0.
