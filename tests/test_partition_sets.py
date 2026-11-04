"""Selected-partition backup and restore checks without USB hardware."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jibo_dfu as j


TAG = "serial-sha256:" + "a" * 64


class PartitionSetTests(unittest.TestCase):
    def test_one_var_backup_remains_valid_after_live_filesystem_uuid_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "var.img"
            with image.open("wb") as stream:
                stream.truncate(j.EXPECTED_VAR_SIZE)
                stream.seek(1024 + 56)
                stream.write(b"\x53\xef")
            manifest = root / "backup-manifest.json"
            manifest.write_text(json.dumps({"kind": "jibo-var-backup",
                                            "device_tag": TAG, "sha256": "saved"}))
            backup = {"image": str(image), "manifest": str(manifest),
                      "sha256": "saved"}
            with patch.object(j, "_sha256_file", return_value="saved"):
                j._verify_var_backup_for_robot(
                    backup, TAG, {"ext4_uuid": "different-live-uuid"})

    def test_available_partitions_excludes_unbounded_or_unexposed_gpt_entries(self):
        layout = {
            "var": {"first_lba": 10, "last_lba": 13, "size_bytes": j.EXPECTED_VAR_SIZE},
            "rootfsA": {"first_lba": 20, "last_lba": 23, "size_bytes": 1024},
            "hidden": {"first_lba": 30, "last_lba": 33, "size_bytes": 1024},
            "oversize": {"first_lba": 40, "last_lba": 43, "size_bytes": 0x80000000},
        }
        with patch.object(j, "_dfu_context", return_value=(
                "1-1", [j.MARKER, "var", "rootfsA", "oversize"], TAG, "")), \
                patch.object(j, "_read_gpt_layout", return_value=layout):
            _, _, available = j.available_backup_partitions("1-1", "dfu-util")
        self.assertEqual(tuple(available), ("var", "rootfsA"))

    def test_backup_set_requests_only_selected_partitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "set"
            layout = {name: {"first_lba": i * 10, "last_lba": i * 10 + 3,
                             "size_bytes": 4, "transfer": "partition"}
                      for i, name in enumerate(("var", "rootfsA", "services"), 1)}
            saved = {"status": "existing verified backup reused",
                     "image": str(Path(temporary) / "var.img"),
                     "sha256": "v" * 64,
                     "manifest": str(Path(temporary) / "var.json")}
            with patch.object(j, "available_backup_partitions", return_value=("1-1", TAG, layout)), \
                    patch.object(j, "backup_var", return_value=saved) as var_backup, \
                    patch.object(j, "_backup_partition_once") as other_backup:
                result = j.backup_partitions(("var",), "1-1", "dfu-util", directory)
            self.assertEqual(result["status"], "backup complete")
            self.assertEqual([item["name"] for item in result["partitions"]], ["var"])
            var_backup.assert_called_once()
            other_backup.assert_not_called()
            self.assertEqual(json.loads((directory / "backup-set.json").read_text())["status"],
                             "complete")

    def _backup_set(self, root):
        entries = []
        layout = {}
        for index, name in enumerate(("rootfsA", "services"), 1):
            directory = root / name
            directory.mkdir()
            image = directory / "partition.img"
            image.write_bytes(name[:4].encode())
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            saved_manifest = directory / "backup-manifest.json"
            saved_manifest.write_text(json.dumps({
                "kind": "jibo-partition-backup", "partition": name,
                "device_tag": TAG, "size_bytes": 4, "sha256": digest,
                "image": image.name}))
            extent = {"first_lba": index * 10, "last_lba": index * 10 + 3,
                      "size_bytes": 4, "transfer": "partition"}
            layout[name] = extent
            entries.append({"name": name, "first_lba": extent["first_lba"],
                            "last_lba": extent["last_lba"], "size_bytes": 4,
                            "image": str(image), "sha256": digest,
                            "backup_manifest": str(saved_manifest)})
        manifest = root / "backup-set.json"
        manifest.write_text(json.dumps({"kind": "jibo-partition-backup-set",
                                        "status": "complete", "device_tag": TAG,
                                        "partitions": entries}))
        return manifest, layout, entries

    def test_restore_checks_selected_images_then_writes_only_selected_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, layout, entries = self._backup_set(root)
            with patch.object(j, "available_backup_partitions", return_value=("1-1", TAG, layout)), \
                    patch.object(j, "run_with_progress") as write, \
                    patch.object(j, "_upload_partition", return_value=entries[0]["sha256"]) as read:
                result = j.restore_partitions(manifest, ("rootfsA",), "1-1", "dfu-util",
                                              root / "operation", True)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["partitions"], ["rootfsA"])
            write.assert_called_once()
            self.assertEqual(read.call_args.args[2], "rootfsA")

    def test_restore_rejects_other_robot_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, layout, _ = self._backup_set(root)
            with patch.object(j, "available_backup_partitions", return_value=(
                    "1-1", "serial-sha256:" + "b" * 64, layout)), \
                    patch.object(j, "run_with_progress") as write:
                with self.assertRaisesRegex(j.DfuError, "different robot"):
                    j.restore_partitions(manifest, ("rootfsA",), "1-1", "dfu-util",
                                         root / "operation", True)
            write.assert_not_called()

    def _flash(self, root, data, confirm=True, backup_status="backup complete"):
        image = root / "custom.img"
        image.write_bytes(data)
        layout = {"rootfsA": {"first_lba": 10, "last_lba": 13, "size_bytes": 1024,
                              "transfer": "partition"}}
        digest = hashlib.sha256(data).hexdigest()
        with patch.object(j, "available_backup_partitions", return_value=("1-1", TAG, layout)), \
                patch.object(j, "backup_partitions", return_value={
                    "status": backup_status, "manifest": str(root / "b.json")}) as backup, \
                patch.object(j, "run_with_progress") as write, \
                patch.object(j, "_upload_partition", return_value=digest) as read:
            result = j.flash_images({"rootfsA": str(image)}, "1-1", "dfu-util",
                                    root / "operation", confirm)
        return result, backup, write, read

    def test_flash_backs_up_then_writes_and_reads_back_only_the_image_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, backup, write, read = self._flash(Path(temporary), b"x" * 512)
            self.assertEqual(result["status"], "verified")
            backup.assert_called_once()
            write.assert_called_once()
            self.assertEqual(read.call_args.args[2:4], ("rootfsA", 512))
            self.assertEqual(read.call_args.args[5], 1024)

    def test_flash_refuses_bad_images_and_cancel_before_any_backup_or_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for data, message in ((b"x" * 1536, "larger than"), (b"x" * 100, "512-byte")):
                with self.assertRaisesRegex(j.DfuError, message):
                    self._flash(root, data)
            result, backup, write, _ = self._flash(root, b"x" * 512, confirm=False)
            self.assertEqual(result["status"], "cancelled")
            backup.assert_not_called()
            write.assert_not_called()
            with self.assertRaisesRegex(j.DfuError, "backup did not finish"):
                self._flash(root, b"x" * 512, backup_status="cancelled")


if __name__ == "__main__":
    unittest.main()
