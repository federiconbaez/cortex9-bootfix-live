import copy
import io
import json
import os
import unittest
from contextlib import redirect_stdout

from bootfix import analyze as an
from bootfix import cli, parse, plan

ESP_PU = "0f0e0d0c-0000-4000-8000-0000000000e1"
ROOT = "11111111-2222-4333-8444-555555555555"
WRONG = "deadbeef-0000-4000-8000-000000000bad"
K = "6.12.0-cx9"

NV_OK = ("BootCurrent: 0001\nTimeout: 1 seconds\nBootOrder: 0001,0000\n"
         "Boot0000* UiApp\tFvVol(7cb8bdc9-f8eb-4f34-aaea-3ee4af6516a1)/FvFile(462caa21-7614-4503-836e-8ab6f4662331)\n"
         f"Boot0001* debian\tHD(1,GPT,{ESP_PU},0x800,0x20000)/File(\\EFI\\debian\\shimx64.efi)\n")

GRUB = (f"search --no-floppy --fs-uuid --set=root {ROOT}\n"
        "menuentry 'Debian GNU/Linux' {\n"
        f"\tsearch --no-floppy --fs-uuid --set=root {ROOT}\n"
        f"\tlinux\t/boot/vmlinuz-{K} root=UUID={ROOT} ro quiet\n"
        f"\tinitrd\t/boot/initrd.img-{K}\n}}\n")
FSTAB = f"UUID={ROOT} / ext4 errors=remount-ro 0 1\nUUID=ABCD-1234 /boot/efi vfat umask=0077 0 1\n"


def healthy_uefi():
    return {
        "disk": "/dev/loop9", "firmware": "uefi", "table": "gpt", "mbr_has_grub": False,
        "partitions": [
            {"path": "/dev/loop9p1", "number": 1, "parttype": an.ESP_GUID, "partuuid": ESP_PU,
             "fstype": "vfat", "uuid": "abcd-1234"},
            {"path": "/dev/loop9p2", "number": 2, "parttype": "4f68bce3-e8cd-4db1-96e7-fbcaf984b709",
             "partuuid": "aaaa0000-0000-4000-8000-000000000002", "fstype": "ext4", "uuid": ROOT}],
        "filesystems": {
            "/dev/loop9p1": {"mounted": True, "files": {"EFI/debian/grub.cfg": f"search.fs_uuid {ROOT} root\n"},
                             "listing": ["EFI/debian/grub.cfg", "EFI/debian/grubx64.efi", "EFI/debian/shimx64.efi"]},
            "/dev/loop9p2": {"mounted": True, "files": {"etc/fstab": FSTAB, "boot/grub/grub.cfg": GRUB},
                             "listing": ["boot/grub/grub.cfg", f"boot/vmlinuz-{K}", f"boot/initrd.img-{K}"]}},
        "lvm": [], "nvram_text": NV_OK}


def codes(snap):
    return [c.code for c in an.analyze(snap) if c.severity == "error"]


class Parsers(unittest.TestCase):
    def test_grub_entry(self):
        e = parse.parse_grub_cfg(GRUB)
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0].kernel, f"/boot/vmlinuz-{K}")
        self.assertEqual(e[0].cmdline["root"], f"UUID={ROOT}")
        self.assertIn(ROOT, e[0].search_uuids)

    def test_grub_search_fs_uuid_and_submenu(self):
        txt = ("search.fs_uuid 1234 root\nsubmenu 'Advanced' {\n menuentry 'x' {\n  linux /v root=/dev/sda2\n }\n}\n")
        e = parse.parse_grub_cfg(txt)
        self.assertEqual(e[0].kernel, "/v")
        self.assertEqual(e[0].search_uuids, ["1234"])

    def test_fstab(self):
        f = parse.parse_fstab("# c\n" + FSTAB + "tmpfs /tmp tmpfs defaults 0 0\n")
        self.assertEqual([x.mountpoint for x in f], ["/", "/boot/efi", "/tmp"])

    def test_efibootmgr(self):
        nv = parse.parse_efibootmgr(NV_OK)
        self.assertEqual(nv.boot_order, ["0001", "0000"])
        e = [x for x in nv.entries if x.num == "0001"][0]
        self.assertEqual((e.partuuid, e.part, e.path, e.label), (ESP_PU, 1, "\\EFI\\debian\\shimx64.efi", "debian"))
        self.assertIsNone([x for x in nv.entries if x.num == "0000"][0].partuuid)

    def test_efibootmgr_new_format(self):
        # efibootmgr 18: etiqueta, tab, ruta del dispositivo
        txt = f"BootOrder: 0003\nBoot0003* ubuntu\tHD(1,GPT,{ESP_PU.upper()},0x800,0x100000)/\\EFI\\ubuntu\\shimx64.efi\n"
        nv = parse.parse_efibootmgr(txt)
        self.assertEqual(nv.entries[0].partuuid, ESP_PU)

    def test_crypttab(self):
        self.assertEqual(parse.parse_crypttab("cryptroot UUID=abc none luks\n"), [("cryptroot", "UUID=abc")])


