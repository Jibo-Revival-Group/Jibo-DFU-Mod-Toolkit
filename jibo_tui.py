"""Stateful curses interface for the Jibo DFU toolkit.

This module deliberately receives the command module as an argument. It does
not import jibo_dfu, which keeps the CLI and the terminal interface acyclic.
"""

import curses
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import re
import sys
import textwrap
import threading
import time

import jibo_flash_policy as flash_policy
import jibo_calibration_client as calibration_service


@dataclass(frozen=True)
class Readiness:
    state: str
    devices: tuple
    port: str = ""
    marker_present: bool = False
    alt_names: tuple = ()
    detail: str = ""
    shofel_dfu_available: bool = False


@dataclass(frozen=True)
class MenuItem:
    key: str
    label: str
    enabled: bool = True
    reason: str = ""
    # Presentation only: "recommended", "group" (expand/collapse) or "status".
    # Text always carries the meaning too, so no-color terminals lose nothing.
    tone: str = ""


FILE_LEVEL_VAR_ALTS = frozenset(("jibo-file-v1", "jibo-file-var-in",
                                 "jibo-file-var-out"))


@dataclass(frozen=True)
class BackupItem(MenuItem):
    """One saved backup in a list; the parts let a window lay the row out in columns."""
    robot: str = ""
    saved: str = ""
    partitions: str = ""
    size: str = ""
    state: str = ""          # complete, missing-files, incomplete or unreadable
    this_robot: bool = False


def _can_edit_var_files(readiness):
    return (readiness.state == "dfu-ready" and
            FILE_LEVEL_VAR_ALTS.issubset(readiness.alt_names))


def _needs_full_var_fallback(api, error):
    status_error = getattr(api, "FileRpcStatusError", None)
    return (status_error is not None and isinstance(error, status_error) and
            error.status in (3, 4))


def _offer_full_var_edit(error, includes_wifi=False):
    if getattr(error, "status", 3) == 4:
        prompt = "The ext4 journal needs recovery, so the RAM loader refused this file request."
        detail = ("A clean Linux unmount can clear the journal. The full transfer reads and "
                  "writes 500 MiB of var after replaying the journal on a local copy.")
    else:
        prompt = "This file's ext4 layout cannot be edited by the current RAM loader."
        detail = "The full transfer reads and writes 500 MiB of var, then checks the result."
        if includes_wifi:
            detail = ("Stock RTM Wi-Fi and network startup files can span multiple blocks; "
                      "the quick writer handles one. "
                      + detail)
    return select_option(
        "Fast file edit unavailable",
        (("cancel", "Cancel without changing the robot"),
         ("full", "Use full var transfer (slower)")),
        prompt=prompt, detail=detail, initial="cancel") == "full"


def inspect_readiness(api, found=None):
    """Read USB state and, for DFU, check for the toolkit's loader marker."""
    found = tuple(api.devices()) if found is None else tuple(found)
    if not found:
        return Readiness("none", found)
    if len(found) != 1:
        ports = ", ".join(device.get("port", "?") for device in found)
        return Readiness("multiple", found, detail="Recovery devices found on ports " + ports)

    device = found[0]
    port = device.get("port", "")
    if device.get("state") == "rcm":
        dfu_available = False
        try:
            dfu_available = bool(api.shofel_dfu_available())
        except Exception:
            pass
        return Readiness("rcm", found, port=port,
                         shofel_dfu_available=dfu_available)
    try:
        executable = api.tool("dfu-util")
        names, output = api.dfu_alternatives(executable, port)
    except Exception as exc:
        return Readiness("dfu-error", found, port=port, detail=str(exc))
    names = tuple(names)
    if "Cannot open DFU device" in output or "Permission denied" in output:
        return Readiness("dfu-error", found, port=port,
                         detail="USB access is unavailable. Relaunch the toolkit with sudo.")
    if not names:
        return Readiness("dfu-error", found, port=port,
                         detail="The DFU alternatives could not be read. Check USB access and refresh.")
    if api.MARKER not in names:
        return Readiness("dfu-no-marker", found, port=port, alt_names=names,
                         detail="Jibo recovery loader marker not found")
    if "var" not in names:
        return Readiness("dfu-profile-incomplete", found, port=port,
                         marker_present=True, alt_names=names,
                         detail="The Jibo recovery loader does not expose the var partition")
    return Readiness("dfu-ready", found, port=port, marker_present=True, alt_names=names)


def status_lines(readiness):
    """Return short, distinct RCM-to-DFU status text for the screen."""
    if readiness.state == "none":
        return ("STEP 1 / 3   Connect the robot over USB",
                "No Jibo recovery device is visible. Put Jibo in RCM mode and connect him by USB.")
    if readiness.state == "multiple":
        return ("USB STATE   Multiple recovery devices detected",
                (readiness.detail or "Connect one robot at a time.") + ".")
    if readiness.state == "rcm":
        return ("STEP 2 / 3   RCM/APX detected",
                "RCM/APX is the entry state. Choose ShofEL to load DFU into RAM.")
    if readiness.state == "dfu-ready":
        return ("STEP 3 / 3   DFU active; Jibo recovery loader ready",
                ("Quick file changes are available." if _can_edit_var_files(readiness) else
                 "The robot is ready for partition and update actions."))
    if readiness.state == "dfu-error":
        return ("USB STATE   DFU detected; loader status unavailable",
                "DFU is not the same as RCM/APX. " + (readiness.detail or "Check USB access."))
    if readiness.state == "dfu-profile-incomplete":
        return ("USB STATE   DFU active; var partition unavailable",
                "The Jibo recovery loader is active, but it does not expose var. Partition actions are disabled.")
    return ("USB STATE   DFU detected; Jibo recovery loader not found",
            "DFU is not the same as RCM/APX. Restart into RCM/APX, then load the matching recovery.")


def _connection_reason(readiness):
    """Why robot actions are unavailable in this USB state ('' when DFU is ready)."""
    if readiness.state == "none":
        return "Connect the robot and enter RCM/APX first."
    if readiness.state == "multiple":
        return "Connect one robot at a time."
    if readiness.state == "rcm":
        return "Enter DFU from RCM/APX first."
    if readiness.state == "dfu-error":
        return "DFU was detected, but its Jibo loader could not be checked."
    if readiness.state == "dfu-profile-incomplete":
        return readiness.detail or "The Jibo recovery loader does not expose var."
    if readiness.state == "dfu-no-marker":
        return "The Jibo recovery loader marker is missing."
    return ""


def build_menu_items(readiness, update_packages=(), hosted_available=False):
    """Build actions with disabled reasons instead of hiding their requirements.

    ``hosted_available`` means the toolkit can offer Phoenix images from jibo.io;
    a chosen image is downloaded and checked before the usual reviewed flash, so
    an empty ./updates folder no longer disables the update action.
    """
    is_rcm = readiness.state == "rcm"
    is_dfu = readiness.state == "dfu-ready"
    connection_reason = _connection_reason(readiness)

    if readiness.state == "rcm":
        shofel_dfu_reason = ("" if readiness.shofel_dfu_available else
                             "The DFU entry helper is unavailable. Run the launcher to build it.")
    elif readiness.state == "multiple":
        shofel_dfu_reason = "Connect one robot at a time."
    elif readiness.state in ("dfu-ready", "dfu-error", "dfu-no-marker",
                              "dfu-profile-incomplete"):
        shofel_dfu_reason = "The robot is already in DFU."
    else:
        shofel_dfu_reason = "Connect the robot and enter RCM/APX first."

    packages = tuple(update_packages)
    update_required = ("rootfsA", "rootfsB", "services", "emmc-000")
    missing_update = [name for name in update_required if name not in readiness.alt_names]
    has_skills = ("skills" in readiness.alt_names or any(
        re.fullmatch(r"skills-\d{3}", name) for name in readiness.alt_names))
    if not has_skills:
        missing_update.append("skills")
    if not is_dfu:
        update_reason = connection_reason
    elif missing_update:
        if "skills" in missing_update:
            update_reason = (
                "This DFU loader cannot write skills. Load a compatible DFU loader to install updates.")
            other_missing = [name for name in missing_update if name != "skills"]
            if other_missing:
                update_reason += " It also does not expose: " + ", ".join(other_missing) + "."
        else:
            update_reason = "The recovery loader does not expose: " + ", ".join(missing_update) + "."
    elif not packages and not hosted_available:
        update_reason = "No packages found in ./updates."
    else:
        update_reason = ""
    quick = " (quick edit)" if _can_edit_var_files(readiness) else ""
    # Owner order: enter DFU, then the combined setup, then installing an image.
    # The separate mode and Wi-Fi edits live in More tools; var backups in Backups.
    items = [
        MenuItem("enter-dfu-shofel", "Enter DFU with ShofEL (RAM loader)",
                 is_rcm and readiness.shofel_dfu_available, shofel_dfu_reason),
        MenuItem("set-mode-wifi", "Set mode and configure Wi-Fi" + quick,
                 is_dfu, connection_reason),
        MenuItem("flash-update", UPDATE_ACTION_LABEL,
                 is_dfu and not missing_update and (bool(packages) or hosted_available),
                 update_reason),
        MenuItem("backups", BACKUPS_MENU_LABEL),
        MenuItem("more-tools", MORE_TOOLS_LABEL),
    ]
    return tuple(items)


BACKUPS_MENU_LABEL = "Backups"
MORE_TOOLS_LABEL = "More tools and local images"

# One how-to for every surface: the README section, the GUI help page and the
# text menu's h screen use these exact sentences (tests compare them).
RCM_HELP_LINK = "How do I put Jibo in RCM mode?"
RCM_HELP_TITLE = "Put Jibo in RCM mode"
RCM_HELP_INTRO = "The toolkit starts from RCM mode, which it shows as RCM/APX."
RCM_HELP_STEPS = (
    "1. Fully shut Jibo down.",
    "2. Open the hatch on the back of his head to reach the round button panel in the photo.",
    "3. Connect the USB cable from your computer to Jibo's micro-USB port under the same hatch.",
    "4. Press and hold the RCM button, the lower of the two small buttons circled in the "
    "photo. Keep holding it while you turn Jibo back on with his power button, then let go "
    "of the RCM button.",
    "5. In RCM mode, the red light on his forehead stays on and you don't hear his startup "
    "chime. If he chimes or starts normally, shut him down and try again.",
    "6. The toolkit then shows RCM/APX detected. Choose Enter DFU with ShofEL (RAM loader).",
)
RCM_HELP_CAPTION = ("The round button panel on the back of Jibo's head, under the hatch. "
                    "RESET is the upper small button; RCM is the lower one.")
RCM_HELP_ALT = ("Jibo's round button panel with two small buttons circled in red: "
                "RESET (upper) and RCM (lower)")
RCM_HELP_IMAGE = "assets/help/rcm-buttons.png"
RCM_HELP_IMAGE_SHA256 = "52a31b30dc04c1b5432facf211697f27ca604fbcfdbb2b26f9bfcf80111b4fe9"


def rcm_help_lines():
    """The text menu has no photo; it points to the README for the picture."""
    return (RCM_HELP_INTRO, "") + RCM_HELP_STEPS + (
        "", "The README's \"Put Jibo in RCM mode\" section has a photo of the buttons. "
        + RCM_HELP_CAPTION)


