"""Real Tk Backups screens. Synthetic data only; no device transport."""
import os
import threading
import time
from unittest.mock import patch
import pytest
import jibo_gui_core as core
import jibo_tui as tui
import jibo_dfu as dfu
from test_gui_window import window
from test_saved_backups import offline, capture_set, BackupApi, NAME

pytestmark = pytest.mark.skipif(os.environ.get("JIBO_GUI_TEST_DISPLAY") != "1",
                                reason="real Tk display required")


def text(widget):
    parts = [str(widget.cget("text"))] if "text" in widget.keys() else []
    return "\n".join(parts + [text(c) for c in widget.winfo_children()])


def test_checklist_shows_detail_sizes_and_never_selects_disabled(window):
    w = window
    p = core.Prompt("multi", "Choose partitions to restore", detail="Review before writing.",
                    initial=("var", "missing"), items=(
                        tui.PartitionItem("var", "var — 524 MB", size_bytes=524288000),
                        tui.PartitionItem("missing", "services — 2.1 GB", False,
                                          "File missing", size_bytes=2097152000)))
    w.show_prompt(p)
    w.root.update()
    assert "Review before writing." in text(w.page)
    assert w.controls["chosen"] == {"var"}
    assert "524 MB" in w.controls["selection_summary"].cget("text")
    w.controls["all"](True)
    assert w.controls["chosen"] == {"var"}
    w.controls["toggle"]("missing")
    assert w.controls["chosen"] == {"var"}
    w.controls["primary"].invoke()
    assert p.wait() == ("var",)


@pytest.mark.parametrize("geometry", ["1080x720", "920x640"])
def test_backup_details_leave_actions_reachable(window, geometry):
    w = window
    w.root.geometry(geometry)
    entry = dict(robot_label="Bright-Quiet-River-Fox", robot_name="Bright-Quiet-River-Fox",
                 created_utc="2026-10-17T14:05:00Z", state="complete", device_tag="same",
                 total_bytes=15658000000, folder="~/Jibo-Backups/Bright-Quiet-River-Fox/2026-10-17",
                 partitions=[dict(name=n, size_bytes=524288000, present=True) for n in
                             ("rootfsA", "rootfsB", "recovery", "services", "var", "skills")])
    p = core.Prompt("select", "Backup of Bright-Quiet-River-Fox",
                    detail="\n".join(tui.backup_detail_lines(entry, "same")),
                    items=tui._normalise_options((("restore", "Restore from this backup"),
                                                 ("open-folder", "Open folder"),
                                                 ("check-files", "Check files"),
                                                 ("delete", "Delete this backup"))))
    w.show_prompt(p)
    w.root.update()
    area = w._scroll_areas[-1]
    for row in w.controls["options"].values():
        area.show(row)
        w.root.update()
        assert row.winfo_height() >= row.winfo_reqheight()
        assert row.winfo_rooty() >= area.canvas.winfo_rooty()
        assert row.winfo_rooty() + row.winfo_height() <= area.canvas.winfo_rooty() + area.canvas.winfo_height()
    for key in ("primary", "cancel"):
        b = w.controls[key]
        assert b.winfo_height() >= b.winfo_reqheight()
        assert b.winfo_rooty() + b.winfo_height() <= w.root.winfo_rooty() + w.root.winfo_height()


