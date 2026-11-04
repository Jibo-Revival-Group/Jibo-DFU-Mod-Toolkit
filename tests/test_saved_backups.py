"""Offline partition-set backend and shared menu acceptance.

Only the injected upload/write boundary handles bytes. Real USB discovery, DFU
commands and sockets are fail-closed; ext4 name extraction uses real debugfs.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import jibo_dfu as dfu
import jibo_gui_core as core
import jibo_tui as tui
from calibration_support import make_image
from gui_fakes import FakeApi

TAG = "serial-sha256:" + "a" * 64
OTHER_TAG = "serial-sha256:" + "b" * 64
NAME = "Bright-Quiet-River-Fox"


@pytest.fixture
def offline(tmp_path, monkeypatch):
    forbidden = []
    def deny(*args, **kwargs):
        forbidden.append((args, kwargs))
        raise AssertionError("Real transport forbidden in offline backup tests")
    monkeypatch.setattr(socket, "socket", deny)
    for name in ("devices", "run", "run_with_progress", "_partition_transfer_argv"):
        monkeypatch.setattr(dfu, name, deny)
    root = tmp_path / "Jibo-Backups"
    root.mkdir()
    var = make_image(tmp_path, "source-var.img", {
        "/jibo/identity.json": json.dumps({"name": NAME}).encode(),
        "/etc/hostname": b"Wrong-Name-Not-Used"})
    monkeypatch.setattr(dfu, "BACKUP_ROOT", root)
    monkeypatch.setattr(dfu, "EXPECTED_VAR_SIZE", var.stat().st_size)
    # Preserve existing calibration code: no event/session/network in these fixtures.
    monkeypatch.setattr(dfu, "_calibration_capture_identity", lambda *a: None)
    monkeypatch.setattr(dfu, "_calibration_backup_event", lambda *a: None)
    layout = {n: dict(first_lba=i*100, last_lba=i*100+10,
                      size_bytes=var.stat().st_size if n == "var" else 4096,
                      transfer="partition") for i, n in enumerate(("rootfsA", "var", "services"), 1)}
    current = {"tag": TAG}
    monkeypatch.setattr(dfu, "available_backup_partitions", lambda *a, **k: ("offline", current["tag"], layout))
    monkeypatch.setattr(dfu, "_dfu_context", lambda *a, **k: ("offline", [dfu.MARKER, "var"], current["tag"]))
    uploads, writes = [], []
    def upload(exe, port, name, size, destination, *a):
        uploads.append(name)
        if name == "var":
            shutil.copyfile(var, destination)
        else:
            Path(destination).write_bytes((name.encode()*size)[:size])
        return dfu._sha256_file(destination)
    monkeypatch.setattr(dfu, "_upload_partition", upload)
    monkeypatch.setattr(dfu, "_upload_var", lambda exe, port, dest: upload(exe, port, "var", var.stat().st_size, dest))
    yield SimpleNamespace(root=root, var=var, current=current, layout=layout,
                          uploads=uploads, writes=writes, forbidden=forbidden, deny=deny)
    assert not forbidden, "A real transport boundary was entered"


def capture_set(fixture, selected=None):
    return dfu.backup_partitions(selected or tuple(fixture.layout), "offline", "fixture-dfu")


def test_fresh_named_set_is_self_contained_and_repeats_reads(offline):
    first = capture_set(offline)
    second = capture_set(offline)
    assert first["manifest"] != second["manifest"]
    assert offline.uploads == ["var", "rootfsA", "services"] * 2
    for result in (first, second):
        folder = Path(result["operation_directory"])
        assert folder.parent.name == NAME
        record = json.loads(Path(result["manifest"]).read_text())
        assert record["device_tag"] == TAG and record["status"] == "complete"
        assert record["every_partition"] is True
        for entry in record["partitions"]:
            assert folder in Path(entry["image"]).parents
            assert Path(entry["backup_manifest"]).is_file()
            assert dfu._sha256_file(entry["image"]) == entry["sha256"]
        assert dfu.check_backup_set_files(result["manifest"])["all_match"]


def test_chosen_backup_reads_only_chosen_and_uses_tag_fallback(offline):
    result = capture_set(offline, ("services",))
    assert offline.uploads == ["services"]
    assert result["robot_label"] == "robot-aaaaaaaa"
    assert not result["robot_name"]


def test_listing_old_and_new_layout_does_not_move_or_rewrite(offline):
    fresh = capture_set(offline)
    old = offline.root / "backup-set-legacy"
    old.mkdir()
    record = json.loads(Path(fresh["manifest"]).read_text())
    record.pop("robot_name")
    record["created_utc"] = "2020-01-01T00:00:00Z"
    path = old / "backup-set.json"
    path.write_text(json.dumps(record))
    before = {str(p): p.read_bytes() for p in offline.root.rglob("*.json")}
    entries = dfu.saved_backups()
    assert [e["layout"] for e in entries] == ["named", "older"]
    assert all(e["robot_label"] == NAME for e in entries)
    assert entries[0]["total_bytes"] == sum(e["size_bytes"] for e in record["partitions"])
    assert before == {str(p): p.read_bytes() for p in offline.root.rglob("*.json")}


def test_legacy_rollback_image_is_listed_without_move_or_unsafe_restore(offline):
    old = dfu.backup_var("offline", "fixture-dfu")
    path = Path(old["manifest"])
    before = path.read_bytes()
    entry, = dfu.saved_backups()
    assert entry["layout"] == "rollback" and entry["robot_label"] == NAME
    assert not entry["restorable"] and "partition backup set" in entry["restore_reason"]
    assert dfu.check_backup_set_files(path)["all_match"]
    assert path.read_bytes() == before and Path(old["image"]).is_file()
    result, prompts = run_flow(BackupApi(offline.root), ("saved-backups", "set:0"))
    detail = next(p for p in prompts if p.title.startswith("Backup of "))
    assert not next(i for i in detail.items if i.key == "restore").enabled
    assert not next(i for i in detail.items if i.key == "delete").enabled


@pytest.mark.parametrize("fault,message", [
    ("robot", "different robot"), ("extent", "GPT extent"), ("hash", "SHA-256"),
    ("partition-manifest", "backup manifest"), ("symlink", "missing or has the wrong size"),
    ("incomplete", "incomplete"), ("duplicate", "duplicate")])
def test_restore_guards_refuse_before_review_and_write(offline, fault, message):
    result = capture_set(offline)
    path = Path(result["manifest"])
    record = json.loads(path.read_text())
    entry = next(e for e in record["partitions"] if e["name"] == "services")
    if fault == "robot": offline.current["tag"] = OTHER_TAG
    if fault == "extent": offline.layout["services"]["first_lba"] += 1
    if fault == "hash": Path(entry["image"]).write_bytes(b"X" * entry["size_bytes"])
    if fault == "partition-manifest": Path(entry["backup_manifest"]).write_text("{}")
    if fault == "symlink":
        image = Path(entry["image"])
        image.unlink()
        image.symlink_to(offline.var)
    if fault == "incomplete": record["status"] = "failed"
    if fault == "duplicate": record["partitions"].append(entry)
    path.write_text(json.dumps(record))
    reviews = []
    with pytest.raises(dfu.DfuError, match=message):
        dfu.restore_partitions(path, ("services",), "offline", "fixture-dfu",
                               confirmation=lambda p: reviews.append(p) or True)
    assert not reviews and not offline.forbidden


def test_restore_cancel_has_no_write_and_positive_exact_partition_readback(offline, monkeypatch):
    result = capture_set(offline)
    reviews = []
    def review(plan):
        reviews.append(plan)
        return False
    assert dfu.restore_partitions(result["manifest"], ("services",), "offline", "fixture-dfu",
                                   confirmation=review)["status"] == "cancelled"
    assert reviews[0]["backup_robot"] == NAME
    assert reviews[0]["partitions"] == [dict(name="services", bytes=4096)]
    writes = []
    monkeypatch.setattr(dfu, "_partition_transfer_argv", lambda exe, port, name, direction, image: [name, direction, image])
    monkeypatch.setattr(dfu, "run_with_progress", lambda argv, **k: writes.append(argv[:2]))
    offline.uploads.clear()
    restored = dfu.restore_partitions(result["manifest"], ("services",), "offline", "fixture-dfu",
                                      confirmation=lambda p: True)
    assert restored["status"] == "verified"
    assert writes == [["services", "-D"]] and offline.uploads == ["services"]


def test_check_files_detects_same_size_corruption(offline):
    result = capture_set(offline, ("services",))
    image = Path(result["partitions"][0]["image"])
    image.write_bytes(b"X" * image.stat().st_size)
    checked = dfu.check_backup_set_files(result["manifest"])
    assert checked["partitions"] == [dict(name="services", status="mismatch")]
    assert not checked["all_match"]


def test_delete_only_selected_set_and_refuses_held_or_legacy(offline):
    one, two = capture_set(offline), capture_set(offline)
    with dfu._backup_set_lock(one["manifest"]):
        with pytest.raises(dfu.DfuError, match="in use"):
            dfu.delete_backup_set(one["manifest"])
    assert dfu.delete_backup_set(one["manifest"])["status"] == "deleted"
    assert not Path(one["operation_directory"]).exists()
    assert dfu.check_backup_set_files(two["manifest"])["all_match"]
    old = offline.root / "backup-set-old"
    old.mkdir()
    (old / "backup-set.json").write_text(Path(two["manifest"]).read_text())
    with pytest.raises(dfu.DfuError, match="Only backups"):
        dfu.delete_backup_set(old)
    assert old.is_dir()


def test_delete_keeps_parent_folder_and_unrelated_files(offline):
    one = capture_set(offline, ("services",))
    folder = Path(one["operation_directory"])
    parent = folder.parent
    sentinel = offline.root / "not-a-backup.txt"
    sentinel.write_text("keep")
    dfu.delete_backup_set(one["manifest"])
    assert parent.is_dir()
    assert sentinel.read_text() == "keep"


def test_delete_refuses_if_host_has_no_file_lock_support(offline, monkeypatch):
    import builtins
    saved = capture_set(offline, ("services",))
    original = builtins.__import__
    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl": raise ImportError("Offline unsupported host")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    with pytest.raises(dfu.DfuError, match="locking is unavailable"):
        dfu.delete_backup_set(saved["manifest"])
    assert Path(saved["manifest"]).is_file()


def test_delete_refuses_unrecorded_files_inside_selected_backup(offline):
    saved = capture_set(offline, ("services",))
    extra = Path(saved["operation_directory"]) / "owners-notes.txt"
    extra.write_text("Keep this unrelated file")
    with pytest.raises(dfu.DfuError, match="unrecorded files"):
        dfu.delete_backup_set(saved["manifest"])
    assert extra.read_text() == "Keep this unrelated file"
    assert Path(saved["manifest"]).is_file()


def test_delete_refuses_symlinked_backup_root(offline, monkeypatch):
    saved = capture_set(offline, ("services",))
    alias = offline.root.parent / "linked-backups"
    alias.symlink_to(offline.root, target_is_directory=True)
    monkeypatch.setattr(dfu, "BACKUP_ROOT", alias)
    manifest = alias / Path(saved["manifest"]).relative_to(offline.root)
    with pytest.raises(dfu.DfuError, match="Only backups"):
        dfu.delete_backup_set(manifest)
    assert Path(saved["manifest"]).is_file()


def test_delete_refuses_shared_file_alias_and_outside_files(offline):
    one, two = capture_set(offline), capture_set(offline)
    other = Path(two["manifest"])
    record = json.loads(other.read_text())
    image = Path(one["partitions"][0]["image"])
    record["partitions"][0]["image"] = str(image.parent / ".." / image.parent.name / image.name)
    other.write_text(json.dumps(record))
    with pytest.raises(dfu.DfuError, match="Another saved backup"):
        dfu.delete_backup_set(one["manifest"])
    with pytest.raises(dfu.DfuError, match="outside its own folder"):
        dfu.delete_backup_set(two["manifest"])
    assert Path(one["manifest"]).is_file() and other.is_file()


def test_low_disk_refuses_before_partition_upload(offline, monkeypatch):
    monkeypatch.setattr(dfu.shutil, "disk_usage", lambda p: SimpleNamespace(free=0))
    with pytest.raises(dfu.DfuError, match="Not enough free space"):
        capture_set(offline)
    assert not offline.uploads


def test_backup_cancel_waits_for_current_partition_keeps_partial_set(offline, monkeypatch):
    stopped = [False]
    original = dfu._upload_var
    def upload(*args, **kw):
        result = original(*args, **kw)
        stopped[0] = True
        return result
    monkeypatch.setattr(dfu, "_upload_var", upload)
    result = dfu.backup_partitions(tuple(offline.layout), "offline", "fixture-dfu",
                                   cancel=lambda: stopped[0])
    assert result["status"] == "cancelled"
    assert offline.uploads == ["var"]
    entry, = dfu.saved_backups()
    assert not entry["restorable"] and entry["state"] == "incomplete"
    assert Path(entry["partitions"][0]["image"]).is_file()


def test_backup_work_can_cancel_but_cannot_close_active_usb():
    token = tui.BackupReadCancel()
    c = core.GuiController(FakeApi())
    c._busy = True
    c._work_cancel = token
    assert c.work_active and not c.download_active
    assert not c.shutdown_if_safe()
    assert c.cancel_download() and token.cancelled()
    assert "partition" in c.work_cancel_label.lower()
    c._busy = False
    c.shutdown()


def test_failed_backup_is_listed_incomplete_and_keeps_saved_files(offline, monkeypatch):
    original = dfu._upload_partition
    def interrupted(exe, port, name, *args):
        if name == "services": raise dfu.DfuError("Offline interrupted read")
        return original(exe, port, name, *args)
    monkeypatch.setattr(dfu, "_upload_partition", interrupted)
    with pytest.raises(dfu.DfuError, match="interrupted read"):
        capture_set(offline)
    entry, = dfu.saved_backups()
    assert entry["state"] == "incomplete" and not entry["restorable"]
    assert [p["name"] for p in entry["partitions"]] == ["var", "rootfsA"]
    assert all(Path(p["image"]).exists() for p in entry["partitions"])


def test_menu_order_and_offline_backup_management():
    readiness = tui.Readiness("none", ())
    assert [i.key for i in tui.build_menu_items(readiness)] == [
        "enter-dfu-shofel", "set-mode-wifi", "flash-update", "backups", "more-tools"]
    options = {i.key: i for i in tui._backup_menu_options(readiness)}
    assert options["saved-backups"].enabled
    assert all(not options[k].enabled for k in ("backup-all", "backup-choose", "restore-backup"))


class BackupApi(FakeApi):
    """UI adapter only; all local file handling stays in the real backend."""
    def __init__(self, root, state="dfu"):
        super().__init__(state)
        self.BACKUP_ROOT = root
    def available_backup_partitions(self, **kwargs): return dfu.available_backup_partitions(**kwargs)
    def backup_free_bytes(self): return dfu.backup_free_bytes()
    def backup_partitions(self, selected, **kwargs): return dfu.backup_partitions(selected, dfu_util="fixture-dfu", **kwargs)
    def saved_backups(self): return dfu.saved_backups()
    def describe_backup_set(self, *a): return dfu.describe_backup_set(*a)
    def check_backup_set_files(self, *a): return dfu.check_backup_set_files(*a)
    def delete_backup_set(self, *a): return dfu.delete_backup_set(*a)
    def restore_partitions(self, *a, **kw): return dfu.restore_partitions(*a, dfu_util="fixture-dfu", **kw)
    def flash_images(self, *a, **kw): return dfu.flash_images(*a, dfu_util="fixture-dfu", **kw)
    def open_folder(self, path):
        self.calls.append(("open_folder", path))
        return True  # Never launch the real desktop in acceptance tests.


def run_flow(api, answers):
    prompts, remaining = [], iter(answers)
    def ask(p):
        prompts.append(p)
        return next(remaining, None)
    c = core.GuiController(api, runner=core.sync_runner, prompter=ask)
    c.request_refresh()
    assert c.start_action("backups")
    outcome = [e[1] for e in c.drain() if e[0] == "finished"][-1]
    return outcome, prompts


def test_controller_backup_all_and_chosen_use_real_backend(offline):
    api = BackupApi(offline.root)
    result, prompts = run_flow(api, ("backup-all", "start"))
    assert result.status == "backup complete" and NAME in result.lines[0]
    assert set(offline.uploads) == set(offline.layout)
    offline.uploads.clear()
    result, prompts = run_flow(api, ("backup-choose", ("services",), "start"))
    assert result.status == "backup complete" and offline.uploads == ["services"]
    assert prompts[1].kind == "multi" and all(i.size_bytes > 0 for i in prompts[1].items)


@pytest.mark.parametrize("answer", [None, "cancel", "confirm"])
def test_manager_delete_is_explicit_cancel_default(offline, answer):
    saved = capture_set(offline)
    result, prompts = run_flow(BackupApi(offline.root, "none"),
                               ("saved-backups", "set:0", "delete", answer))
    confirmation = next(p for p in prompts if p.kind == "confirm")
    assert confirmation.initial == "cancel" and NAME in confirmation.detail
    assert Path(saved["manifest"]).exists() == (answer != "confirm")
    assert result.status == ("deleted" if answer == "confirm" else "cancelled")


def test_manager_wrong_robot_restore_disabled_and_backend_still_rejects(offline):
    capture_set(offline)
    offline.current["tag"] = OTHER_TAG
    result, prompts = run_flow(BackupApi(offline.root), ("saved-backups", "set:0", "restore"))
    details = next(p for p in prompts if p.title.startswith("Backup of "))
    restore = next(i for i in details.items if i.key == "restore")
    assert not restore.enabled and "different robot" in restore.reason
    assert not offline.forbidden and result.status == "cancelled"


def test_controller_restore_review_can_cancel_real_backend(offline):
    capture_set(offline)
    result, prompts = run_flow(BackupApi(offline.root), ("restore-backup", "set:0", ("services",), None))
    assert result.status == "cancelled"
    assert prompts[-1].kind == "confirm" and prompts[-1].initial == "cancel"
    assert NAME in prompts[-1].detail and "services (4096 bytes)" in prompts[-1].detail
    assert not offline.forbidden


def test_tui_backup_menu_runs_real_backend_with_only_selected_partition(offline):
    api = BackupApi(offline.root)
    answers = iter(("backup-choose", "start"))
    with patch.object(tui, "select_option", side_effect=lambda *a, **k: next(answers)), \
            patch.object(tui, "select_multiple_options", return_value=("services",)), \
            patch.object(tui, "_terminal_available", return_value=False):
        result = tui.execute_action(api, "backups", tui.inspect_readiness(api))
    assert result["status"] == "backup complete"
    assert offline.uploads == ["services"]
    assert dfu.check_backup_set_files(result["manifest"])["all_match"]