UPDATE_ACTION_LABEL = "Install a Phoenix image or an official update"
HOSTED_MODULE = "jibo_hosted_updates"
CHOOSER_TIMEOUT = 5.0
RECOMMENDED_SUFFIX = "\u2014 recommended"
RECOMMENDED_DETAIL = "Gets the image from jibo.io, checks it, then asks before writing."
KEEP_DATA_LABEL = flash_policy.KEEP_LABEL
_HOSTED_ERROR_TEXT = {
    "offline": "Can't reach jibo.io right now.",
    "signature": "The Phoenix image list could not be checked, so it is not shown.",
    "hash": "The download did not match its published checksum and was not used.",
    "expired": "The Phoenix image list is out of date, so it is not shown.",
    "incompatible": "This image does not fit this toolkit or robot.",
    "canceled": "Download canceled. Start it again to pick up where it stopped.",
    "http": "jibo.io answered with an error.",
    "disk": "The toolkit could not use its image folder. Check access and free space.",
    "local": "Something went wrong on this computer while checking for Phoenix images.",
    "rate_limited": "jibo.io is busy. Try again in a few minutes.",
    "download-stopped": "The download stopped. Nothing was written to the robot. Try again to continue where it stopped.",
    "dependency": "This copy of the toolkit can't check Phoenix images.",
    "check-canceled": "The image list check was canceled.",
}
_PHASE_TEXT = {
    "index": "Checking the image list", "manifest": "Checking the image details",
    "download": "Downloading", "verify": "Checking the image",
    "prepare": "Preparing Jibo's software", "done": "Getting ready for your review",
    "error": "Download stopped", "canceled": "Download canceled",
}


def hosted_module(api=None):
    """Return the hosted-image backend exposed by the device API, or None.

    The real ``jibo_dfu`` imports ``jibo_hosted_updates`` as ``hosted`` and
    provides ``flash_hosted_update``; without both, this toolkit cannot install
    a Phoenix image, so the chooser stays the plain local-package list. Nothing
    here imports or contacts the network.
    """
    injected = getattr(api, "hosted", None)
    if injected is None or injected is False:
        return None  # older device layer, or an embedder/test turned it off
    if not callable(getattr(api, "flash_hosted_update", None)):
        return None
    return injected


def hosted_folders():
    """``(updates, cache)`` in persistent toolkit-owned per-user storage."""
    from jibo_hosted_updates import default_updates_dir
    updates = default_updates_dir()
    return updates, updates / ".hosted"


def hosted_error_text(code, fallback=""):
    code, _, detail = str(code or "").partition(": ")
    text = _HOSTED_ERROR_TEXT.get(code) or "Phoenix images are unavailable right now."
    if code in ("http", "rate_limited", "disk") and (detail or fallback):
        text += " " + (detail or fallback)
    return text


def _human_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1000.0
    return "%.1f GB" % value