class Analyzer(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(codes(healthy_uefi()), [])

    def test_fstab_root_uuid(self):
        s = healthy_uefi()
        s["filesystems"]["/dev/loop9p2"]["files"]["etc/fstab"] = FSTAB.replace(ROOT, WRONG)
        self.assertEqual(codes(s), ["fstab.root_uuid_missing"])

    def test_grub_root_uuid(self):
        s = healthy_uefi()
        s["filesystems"]["/dev/loop9p2"]["files"]["boot/grub/grub.cfg"] = GRUB.replace(ROOT, WRONG)
        self.assertEqual(codes(s)[0], "grub.root_uuid_missing")

    def test_grub_cfg_missing_ignores_esp_stub(self):
        s = healthy_uefi()
        del s["filesystems"]["/dev/loop9p2"]["files"]["boot/grub/grub.cfg"]
        self.assertEqual(codes(s), ["grub.cfg_missing"])

    def test_no_nvram_entry(self):
        s = healthy_uefi()
        s["nvram_text"] = "BootOrder: 0000\nBoot0000* UiApp\tFvVol(x)/FvFile(y)\n"
        self.assertEqual(codes(s), ["uefi.no_boot_entry"])

    def test_no_nvram_entry_but_fallback(self):
        s = healthy_uefi()
        s["nvram_text"] = "BootOrder: 0000\n"
        s["filesystems"]["/dev/loop9p1"]["listing"].append("EFI/BOOT/BOOTX64.EFI")
        self.assertEqual(codes(s), [])
        self.assertIn("uefi.fallback_only", [c.code for c in an.analyze(s)])

    def test_loader_missing(self):
        s = healthy_uefi()
        s["filesystems"]["/dev/loop9p1"]["listing"].remove("EFI/debian/shimx64.efi")
        self.assertEqual(codes(s), ["uefi.loader_missing"])

    def test_loader_case_insensitive(self):
        s = healthy_uefi()
        s["filesystems"]["/dev/loop9p1"]["listing"] = ["efi/DEBIAN/SHIMX64.EFI"]
        self.assertEqual(codes(s), [])

    def test_esp_not_in_fstab(self):
        s = healthy_uefi()
        s["filesystems"]["/dev/loop9p2"]["files"]["etc/fstab"] = FSTAB.splitlines()[0] + "\n"
        self.assertEqual(codes(s), ["fstab.esp_missing"])

    def test_no_esp(self):
        s = healthy_uefi()
        s["partitions"][0]["parttype"] = "0fc63daf-8483-4772-8e79-3d69d8477de4"
        self.assertEqual(codes(s)[0], "uefi.no_esp")

    def test_bios(self):
        s = healthy_uefi()
        s.update(firmware="bios", table="dos", mbr_has_grub=True)
        self.assertEqual(codes(s), [])
        s["mbr_has_grub"] = False
        self.assertEqual(codes(s), ["bios.mbr_no_grub"])
        s.update(table="gpt", mbr_has_grub=True)
        self.assertEqual(codes(s), ["bios.no_bios_grub_partition"])

    def test_kernel_missing(self):
        s = healthy_uefi()
        s["filesystems"]["/dev/loop9p2"]["listing"].remove(f"boot/vmlinuz-{K}")
        self.assertEqual(codes(s), ["grub.kernel_missing"])

    def _luks(self, requested):
        s = healthy_uefi()
        boot = "22222222-3333-4444-8555-666666666666"
        luks = "44444444-5555-4666-8777-888888888888"
        g = (f"menuentry 'D' {{\n search --fs-uuid --set=root {boot}\n"
             f" linux /vmlinuz-{K} root=/dev/mapper/cryptroot ro rd.luks.uuid=luks-{requested}\n}}\n")
        s["partitions"][1].update(fstype="ext4", uuid=boot)
        s["partitions"].append({"path": "/dev/loop9p3", "number": 3, "parttype": "x", "partuuid": "y",
                                "fstype": "crypto_LUKS", "uuid": luks})
        s["filesystems"]["/dev/loop9p2"] = {"mounted": True, "files": {"grub/grub.cfg": g},
                                            "listing": ["grub/grub.cfg", f"vmlinuz-{K}"]}
        return s, luks

    def test_luks_ok_and_mismatch(self):
        s, luks = self._luks("44444444-5555-4666-8777-888888888888")
        self.assertEqual(codes(s), [])
        s2, _ = self._luks(WRONG)
        self.assertEqual(codes(s2), ["luks.uuid_mismatch"])

    def test_lvm(self):
        s, _ = self._luks("x")
        s["partitions"][2]["fstype"] = "LVM2_member"
        g = s["filesystems"]["/dev/loop9p2"]["files"]["grub/grub.cfg"]
        s["filesystems"]["/dev/loop9p2"]["files"]["grub/grub.cfg"] = g.replace(
            "root=/dev/mapper/cryptroot ro rd.luks.uuid=luks-x", "root=/dev/mapper/cx9vg-root ro")
        s["lvm"] = [{"vg": "cx9vg", "lv": "lvroot", "pv": "/dev/loop9p3"}]
        self.assertEqual(codes(s), ["lvm.lv_missing"])
        s["lvm"] = [{"vg": "cx9vg", "lv": "root", "pv": "/dev/loop9p3"}]
        self.assertEqual(codes(s), [])

    def test_lvm_dash_escaping(self):
        self.assertIn("my--vg-lv--root", an._lvm_names([{"vg": "my-vg", "lv": "lv-root"}]))

    def test_btrfs_subvol(self):
        s = healthy_uefi()
        bt = "33333333-4444-4555-8666-777777777777"
        s["partitions"][1].update(fstype="btrfs", uuid=bt)
        g = (f"menuentry 'D' {{\n search --fs-uuid --set=root {bt}\n"
             f" linux /@/boot/vmlinuz-{K} root=UUID={bt} rootflags=subvol=@root ro\n}}\n")
        s["filesystems"]["/dev/loop9p2"] = {
            "mounted": True, "subvols": ["@"],
            "files": {"@/boot/grub/grub.cfg": g,
                      "@/etc/fstab": f"UUID={bt} / btrfs subvol=@ 0 0\nUUID=ABCD-1234 /boot/efi vfat x 0 1\n"},
            "listing": ["@/boot/grub/grub.cfg", f"@/boot/vmlinuz-{K}"]}
        self.assertEqual(codes(s), ["btrfs.subvol_missing"])
        s["filesystems"]["/dev/loop9p2"]["files"]["@/boot/grub/grub.cfg"] = g.replace("@root", "@")
        self.assertEqual(codes(s), [])

    def test_ranking_firmware_first(self):
        s = healthy_uefi()
        s["nvram_text"] = "BootOrder: 0000\n"
        s["filesystems"]["/dev/loop9p2"]["files"]["etc/fstab"] = FSTAB.replace(ROOT, WRONG)
        self.assertEqual(codes(s), ["uefi.no_boot_entry", "fstab.root_uuid_missing"])


class PlanAndCli(unittest.TestCase):
    def test_plan_has_backup_first_and_no_execution(self):
        s = healthy_uefi()
        s["nvram_text"] = "BootOrder: 0000\n"
        txt = plan.render(an.analyze(s), s)
        self.assertIn("Causa probable: [uefi.no_boot_entry]", txt)
        self.assertIn("NO se ejecutó nada", txt)
        self.assertLess(txt.index("dd if=/dev/loop9 of=mbr-y-gpt.bin"), txt.index("efibootmgr --create"))

    def test_plan_healthy(self):
        s = healthy_uefi()
        self.assertIn("no se encontró una causa", plan.render(an.analyze(s), s))

    def test_cli_analyze_offline(self):
        s = healthy_uefi()
        s["filesystems"]["/dev/loop9p2"]["files"]["etc/fstab"] = FSTAB.replace(ROOT, WRONG)
        d = os.path.join(os.getcwd(), ".lab")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "snap.json")
        with open(p, "w") as fh:
            json.dump(s, fh)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["analyze", p, "--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(buf.getvalue())["causes"][0]["code"], "fstab.root_uuid_missing")

    def test_diagnose_requires_root(self):
        if os.geteuid() == 0:
            self.assertTrue(True)  # en CI como root no aplica; el camino root lo cubre lab.measure
            return
        self.assertEqual(cli.main(["diagnose", "--disk", "/dev/null"]), 3)

    def test_snapshot_not_mutated(self):
        s = healthy_uefi()
        before = copy.deepcopy(s)
        an.analyze(s)
        self.assertEqual(s, before)