def test_real_window_drives_backend_backup_and_cancel_default_restore(offline):
    import jibo_gui as gui
    api = BackupApi(offline.root)
    c = core.GuiController(api)
    root = gui.create_root()
    w = gui.ToolkitWindow(root, c)
    errors = []
    gate = threading.Event()
    entered = threading.Event()
    root.report_callback_exception = lambda *a: errors.append(a)
    def pump(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            root.update()
            assert not errors
            if predicate(): return
            time.sleep(0.01)
        raise AssertionError("Timed out on " + w.page_name)
    def choose(key):
        previous = w.prompt
        w.controls["choose"](key)
        w.controls["primary"].invoke()
        pump(lambda: w.prompt is not None and w.prompt is not previous)
    try:
        with core.capture_output(c):
            pump(lambda: c.snapshot().checked)
            w.render_state()
            assert list(w.action_rows) == ["enter-dfu-shofel", "set-mode-wifi", "flash-update", "backups", "more-tools"]
            w.action_rows["backups"].activate()
            pump(lambda: w.prompt is not None)
            choose("backup-choose")
            assert w.prompt.kind == "multi"
            w.controls["all"](False)
            w.controls["toggle"]("services")
            w.controls["primary"].invoke()
            pump(lambda: w.prompt is not None and w.prompt.kind == "select")
            w.controls["choose"]("start")
            w.controls["primary"].invoke()
            pump(lambda: not c.busy and w.page_name == "result")
            assert offline.uploads == ["services"]
            assert len(api.saved_backups()) == 1
            w.show_home()
            w.open_action("backups")
            pump(lambda: w.prompt is not None)
            choose("saved-backups")
            choose("set:0")
            choose("restore")
            assert w.prompt.kind == "multi"
            w.controls["primary"].invoke()
            pump(lambda: w.prompt is not None and w.prompt.kind == "confirm")
            assert w.prompt.initial == "cancel"
            assert root.focus_get() is w.controls["cancel"]
            assert "services (4096 bytes)" in w.controls["review"].get()
            w.controls["cancel"].invoke()
            pump(lambda: not c.busy and w.page_name == "result")
            assert not offline.forbidden and len(api.saved_backups()) == 1
            original = dfu._upload_var
            def held_read(*args, **kw):
                entered.set()
                assert gate.wait(10), "UI did not release the offline read"
                return original(*args, **kw)
            offline.uploads.clear()
            with patch.object(dfu, "_upload_var", held_read):
                w.show_home()
                w.open_action("backups")
                pump(lambda: w.prompt is not None)
                choose("backup-all")
                w.controls["choose"]("start")
                w.controls["primary"].invoke()
                pump(lambda: entered.is_set() and w.progress_cancel.winfo_viewable())
                assert w.progress_cancel.cget("text") == "Stop after this partition"
                w.progress_cancel.invoke()
                assert not c.shutdown_if_safe(), "Closing must never cut a USB read"
                gate.set()
                pump(lambda: not c.busy and w.page_name == "result")
            assert offline.uploads == ["var"]
            assert any(e["state"] == "incomplete" for e in api.saved_backups())
    finally:
        gate.set()
        c.shutdown()
        if not w.closed: w.close()


def test_real_window_flash_whole_disk_image_lists_its_partitions_and_cancel_writes_nothing(offline, tmp_path):
    import jibo_gui as gui
    from test_partition_sets import make_disk
    image, layout = make_disk(tmp_path)
    live = {name: layout[name] for name in ("rootfsA", "services", "var")}
    live["var"] = dict(live["var"], first_lba=live["var"]["first_lba"] + 8)  # moved on this robot
    offline.layout.clear()
    offline.layout.update(live)
    api = BackupApi(offline.root)
    c = core.GuiController(api)
    root = gui.create_root()
    w = gui.ToolkitWindow(root, c)
    errors = []
    root.report_callback_exception = lambda *a: errors.append(a)
    def pump(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            root.update()
            assert not errors
            if predicate(): return
            time.sleep(0.01)
        raise AssertionError("Timed out on " + w.page_name)
    try:
        with core.capture_output(c):
            pump(lambda: c.snapshot().checked)
            w.render_state()
            w.action_rows["backups"].activate()
            pump(lambda: w.prompt is not None)
            previous = w.prompt
            w.controls["choose"]("flash-images")
            w.controls["primary"].invoke()
            pump(lambda: w.prompt is not None and w.prompt is not previous and w.prompt.kind == "text")
            assert ".bin" in w.prompt.prompt
            w.controls["value"].set(str(image))
            w.controls["primary"].invoke()
            pump(lambda: w.prompt is not None and w.prompt.kind == "multi")
            assert w.controls["chosen"] == {"rootfsA", "services"}  # var moved, others not on USB
            w.controls["toggle"]("rootfsA")
            w.controls["primary"].invoke()
            pump(lambda: w.prompt is not None and w.prompt.kind == "confirm")
            assert w.prompt.initial == "cancel"
            assert root.focus_get() is w.controls["cancel"]
            review = w.controls["review"].get()
            assert "services (8192 bytes)" in review and "rootfsA" not in review
            assert "{} at byte {}".format(image, layout["services"]["first_lba"] * 512) in review
            w.controls["cancel"].invoke()
            pump(lambda: not c.busy and w.page_name == "result")
            assert offline.uploads == [] and not offline.forbidden
            assert api.saved_backups() == []
    finally:
        c.shutdown()
        if not w.closed: w.close()