def format_progress(progress):
    """One plain line with bytes, percent, rate and time left when known."""
    if getattr(progress, "phase", "") == "resume":
        return str(progress.message)
    text = _PHASE_TEXT.get(getattr(progress, "phase", ""), "Working")
    if getattr(progress, "phase", "") in ("verify", "prepare") and getattr(progress, "message", ""):
        text = str(progress.message)
    percent = getattr(progress, "percent", None)
    if percent is not None:
        text += " %d%%" % int(max(0.0, min(100.0, percent)))
    done, total = getattr(progress, "bytes", 0) or 0, getattr(progress, "total", None)
    details = []
    if done or total:
        details.append(_human_bytes(done) + (" of " + _human_bytes(total) if total else ""))
    rate, eta = getattr(progress, "rate", None), getattr(progress, "eta", None)
    if rate:
        details.append(_human_bytes(rate) + "/s")
    if eta is not None and getattr(progress, "phase", "") == "download":
        seconds = int(max(0, eta))
        details.append(("about %d min left" % ((seconds + 59) // 60)) if seconds >= 60
                       else ("%d s left" % seconds))
    return text + (" \u2014 " + ", ".join(details) if details else "")


def _image_label(image, recommended=False):
    label = "Phoenix " + str(image.version)
    if recommended:
        label += " " + RECOMMENDED_SUFFIX
    return label


def _same_file(left, right):
    try:
        return Path(left).resolve() == Path(right).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return str(left) == str(right)


def hosted_chooser_items(catalog=None, local_hosted=(), packages=(), show_older=False,
                         error_code=None):
    """Build the image chooser in display order.

    Returns ``(items, choices)``. ``choices`` maps an enabled key to
    ``("hosted", HostedImage)`` or ``("local", Path)``. The signed latest image
    comes first; a missing, incompatible or failed latest never promotes an
    older row. Local packages keep their original order below. Previously
    downloaded hosted archives are not advertised as retained local images.
    """
    items, choices = [], {}
    images = tuple(getattr(catalog, "images", ()) or ()) if catalog is not None else ()
    latest_id = getattr(catalog, "latest_id", None) if catalog is not None else None
    latest = next((image for image in images if image.id == latest_id and image.is_latest), None)
    saved_list = bool(getattr(catalog, "from_cache", False))
    problem = error_code or (getattr(catalog, "error", None) if catalog is not None else None)
    # Preserve transport/local diagnostics so screenshots identify the cause.
    if latest is not None and latest.offered:
        label = _image_label(latest, recommended=True)
        if saved_list:
            label += " (saved list)"
        enabled = bool(latest.compatible)
        reason = RECOMMENDED_DETAIL if enabled else (hosted_error_text("incompatible") +
                                     (" " + latest.incompatible_reason
                                      if latest.incompatible_reason else ""))
        items.append(MenuItem("hosted:" + latest.id, label, enabled, reason, tone="recommended"))
        if enabled:
            choices["hosted:" + latest.id] = ("hosted", latest)
    if problem:
        items.append(MenuItem("status:catalog", "Phoenix images: " + hosted_error_text(problem) +
                              (" Showing the last checked list." if saved_list else "") +
                              " Local packages below still work.", False,
                              hosted_error_text(problem), tone="status"))
    elif catalog is None:
        items.append(MenuItem("status:catalog", "Phoenix images: not checked yet.", False,
                              "Local packages below still work.", tone="status"))
    shown_hosted = [image for image in images if image.offered]
    hosted_paths = [image.downloaded_path for image in tuple(shown_hosted) + tuple(local_hosted or ())
                    if getattr(image, "downloaded_path", None)]
    for index, package in enumerate(packages or ()):
        if any(_same_file(package, path) for path in hosted_paths):
            continue
        key = "local:%d" % index
        items.append(MenuItem(key, Path(package).name))
        choices[key] = ("local", Path(package))
    older = [image for image in shown_hosted if latest is None or image.id != latest.id]
    if older:
        items.append(MenuItem("older", ("Hide older Phoenix images" if show_older else
                                        "Show older Phoenix images (%d)" % len(older)),
                              tone="group"))
        if show_older:
            for image in older:
                key = "hosted:" + image.id
                enabled = bool(image.compatible)
                items.append(MenuItem(key, "  " + _image_label(image) +
                                      (" \u2014 " + image.released_at if image.released_at else ""),
                                      enabled, "" if enabled else
                                      hosted_error_text("incompatible")))
                if enabled:
                    choices[key] = ("hosted", image)
    return tuple(items), choices


def flash_policy_items():
    return (MenuItem(flash_policy.KEEP, flash_policy.KEEP_LABEL, reason=flash_policy.KEEP_DETAIL,
                     tone="recommended"),
            MenuItem(flash_policy.WITHOUT, flash_policy.WITHOUT_LABEL,
                     reason=flash_policy.WITHOUT_DETAIL, tone="danger"))


def hosted_policy_items(hosted, image):
    """The same fresh-setup choices apply to every authenticated image."""
    return flash_policy_items()


def _choose_flash_policy():
    return select_option(
        "Choose calibration handling", flash_policy_items(),
        prompt="Choose what to put back after flashing.",
        detail="Both choices start with a fresh image. A new verified backup is saved before writing.",
        initial=flash_policy.KEEP)


def _unsafe_flash_confirmation():
    return confirm_action(flash_policy.UNSAFE_TITLE, flash_policy.UNSAFE_DETAIL)


def looks_like_phoenix_package(path):
    """Phoenix archives use phoenix-*.tar.bz2 names; the backend still decides."""
    return Path(str(path)).name.lower().startswith("phoenix-")


def _error_code(error):
    return getattr(error, "code", None) or "local"


def _progress_session(screen, title, detail, events, cancel, thread):
    _start_screen(screen)
    screen.timeout(100)
    dim = getattr(curses, "A_DIM", 0)
    line, cancelling = "Starting...", False
    started = time.monotonic()
    while True:
        try:
            while True:
                line = format_progress(events.get_nowait())
        except queue.Empty:
            pass
        row, height, _width = _screen_heading(screen, title, detail)
        _addstr(screen, row + 1, 0, line)
        _addstr(screen, height - 2, 0, "Elapsed: {} s".format(int(time.monotonic() - started)), dim)
        _addstr(screen, height - 1, 0,
                getattr(cancel, "cancel_message", "Canceling...") if cancelling else
                "Esc or c  " + getattr(cancel, "cancel_label", "Cancel"), dim)
        screen.refresh()
        if not thread.is_alive():
            return
        key = screen.getch()
        if key in (27, ord("c"), ord("C")) and not cancelling:
            cancel.cancel()
            cancelling = True


def run_with_progress(title, work, cancel, detail=""):
    """Run ``work(report, cancel)`` off the screen thread; return ``(value, error)``.

    The terminal shows the latest progress line and cancels on Esc or c. Network
    work never runs on the thread that draws the screen.
    """
    events, box = queue.Queue(), {}

    def target():
        try:
            box["value"] = work(events.put, cancel)
        except Exception as exc:  # handed back to the flow, never raised here
            box["error"] = exc

    context = copy_context()
    thread = threading.Thread(target=lambda: context.run(target), name="jibo-progress-work", daemon=True)
    thread.start()
    try:
        if _terminal_available():
            curses.wrapper(lambda screen: _progress_session(
                screen, title, detail, events, cancel, thread))
        thread.join()
    except KeyboardInterrupt:
        cancel.cancel()
        thread.join()
        raise
    return box.get("value"), box.get("error")


def _load_chooser(hosted):
    """Fetch the signed list (5 s) and the verified downloads; never raises."""
    updates_dir, cache_dir = hosted_folders()
    catalog, error = run_with_progress(
        "Checking for Phoenix images",
        lambda report, cancel: hosted.fetch_catalog(cache_dir=cache_dir, timeout=CHOOSER_TIMEOUT,
                                                    cancel=cancel),
        hosted.CancelToken(), detail="Looking for the latest Phoenix image on jibo.io.")
    code = None
    if error is not None:
        catalog = None
        code = "check-canceled" if _error_code(error) == "canceled" else _error_code(error)
        if code in ("http", "rate_limited", "disk"):
            code += ": " + str(error)
        # The user-facing sentence stays generic; the full diagnostic (exception
        # class, message, code, URL/status when present) always goes to the
        # operation log so "Show operation log" has more than the bare text.
        print("Phoenix image list check failed: {}: {}".format(
            type(error).__name__, error), flush=True)
        if code == "check-canceled":
            # Canceling the check may still show the last signed list (no network).
            try:
                catalog = hosted.cached_catalog(cache_dir=cache_dir, error=code)
            except Exception:
                catalog = None
    try:
        local = tuple(hosted.local_hosted_images(updates_dir))
        legacy = Path.cwd() / "updates"
        if legacy != updates_dir:
            local += tuple(hosted.local_hosted_images(legacy))
    except Exception:
        local = ()
    return catalog, local, code


def _choose_image(api, hosted, title, include_local):
    updates_dir, _ = hosted_folders()
    packages = ()
    if include_local:
        try:
            packages = tuple(api._update_candidates(Path.cwd() / "updates"))
        except Exception:
            packages = ()
    catalog, local, code = _load_chooser(hosted)
    show_older = False
    while True:
        items, choices = hosted_chooser_items(catalog, local, packages, show_older, code)
        if not choices and not any(item.key == "older" for item in items):
            return None, "No Phoenix images or local packages are available. " + \
                (hosted_error_text(code) if code else "")
        key = select_option(title, items, prompt="Select an image to continue.",
                            detail="Phoenix images come from jibo.io and are checked before use. "
                                   "Local packages come from your updates folder.")
        if key is None:
            return None, "No image selected."
        if key == "older":
            show_older = not show_older
            continue
        if key in choices:
            return choices[key], ""


def _download_chosen(hosted, image, session):
    """Return ``(path, message)``; resumes a stopped download of the same image."""
    path, error = run_with_progress(
        "Downloading Phoenix " + str(image.version),
        lambda report, cancel: download_for_flash(hosted, image.id, dest_dir=session.directory,
                                                progress=report, cancel=cancel),
        hosted.CancelToken(),
        detail="Downloading for this flash. An interrupted download can resume; the image is removed when the flash attempt ends.")
    if error is not None:
        session.interrupted = _error_code(error) in ("canceled", "offline", "http", "rate_limited", "download-stopped")
        if _error_code(error) == "download-stopped":
            raise error
        return None, hosted_error_text(_error_code(error), str(error))
    session.complete = True
    return path, ""


def _hosted_confirmation_details(plan):
    details = _confirmation_details(plan, "Review the Phoenix image install before writing.")
    phoenix = plan.get("phoenix") if isinstance(plan, dict) else None
    if not isinstance(phoenix, dict):
        return details
    lines = [details, "", flash_policy.PHOENIX_LINE, "",
             "This is a Phoenix image on an official Jibo base, not an official Jibo release."]
    for key, label in (("id", "Phoenix image"), ("base_release", "Official base"),
                       ("profile", "Profile"), ("archive_sha256", "Archive SHA-256"),
                       ("scope", "What it changes"), ("var_retention", "This robot's data")):
        if phoenix.get(key):
            lines.append("{}: {}".format(label, phoenix[key]))
    if phoenix.get("var_write_bytes"):
        lines.append("Var write: {} bytes. The write finishes when Jibo accepts the transfer.".format(phoenix["var_write_bytes"]))
    if phoenix.get("var_candidate_sha256"):
        lines.append("New var SHA-256: {}".format(phoenix["var_candidate_sha256"]))
    lines.append("Only var gets a rollback copy. The boot chain and partition table are not changed "
                 "or repaired by this install.")
    return "\n".join(lines)


def _run_image_work(callback, label):
    import jibo_flash_contract as contract
    from jibo_hosted_updates import CancelToken
    def work(report, cancel):
        with contract.operation(report, cancel):
            if label.startswith('Preparing'):
                contract.phase('prepare')
            return callback()
    result, error = run_with_progress(label, work, CancelToken())
    if error is not None:
        raise error
    return result


def _install_hosted(api, hosted, image, path, port, policy=None):
    chosen_before_download = policy is not None
    policy = policy or _choose_flash_policy()
    if policy is None:
        return {"status": "cancelled", "message": "No calibration choice selected. Nothing was written."}
    if not chosen_before_download and policy == flash_policy.WITHOUT and not _unsafe_flash_confirmation():
        return {"status": "cancelled", "message": "Nothing was written."}
    confirmation = lambda plan: confirm_action("Confirm Phoenix image install",
                                               _hosted_confirmation_details(plan))
    result = api.flash_hosted_update(path, var_policy=policy, port=port,
                                     local_work=_run_image_work, confirmation=confirmation,
                                     unsafe_without_calibration=policy == flash_policy.WITHOUT)
    # The backend's result is the reviewed flash result; name the image for the summary.
    return {**result, "phoenix_image": str(image.version)} if isinstance(result, dict) else result


_UPDATE_RECORD = re.compile(r"^Update record: (.+)$", re.M)


def failed_update_record(error):
    """The operation record a failed flash names (``...\\nUpdate record: PATH``), else None."""
    match = _UPDATE_RECORD.search(str(error))
    return match.group(1).strip() if match else None


def _run_hosted_update(api, hosted, port=None):
    choice, message = _choose_image(api, hosted, "Choose an image to install", include_local=True)
    if choice is None:
        return {"status": "cancelled", "message": message}
    kind, value = choice
    if kind == "local":
        return _run_local_update(api, value, port, phoenix_marked=looks_like_phoenix_package(value))
    policy = _choose_flash_policy()
    if policy is None:
        return {"status": "cancelled", "message": "No calibration choice selected. Nothing was written."}
    if policy == flash_policy.WITHOUT and not _unsafe_flash_confirmation():
        return {"status": "cancelled", "message": "Nothing was written."}
    updates_dir, _ = hosted_folders()
    with hosted.flash_download_session(value.id, dest_dir=updates_dir) as session:
        path, message = _download_chosen(hosted, value, session)
        if path is None:
            return {"status": "cancelled", "message": message}
        return _install_hosted(api, hosted, value, path, port, policy=policy)


def _addstr(screen, y, x, value, attr=0, width=None):
    try:
        height, columns = screen.getmaxyx()
        if y < 0 or y >= height or x >= columns:
            return
        available = max(0, columns - x - 1)
        if width is not None:
            available = min(available, width)
        screen.addnstr(y, x, str(value), available, attr)
    except curses.error:
        pass


def _wrap(value, width):
    return textwrap.wrap(str(value), max(1, width), break_long_words=True,
                         break_on_hyphens=False) or [""]


def _terminal_available():
    return (getattr(sys.stdin, "isatty", lambda: False)() and
            getattr(sys.stdout, "isatty", lambda: False)())


def _start_screen(screen):
    try:
        screen.keypad(True)
    except Exception:
        pass
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if hasattr(curses, "use_default_colors"):
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass


def _screen_heading(screen, title, detail=""):
    screen.erase()
    height, width = screen.getmaxyx()
    bold = getattr(curses, "A_BOLD", 0)
    dim = getattr(curses, "A_DIM", 0)
    _addstr(screen, 0, 0, "JIBO DFU TOOLKIT", bold)
    _addstr(screen, 2, 0, title, bold)
    line = 3
    for paragraph in str(detail or "").splitlines():
        for segment in _wrap(paragraph, max(1, width - 2)):
            if line >= height - 3:
                return line, height, width
            _addstr(screen, line, 0, segment, dim)
            line += 1
    return line, height, width


def _normalise_options(options):
    normalised = []
    for option in options:
        if isinstance(option, MenuItem):
            normalised.append(option)
        elif len(option) == 2:
            key, label = option
            normalised.append(MenuItem(str(key), str(label)))
        elif len(option) == 3:
            key, label, enabled = option
            normalised.append(MenuItem(str(key), str(label), bool(enabled)))
        else:
            key, label, enabled, reason = option
            normalised.append(MenuItem(str(key), str(label), bool(enabled), str(reason)))
    return tuple(normalised)


def _move_menu_selection(options, current, step):
    if not options:
        return 0
    return (current + step) % len(options)


def _select_session(screen, title, options, prompt="", detail="", initial=None,
                    cancel_label="Cancel"):
    _start_screen(screen)
    items = _normalise_options(options)
    selected = 0
    if initial is not None:
        selected = next((i for i, item in enumerate(items) if item.key == initial), 0)
    while True:
        row, height, width = _screen_heading(screen, title, detail)
        row += 1
        dim = getattr(curses, "A_DIM", 0)
        reverse = getattr(curses, "A_REVERSE", 0)
        hint = (items[selected].reason if items and items[selected].reason else prompt)
        if items and not items[selected].enabled and not hint:
            hint = "This action is unavailable."
        hint_lines = _wrap(hint, max(1, width - 2))
        available = max(1, height - row - len(hint_lines) - 2)
        start = max(0, min(selected - available + 1, len(items) - available))
        for index in range(start, min(len(items), start + available)):
            item = items[index]
            text = ("> " if index == selected else "  ") + item.label
            attr = 0 if item.enabled else dim
            if item.enabled and item.tone == "recommended":
                attr |= getattr(curses, "A_BOLD", 0)
            if item.tone == "danger":
                try:
                    curses.init_pair(1, curses.COLOR_RED, -1)
                    attr |= curses.color_pair(1) | getattr(curses, "A_BOLD", 0)
                except curses.error:
                    attr |= getattr(curses, "A_BOLD", 0)
            if index == selected:
                attr |= reverse
            _addstr(screen, row + index - start, 0, text, attr)

        hint_y = min(height - 2, row + available)
        for offset, segment in enumerate(hint_lines[:max(0, height - hint_y - 1)]):
            _addstr(screen, hint_y + offset, 0, segment, dim)
        _addstr(screen, height - 1, 0,
                "↑/↓ Select   Enter Open   Esc " + str(cancel_label), dim)
        screen.refresh()

        key = screen.getch()
        if key in (27, ord("q"), ord("Q")):
            return None
        if key in (curses.KEY_UP, ord("k")):
            selected = _move_menu_selection(items, selected, -1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = _move_menu_selection(items, selected, 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            if not items:
                return None
            if items[selected].enabled:
                return items[selected].key


def select_option(title, options, prompt="", detail="", cancel_label="Cancel", initial=None):
    """Show a selectable curses menu and return its key, or None on cancel.

    ``options`` may contain MenuItem instances or tuples of ``(key, label)``,
    ``(key, label, enabled)``, or ``(key, label, enabled, reason)``.
    Disabled rows remain visible and explain why they cannot be opened.
    """
    if not _terminal_available():
        return None
    return curses.wrapper(lambda screen: _select_session(
        screen, title, options, prompt, detail, initial, cancel_label))


def _multi_select_session(screen, title, options, initial=(), detail=""):
    _start_screen(screen)
    items = _normalise_options(options)
    allowed = {item.key for item in items if item.enabled}
    chosen = set(initial) & allowed
    selected = 0
    while True:
        row, height, width = _screen_heading(screen, title, detail)
        row += 1
        dim = getattr(curses, "A_DIM", 0)
        reverse = getattr(curses, "A_REVERSE", 0)
        rows = len(items) + 1
        available = max(1, height - row - 4)
        start = max(0, min(selected - available + 1, rows - available))
        for index in range(start, min(rows, start + available)):
            if index == len(items):
                label = "Continue with {} selected".format(len(chosen))
            else:
                label = "[{}] {}".format("x" if items[index].key in chosen else
                                         " " if items[index].enabled else "-",
                                         items[index].label)
            attr = reverse if index == selected else (0 if index == len(items) or
                                                      items[index].enabled else dim)
            _addstr(screen, row + index - start, 0,
                    ("> " if index == selected else "  ") + label, attr)
        _addstr(screen, height - 2, 0, "Space/Enter Toggle   A Select all   N Clear", dim)
        _addstr(screen, height - 1, 0,
                "↑/↓ Select   Enter Continue   Esc Cancel", dim)
        screen.refresh()
        key = screen.getch()
        if key in (27, ord("q"), ord("Q")):
            return None
        if key in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % rows
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % rows
        elif key in (ord("a"), ord("A")):
            chosen = set(allowed)
        elif key in (ord("n"), ord("N")):
            chosen.clear()
        elif key in (ord(" "), curses.KEY_ENTER, 10, 13):
            if selected == len(items):
                if chosen:
                    return tuple(item.key for item in items if item.key in chosen)
            elif items[selected].enabled:
                key_name = items[selected].key
                if key_name in chosen:
                    chosen.remove(key_name)
                else:
                    chosen.add(key_name)


def select_multiple_options(title, options, initial=(), detail=""):
    """Use the same arrow-key menu style for a partition checklist."""
    if not _terminal_available():
        return None
    return curses.wrapper(lambda screen: _multi_select_session(
        screen, title, options, initial, detail))


def _read_key(screen):
    get_wch = getattr(screen, "get_wch", None)
    if get_wch is not None:
        try:
            return get_wch()
        except (AttributeError, curses.error):
            pass
    return screen.getch()


def _text_session(screen, title, prompt, default="", password=False, detail=""):
    _start_screen(screen)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    value = list(str(default))
    while True:
        row, height, width = _screen_heading(screen, title, detail)
        row += 1
        _addstr(screen, row, 0, prompt, getattr(curses, "A_BOLD", 0))
        row += 2
        _addstr(screen, row, 0, "> ")
        visible = "•" * len(value) if password else "".join(value)
        display_width = max(1, width - 4)
        if len(visible) > display_width:
            visible = visible[-display_width:]
        _addstr(screen, row, 2, visible, width=display_width)
        _addstr(screen, row, min(width - 2, 2 + len(visible)), "▏", getattr(curses, "A_REVERSE", 0))
        _addstr(screen, height - 1, 0, "Enter Accept   Esc Cancel   Ctrl-U Clear", getattr(curses, "A_DIM", 0))
        try:
            screen.move(row, min(width - 1, 2 + len(visible)))
        except (AttributeError, curses.error):
            pass
        screen.refresh()

        key = _read_key(screen)
        if isinstance(key, str):
            if key in ("\n", "\r"):
                return "".join(value)
            if key == "\x1b":
                return None
            if key in ("\x7f", "\b"):
                if value:
                    value.pop()
                continue
            if key == "\x15":
                value.clear()
                continue
            if len(key) == 1 and key.isprintable():
                value.append(key)
            continue
        if key in (27,):
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            return "".join(value)
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if value:
                value.pop()
        elif key == 21:
            value.clear()
        elif 32 <= key <= 0x10FFFF and key < curses.KEY_MIN:
            try:
                value.append(chr(key))
            except ValueError:
                pass


def text_input(title, prompt, default="", password=False, detail=""):
    """Collect a line in curses. Password characters are never drawn literally."""
    if not _terminal_available():
        return None
    return curses.wrapper(lambda screen: _text_session(
        screen, title, prompt, default, password, detail))


def _confirmed_wifi_password(title):
    while True:
        password = text_input(title, "Wi-Fi password:", password=True,
                              detail="The password is hidden while you type.")
        if password is None:
            return None
        repeated = text_input(title, "Confirm Wi-Fi password:", password=True,
                              detail="Enter the same password again.")
        if repeated is None:
            return None
        if password == repeated:
            return password
        password = repeated = None
        choice = select_option(
            "Passwords do not match",
            (("retry", "Enter the password again"),
             ("cancel", "Cancel Wi-Fi setup")),
            prompt="The two passwords differed. No Wi-Fi change was made.",
            initial="retry")
        if choice != "retry":
            return None


def confirm_action(title, message, phrase=None):
    """Ask for explicit confirmation with Cancel selected by default.

    The optional phrase is shown as context for older callers. It is not typed;
    automation confirmations remain the responsibility of the command line.
    """
    detail = str(message or "")
    if phrase:
        detail += "\nConfirmation: " + str(phrase)
    choice = select_option(
        title,
        (MenuItem("cancel", "Cancel"), MenuItem("confirm", "Confirm",
             tone="danger" if title == flash_policy.UNSAFE_TITLE else "")),
        prompt="Review the details, then select Confirm to continue.",
        detail=detail,
        initial="cancel")
    return choice == "confirm"


def _screen_lines(content):
    if isinstance(content, dict):
        content = json.dumps(content, indent=2, default=str)
    if isinstance(content, str):
        return content.splitlines() or [""]
    return [str(line) for line in content]


def _result_summary(action, result):
    """Keep the guided view focused on the outcome; detailed records stay on disk."""
    if not isinstance(result, dict):
        return _screen_lines(result)
    status = result.get("status", "complete")
    if status == "cancelled":
        return [result.get("message", "No change was made.")]
    if action == "set-mode" and (result.get("mode") or result.get("new_mode")):
        mode = result.get("mode") or result["new_mode"]
        if status == "already current":
            return ["The robot is already in {} mode.".format(mode)]
        lines = ["Robot mode set to {}.".format(mode)]
    elif action == "configure-wifi":
        lines = ["Saved Wi-Fi network: {!r}.".format(result["ssid"])
                 if result.get("ssid") else "Wi-Fi configuration updated."]
        if result.get("rtm2_wifi_startup_adjusted"):
            lines.append("RTM2 TI radio startup was adjusted; restart the robot to try Wi-Fi.")
        if result.get("wifi_server_check_hook_added"):
            lines.append("Wi-Fi startup will skip Jibo's retired server check after a restart.")
    elif action == "set-mode-wifi":
        lines = ["Robot mode set to {}.".format(result.get("mode") or result.get("new_mode")),
                 "Saved Wi-Fi network: {!r}.".format(result.get("ssid"))]
        if result.get("rtm2_wifi_startup_adjusted"):
            lines.append("RTM2 TI radio startup was adjusted; restart the robot to try Wi-Fi.")
        if result.get("wifi_server_check_hook_added"):
            lines.append("Wi-Fi startup will skip Jibo's retired server check after a restart.")
    elif action == "backup-var":
        lines = ["Var backup is ready."]
        if result.get("image"):
            lines.append("Saved at: " + result["image"])
    elif action == "backups":
        return _backups_summary(result)
    elif result.get("partitions") and result.get("manifest"):
        names = [entry.get("name", "?") if isinstance(entry, dict) else str(entry)
                 for entry in result["partitions"]]
        lines = [("Backup ready: " if status == "backup complete" else "Restored: ") +
                 ", ".join(names) + ".", "Record: " + str(result["manifest"])]
    elif action == "flash-update":
        if result.get("phoenix_image"):
            lines = ["Phoenix {} install: {}.".format(result["phoenix_image"], status)]
            if result.get("manifest"):
                lines.append("Record: " + str(result["manifest"]))
        else:
            lines = ["Official update: {}.".format(status)]
    elif action == "probe-dfu-gpt" or "partition_sizes_bytes" in result:
        lines = ["Partition layout checked."]
    elif action == "write-var":
        lines = ["Edited var image: {}.".format(status)]
    elif result.get("writes"):
        lines = ["File changes: {}.".format(status)]
    elif "mode" in result and "wifi_configured" in result:
        lines = ["Mode: {}".format(result["mode"]),
                 "Saved Wi-Fi networks: {}".format(result.get("wifi_network_count", 0))]
    elif result.get("image"):
        lines = ["Image ready: " + str(result["image"])]
    else:
        return _screen_lines(result)
    if result.get("operation_directory"):
        lines.append("Operation record: " + str(result["operation_directory"]))
    if calibration_service.CALIBRATION_SHARING_AVAILABLE and result.get("calibration_sharing") is not None:
        lines.append("Local backup: verified")
        lines.append("Calibration sharing: " + result["calibration_sharing"])
        if result.get("calibration_sharing_detail"):
            lines.extend(result["calibration_sharing_detail"].splitlines())
    return lines


def _partition_list(result):
    return [entry.get("name", "?") if isinstance(entry, dict) else str(entry)
            for entry in result.get("partitions") or ()]


def _backups_summary(result):
    """Outcome lines for every action in the Backups menu."""
    status = result.get("status")
    if result.get("image") and "partitions" not in result:
        lines = ["Var backup is ready.", "Saved at: " + str(result["image"])]
        if calibration_service.CALIBRATION_SHARING_AVAILABLE and \
                result.get("calibration_sharing") is not None:
            lines.append("Local backup: verified")
            lines.append("Calibration sharing: " + result["calibration_sharing"])
            if result.get("calibration_sharing_detail"):
                lines.extend(result["calibration_sharing_detail"].splitlines())
        return lines
    if status == "backup complete" and result.get("manifest"):
        lines = ["Backup saved for {}.".format(result.get("robot_label") or "this Jibo"),
                 "Partitions: " + ", ".join(_partition_list(result)),
                 "Folder: " + str(Path(result["manifest"]).parent)]
        if result.get("robot_label") and not result.get("robot_name"):
            lines.append("Jibo's name was not found, so the folder uses a short USB tag.")
        return lines
    if status == "verified" and result.get("partitions"):
        lines = ["Restored and checked: " + ", ".join(_partition_list(result)) + "."]
        if result.get("manifest"):
            lines.append("Restore record: " + str(result["manifest"]))
        return lines
    if status == "deleted":
        return ["Deleted the backup of {} saved {}.".format(
                    result.get("robot_label") or "this Jibo",
                    backup_time_label(result.get("created_utc"))),
                "It was removed from this computer. The robot was not changed."]
    if status == "cancelled":
        return [result.get("message", "No change was made.")]
    return _screen_lines(result)


def _message_session(screen, title, lines, wait=True):
    _start_screen(screen)
    offset = 0
    while True:
        row, height, width = _screen_heading(screen, title)
        row += 1
        view_height = max(1, height - row - 3)
        wrapped = []
        for line in lines:
            wrapped.extend(_wrap(line, max(1, width - 2)))
        max_offset = max(0, len(wrapped) - view_height)
        offset = min(offset, max_offset)
        for index, line in enumerate(wrapped[offset:offset + view_height]):
            _addstr(screen, row + index, 0, line)
        footer = "↑/↓ Scroll   Enter Back"
        if not wait:
            footer = "Enter Back"
        elif max_offset:
            footer = "↑/↓ Scroll   PgUp/PgDn   Enter Back"
        _addstr(screen, height - 1, 0, footer, getattr(curses, "A_DIM", 0))
        screen.refresh()
        if not wait:
            return
        key = screen.getch()
        if key in (10, 13, curses.KEY_ENTER, 27, ord("q"), ord("Q")):
            return
        if key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            offset = min(max_offset, offset + 1)
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - view_height)
        elif key == curses.KEY_NPAGE:
            offset = min(max_offset, offset + view_height)
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = max_offset


def show_screen(title, lines, wait=True):
    """Show a result, plan, or explanation in the shared curses style."""
    if not _terminal_available():
        return None
    return curses.wrapper(lambda screen: _message_session(
        screen, title, _screen_lines(lines), wait))


class TerminalMenu:
    def __init__(self, api):
        self.api = api
        self.readiness = Readiness("none", ())
        self.items = ()
        self.selected = 0
        self.command = None
        self.note = ""
        self.refresh()

    def refresh(self, found=None):
        previous_readiness = self.readiness
        previous_items = self.items
        selected_key = self.items[self.selected].key if self.items else None
        self.readiness = inspect_readiness(self.api, found)
        folder = Path.cwd() / "updates"
        try:
            packages = self.api._update_candidates(folder)
        except Exception:
            packages = ()
        self.items = build_menu_items(self.readiness, packages,
                                      hosted_available=hosted_module(self.api) is not None)
        self.selected = next((index for index, item in enumerate(self.items)
                              if item.key == selected_key),
                             min(self.selected, len(self.items) - 1) if self.items else 0)
        return self.readiness != previous_readiness or self.items != previous_items

    def render(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()
        normal = 0
        dim = getattr(curses, "A_DIM", 0)
        bold = getattr(curses, "A_BOLD", 0)
        reverse = getattr(curses, "A_REVERSE", 0)
        _addstr(screen, 0, 0, "JIBO DFU TOOLKIT", bold)
        _addstr(screen, 1, 0, "USB flow:  Connect  →  RCM/APX  →  DFU  →  Choose an action")
        first, second = status_lines(self.readiness)
        _addstr(screen, 3, 0, first, bold)
        line = 4
        for segment in _wrap(second, max(1, width - 2)):
            _addstr(screen, line, 0, segment)
            line += 1
        if self.readiness.port:
            _addstr(screen, line, 0, "USB port " + self.readiness.port)
            line += 1
        line += 1
        _addstr(screen, line, 0, "ACTIONS   (↑/↓ select, Enter open, r refresh, h RCM help, q quit; USB auto-refreshes)", bold)
        first_item_line = line + 1
        for index, item in enumerate(self.items):
            y = first_item_line + index
            marker = "> " if index == self.selected else "  "
            label = marker + item.label
            attr = normal if item.enabled else dim
            if index == self.selected:
                attr |= reverse
            _addstr(screen, y, 0, label, attr)
            if not item.enabled:
                status_col = min(width - 1, max(32, len(label) + 2))
                if status_col < width - 1:
                    _addstr(screen, y, status_col, "unavailable", dim,
                            width=max(0, width - status_col - 1))

        hint_y = min(height - 2, first_item_line + len(self.items) + 1)
        if self.selected < len(self.items):
            selected_item = self.items[self.selected]
            explanation = selected_item.reason if not selected_item.enabled else _action_hint(selected_item.key)
            for offset, segment in enumerate(_wrap(explanation, max(1, width - 2))):
                _addstr(screen, hint_y + offset, 0, segment, dim)
        if self.note:
            _addstr(screen, max(0, height - 1), 0, self.note, bold)
        else:
            _addstr(screen, max(0, height - 1), 0, "q Quit   r Refresh   h " + RCM_HELP_LINK, dim)
        screen.refresh()

    def session(self, screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            screen.keypad(True)
        except Exception:
            pass
        # A timed read lets the menu rebuild enabled actions after a USB
        # disconnect, reconnect, or RCM-to-DFU transition without a keypress.
        screen.timeout(1500)
        if hasattr(curses, "use_default_colors"):
            try:
                curses.start_color()
                curses.use_default_colors()
            except curses.error:
                pass
        while True:
            self.render(screen)
            key = screen.getch()
            if key == -1:
                if self.refresh():
                    self.note = "USB state changed. Available actions updated."
                continue
            if key in (ord("q"), ord("Q"), 27):
                self.command = "quit"
                return
            if key in (ord("h"), ord("H"), ord("?")):
                self.command = "rcm-help"
                return
            if key in (ord("r"), ord("R")):
                self.note = "Refreshing USB state..."
                self.render(screen)
                self.refresh()
                self.note = "USB state refreshed."
            elif key in (curses.KEY_UP, ord("k")):
                self.selected = (self.selected - 1) % len(self.items)
                self.note = ""
            elif key in (curses.KEY_DOWN, ord("j")):
                self.selected = (self.selected + 1) % len(self.items)
                self.note = ""
            elif key in (curses.KEY_ENTER, 10, 13):
                if not self.items:
                    continue
                selected_key = self.items[self.selected].key
                # Recheck immediately before dispatch so an unplugged robot
                # cannot open an action enabled by an earlier USB snapshot.
                self.refresh()
                item = next((candidate for candidate in self.items
                             if candidate.key == selected_key), None)
                if item is None:
                    self.note = "The selected action is no longer available."
                    continue
                if not item.enabled:
                    self.note = item.reason
                    continue
                self.command = item.key
                return


def _action_hint(key):
    hints = {
        "enter-dfu-shofel": "Initializes the selected SDRAM profile and starts the RAM recovery loader from RCM/APX.",
        "probe-dfu-gpt": "Reads and checks the partition layout without writing eMMC.",
        "backup-var": "Read the robot's var partition and save one reusable local rollback image.",
        "set-mode": "Choose a mode; review the proposed change and confirm before writing.",
        "configure-wifi": "Enter a network; review the proposed change and confirm before writing.",
        "set-mode-wifi": "Choose a mode and network; apply both in one reviewed operation.",
        "flash-update": "Choose a package and var policy; review the flash plan before confirming.",
        "write-var": "Select an edited image; review and confirm before writing.",
        "inspect-backup": "Read mode and Wi-Fi presence from a local image without showing credentials.",
        "edit-backup": "Create an edited copy of a local image. The source image is kept unchanged.",
        "backups": "Back up every partition or chosen ones, restore, and see saved backups. "
                   "Backups stay on this computer.",
        "more-tools": "Set mode or Wi-Fi on their own, check the partition layout, and edit "
                      "local var images.",
        "backup-partitions": "Select GPT partitions to save. Existing verified copies are reused.",
        "restore-partitions": "Select partitions from a backup set and verify every write by readback.",
    }
    return hints.get(key, "")


def _confirmation_details(plan, introduction):
    if not isinstance(plan, dict):
        return introduction + ("\n" + str(plan) if plan else "")
    lines = [introduction]
    if plan.get("changes"):
        lines.append("USB port: " + str(plan.get("usb_port", "selected robot")))
        for change in plan["changes"]:
            lines.append("Change: " + (change.get("description") or change["path"]))
            lines.append("File: {} on {}".format(change["path"], change["partition"]))
        if any(change["partition"] == "var" for change in plan["changes"]):
            lines.append("A saved var rollback copy is checked before writing.")
        if any(change["partition"] != "var" for change in plan["changes"]):
            lines.append("Other partitions are backed up only when you select a backup action.")
        lines.append("The edited files and their permissions are checked after writing.")
        return "\n".join(lines)
    fields = (
        ("operation", "Operation"),
        ("package", "Package"),
        ("version", "Version"),
        ("usb_port", "USB port"),
        ("filesystem_resize", "Filesystem resize"),
        ("calibration_choice", "Calibration choice"),
        ("calibration_detail", "Calibration details"),
        ("rollback_backup", "Rollback backup"),
        ("partition", "Partition"),
        ("size_bytes", "Size (bytes)"),
        ("before_sha256", "Current var SHA-256"),
        ("candidate_sha256", "Edited var SHA-256"),
        ("baseline_backup", "Rollback backup"),
        ("baseline_sha256", "Backup SHA-256"),
        ("source_manifest", "Backup set"),
        ("calibration_source", "Calibration source"),
        ("calibration_files", "Calibration files"),
        ("calibration_revision", "Calibration revision"),
        ("calibration_digest", "Calibration digest"),
        ("identity_source", "Identity source"),
        ("readback_policy", "Readback policy"),
    )
    for key, label in fields:
        if plan.get(key) is not None:
            lines.append("{}: {}".format(label, plan[key]))
    if plan.get("backup_robot"):
        lines.append("Backup of: " + str(plan["backup_robot"]))
    if plan.get("backup_created_utc"):
        lines.append("Backup saved: " + backup_time_label(plan["backup_created_utc"]))
    partitions = plan.get("partitions")
    if partitions:
        lines.append("Partitions:")
        for partition in partitions:
            if isinstance(partition, dict):
                name = partition.get("name", "unknown")
                size = partition.get("bytes")
                lines.append("  {}{}{}".format(name, " ({} bytes)".format(size) if size is not None else "",
                                               " \u2190 " + partition["image"] if partition.get("image") else ""))
            else:
                lines.append("  " + str(partition))
    changes = plan.get("changes")
    if changes:
        lines.append("Files to change:")
        for change in changes:
            lines.append("  {}:{} ({} → {} bytes)".format(
                change["partition"], change["path"],
                change["before_size_bytes"], change["candidate_size_bytes"]))
    if len(lines) == 1:
        lines.append(json.dumps(plan, sort_keys=True, default=str))
    return "\n".join(lines)


def _mode_options():
    return (
        ("normal", "normal — standard use"),
        ("developer", "developer — selected development services"),
        ("int-developer", "int-developer — broader internal development mode"),
        ("oobe", "oobe — setup and onboarding"),
    )


def _select_mode(title="Set robot mode"):
    return select_option(
        title, _mode_options(),
        prompt="Choose the mode to apply.",
        detail="Mode changes are written to the robot only after the change plan is reviewed.")


def _run_local_update(api, package, port=None, phoenix_marked=False):
    """Review calibration choice, unsafe consent, then the partition write plan."""
    policy = _choose_flash_policy()
    if policy is None:
        return {"status": "cancelled", "message": "No calibration choice selected."}
    if policy == flash_policy.WITHOUT and not _unsafe_flash_confirmation():
        return {"status": "cancelled", "message": "Nothing was written."}

    def confirmation(plan):
        if isinstance(plan, dict) and plan.get("phoenix"):
            return confirm_action("Confirm Phoenix image install", _hosted_confirmation_details(plan))
        return confirm_action(
            "Confirm full-flash update",
            _confirmation_details(plan, "Review the package, calibration choice, and partitions before writing."))
    return api.flash_update(package, var_policy=policy, port=port,
                            confirmation=confirmation,
                            unsafe_without_calibration=policy == flash_policy.WITHOUT)


def _run_update(api, port=None):
    hosted = hosted_module(api)
    if hosted is not None:
        return _run_hosted_update(api, hosted, port)
    folder = Path.cwd() / "updates"
    packages = api._update_candidates(folder)
    if not packages:
        return {"status": "cancelled", "message": "No official full-flash packages found in " + str(folder)}
    package_key = select_option(
        "Choose an official update package",
        tuple((str(index), package.name) for index, package in enumerate(packages)),
        prompt="Select a package to continue.",
        detail="Packages are read from " + str(folder) + ".")
    if package_key is None:
        return {"status": "cancelled", "message": "No package selected."}
    package = packages[int(package_key)]
    return _run_local_update(api, package, port, phoenix_marked=looks_like_phoenix_package(package))


# -- Backups menu ----------------------------------------------------------------

# Planning estimates only, not deadlines or measured hardware guarantees.
# USB speed varies; reads may be slower, and restores also read back each write.
ESTIMATE_READ_BYTES_PER_SECOND = 4 * 1024 * 1024
ESTIMATE_RESTORE_FACTOR = 3
BACKUPS_TITLE = "Backups"
SAVED_BACKUPS_TITLE = "Saved backups"
RESTORE_REVIEW_INTRO = ("These partitions on the connected Jibo will be replaced with this backup. "
                        "Each one is read back and checked after writing.")
OTHER_FOLDER_KEY = "other-folder"
BACKUP_DETAIL_KEYS = ("restore", "open-folder", "check-files", "delete")
_STATE_TEXT = {"complete": "Ready to restore", "missing-files": "Files missing",
               "incomplete": "Not finished", "unreadable": "Cannot be read",
               "rollback-image": "Original rollback image (kept)"}


@dataclass(frozen=True)
class PartitionItem(MenuItem):
    """A checklist row that carries its size, so a window can total the selection."""
    size_bytes: int = 0


class BackupReadCancel:
    """Cooperative read-only stop: finish the current partition, never cut USB."""
    allow_close = False
    cancel_label = "Stop after this partition"
    cancel_message = "Stopping after this partition. Saved files will be kept."

    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    def cancelled(self):
        return self._event.is_set()


@dataclass(frozen=True)
class BackupReadProgress:
    message: str
    phase: str = "prepare"


def size_label(value):
    """'524 MB' below one gigabyte, else '1.0 GB' (decimal units, like file browsers)."""
    value = max(0, value or 0)
    if value < 1e9:
        return "{} MB".format(int(round(value / 1e6)))
    return "{:.1f} GB".format(value / 1e9)


def estimate_minutes(size_bytes, factor=1):
    """Rough planning minutes for moving size_bytes over USB."""
    seconds = (size_bytes or 0) * factor / ESTIMATE_READ_BYTES_PER_SECOND
    return max(1, int(math.ceil(seconds / 60.0)))


def estimate_text(size_bytes, factor=1):
    """'about 3 minutes', 'about 1 hour 10 min'."""
    minutes = estimate_minutes(size_bytes, factor)
    if minutes < 60:
        return "about {} minute{}".format(minutes, "" if minutes == 1 else "s")
    hours, rest = divmod(minutes, 60)
    text = "about {} hour{}".format(hours, "" if hours == 1 else "s")
    return text + (" {} min".format(rest) if rest else "")


def selection_summary(count, size_bytes):
    """Count and actual selected bytes for a partition checklist."""
    if not count:
        return "Nothing selected"
    return "{} selected \u00b7 {}".format(count, size_label(size_bytes))


def backup_time_label(created_utc):
    """'Oct 17, 2026 14:05 UTC' from the saved ISO time, or 'Unknown date'."""
    try:
        moment = datetime.strptime(str(created_utc), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "Unknown date"
    return moment.strftime("%b ") + str(moment.day) + moment.strftime(", %Y %H:%M UTC")


def _partition_names(entry, limit=4):
    names = [item["name"] for item in entry["partitions"]]
    if entry.get("every_partition") and len(names) > limit:
        return "All {} partitions".format(len(names))
    if len(names) > limit:
        return ", ".join(names[:limit]) + " +{} more".format(len(names) - limit)
    return ", ".join(names) or "No partitions"


def saved_backup_items(entries, current_tag=None):
    """Rows for a saved-backups list: robot, date, partitions, size and status."""
    items = []
    for index, entry in enumerate(entries):
        this_robot = bool(current_tag) and entry.get("device_tag") == current_tag
        state = _STATE_TEXT.get(entry["state"], "Cannot be read")
        saved = backup_time_label(entry["created_utc"])
        partitions = _partition_names(entry)
        size = size_label(entry["total_bytes"])
        items.append(BackupItem(
            "set:%d" % index, "{}{} \u2014 {}".format(
                entry["robot_label"], " (connected)" if this_robot else "", saved),
            entry["state"] != "unreadable",
            "{} \u00b7 {} \u00b7 {}".format(partitions, size, state),
            robot=entry["robot_label"], saved=saved, partitions=partitions, size=size,
            state=entry["state"], this_robot=this_robot))
    return tuple(items)


def backup_detail_lines(entry, current_tag=None, identified=True):
    """Plain details for one saved backup. The raw USB identity is never shown."""
    lines = ["Robot: " + entry["robot_label"]]
    if entry.get("robot_name") is None:
        lines.append("Name: not found in this backup, so it is labelled by a short USB tag.")
    lines += ["Saved: " + backup_time_label(entry["created_utc"]),
              "Status: " + _STATE_TEXT.get(entry["state"], "Cannot be read")]
    if current_tag:
        lines.append("Connected Jibo: " + ("this backup's robot" if entry.get("device_tag") ==
                                           current_tag else "a different robot"))
    elif not identified:
        lines.append("Connected Jibo: could not be identified")
    lines.append("Size: " + size_label(entry["total_bytes"]))
    lines.append("Partitions:")
    for item in entry["partitions"]:
        lines.append("  {}  {}{}".format(item["name"], size_label(item["size_bytes"]),
                                         "" if item["present"] else "  (file missing)"))
    lines.append("Folder: " + entry["folder"])
    if entry.get("restore_reason"):
        lines.append(entry["restore_reason"])
    return lines


def _connected_robot(api, readiness):
    """``(tag, identified)``: the connected robot's USB identity tag, read-only.

    ``identified`` is False only when DFU is ready but the robot could not be read.
    """
    if readiness.state != "dfu-ready":
        return None, True
    try:
        _port, tag, _available = api.available_backup_partitions(port=readiness.port)
    except Exception:
        return None, False
    return tag, bool(tag)


FLASH_IMAGE_TITLE = "Flash an .img or .bin"


def _backup_menu_options(readiness):
    reason = _connection_reason(readiness)
    ready = readiness.state == "dfu-ready"
    return (
        MenuItem("backup-all", "Back up every partition", ready,
                 reason or "Reads every partition this robot shares over USB into one new "
                           "backup on this computer."),
        MenuItem("backup-choose", "Back up specific partitions", ready,
                 reason or "Choose the partitions to save. Sizes and time estimates are shown."),
        MenuItem("restore-backup", "Restore partitions", ready,
                 reason or "Pick a saved backup of this Jibo and the partitions to put back. "
                           "You review everything before anything is written."),
        MenuItem("flash-images", FLASH_IMAGE_TITLE, ready,
                 reason or "Write a whole-disk .img/.bin, split into its partitions, or one "
                           "partition image. Those partitions are backed up first and checked "
                           "after writing."),
        MenuItem("saved-backups", SAVED_BACKUPS_TITLE,
                 reason="Every backup on this computer: robot, date, partitions and size."),
        MenuItem("backup-var", "Save or check a var backup", ready,
                 reason or "Reads the var partition (524 MB) and keeps one checked copy. "
                           "An earlier verified copy of this robot is reused."),
    )


def _run_backup_set(api, readiness, selected, available, title):
    """Show what will be read, then read it into one new named backup."""
    total = sum(available[name]["size_bytes"] for name in selected)
    lines = ["{} partition{}: {}".format(len(selected), "" if len(selected) == 1 else "s",
                                         ", ".join(selected)),
             "Size: {} \u00b7 {}".format(size_label(total), estimate_text(total))]
    try:
        free = api.backup_free_bytes()
    except Exception:
        free = None
    if free is not None:
        if free < total + 1024 ** 3:
            return {"status": "cancelled",
                    "message": "Not enough free space for this backup. It needs {} and this "
                               "computer has {} free. Nothing was read from the robot.".format(
                                   size_label(total + 1024 ** 3), size_label(free))}
        lines.append("Free space on this computer: " + size_label(free))
    lines += ["Saved in: {} (a folder named after this Jibo)".format(
                  getattr(api, "BACKUP_ROOT", "Jibo-Backups")),
              "This only reads from the robot. Keep the USB cable connected until it finishes."]
    choice = select_option(title, (MenuItem("start", "Start backup", tone="recommended"),),
                           prompt="Start the backup?", detail="\n".join(lines), initial="start")
    if choice != "start":
        return {"status": "cancelled", "message": "No backup was started."}
    result, error = run_with_progress(
        title, lambda report, token: api.backup_partitions(
            tuple(selected), port=readiness.port, cancel=token.cancelled,
            progress=lambda message: report(BackupReadProgress(message))),
        BackupReadCancel(), detail="Read only. Stop waits for the current partition to finish; "
                                  "saved files are kept. Keep USB connected.")
    if error is not None:
        raise error
    return result


def partition_checklist(available):
    """Checklist rows: partition name, size and a rough read time."""
    return tuple(PartitionItem(name, "{}  \u2014  {}".format(name, size_label(extent["size_bytes"])),
                               reason="{} to read; USB speed varies.".format(
                                   estimate_text(extent["size_bytes"]).capitalize()),
                               size_bytes=extent["size_bytes"])
                 for name, extent in available.items())


def _choose_partitions_to_back_up(api, readiness):
    _, _, available = api.available_backup_partitions(port=readiness.port)
    if not available:
        raise RuntimeError("This DFU loader exposes no complete partitions for backup.")
    selected = select_multiple_options(
        "Back up specific partitions", partition_checklist(available),
        initial=("var",) if "var" in available else (),
        detail="These are the partitions this robot shares over USB. Times are rough "
               "estimates, not time limits; USB speed varies.")
    if selected is None:
        return {"status": "cancelled", "message": "No backup was started."}
    return _run_backup_set(api, readiness, selected, available, "Back up specific partitions")


def _restore_from_entry(api, readiness, entry):
    """Choose partitions from one saved backup; the backend checks, reviews and writes."""
    choices = tuple(PartitionItem(
        item["name"], "{}  \u2014  {}".format(item["name"], size_label(item["size_bytes"])),
        item["present"], ("{} to write and check; USB speed varies.".format(
            estimate_text(item["size_bytes"], ESTIMATE_RESTORE_FACTOR).capitalize())
            if item["present"] else "This file is missing from the backup folder."),
        size_bytes=item["size_bytes"])
        for item in entry["partitions"])
    present = tuple(item.key for item in choices if item.enabled)
    if not present:
        return {"status": "cancelled", "message": "This backup has no files that can be restored."}
    selected = select_multiple_options(
        "Choose partitions to restore", choices, initial=present,
        detail="Only the partitions you check are replaced. You review the full list "
               "before anything is written.")
    if selected is None:
        return {"status": "cancelled", "message": "No restore was started."}
    confirmation = lambda plan: confirm_action(
        "Confirm partition restore", _confirmation_details(plan, RESTORE_REVIEW_INTRO))
    return api.restore_partitions(Path(entry["manifest"]), selected, port=readiness.port,
                                  confirmation=confirmation)


def _restore_from_other_folder(api, readiness):
    source = text_input("Restore partitions", "Path to backup-set.json or its folder:",
                        detail="Use this for a backup kept outside the backup folder, such as "
                               "one copied to another drive.")
    if source is None or not source.strip():
        return {"status": "cancelled", "message": "No restore was started."}
    manifest = Path(source.strip()).expanduser()
    if manifest.is_dir():
        manifest /= "backup-set.json"
    entry = api.describe_backup_set(manifest)
    if entry["state"] == "unreadable":
        raise RuntimeError("That file is not a backup made by this toolkit.")
    if not entry["restorable"]:
        return {"status": "cancelled", "message": "This backup is not complete, so it cannot be "
                                                  "restored. Nothing was written."}
    return _restore_from_entry(api, readiness, entry)


def _flash_images_flow(api, readiness):
    """One .img/.bin: a whole-disk image is split by its GPT; any other image goes to one partition."""
    _, _, available = api.available_backup_partitions(port=readiness.port)
    if not available:
        raise RuntimeError("This DFU loader exposes no complete partitions.")
    path = text_input(FLASH_IMAGE_TITLE, "Path to the .img or .bin file:",
                      detail="A whole-disk image (with a partition table) is split into its "
                             "partitions. Any other image is written to one partition you choose.")
    if path is None or not path.strip():
        return {"status": "cancelled", "message": "Nothing was written."}
    image = Path(path.strip()).expanduser()
    confirmation = lambda plan: confirm_action(
        "Confirm image flash", _confirmation_details(
            plan, "These partitions will be backed up, then replaced from your image."))
    layout = api.disk_image_layout(image)
    if layout is None:
        name = select_option(FLASH_IMAGE_TITLE, partition_checklist(available),
                             prompt="Which partition should {} replace?".format(image.name),
                             detail="This image has no partition table, so it is written to one "
                                    "partition. A smaller image replaces only its own bytes.")
        if name is None:
            return {"status": "cancelled", "message": "Nothing was written."}
        return api.flash_images({name: str(image)}, port=readiness.port, confirmation=confirmation)
    keys = ("first_lba", "last_lba", "size_bytes")
    choices = tuple(PartitionItem(
        name, "{}  \u2014  {}".format(name, size_label(extent["size_bytes"])),
        name in available and all(available[name][k] == extent[k] for k in keys),
        "Not available over USB on this Jibo." if name not in available else
        "At a different place than on this Jibo, so it cannot be written."
        if any(available[name][k] != extent[k] for k in keys) else
        "{} to write and check; USB speed varies.".format(
            estimate_text(extent["size_bytes"], ESTIMATE_RESTORE_FACTOR).capitalize()),
        size_bytes=extent["size_bytes"]) for name, extent in layout.items())
    usable = tuple(item.key for item in choices if item.enabled)
    if not usable:
        return {"status": "cancelled", "message": "None of this image's partitions match this "
                                                  "Jibo's layout. Nothing was written."}
    selected = select_multiple_options(
        "Choose partitions to flash", choices, initial=usable,
        detail="These partitions are in {}. Only the ones you check are written; the partition "
               "table itself is never written.".format(image.name))
    if selected is None:
        return {"status": "cancelled", "message": "Nothing was written."}
    return api.flash_disk_image(image, selected, port=readiness.port, confirmation=confirmation)


def _restore_partitions_flow(api, readiness):
    tag, identified = _connected_robot(api, readiness)
    if not identified:
        raise RuntimeError("The connected Jibo could not be identified. Refresh USB and try again.")
    entries = [entry for entry in api.saved_backups() if entry["device_tag"] == tag]
    items = saved_backup_items(entries, tag) + (
        MenuItem(OTHER_FOLDER_KEY, "Choose a backup from another folder\u2026",
                 reason="For a backup kept outside the backup folder."),)
    detail = ("Backups of the connected Jibo, newest first." if entries else
              "No backups of the connected Jibo were found in {}.".format(
                  getattr(api, "BACKUP_ROOT", "the backup folder")))
    key = select_option("Restore partitions", items,
                        prompt="Choose the backup to restore from.", detail=detail,
                        initial=items[0].key, cancel_label="Back")
    if key is None:
        return {"status": "cancelled", "message": "No restore was started."}
    if key == OTHER_FOLDER_KEY:
        return _restore_from_other_folder(api, readiness)
    entry = entries[int(key.partition(":")[2])]
    if not entry["restorable"]:
        return {"status": "cancelled", "message": "This backup is not complete, so it cannot be "
                                                  "restored. Nothing was written."}
    return _restore_from_entry(api, readiness, entry)


def _check_lines(result):
    words = {"match": "matches its checksum", "mismatch": "does not match its checksum",
             "missing": "is missing"}
    lines = ["Every saved file matches its checksum." if result.get("all_match") else
             "Some saved files do not match. Do not restore from this backup."]
    lines += ["{}: {}".format(item["name"], words.get(item["status"], item["status"]))
              for item in result.get("partitions") or ()]
    return lines


def _backup_detail_flow(api, readiness, entry, tag, identified):
    """One backup's details: Restore, Open folder, Check files or Delete.

    Returns None for Back. Opening the folder and checking files come back here.
    """
    ready = readiness.state == "dfu-ready"
    same_robot = bool(tag) and entry["device_tag"] == tag
    if not ready:
        restore_reason = _connection_reason(readiness)
    elif not identified:
        restore_reason = "The connected Jibo could not be identified. Refresh USB and try again."
    elif not same_robot:
        restore_reason = ("The connected Jibo is a different robot. Connect this backup's Jibo "
                          "to restore it.")
    elif entry.get("restore_reason"):
        restore_reason = entry["restore_reason"]
    elif not entry["restorable"]:
        restore_reason = "This backup is not complete, so it cannot be restored."
    else:
        restore_reason = ""
    deletable = entry.get("layout") == "named" and entry["status"] != "reading"
    options = (
        MenuItem("restore", "Restore from this backup", not restore_reason,
                 restore_reason or "Choose the partitions to put back, then review before "
                                   "anything is written."),
        MenuItem("open-folder", "Open folder", reason="Show this backup's files on this computer."),
        MenuItem("check-files", "Check files", entry["state"] in ("complete", "rollback-image"),
                 "Re-reads the saved files on this computer and compares their checksums. "
                 "The robot is not used." if entry["state"] in ("complete", "rollback-image") else
                 "Some files are missing from this backup."),
        MenuItem("delete", "Delete this backup", deletable,
                 "Removes this backup from this computer. The robot is not changed."
                 if deletable else "Backups made before the Backups menu share files with "
                                   "other backups, so they are not deleted here.",
                 tone="danger" if deletable else ""),
    )
    note = ""
    while True:
        detail = "\n".join(([note, ""] if note else []) +
                           backup_detail_lines(entry, tag, identified))
        choice = select_option("Backup of " + entry["robot_label"], options,
                               prompt="Choose what to do with this backup.", detail=detail,
                               cancel_label="Back")
        note = ""
        if choice is None:
            return None
        if choice == "restore":
            return _restore_from_entry(api, readiness, entry)
        if choice == "open-folder":
            if api.open_folder(entry["folder"]):
                note = "Opened this backup's folder in your file browser."
            else:
                note = "Could not open a file browser. The files are in the folder shown below."
            continue
        if choice == "check-files":
            show_screen("Check files", _check_lines(api.check_backup_set_files(entry["manifest"])))
            continue
        if not confirm_action(
                "Delete this backup?",
                "Delete the backup of {} saved {}?\n\n{} of files will be removed from this "
                "computer. This cannot be undone. The robot is not changed.\n\nFolder: {}".format(
                    entry["robot_label"], backup_time_label(entry["created_utc"]),
                    size_label(entry["total_bytes"]), entry["folder"])):
            note = "The backup was kept."
            continue
        return api.delete_backup_set(entry["manifest"])


def _saved_backups_flow(api, readiness):
    tag, identified = _connected_robot(api, readiness)
    while True:
        entries = list(api.saved_backups())
        if not entries:
            return {"status": "cancelled", "message": "No saved backups were found in {}.".format(
                getattr(api, "BACKUP_ROOT", "the backup folder"))}
        key = select_option(SAVED_BACKUPS_TITLE, saved_backup_items(entries, tag),
                            prompt="Choose a backup to see its details.",
                            detail="Newest first. Backups are kept only on this computer, "
                                   "in {}.".format(getattr(api, "BACKUP_ROOT", "the backup folder")),
                            cancel_label="Back")
        if key is None:
            return {"status": "cancelled", "message": "No backup was changed."}
        result = _backup_detail_flow(api, readiness, entries[int(key.partition(":")[2])], tag,
                                     identified)
        if result is not None:
            return result


def _backups_menu(api, readiness):
    selected = select_option(BACKUPS_TITLE, _backup_menu_options(readiness),
                             prompt="Choose a backup action.",
                             detail="Backups are saved on this computer in a folder named after "
                                    "each Jibo. Making a backup never writes to the robot.")
    if selected is None:
        return {"status": "cancelled", "message": "No backup action selected."}
    if selected in ("backup-all", "backup-choose", "restore-backup", "flash-images", "backup-var") and \
            readiness.state != "dfu-ready":
        raise RuntimeError("This backup action is available only while the robot is in DFU.")
    if selected == "backup-all":
        _, _, available = api.available_backup_partitions(port=readiness.port)
        if not available:
            raise RuntimeError("This DFU loader exposes no complete partitions for backup.")
        return _run_backup_set(api, readiness, tuple(available), available,
                               "Back up every partition")
    if selected == "backup-choose":
        return _choose_partitions_to_back_up(api, readiness)
    if selected == "restore-backup":
        return _restore_partitions_flow(api, readiness)
    if selected == "flash-images":
        return _flash_images_flow(api, readiness)
    if selected == "saved-backups":
        return _saved_backups_flow(api, readiness)
    return api.backup_var(port=readiness.port)


def execute_action(api, key, readiness):
    """One calibration policy/session for the same GUI and TUI action."""
    if not calibration_service.CALIBRATION_SHARING_AVAILABLE:
        return _execute_action(api, key, readiness)
    try:
        from jibo_calibration_flow import SharingFlow, operation
    except ImportError:
        return _execute_action(api, key, readiness)  # Older packaged offline installs remain usable.
    with operation(SharingFlow(api, select_option)) as sharing:
        result = _execute_action(api, key, readiness)
        if isinstance(result, dict) and sharing.outcome is not None:
            result = dict(result, calibration_sharing=sharing.outcome,
                          calibration_sharing_detail=sharing.detail)
        return result


def _execute_action(api, key, readiness):
    """Run an action selected from the main screen using the shared curses prompts."""
    if key == "enter-dfu-shofel":
        if readiness.state != "rcm":
            raise RuntimeError("ShofEL DFU entry is available only while the robot is in RCM/APX.")
        if not api.shofel_dfu_available():
            raise RuntimeError("The launch-enabled ShofEL DFU tool and payload are unavailable.")
        if not confirm_action(
                "Enter DFU with ShofEL",
                "Use the Meerkat Rev02 SDRAM profile (RAM slot 2 or 3, selected "
                "from the robot's strap) to start recovery on USB port {}?\n"
                "Slot 3 has not been tested on a live robot. No partition write "
                "is performed during entry.".format(
                    readiness.port)):
            return {"status": "cancelled", "message": "DFU entry was cancelled."}
        return api.enter_shofel_dfu(port=readiness.port,
                                    confirm_meerkat_rev02=True)
    if key in ("calibration-sharing", "calibration-lookup", "calibration-remove"):
        calibration_service.require_available()
        from jibo_calibration_flow import active
        sharing = active()
        identity = api.calibration_identity(port=readiness.port)
        if key == "calibration-sharing":
            return sharing.settings(identity)
        if key == "calibration-remove":
            return sharing.remove(identity)
        download = sharing.lookup_for_fresh_setup(identity)
        return {"status": "complete" if download else "cancelled",
                "message": "Reviewed calibration verified; no robot write was made. Use fresh setup to apply it with local identity." if download else "No downloaded calibration used."}
    if key == "backup-var":
        return api.backup_var(port=readiness.port)
    if key == "probe-dfu-gpt":
        if readiness.state != "dfu-ready":
            raise RuntimeError("The partition layout check is available only while the robot is in DFU.")
        return api.probe_dfu_gpt(port=readiness.port)
    if key == "set-mode":
        mode = _select_mode()
        if mode is None:
            return {"status": "cancelled", "message": "No mode selected."}
        direct_edit = _can_edit_var_files(readiness)
        confirmation = lambda plan: confirm_action(
            "Confirm mode change",
            _confirmation_details(plan, "Review the file change before changing the robot mode."
                                  if plan.get("changes") else
                                  "Review the var write plan before changing the robot mode."))
        if direct_edit:
            try:
                return api.set_mode_file_live(mode, port=readiness.port,
                                              confirmation=confirmation, guided=True)
            except Exception as exc:
                if not _needs_full_var_fallback(api, exc):
                    raise
                if not _offer_full_var_edit(exc):
                    return {"status": "cancelled", "message": "The mode was not changed."}
        return api.set_mode_live(mode, port=readiness.port,
                                 confirmation=confirmation)
    if key == "configure-wifi":
        ssid = text_input("Configure Wi-Fi", "Wi-Fi network name (SSID):")
        if ssid is None:
            return {"status": "cancelled", "message": "Wi-Fi setup was cancelled."}
        kind = select_option(
            "Choose network type",
            (("protected", "Protected WPA/WPA2 network"), ("open", "Open network")),
            prompt="Choose the security type for this network.")
        if kind is None:
            return {"status": "cancelled", "message": "No network type selected."}
        open_network = kind == "open"
        password = None
        if not open_network:
            password = _confirmed_wifi_password("Configure Wi-Fi")
            if password is None:
                return {"status": "cancelled", "message": "Wi-Fi setup was cancelled."}
        direct_edit = _can_edit_var_files(readiness)
        confirmation = lambda plan: confirm_action(
            "Confirm Wi-Fi change",
            _confirmation_details(plan, "Review the file change before saving this network."
                                  if plan.get("changes") else
                                  "Review the var write plan before saving this network."))
        try:
            if direct_edit:
                try:
                    return api.configure_wifi_file_live(
                        ssid, password, open_network, port=readiness.port,
                        confirmation=confirmation, guided=True)
                except Exception as exc:
                    if not _needs_full_var_fallback(api, exc):
                        raise
                    if not _offer_full_var_edit(exc, includes_wifi=True):
                        return {"status": "cancelled", "message": "Wi-Fi was not changed."}
            return api.configure_wifi_live(ssid, password, open_network,
                                           port=readiness.port, confirmation=confirmation)
        finally:
            password = None
    if key == "set-mode-wifi":
        mode = _select_mode("Set mode and configure Wi-Fi")
        if mode is None:
            return {"status": "cancelled", "message": "No mode selected."}
        ssid = text_input("Set mode and configure Wi-Fi", "Wi-Fi network name (SSID):")
        if ssid is None:
            return {"status": "cancelled", "message": "Wi-Fi setup was cancelled."}
        kind = select_option(
            "Choose network type",
            (("protected", "Protected WPA/WPA2 network"), ("open", "Open network")),
            prompt="Choose the security type for this network.")
        if kind is None:
            return {"status": "cancelled", "message": "No network type selected."}
        open_network = kind == "open"
        password = None
        if not open_network:
            password = _confirmed_wifi_password("Set mode and configure Wi-Fi")
            if password is None:
                return {"status": "cancelled", "message": "Wi-Fi setup was cancelled."}
        network_status = None
        if mode in ("developer", "int-developer"):
            selected = select_option(
                "Boot screen network status",
                (("install", "Show Wi-Fi status and IP on the boot screen"),
                 ("keep", "Leave the current boot-screen setting unchanged"),
                 ("remove", "Remove the boot-screen Wi-Fi/IP status")),
                prompt="This diagnostic uses only local network status, not the cloud.",
                detail="Install/remove uses one full 500 MiB var transaction with mode and Wi-Fi, "
                       "rollback backup and SHA-256 readback. No services partition is rewritten. "
                       "Unknown SSM hashes safely keep the stock screen.", initial="install")
            if selected is None:
                return {"status": "cancelled", "message": "Mode and Wi-Fi were not changed."}
            if selected not in ("install", "keep", "remove"):
                raise ValueError("Unknown boot-screen setting.")
            network_status = None if selected == "keep" else selected
        confirmation = lambda plan: confirm_action(
            "Confirm mode and Wi-Fi change",
            _confirmation_details(plan, "Review all changes before writing to the robot."))
        try:
            if network_status is not None:
                return api.set_mode_wifi_live(
                    mode, ssid, password, open_network, port=readiness.port,
                    confirmation=confirmation, network_status=network_status)
            if _can_edit_var_files(readiness):
                try:
                    return api.set_mode_wifi_file_live(
                        mode, ssid, password, open_network, port=readiness.port,
                        confirmation=confirmation, guided=True)
                except Exception as exc:
                    if not _needs_full_var_fallback(api, exc):
                        raise
                    if not _offer_full_var_edit(exc, includes_wifi=True):
                        return {"status": "cancelled", "message": "Mode and Wi-Fi were not changed."}
            return api.set_mode_wifi_live(mode, ssid, password, open_network,
                                          port=readiness.port, confirmation=confirmation)
        finally:
            password = None
    if key == "remove-boot-network-status":
        if readiness.state != "dfu-ready":
            raise RuntimeError("Boot-screen removal is available only while the robot is in DFU.")
        confirmation = lambda plan: confirm_action(
            "Confirm boot-screen status removal",
            _confirmation_details(plan, "Remove only the diagnostic var files and loopback hook."))
        return api.boot_network_status_live("remove", port=readiness.port, confirmation=confirmation)
    if key == "flash-update":
        return _run_update(api, port=readiness.port)
    if key == "backups":
        return _backups_menu(api, readiness)
    if key == "more-tools":
        options = []
        if readiness.state == "dfu-ready":
            quick = " (quick edit)" if _can_edit_var_files(readiness) else ""
            options.extend((("set-mode", "Set robot mode" + quick),
                            ("configure-wifi", "Add or update a Wi-Fi network" + quick),
                            ("probe-dfu-gpt", "Check partition layout"),
                            ("write-var", "Write an edited var image"),
                            ("remove-boot-network-status", "Remove boot-screen Wi-Fi/IP status")))
            if calibration_service.CALIBRATION_SHARING_AVAILABLE:
                options.extend((("calibration-sharing", "Calibration sharing"),
                                ("calibration-lookup", "Look up this Jibo's calibration"),
                                ("calibration-remove", "Remove my shared calibration")))
        options.extend((("inspect-backup", "Inspect a local var backup"),
                        ("edit-backup", "Edit a local var backup")))
        selected = select_option("More tools", options,
                                 prompt="Choose a maintenance or local-image action.")
        if selected is None:
            return {"status": "cancelled", "message": "No tool selected."}
        return execute_action(api, selected, readiness)
    if key == "backup-partitions":
        _, _, available = api.available_backup_partitions(port=readiness.port)
        if not available:
            raise RuntimeError("This DFU loader exposes no complete GPT partitions for backup.")
        choices = tuple((name, "{}  ({:.1f} MiB)".format(
            name, extent["size_bytes"] / (1024 * 1024)))
            for name, extent in available.items())
        selected = select_multiple_options("Back up partitions", choices, initial=("var",))
        if selected is None:
            return {"status": "cancelled", "message": "No backup was started."}
        return api.backup_partitions(selected, port=readiness.port)
    if key == "restore-partitions":
        source = text_input("Restore partitions", "Path to backup-set.json or its folder:")
        if source is None or not source.strip():
            return {"status": "cancelled", "message": "No restore was started."}
        manifest = Path(source.strip()).expanduser()
        if manifest.is_dir():
            manifest /= "backup-set.json"
        backup_set = json.loads(manifest.read_text())
        entries = backup_set.get("partitions", ())
        if not entries:
            raise RuntimeError("The selected backup set lists no partitions.")
        choices = tuple((entry["name"], "{}  ({:.1f} MiB)".format(
            entry["name"], entry["size_bytes"] / (1024 * 1024)))
            for entry in entries)
        selected = select_multiple_options("Choose partitions to restore", choices,
                                           initial=tuple(entry["name"] for entry in entries))
        if selected is None:
            return {"status": "cancelled", "message": "No restore was started."}
        confirmation = lambda plan: confirm_action(
            "Confirm partition restore",
            _confirmation_details(plan, "These partitions will be replaced with the saved images."))
        return api.restore_partitions(manifest, selected, port=readiness.port,
                                      confirmation=confirmation)
    if key == "write-var":
        image = text_input("Write an edited var image", "Path to edited 500 MiB var image:")
        if image is None or not image.strip():
            return {"status": "cancelled", "message": "No image selected."}
        confirmation = lambda plan: confirm_action(
            "Confirm var write",
            _confirmation_details(plan, "Review the var write plan before writing to the robot."))
        return api.write_var(image.strip(), port=readiness.port,
                             confirmation=confirmation)
    if key == "inspect-backup":
        image = text_input("Inspect a var backup", "Path to var backup image:")
        if image is None or not image.strip():
            return {"status": "cancelled", "message": "No image selected."}
        return api.images.inspect_var(image.strip())
    if key == "edit-backup":
        choice = select_option(
            "Edit a local var backup",
            (("mode", "Change mode"), ("wifi", "Add or update Wi-Fi network"),
             ("inspect", "Inspect image")),
            prompt="Choose a local image action.",
            detail="These actions create no robot writes.")
        if choice is None:
            return {"status": "cancelled", "message": "No offline action selected."}
        source = text_input("Edit a local var backup", "Path to var backup image:")
        if source is None or not source.strip():
            return {"status": "cancelled", "message": "No image selected."}
        source = source.strip()
        if choice == "inspect":
            return api.images.inspect_var(source)
        if choice == "mode":
            mode = _select_mode("Choose mode for the local image")
            if mode is None:
                return {"status": "cancelled", "message": "No mode selected."}
            return api.images.edit_mode(source, api._default_edit_path(source), mode)

        ssid = text_input("Configure Wi-Fi in a local image", "Wi-Fi network name (SSID):")
        if ssid is None:
            return {"status": "cancelled", "message": "Wi-Fi edit was cancelled."}
        kind = select_option(
            "Choose network type",
            (("protected", "Protected WPA/WPA2 network"), ("open", "Open network")),
            prompt="Choose the security type for this network.")
        if kind is None:
            return {"status": "cancelled", "message": "Wi-Fi edit was cancelled."}
        open_network = kind == "open"
        password = None
        if not open_network:
            password = _confirmed_wifi_password("Configure Wi-Fi in a local image")
            if password is None:
                return {"status": "cancelled", "message": "Wi-Fi edit was cancelled."}
        try:
            return api.images.edit_wifi(source, api._default_edit_path(source), ssid,
                                        password, open_network)
        finally:
            password = None
    raise ValueError("Unknown menu action: " + str(key))


def run(api_module):
    """Run the UI for an interactive terminal; return None when there is none."""
    if not _terminal_available():
        return None

    app = TerminalMenu(api_module)
    while True:
        app.command = None
        curses.wrapper(app.session)
        if app.command in (None, "quit"):
            return 0
        if app.command == "rcm-help":
            show_screen(RCM_HELP_TITLE, rcm_help_lines())
            continue
        action = app.command
        label = next(item.label for item in app.items if item.key == action)
        try:
            result = execute_action(api_module, action, app.readiness)
            if action != "enter-dfu-shofel" or not (
                    isinstance(result, dict) and result.get("state") == "dfu"):
                show_screen(label, _result_summary(action, result))
        except KeyboardInterrupt:
            show_screen(label, "Cancelled.")
        except Exception as exc:
            current = tuple(api_module.devices())
            if not current and app.readiness.devices:
                show_screen("Robot disconnected", "The robot is no longer visible over USB.\n" + str(exc))
            else:
                show_screen("Action failed", str(exc))
        app.refresh()


HOSTED_DOWNLOAD_ATTEMPTS = 6
HOSTED_DOWNLOAD_BACKOFF = (2, 5, 10, 20, 30)


def _download_error_chain(error):
    """Transport wrappers can retain HTTP headers on a chained exception."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend((getattr(current, '__cause__', None), getattr(current, '__context__', None)))


def _transient_download_error(error):
    code = _error_code(error)
    if code in ('offline', 'rate_limited'):
        return True
    if code != 'http':
        return False
    for current in _download_error_chain(error):
        status = getattr(current, 'code', None)
        if isinstance(status, int):
            return status == 429 or 500 <= status <= 599
    match = re.fullmatch(r'Hosted HTTP status (\d{3})', str(error))
    if match:
        status = int(match.group(1))
        return status == 429 or 500 <= status <= 599
    # HTTP also represents signed-size/range/encoding contract failures.
    # Retry only the downloader's explicitly incomplete transport messages.
    return str(error) in ('Archive was truncated; partial retained for exact-ID resume',
                          'Hosted metadata was truncated')


def _download_retry_delay(error, attempt):
    """Never shorten a valid server Retry-After below the selected backoff."""
    from email.utils import parsedate_to_datetime
    import math
    delay = HOSTED_DOWNLOAD_BACKOFF[attempt - 1]
    for current in _download_error_chain(error):
        value = getattr(current, 'retry_after', None)
        headers = getattr(current, 'headers', None)
        if value is None and headers is not None:
            value = headers.get('Retry-After')
        if value is None:
            continue
        try:
            text = str(value).strip()
            seconds = float(text) if text.isascii() and text.isdecimal() else None
            if seconds is None:
                date = parsedate_to_datetime(text)
                if date.tzinfo is not None:
                    seconds = max(0.0, date.timestamp() - time.time())
            if seconds is not None and math.isfinite(seconds) and seconds >= 0:
                delay = max(delay, seconds)
        except (ValueError, TypeError, OverflowError):
            pass
    return delay


def _check_download_cancel(cancel, hosted):
    if cancel is not None and cancel.cancelled:
        raise hosted.HostedError('canceled', 'Download canceled. Nothing was written to the robot.')


def _wait_for_download_retry(seconds, cancel, hosted):
    deadline = time.monotonic() + seconds
    while True:
        _check_download_cancel(cancel, hosted)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def download_for_flash(hosted, image_id, *, dest_dir, progress=None, cancel=None):
    """Resume transient failures in one flow; never retry untrusted image data."""
    from types import SimpleNamespace
    pinned_id = None if image_id == 'latest' else image_id
    for attempt in range(1, HOSTED_DOWNLOAD_ATTEMPTS + 1):
        _check_download_cancel(cancel, hosted)
        try:
            if pinned_id is None:
                catalog = hosted.fetch_catalog(cache_dir=Path(dest_dir) / '.hosted', cancel=cancel)
                pinned_id = catalog.latest_id
                if not pinned_id:
                    raise hosted.HostedError('incompatible', 'No latest Phoenix image is offered.')
            return hosted.download_image(pinned_id, dest_dir=dest_dir, progress=progress, cancel=cancel)
        except hosted.HostedError as error:
            _check_download_cancel(cancel, hosted)
            if not _transient_download_error(error):
                raise
            if attempt == HOSTED_DOWNLOAD_ATTEMPTS:
                message = hosted_error_text('download-stopped')
                if _error_code(error) in ('http', 'rate_limited'):
                    message += ' ' + hosted_error_text(_error_code(error), str(error))
                raise hosted.HostedError('download-stopped', message) from error
            message = 'Connection dropped, resuming… (attempt {} of {})'.format(attempt + 1, HOSTED_DOWNLOAD_ATTEMPTS)
            if progress is not None:
                progress(SimpleNamespace(phase='resume', bytes=0, total=None, percent=None,
                                         rate=None, eta=None, message=message))
            else:
                print(message, flush=True)
            _wait_for_download_retry(_download_retry_delay(error, attempt), cancel, hosted)
