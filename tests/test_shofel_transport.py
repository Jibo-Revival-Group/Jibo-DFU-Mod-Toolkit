"""DFU entry through ShofEL and DFU-only partition reads."""
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
import zlib
from unittest.mock import patch

import jibo_dfu as toolkit


def make_gpt_prefix(var_size=toolkit.EXPECTED_VAR_SIZE, names_and_sizes=None):
    names_and_sizes = names_and_sizes or (
        ("rootfsA", 1_048_576_000),
        ("rootfsB", 1_048_576_000),
        ("recovery", 52_428_800),
        ("services", 2_097_152_000),
        ("var", var_size),
        ("skills", 10_991_139_328),
    )
    entries = bytearray(128 * 128)
    first_lba = 34
    for index, (name, size) in enumerate(names_and_sizes):
        count = size // 512
        entry = memoryview(entries)[index * 128:(index + 1) * 128]
        entry[:16] = bytes([index + 1]) * 16
        entry[16:32] = bytes([index + 20]) * 16
        struct.pack_into("<QQQ", entry, 32, first_lba, first_lba + count - 1, 0)
        entry[56:128] = name.encode("utf-16le").ljust(72, b"\x00")
        first_lba += count

    data = bytearray(32 * 1024)
    header = memoryview(data)[512:1024]
    header[:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<I", header, 16, 0)
    struct.pack_into("<I", header, 20, 0)
    struct.pack_into("<QQQQ", header, 24, 1, first_lba, 34, first_lba - 1)
    header[56:72] = b"D" * 16
    struct.pack_into("<QIII", header, 72, 2, 128, 128, zlib.crc32(entries) & 0xFFFFFFFF)
    struct.pack_into("<I", header, 16, zlib.crc32(header[:92]) & 0xFFFFFFFF)
    data[1024:1024 + len(entries)] = entries
    return bytes(data)


class ShofelTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backups = self.root / "Jibo-Backups"
        self.var = b"V" * 4096

    def test_shofel_entry_requires_profile_confirmation_before_usb_access(self):
        with patch.object(toolkit, "_shofel_dfu_tool") as locate, \
                patch.object(toolkit, "devices") as connected:
            with self.assertRaisesRegex(toolkit.DfuError, "Confirm the Meerkat Rev02"):
                toolkit.enter_shofel_dfu(port="1-2")
        locate.assert_not_called()
        connected.assert_not_called()

    def test_shofel_tool_requires_launch_enabled_host(self):
        directory = self.root / "shofel-launch-gate"
        directory.mkdir()
        executable = directory / "shofel2_t124"
        executable.write_bytes(b"host")
        executable.chmod(0o755)
        (directory / "intermezzo.bin").write_bytes(b"intermezzo")
        (directory / "dfu_stage2.bin").write_bytes(b"stage payload")
        with patch.object(toolkit, "run", return_value="dfu-stage-launch=0\n") as capability:
            with self.assertRaisesRegex(toolkit.DfuError, "cannot start"):
                toolkit._shofel_dfu_tool(str(executable))
        capability.assert_called_once_with([str(executable.resolve()), "--dfu-stage-capability"], timeout=5)

    def test_shofel_entry_launches_then_checks_same_port_marker_and_var(self):
        directory = self.root / "shofel-launch"
        directory.mkdir()
        executable = directory / "shofel2_t124"
        executable.write_bytes(b"host")
        image = self.root / "loader.bin"
        image.write_bytes(b"pinned loader")
        states = [[{"port": "1-2", "state": "rcm"}],
                  [{"port": "1-2", "state": "dfu"}]]
        with patch.object(toolkit, "_shofel_dfu_tool", return_value=str(executable)), \
                patch.object(toolkit, "devices", side_effect=states), \
                patch.object(toolkit, "tool", return_value="dfu-util"), \
                patch.object(toolkit, "run_with_progress",
                             return_value="Starting verified ARM-state SPL at 0x80108000.") as transfer, \
                patch.object(toolkit, "dfu_alternatives",
                             return_value=([toolkit.MARKER, "var"], "")) as alternatives:
            result = toolkit.enter_shofel_dfu(
                port="1-2", loader=image, confirm_meerkat_rev02=True)
        argv = transfer.call_args.args[0]
        self.assertEqual(argv, [str(executable), "--usb-port-path", "1-2", "DFU_STAGE",
                                str(image.resolve()), "--confirm-meerkat-rev02", "--launch"])
        self.assertTrue(transfer.call_args.kwargs["byte_progress"])
        self.assertEqual(transfer.call_args.kwargs["cwd"], str(directory))
        alternatives.assert_called_once_with("dfu-util", "1-2")
        self.assertEqual(result["state"], "dfu")
        self.assertTrue(result["loader_verified"])
        self.assertEqual(result["entry_transport"], "ShofEL")

    def test_shofel_entry_requires_live_loader_marker_and_var(self):
        loader = self.root / "loader.bin"
        loader.write_bytes(b"pinned loader")
        with patch.object(toolkit, "_shofel_dfu_tool", return_value="/opt/shofel/shofel2_t124"), \
                patch.object(toolkit, "devices", side_effect=[
                    [{"port": "1-2", "state": "rcm"}], [{"port": "1-2", "state": "dfu"}]]), \
                patch.object(toolkit, "tool", return_value="dfu-util"), \
                patch.object(toolkit, "run_with_progress",
                             return_value="Starting verified ARM-state SPL at 0x80108000."), \
                patch.object(toolkit, "dfu_alternatives", return_value=(["var"], "")):
            with self.assertRaisesRegex(toolkit.DfuError, "missing: jibo-dfu-v1"):
                toolkit.enter_shofel_dfu(loader=loader, confirm_meerkat_rev02=True)

    def test_dfu_gpt_probe_uses_bounded_read_without_saving_a_backup(self):
        with patch.object(toolkit, "_dfu_context",
                          return_value=("1-2", [toolkit.MARKER, "var", "emmc-000"], "device")), \
                patch.object(toolkit.bounded, "read_dfu_alt_prefix",
                             return_value=make_gpt_prefix()[:toolkit.bounded.MARKER_BYTES]) as bounded_read, \
                patch.object(toolkit, "run_with_progress") as upload:
            result = toolkit.probe_dfu_gpt(port="1-2", dfu_util="dfu-util")
        bounded_read.assert_called_once_with("1-2")
        upload.assert_not_called()
        self.assertEqual(result["partition_sizes_bytes"]["var"], toolkit.EXPECTED_VAR_SIZE)
        self.assertEqual(result["bytes_read"], toolkit.bounded.MARKER_BYTES)
        self.assertFalse(result["backup_created"])

    def test_var_backup_reads_named_dfu_alternative_and_records_private_manifest(self):
        listing = ('Found DFU: alt=0, name="jibo-dfu-v1"\n'
                   'Found DFU: alt=1, name="var", size=4096, serial="synthetic-device"\n')
        names = [toolkit.MARKER, "var"]

        def upload(argv, **kwargs):
            self.assertEqual(argv, ["dfu-util", "-d", "0955:701a", "--path", "1-2",
                                    "-a", "var", "-U", argv[-3], "-Z", str(len(self.var))])
            self.assertEqual(kwargs["timeout"], 900)
            self.assertEqual(kwargs["progress_size"], len(self.var))
            Path(argv[-3]).write_bytes(self.var)

        with patch.dict(os.environ, {"JIBO_DFU_TRANSFER": "dfu-util"}), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "dfu"}]), \
                patch.object(toolkit, "dfu_alternatives", return_value=(names, listing)), \
                patch.object(toolkit, "BACKUP_ROOT", self.backups), \
                patch.object(toolkit, "EXPECTED_VAR_SIZE", len(self.var)), \
                patch.object(toolkit, "run_with_progress", side_effect=upload):
            result = toolkit.backup_var(port="1-2", dfu_util="dfu-util")

        image = Path(result["image"])
        manifest_path = Path(result["manifest"])
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(image.read_bytes(), self.var)
        self.assertEqual(image.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)
        self.assertEqual(result["sha256"], hashlib.sha256(self.var).hexdigest())
        self.assertEqual(manifest["transport"], "USB DFU upload")
        self.assertEqual(manifest["partition"], "var")
        self.assertEqual(manifest["size_bytes"], len(self.var))
        self.assertTrue(manifest["device_tag"].startswith("serial-sha256:"))
        self.assertNotIn("synthetic-device", json.dumps(manifest))

    def test_unknown_usb_serial_requires_current_var_before_backup_reuse(self):
        old = self.backups / "old-robot"
        old.mkdir(parents=True)
        (old / "var.img").write_bytes(b"M" * len(self.var))
        old_hash = hashlib.sha256((old / "var.img").read_bytes()).hexdigest()
        (old / "backup-manifest.json").write_text(json.dumps({
            "kind": "jibo-var-backup", "image": "var.img", "sha256": old_hash,
            "device_tag": "serial-sha256:" + hashlib.sha256(b"UNKNOWN").hexdigest()}))
        listing = ('Found DFU: alt=0, name="jibo-dfu-v1"\n'
                   'Found DFU: alt=1, name="var", serial="UNKNOWN"\n')

        def upload(_dfu_util, _port, destination):
            Path(destination).write_bytes(self.var)
            return hashlib.sha256(self.var).hexdigest()

        with patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "dfu"}]), \
                patch.object(toolkit, "dfu_alternatives",
                             return_value=([toolkit.MARKER, "var"], listing)), \
                patch.object(toolkit, "BACKUP_ROOT", self.backups), \
                patch.object(toolkit, "EXPECTED_VAR_SIZE", len(self.var)), \
                patch.object(toolkit, "_upload_var", side_effect=upload) as read_var:
            result = toolkit.backup_var(port="1-2", dfu_util="dfu-util")
            repeat = toolkit.backup_var(port="1-2", dfu_util="dfu-util")

        self.assertEqual(read_var.call_count, 2)
        self.assertEqual(result["status"], "backup complete")
        self.assertNotEqual(Path(result["image"]), old / "var.img")
        self.assertEqual(Path(result["image"]).read_bytes(), self.var)
        self.assertEqual(repeat["image"], result["image"])
        self.assertEqual(len(list(self.backups.glob("var-backup-*/var.img"))), 1)

    def test_var_backup_discards_a_short_dfu_read(self):
        listing = ('Found DFU: alt=0, name="jibo-dfu-v1"\n'
                   'Found DFU: alt=1, name="var", size=4096\n')

        def short_upload(argv, **_kwargs):
            Path(argv[argv.index("-U") + 1]).write_bytes(self.var[:-1])

        with patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "dfu"}]), \
                patch.object(toolkit, "dfu_alternatives",
                             return_value=([toolkit.MARKER, "var"], listing)), \
                patch.object(toolkit, "BACKUP_ROOT", self.backups), \
                patch.object(toolkit, "EXPECTED_VAR_SIZE", len(self.var)), \
                patch.object(toolkit, "run_with_progress", side_effect=short_upload):
            with self.assertRaisesRegex(toolkit.DfuError, "expected 524288000"):
                toolkit.backup_var(port="1-2", dfu_util="dfu-util")

        self.assertEqual(list(self.backups.glob("var-backup-*/var.img")), [])
        manifests = list(self.backups.glob("var-backup-*/backup-manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
