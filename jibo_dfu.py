#!/usr/bin/env python3
"""Guided terminal menu for Jibo recovery, var backups, and safe image edits."""
import argparse
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import struct
import sys
import tempfile
import time

import jibo_dfu_bounded as bounded
import jibo_images as images
import jibo_updates as updates
import jibo_flash_policy as flash_policy
import jibo_hosted_updates as hosted
import jibo_calibration_client as calibration_service


ROOT = Path(__file__).resolve().parent
RCM = ("0955", "7740")
DFU = ("0955", "701a")
MARKER = "jibo-dfu-v1"
FILE_LEVEL_MARKER = "jibo-file-v1"
FILE_LEVEL_PARTITIONS = ("rootfsA", "rootfsB", "services", "skills", "var")
FILE_LEVEL_MAX_BYTES = 4096
FILE_RPC_MAGIC = b"JIBOFL1\0"
FILE_RPC_RESPONSE_MAGIC = b"JIBOR1\0\0"
FILE_RPC_HEADER = struct.Struct("<8sBHI16s")
FILE_RPC_RESPONSE_HEADER = struct.Struct("<8s16sBI32s")
FILE_RPC_READ = 1
FILE_RPC_WRITE = 2
FILE_RPC_STAT = 3
FILE_STAT_STRUCT = struct.Struct("<QIIIIIII16s")
FILE_WRITE_PRECONDITION = struct.Struct("<QIIIII16s32s")
FILE_RPC_MAX_REQUEST = (FILE_RPC_HEADER.size + 255 +
                        FILE_WRITE_PRECONDITION.size + FILE_LEVEL_MAX_BYTES)
EXPECTED_VAR_SIZE = 524_288_000
WRITE_CONFIRMATION = "WRITE VAR"
UPDATE_CONFIRMATION = "FLASH UPDATE"
UPDATE_ORDER = ("rootfsA", "rootfsB", "services", "skills", "var")
SKILLS_SECTOR_SIZE = 512
SKILLS_CHUNK_SECTORS = 0x200000
SKILLS_CHUNK_BYTES = SKILLS_SECTOR_SIZE * SKILLS_CHUNK_SECTORS
DEFAULT_LOADER = ROOT / "loader.bin"
if not DEFAULT_LOADER.is_file() and (ROOT / "assets" / "loader.bin").is_file():
    # Source checkouts keep the pinned loader in assets/; packages carry it at the root.
    DEFAULT_LOADER = ROOT / "assets" / "loader.bin"
PIPELINE_HELPER = ROOT / "jibo_dfu_pipeline.py"


def _invoking_user():
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        try:
            uid = int(os.environ["SUDO_UID"])
            gid = int(os.environ.get("SUDO_GID", "-1"))
            return uid, gid, Path(pwd.getpwuid(uid).pw_dir)
        except (ValueError, KeyError, OSError):
            pass
    return None


def _chown_to_invoking_user(path):
    owner = _invoking_user()
    if owner:
        os.chown(path, owner[0], owner[1])


_OWNER = _invoking_user()
BACKUP_ROOT = (_OWNER[2] if _OWNER else Path.home()) / "Jibo-Backups"


class DfuError(Exception):
    pass


class FileRpcStatusError(DfuError):
    def __init__(self, status, detail=None):
        self.status = status
        if status == 3:
            message = "The file or ext4 filesystem uses a layout the RAM loader does not support (file mailbox status 3)."
        elif status == 4:
            message = ("The ext4 journal needs recovery or could not be verified cleanly "
                       "(file mailbox status 4). The loader refused to read the target file or "
                       "write the partition. A clean Linux unmount can clear the journal; "
                       "the guided menu also offers a full-var transfer.")
        else:
            message = "The file mailbox rejected the request with status {}.".format(status)
        if detail:
            message += " " + detail
        super().__init__(message)


def devices(root=None):
    if root is None and sys.platform == "darwin":
        try:
            return bounded.list_recovery_devices()
        except bounded.BoundedDfuError as exc:
            raise DfuError(str(exc)) from exc
    root = Path(root) if root is not None else Path("/sys/bus/usb/devices")
    found = []
    for path in sorted(root.glob("*")):
        try:
            pair = tuple((path / name).read_text().strip().lower()
                         for name in ("idVendor", "idProduct"))
        except OSError:
            continue
        if pair in (RCM, DFU):
            found.append({"port": path.name, "state": "rcm" if pair == RCM else "dfu"})
    return found


def select_device(found, port=None):
    selected = [device for device in found if port is None or device["port"] == port]
    if len(selected) > 1:
        raise DfuError("Multiple robots connected; select one using --port.")
    return selected[0] if selected else None


def run(argv, timeout=30):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                env=_runtime_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DfuError(str(exc)) from exc
    if result.returncode:
        detail = re.sub(r'(serial=")[^"]*(")', r"\1[redacted]\2", result.stdout + result.stderr)
        raise DfuError("Command failed: " + argv[0] + "\n" + detail)
    return result.stdout + result.stderr


def _runtime_env():
    env = os.environ.copy()
    if (ROOT / "lib").is_dir():
        env["LD_LIBRARY_PATH"] = str(ROOT / "lib") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def _transfer_output_size(path):
    """Track a completed output or its private, in-progress temporary sibling."""
    path = Path(path)
    try:
        return path.stat().st_size
    except OSError:
        pass
    try:
        return max((candidate.stat().st_size for candidate in
                    path.parent.glob(path.name + ".tmp.*") if candidate.is_file()),
                   default=0)
    except OSError:
        return 0


def _byte_progress_from_log(log):
    """Read the latest `Staged N / M bytes` status without moving the writer offset."""
    try:
        size = os.fstat(log.fileno()).st_size
        if size <= 0:
            return None
        start = max(0, size - 512)
        tail = os.pread(log.fileno(), size - start, start)
    except (AttributeError, OSError, ValueError):
        return None
    matches = re.findall(rb"Staged\s+(\d+)\s*/\s*(\d+)\s+bytes", tail)
    if not matches:
        return None
    transferred, total = (int(value) for value in matches[-1])
    if total <= 0:
        return None
    return min(transferred, total), total


def _dfu_download_progress_from_log(log, expected_size):
    """Read dfu-util's latest Download byte count from its carriage-return log."""
    try:
        size = os.fstat(log.fileno()).st_size
        if size <= 0:
            return None
        start = max(0, size - 2048)
        tail = os.pread(log.fileno(), size - start, start)
    except (AttributeError, OSError, ValueError):
        return None
    matches = re.findall(rb"Download\s+\[[^\]\r\n]*\]\s+\d+%\s+(\d+)\s+bytes", tail)
    return min(int(matches[-1]), expected_size) if matches else None


def _dfu_download_final_count(output, allow_progress_completion=False):
    """Require an exact final byte count; bounded mailboxes accept dfu-util 0.9's completion log."""
    totals = re.findall(r"Sent a total of (\d+) bytes", output)
    if totals:
        return int(totals[-1])
    if not allow_progress_completion:
        return None
    completed = re.findall(r"Download\s+\[[^\]\r\n]*\]\s+100%\s+(\d+)\s+bytes", output)
    if (not completed or "Download done." not in output or "Done!" not in output or
            not re.search(r"state\(2\)\s*=\s*dfuIDLE,\s*status\(0\)", output)):
        return None
    return int(completed[-1])


def run_with_progress(argv, timeout, label, cwd=None, progress_path=None, progress_size=None,
                      byte_progress=False, download_size=None,
                      allow_progress_completion=False, display=True):
    """Run a quiet transfer with progress when byte counts are available."""
    started = time.monotonic()
    terminal = sys.stderr
    interactive = terminal.isatty()
    if display and not interactive:
        print(label + "...", file=terminal, flush=True)
    elif display and ((progress_path is not None and progress_size) or download_size):
        print(label, file=terminal, flush=True)

    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                       text=True, env=_runtime_env(), cwd=cwd)
            timed_out = False
            spinner = "|/-\\"
            frames = 0
            last_status_length = 0
            last_report = started
            try:
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout:
                        process.kill()
                        process.wait()
                        timed_out = True
                        break
                    byte_transfer = _byte_progress_from_log(log) if byte_progress else None
                    downloaded = (_dfu_download_progress_from_log(log, download_size)
                                  if download_size else None)
                    if byte_transfer is not None:
                        transferred, total = byte_transfer
                        filled = int(20 * transferred / total)
                        status = "[{}{}] {:5.1f}% | {:,}/{:,} KiB | {:0.0f}s".format(
                            "#" * filled, "-" * (20 - filled),
                            100 * transferred / total,
                            transferred // 1024, total // 1024, elapsed)
                    elif download_size or (progress_path is not None and progress_size):
                        total_size = download_size or progress_size
                        if download_size:
                            transferred = downloaded if downloaded is not None else 0
                        else:
                            transferred = min(_transfer_output_size(progress_path), total_size)
                        mib = 1024 * 1024
                        filled = int(20 * transferred / total_size)
                        status = "[{}{}] {:5.1f}% | {:.1f}/{:.1f} MiB | {:.2f} MiB/s | {:0.0f}s".format(
                            "#" * filled, "-" * (20 - filled),
                            100 * transferred / total_size,
                            transferred / mib, total_size / mib,
                            transferred / mib / max(elapsed, 0.001), elapsed)
                    else:
                        status = "{} {} {:0.0f}s".format(label, spinner[frames % len(spinner)], elapsed)
                    if display and interactive:
                        terminal.write("\r" + status)
                        terminal.flush()
                        last_status_length = len(status)
                        frames += 1
                    elif display and (progress_path is not None or byte_transfer is not None or download_size) and \
                            elapsed - (last_report - started) >= 5:
                        print(status, file=terminal, flush=True)
                        last_report = time.monotonic()
                    time.sleep(0.15)
            except KeyboardInterrupt as exc:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                if display and interactive:
                    terminal.write("\r" + " " * last_status_length + "\r")
                    terminal.flush()
                raise DfuError("The partition read was interrupted; the partial dump was discarded.") from exc

            elapsed = time.monotonic() - started
            if display and interactive:
                terminal.write("\r" + " " * last_status_length + "\r")
            result_code = process.returncode
            log.seek(0)
            output = log.read()

    except OSError as exc:
        raise DfuError("Could not start transfer command: " + str(exc)) from exc

    command = Path(argv[1]).name if argv[0] == sys.executable and len(argv) > 1 else argv[0]
    if timed_out:
        raise DfuError("Command timed out after {} seconds: {}\n{}".format(
            timeout, command, _transfer_error_detail(output)))
    if result_code:
        raise DfuError("Command failed: {}\n{}".format(command, _transfer_error_detail(output)))
    if download_size:
        transferred = _dfu_download_final_count(output, allow_progress_completion)
        if transferred != download_size:
            reported = str(transferred) if transferred is not None else "no byte count"
            raise DfuError("DFU write ended after reporting {} bytes; expected {}. "
                           "No further partition writes were attempted. {}".format(
                               reported, download_size, _transfer_error_detail(output)))
    completion = label.replace("Reading", "Read", 1)
    if display:
        print("{} in {:.1f}s.".format(completion, elapsed), file=terminal, flush=True)
    return output


def _call_with_progress(callback, label):
    """Show elapsed time while a local package check or image prep runs."""
    started = time.monotonic()
    terminal = sys.stderr
    interactive = terminal.isatty()
    if not interactive:
        print(label + "...", file=terminal, flush=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def checked_work():
            import jibo_flash_contract as contract
            def report(event):
                import jibo_tui
                print("\r" + jibo_tui.format_progress(event), file=terminal, flush=True)
            with contract.operation(report):
                if label.startswith('Preparing'):
                    contract.phase('prepare')
                return callback()
        future = pool.submit(checked_work)
        frames = 0
        spinner = "|/-\\"
        last_length = 0
        while not future.done():
            if interactive:
                status = "{} {} {:0.0f}s".format(label, spinner[frames % 4], time.monotonic() - started)
                terminal.write("\r" + status)
                terminal.flush()
                last_length = len(status)
                frames += 1
            time.sleep(0.15)
        if interactive:
            terminal.write("\r" + " " * last_length + "\r")
            terminal.flush()
        result = future.result()
    print("{} completed in {:.1f}s.".format(label, time.monotonic() - started),
          file=terminal, flush=True)
    return result


def _transfer_error_detail(output):
    detail = re.sub(r'(serial=")[^"]*(")', r"\1[redacted]\2", output).strip()
    detail = re.sub(r'(Chip ID:\s*)[^\r\n]*',
                    r"\1[redacted]", detail, flags=re.IGNORECASE)
    if len(detail) > 2000:
        detail = "…\n" + detail[-2000:]
    return detail


def tool(name, override=None):
    override = override or (os.environ.get("JIBO_DFU_UTIL") if name == "dfu-util" else None)
    candidate = override or str(ROOT / "tools" / name)
    # Linux packages can leave ELF executables in tools/; Macs use native tools.
    if (override or sys.platform != "darwin") and Path(candidate).is_file():
        return str(Path(candidate).resolve())
    if override:
        raise DfuError("Tool does not exist: " + override)
    located = shutil.which(name)
    if not located:
        raise DfuError("Missing " + name + "; install it or provide --" + name + ".")
    return located


def _usbfs_available():
    return sys.platform.startswith("linux") and Path("/dev/bus/usb").is_dir()


def _partition_transfer_argv(dfu_util, port, alternative, option, path, size=None,
                             alternative_size=None):
    """Build a whole-alternative read (-U) or write (-D) command.

    The pipelined helper queues DFU requests instead of waiting a USB round
    trip for each block. JIBO_DFU_TRANSFER=dfu-util selects dfu-util. A read
    of the first size bytes of a larger alternative still reads the helper's
    whole alternative_size, so the loader's read cursor ends at the start.
    """
    mode = os.environ.get("JIBO_DFU_TRANSFER", "pipelined")
    if mode not in ("pipelined", "dfu-util"):
        raise DfuError("JIBO_DFU_TRANSFER must be pipelined or dfu-util.")
    argv = ["-d", "0955:701a", "--path", port, "-a", alternative, option, str(path)]
    if option == "-U":
        argv += ["-Z", str(size)]
    if mode == "pipelined" and PIPELINE_HELPER.is_file() and _usbfs_available():
        if option == "-U" and alternative_size is not None and alternative_size != size:
            argv[-1] = str(alternative_size)
            argv += ["--prefix", str(size)]
        return [sys.executable, str(PIPELINE_HELPER)] + argv
    return [dfu_util] + argv


def _shofel_dfu_tool(override=None):
    """Find a matched ShofEL host and launch-enabled RAM payload."""
    candidate = override or os.environ.get("JIBO_SHOFEL2") or str(ROOT / "tools" / "shofel2_t124")
    # tools/ may hold Linux ELF executables from a package build; Macs use a native host.
    if candidate == str(ROOT / "tools" / "shofel2_t124") and sys.platform == "darwin":
        candidate = None
    if not candidate or not Path(candidate).is_file():
        if override:
            raise DfuError("ShofEL host tool does not exist: " + str(override))
        candidate = shutil.which("shofel2_t124")
    if not candidate:
        raise DfuError("Missing shofel2_t124; run the launcher to build it, install it, or provide --shofel.")
    executable = str(Path(candidate).resolve())
    if not os.access(executable, os.X_OK):
        raise DfuError("ShofEL host tool is not executable: " + executable)
    intermezzo = Path(executable).parent / "intermezzo.bin"
    if not intermezzo.is_file() or intermezzo.stat().st_size == 0:
        raise DfuError("Missing non-empty intermezzo.bin next to " + executable)
    payload = Path(executable).parent / "dfu_stage2.bin"
    if not payload.is_file() or payload.stat().st_size == 0:
        raise DfuError("Missing non-empty dfu_stage2.bin next to " + executable)
    capability = run([executable, "--dfu-stage-capability"], timeout=5).strip()
    if capability != "dfu-stage-launch=1":
        raise DfuError("This ShofEL host cannot start the RAM DFU loader. Build and package the launch-enabled host and matching stage payload.")
    return executable


def shofel_dfu_available():
    """Return whether the local ShofEL pair can start the RAM DFU loader."""
    try:
        _shofel_dfu_tool()
        return DEFAULT_LOADER.is_file()
    except (DfuError, OSError):
        return False


def enter_shofel_dfu(port=None, shofel=None, loader=None, dfu_util=None,
                     timeout=120, confirm_meerkat_rev02=False):
    """Start the pinned RAM loader through ShofEL, then confirm DFU on the same port."""
    if not confirm_meerkat_rev02:
        raise DfuError("Confirm the Meerkat Rev02 SDRAM profile before entering DFU through ShofEL.")
    if not 0 < timeout <= 600:
        raise DfuError("Timeout must be between 0 and 600 seconds.")
    executable = _shofel_dfu_tool(shofel)
    image = Path(loader) if loader is not None else DEFAULT_LOADER
    if image.is_symlink() or not image.is_file():
        raise DfuError("Missing or symlinked RAM DFU loader image: " + str(image))
    image = image.resolve()
    selected = select_device(devices(), port)
    if selected is None or selected["state"] != "rcm":
        raise DfuError("Connect the robot in RCM/APX before entering DFU through ShofEL.")
    port = selected["port"]
    dfu_util = tool("dfu-util", dfu_util)
    output = run_with_progress(
        [executable, "--usb-port-path", port, "DFU_STAGE", str(image),
         "--confirm-meerkat-rev02", "--launch"],
        timeout=180, label="Loading the RAM DFU program", cwd=str(Path(executable).parent),
        byte_progress=True)
    if "Starting verified ARM-state SPL at 0x80108000." not in output:
        raise DfuError("ShofEL did not confirm the loader launch.")

    print("Waiting for DFU on USB port " + port + "…", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = select_device(devices(), port)
        if current and current["state"] == "dfu":
            names, _ = _settled_dfu_alternatives(dfu_util, port, deadline=deadline)
            return {"port": port, "state": "dfu", "already_running": False,
                    "loader_verified": True, "alternatives": names,
                    "entry_transport": "ShofEL"}
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    raise DfuError("The RAM loader started, but DFU did not appear on USB port " +
                   port + ". Check USB forwarding and the robot's connection.")


def dfu_alternatives(executable, port):
    deadline = _DFU_LISTING_DEADLINE.get()
    options = {} if deadline is None else {"timeout": max(0.001, min(30, deadline - time.monotonic()))}
    output = run([executable, "-d", "0955:701a", "--path", port, "-l"], **options)
    names = re.findall(r'name="([^"]+)"', output)
    return names, output


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_operation_dir(out=None, prefix="var-backup"):
    if out is not None:
        directory = Path(out).expanduser().resolve()
        if directory.exists():
            raise DfuError("Output directory already exists; choose a new directory: " + str(directory))
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700)
    else:
        BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        BACKUP_ROOT.chmod(0o700)
        _chown_to_invoking_user(BACKUP_ROOT)
        directory = Path(tempfile.mkdtemp(prefix=prefix + "-", dir=BACKUP_ROOT))
    directory.chmod(0o700)
    _chown_to_invoking_user(directory)
    return directory


def _private_write(path, content):
    path = Path(path)
    path.write_text(json.dumps(content, indent=2) + "\n")
    path.chmod(0o600)
    _chown_to_invoking_user(path)


def _sha256_file(path):
    return images.sha256_file(path)


def _dfu_context(port, dfu_util, include_output=False):
    selected = select_device(devices(), port)
    if selected is None:
        raise DfuError("No Jibo RCM/DFU device detected. Connect the robot by USB first.")
    if selected["state"] != "dfu":
        raise DfuError("The robot is in RCM/APX. Enter DFU first, then run this command again.")
    names, output = _settled_dfu_alternatives(dfu_util, selected["port"])
    reported = None
    for line in output.splitlines():
        if re.search(r'name="var"', line):
            match = re.search(r"\bsize\s*=\s*(\d+)", line)
            if match:
                reported = int(match.group(1))
                break
    if reported is not None and reported != EXPECTED_VAR_SIZE:
        raise DfuError("The device reports a var size of " + str(reported) +
                       " bytes; this candidate profile expects 524288000. No transfer was attempted.")
    serial = re.search(r'serial="([^"]*)"', output)
    serial_value = serial.group(1).strip() if serial else ""
    if serial_value and serial_value.lower() not in {"unknown", "none", "null", "n/a"} and \
            serial_value.strip("0"):
        device_tag = "serial-sha256:" + hashlib.sha256(serial_value.encode("utf-8")).hexdigest()
    else:
        device_tag = "usb-identity-unavailable"
    context = (selected["port"], names, device_tag)
    return context + (output,) if include_output else context


def _upload_var(dfu_util, port, destination):
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    try:
        run_with_progress(
            _partition_transfer_argv(dfu_util, port, "var", "-U", destination, EXPECTED_VAR_SIZE),
            timeout=900, label="Reading the 500 MiB var partition from USB",
            progress_path=destination, progress_size=EXPECTED_VAR_SIZE)
        if not destination.is_file():
            raise DfuError("The DFU read completed without creating the var image.")
        destination.chmod(0o600)
        _chown_to_invoking_user(destination)
        size = destination.stat().st_size
        if size != EXPECTED_VAR_SIZE:
            raise DfuError("The uploaded var image is " + str(size) + " bytes; expected 524288000.")
        return _sha256_file(destination)
    except (DfuError, OSError):
        destination.unlink(missing_ok=True)
        raise


def _upload_partition(dfu_util, port, partition, size, destination, alternative_size=None):
    """Read the first size bytes of one named DFU partition into a private local file.

    alternative_size is the partition's whole size when size is only a prefix.
    """
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    try:
        run_with_progress(
            _partition_transfer_argv(dfu_util, port, partition, "-U", destination, size,
                                     alternative_size),
            timeout=14400, label="Reading {} ({} bytes) from USB".format(partition, size),
            progress_path=destination, progress_size=size)
        if destination.stat().st_size != size:
            raise DfuError("Uploaded {} has {} bytes; expected {}.".format(
                partition, destination.stat().st_size, size))
        destination.chmod(0o600)
        _chown_to_invoking_user(destination)
        return _sha256_file(destination)
    except (DfuError, OSError):
        destination.unlink(missing_ok=True)
        raise


def _find_partition_backup(device_tag, partition, size):
    if not BACKUP_ROOT.is_dir():
        return None
    for path in sorted(BACKUP_ROOT.glob("partition-backup-*/backup-manifest.json")):
        try:
            if path.is_symlink():
                continue
            record = json.loads(path.read_text())
            recorded_tags = set(record.get("device_tags", ()))
            if record.get("device_tag"):
                recorded_tags.add(record["device_tag"])
            if (record.get("kind") != "jibo-partition-backup" or
                    device_tag not in recorded_tags or
                    record.get("partition") != partition or record.get("size_bytes") != size):
                continue
            image = path.parent / "partition.img"
            if image.is_symlink() or not image.is_file() or image.stat().st_size != size:
                continue
            digest = _sha256_file(image)
            if digest == record.get("sha256"):
                return {"image": str(image), "sha256": digest, "manifest": str(path),
                        "status": "existing verified backup reused"}
        except (OSError, ValueError, TypeError):
            continue
    return None


def _ext4_uuid_from_image(image):
    image = Path(image)
    if image.is_symlink() or not image.is_file() or image.stat().st_size < 1080:
        raise DfuError("The saved partition image is too short to contain an ext4 superblock.")
    with image.open("rb") as source:
        source.seek(1024)
        superblock = source.read(1024)
    if len(superblock) < 120 or superblock[56:58] != b"\x53\xef":
        raise DfuError("The saved partition image does not contain a valid ext4 superblock.")
    return superblock[104:120].hex()


def _verify_or_bind_partition_identity(backup, identity):
    if not identity:
        return
    if _ext4_uuid_from_image(backup["image"]) != identity.get("ext4_uuid"):
        raise DfuError("The rollback image ext4 UUID does not match the connected partition.")
    manifest = Path(backup["manifest"])
    try:
        record = json.loads(manifest.read_text())
        if (record.get("sha256") != backup["sha256"] or
                _sha256_file(backup["image"]) != backup["sha256"]):
            raise DfuError("The saved partition manifest hash does not match its image.")
        recorded = record.get("partition_identity")
        if recorded is not None and recorded != identity:
            raise DfuError("The saved rollback image belongs to a different partition layout or ext4 UUID.")
        record["partition_identity"] = identity
        _private_write(manifest, record)
    except (OSError, ValueError, TypeError) as exc:
        raise DfuError("Could not verify the rollback image's partition identity.") from exc


def _verify_var_backup_for_robot(backup, device_tag, live_identity=None):
    """A saved var from this eMMC remains useful after mode or filesystem changes."""
    if not _has_unique_device_tag(device_tag):
        return _verify_or_bind_partition_identity(backup, live_identity)
    manifest = Path(backup["manifest"])
    try:
        record = json.loads(manifest.read_text())
        tags = set(record.get("device_tags", ()))
        tags.add(record.get("device_tag"))
        if (record.get("kind") != "jibo-var-backup" or device_tag not in tags or
                record.get("sha256") != backup["sha256"] or
                _sha256_file(backup["image"]) != backup["sha256"] or
                Path(backup["image"]).stat().st_size != EXPECTED_VAR_SIZE):
            raise DfuError("The saved var backup does not match this robot or its manifest.")
        _ext4_uuid_from_image(backup["image"])
    except (OSError, ValueError, TypeError) as exc:
        raise DfuError("Could not validate this robot's saved var backup.") from exc


def _find_partition_backup_by_hash(partition, size, digest):
    if not BACKUP_ROOT.is_dir():
        return None
    for path in sorted(BACKUP_ROOT.glob("partition-backup-*/backup-manifest.json")):
        try:
            if path.is_symlink():
                continue
            record = json.loads(path.read_text())
            if (record.get("kind") != "jibo-partition-backup" or
                    record.get("partition") != partition or
                    record.get("size_bytes") != size or record.get("sha256") != digest):
                continue
            image = path.parent / "partition.img"
            if image.is_symlink() or not image.is_file() or image.stat().st_size != size:
                continue
            if _sha256_file(image) == digest:
                return {"image": str(image), "sha256": digest, "manifest": str(path),
                        "status": "existing verified backup reused"}
        except (OSError, ValueError, TypeError):
            continue
    return None


def _bind_partition_backup_identity(backup, device_tag, digest, identity=None):
    if not _has_unique_device_tag(device_tag) and not identity:
        return
    manifest = Path(backup["manifest"])
    try:
        record = json.loads(manifest.read_text())
        if (record.get("kind") != "jibo-partition-backup" or
                record.get("sha256") != digest or
                _sha256_file(backup["image"]) != digest):
            raise DfuError("The saved partition backup changed during identity verification.")
        if _has_unique_device_tag(device_tag):
            tags = list(dict.fromkeys([*record.get("device_tags", ()),
                                       record.get("device_tag"), device_tag]))
            record["device_tags"] = [tag for tag in tags if tag]
        if identity:
            if _ext4_uuid_from_image(backup["image"]) != identity.get("ext4_uuid"):
                raise DfuError("The saved partition image ext4 UUID does not match the live partition.")
            record["partition_identity"] = identity
        _private_write(manifest, record)
    except (OSError, ValueError, TypeError) as exc:
        raise DfuError("Could not record the verified robot identity on its partition backup.") from exc


def _backup_partition_once(dfu_util, port, device_tag, partition, size, identity=None,
                           out=None):
    """Save one partition.

    Without ``out`` this keeps the shared store's behavior: a verified copy
    already saved for this robot is reused, and identical bytes share one copy.
    With ``out`` (a folder inside a named backup set) the partition is always
    read again into that new folder, with the same manifest format.
    """
    if out is None and _has_unique_device_tag(device_tag):
        existing = _find_partition_backup(device_tag, partition, size)
        if existing:
            try:
                _verify_or_bind_partition_identity(existing, identity)
                return existing
            except DfuError:
                # Do not reuse a same-size backup from another formatted extent.
                pass
    space_root = BACKUP_ROOT.parent if out is None else Path(out).expanduser().resolve().parent
    if shutil.disk_usage(space_root).free < size + 1024 ** 3:
        raise DfuError("Not enough free space to save the original {} partition.".format(partition))
    directory = _new_operation_dir(out, "partition-backup")
    image = directory / "partition.img"
    try:
        digest = _upload_partition(dfu_util, port, partition, size, image)
        if identity and _ext4_uuid_from_image(image) != identity.get("ext4_uuid"):
            raise DfuError("The downloaded {} image ext4 UUID does not match the live partition."
                           .format(partition))
        existing = _find_partition_backup_by_hash(partition, size, digest) if out is None else None
        if existing:
            image.unlink(missing_ok=True)
            directory.rmdir()
            _bind_partition_backup_identity(existing, device_tag, digest, identity)
            existing["status"] = "existing verified backup reused"
            return existing
        manifest = directory / "backup-manifest.json"
        _private_write(manifest, {"schema": 1, "kind": "jibo-partition-backup",
                                  "created_utc": _utc_now(), "device_tag": device_tag,
                                  "device_tags": [device_tag] if _has_unique_device_tag(device_tag) else [],
                                  "usb_port": port, "partition": partition,
                                  "size_bytes": size, "sha256": digest, "image": image.name,
                                  "partition_identity": identity})
        return {"image": str(image), "sha256": digest, "manifest": str(manifest),
                "status": "backup complete"}
    except (DfuError, OSError):
        image.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass
        raise


def _read_gpt_layout(dfu_util, port, names):
    if MARKER not in names:
        raise DfuError("This DFU loader does not expose the read-only GPT marker.")
    try:
        raw = bounded.read_dfu_alt_prefix(port)
        updates.parse_gpt_prefix(raw)
        return updates.parse_gpt_layout_prefix(raw)
    except (bounded.BoundedDfuError, updates.UpdateError) as exc:
        raise DfuError("Could not verify the robot's GPT partition sizes: " + str(exc)) from exc


def _read_gpt_capacities(dfu_util, port, names):
    layout = _read_gpt_layout(dfu_util, port, names)
    return {name: details["size_bytes"] for name, details in layout.items()}


def probe_dfu_gpt(port=None, dfu_util=None):
    """Validate the live GPT through DFU without saving a partition backup."""
    dfu_util = dfu_util or tool("dfu-util")
    port, names, _ = _dfu_context(port, dfu_util)
    capacities = _read_gpt_capacities(dfu_util, port, names)
    required = ("rootfsA", "rootfsB", "services", "skills", "var")
    missing = [name for name in required if name not in capacities]
    if missing:
        raise DfuError("The live GPT is missing Jibo partitions: " + ", ".join(missing))
    if capacities["var"] != EXPECTED_VAR_SIZE:
        raise DfuError("The live GPT reports an unexpected var size: " + str(capacities["var"]))
    return {"status": "partition table read and checked", "port": port,
            "partition_sizes_bytes": {name: capacities[name] for name in required},
            "bytes_read": bounded.MARKER_BYTES, "backup_created": False}


def available_backup_partitions(port=None, dfu_util=None):
    """List GPT partitions whose complete contents can be transferred through DFU."""
    dfu_util = dfu_util or tool("dfu-util")
    port, names, device_tag, listing = _dfu_context(port, dfu_util, include_output=True)
    layout = _read_gpt_layout(dfu_util, port, names)
    available = {}
    for name, extent in sorted(layout.items(), key=lambda item: item[1]["first_lba"]):
        size = extent["size_bytes"]
        if name == "skills" and name not in names:
            chunks = _validate_skills_chunk_alternatives(size, names, listing)
            if chunks:
                available[name] = {**extent, "transfer": "bounded chunks"}
        elif name in names and 0 < size <= 0x7fffffff:
            if name != "var" or size == EXPECTED_VAR_SIZE:
                available[name] = {**extent, "transfer": "partition"}
    return port, device_tag, available


def _selected_partitions(requested, available):
    selected = tuple(dict.fromkeys(requested))
    if not selected:
        raise DfuError("Select at least one partition.")
    missing = [name for name in selected if name not in available]
    if missing:
        raise DfuError("These GPT partitions are not available through DFU: " + ", ".join(missing))
    return selected


def _flock(stream, exclusive=False, wait=0.0):
    """Non-blocking advisory lock with a short retry; False when it stays held elsewhere."""
    try:
        import fcntl
    except ImportError as exc:
        raise DfuError("Backup locking is unavailable on this host. Nothing was deleted or restored.") from exc
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(stream.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) |
                        fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _unlock(stream):
    try:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


@contextmanager
def _backup_set_lock(manifest, exclusive=False):
    """Hold one backup set while it is written or deleted, across toolkit windows."""
    with open(manifest, "rb") as stream:
        if not _flock(stream, exclusive):
            raise DfuError("This backup is in use by another toolkit window. "
                           "Try again when that finishes.")
        try:
            yield
        finally:
            _unlock(stream)


def backup_partitions(partitions, port=None, dfu_util=None, out=None, *, cancel=None, progress=None):
    """Read selected GPT partitions into one new, dated backup set named after the robot.

    The set is ``Jibo-Backups/<robot name>/<UTC date-time>/``: ``backup-set.json``
    plus one folder per partition, each read fresh from the robot, so the folder
    holds the whole backup. The name is read from var on this computer and never
    by writing to the robot; without one, the folder uses a short tag of the USB
    identity. That USB identity (device_tag) and each partition's own manifest stay
    the restore safety check, exactly as for older backup sets. ``out`` names the
    set folder instead (it must not exist yet).
    """
    dfu_util = dfu_util or tool("dfu-util")
    port, device_tag, available = available_backup_partitions(port, dfu_util)
    if not _has_unique_device_tag(device_tag):
        raise DfuError("Partition backup sets require a stable robot identity from the DFU loader.")
    selected = _selected_partitions(partitions, available)
    needed = sum(available[name]["size_bytes"] for name in selected)
    space_root = Path(out).expanduser().resolve().parent if out is not None else BACKUP_ROOT.parent
    while not space_root.exists() and space_root != space_root.parent:
        space_root = space_root.parent
    if shutil.disk_usage(space_root).free < needed + 1024 ** 3:
        raise DfuError("Not enough free space for this backup: it needs {:.1f} GB plus 1 GB to "
                       "spare. Nothing was read from the robot.".format(needed / 1e9))
    created = _utc_now()
    robot_name = _saved_robot_name(device_tag)
    name_source = "an earlier var backup of this robot" if robot_name else None
    label = backup_folder_label(robot_name, device_tag)
    directory = (_new_operation_dir(out, "backup-set") if out is not None else
                 _new_backup_set_dir(label, created))
    manifest = directory / BACKUP_SET_MANIFEST
    record = {"schema": 1, "kind": "jibo-partition-backup-set", "created_utc": created,
              "device_tag": device_tag, "robot_name": robot_name,
              "robot_name_source": name_source, "robot_label": label,
              "every_partition": set(selected) == set(available),
              "usb_port": port, "partitions": [], "status": "reading"}
    _private_write(manifest, record)
    # var carries the robot's name, so it is read first and can name the folder.
    reading = tuple(sorted(selected, key=lambda partition: partition != "var"))
    try:
        with _backup_set_lock(manifest):
            complete = _read_backup_set(reading, available, port, dfu_util, device_tag, out, record,
                                        directory, manifest, label, cancel, progress)
        directory = Path(record["folder"])
        manifest = directory / BACKUP_SET_MANIFEST
        order = {name: index for index, name in enumerate(selected)}
        record["partitions"].sort(key=lambda entry: order[entry["name"]])
        del record["folder"]
        record["status"] = "complete" if complete else "cancelled"
        _private_write(manifest, record)
        if not complete:
            return {"status": "cancelled", "manifest": str(manifest),
                    "message": "Backup stopped after the current partition. Saved files were kept "
                               "in {}. This partial backup cannot be restored as a set.".format(directory)}
        return {"status": "backup complete", "manifest": str(manifest),
                "operation_directory": str(directory), "robot_name": record["robot_name"],
                "robot_label": record["robot_label"], "partitions": record["partitions"]}
    except Exception as exc:
        directory = Path(record.pop("folder", directory))
        record.update(status="failed", error=str(exc))
        _private_write(directory / BACKUP_SET_MANIFEST, record)
        raise


def _read_backup_set(reading, available, port, dfu_util, device_tag, out, record,
                     directory, manifest, label, cancel=None, progress=None):
    """Read each partition into the set folder.

    ``record["folder"]`` follows the folder when var supplies the robot's name and
    the set moves to that robot's folder before any other partition is read.
    """
    record["folder"] = str(directory)
    for index, name in enumerate(reading, 1):
        if cancel is not None and cancel():
            return False
        message = "Reading {} ({} of {} partitions)".format(name, index, len(reading))
        print(message, flush=True)
        if progress is not None:
            progress(message)
        extent = available[name]
        if name == "var":
            saved = backup_var(port=port, dfu_util=dfu_util, out=directory / name)
            found = robot_name_from_var_image(saved["image"])
            if found:
                if out is None and backup_folder_label(found, device_tag) != label:
                    label = backup_folder_label(found, device_tag)
                    directory = _move_backup_set(directory, label)
                    manifest = directory / BACKUP_SET_MANIFEST
                    record["folder"] = str(directory)
                    saved = dict(saved, image=str(directory / name / Path(saved["image"]).name),
                                 manifest=str(directory / name / "backup-manifest.json"))
                record.update(robot_name=found, robot_name_source="var in this backup",
                              robot_label=label)
        elif name == "skills" and extent["transfer"] == "bounded chunks":
            names, listing = dfu_alternatives(dfu_util, port)
            chunks = _validate_skills_chunk_alternatives(extent["size_bytes"], names, listing)
            saved = _backup_skills_partition_once(
                dfu_util, port, device_tag, extent["size_bytes"], chunks, out=directory / name)
        else:
            saved = _backup_partition_once(dfu_util, port, device_tag, name,
                                           extent["size_bytes"], out=directory / name)
        record["partitions"].append({
            "name": name, "size_bytes": extent["size_bytes"],
            "first_lba": extent["first_lba"], "last_lba": extent["last_lba"],
            "image": str(Path(saved["image"]).resolve()), "sha256": saved["sha256"],
            "backup_manifest": str(Path(saved["manifest"]).resolve()),
            "backup_status": saved["status"]})
        _private_write(manifest, {key: value for key, value in record.items() if key != "folder"})
    return True


# -- Saved backup sets: robot names, folders and the list ----------------------

BACKUP_SET_MANIFEST = "backup-set.json"
# Jibo's factory name: three or four hyphenated words, as checked for identity.json.
ROBOT_NAME_PATTERN = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+){2,3}")
_TOOLKIT_FOLDER_PREFIXES = ("var-backup", "partition-backup", "backup-set", "restore-set",
                            "write-var", "set-mode", "configure-wifi", "flash-update",
                            "operation", "calibration")


def _valid_robot_name(value):
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if ROBOT_NAME_PATTERN.fullmatch(value) else None


def robot_name_from_var_image(image):
    """Read Jibo's name from a var image on this computer; None when it is absent.

    Uses /jibo/identity.json, then /etc/hostname. Only the name is read and
    returned; the image is opened read-only and never changed.
    """
    image = Path(image)
    try:
        if image.is_symlink() or not image.is_file():
            return None
        with tempfile.TemporaryDirectory(prefix="jibo-robot-name-") as temporary:
            raw = images._extract(image, images.FACTORY_IDENTITY_PATH,
                                  Path(temporary) / "identity", optional=True)
            if raw and len(raw) <= 65536:
                try:
                    name = _valid_robot_name(json.loads(raw.decode("utf-8")).get("name"))
                except (UnicodeDecodeError, ValueError, AttributeError):
                    name = None
                if name:
                    return name
            raw = images._extract(image, "/etc/hostname", Path(temporary) / "hostname",
                                  optional=True)
            return _valid_robot_name(raw) if raw and len(raw) <= 256 else None
    except (images.ImageError, OSError):
        return None


def short_robot_tag(device_tag):
    """A short, stable label from the USB identity hash for robots without a known name."""
    match = re.fullmatch(r"serial-sha256:([0-9a-f]{64})", str(device_tag or ""))
    return "robot-" + match.group(1)[:8] if match else "unknown-robot"


def backup_folder_label(name, device_tag):
    """Folder-safe label: the robot's name when known, else its short USB tag."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or ""))
    label = re.sub(r"-{2,}", "-", label).strip(".-_")[:64].strip(".-_")
    if not re.search(r"[A-Za-z0-9]", label):
        return short_robot_tag(device_tag)
    if label.lower().startswith(_TOOLKIT_FOLDER_PREFIXES):
        label = "robot-" + label
    return label


def _backup_folder_stamp(created_utc):
    return re.sub(r"[^0-9A-Za-z_-]", "-", created_utc.replace("T", "_").replace(":", "-"))


def _robot_backup_dir(label):
    """Jibo-Backups/<label>/, created privately; refuses a link or a file there."""
    BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUP_ROOT.chmod(0o700)
    _chown_to_invoking_user(BACKUP_ROOT)
    robot_dir = BACKUP_ROOT / label
    if robot_dir.is_symlink():
        raise DfuError("The backup folder for this robot is a link; choose another folder: " +
                       str(robot_dir))
    robot_dir.mkdir(mode=0o700, exist_ok=True)
    if robot_dir.is_symlink() or not robot_dir.is_dir():
        raise DfuError("The backup folder for this robot is not a folder: " + str(robot_dir))
    robot_dir.chmod(0o700)
    _chown_to_invoking_user(robot_dir)
    return robot_dir


def _new_backup_set_dir(label, created_utc):
    """Create Jibo-Backups/<label>/<UTC time>/ privately, never reusing a folder."""
    robot_dir = _robot_backup_dir(label)
    stamp = _backup_folder_stamp(created_utc)
    for attempt in range(1, 100):
        directory = robot_dir / (stamp if attempt == 1 else "{}-{}".format(stamp, attempt))
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        directory.chmod(0o700)
        _chown_to_invoking_user(directory)
        return directory
    raise DfuError("Could not create a new backup folder in " + str(robot_dir))


def _move_backup_set(directory, label):
    """Move a set that is still being written to its robot's folder; keep it on failure."""
    directory = Path(directory)
    try:
        robot_dir = _robot_backup_dir(label)
        for attempt in range(1, 100):
            target = robot_dir / (directory.name if attempt == 1 else
                                  "{}-{}".format(directory.name, attempt))
            if target.exists() or target.is_symlink():
                continue
            os.rename(directory, target)
            try:
                directory.parent.rmdir()  # only when the provisional folder is now empty
            except OSError:
                pass
            return target
    except (OSError, DfuError):
        pass
    return directory


DELETING_PREFIX = ".deleting-"


def _being_deleted(path):
    """True inside a backup folder that delete_backup_set is removing."""
    try:
        parts = Path(path).relative_to(BACKUP_ROOT).parts
    except ValueError:
        return False
    return any(part.startswith(DELETING_PREFIX) for part in parts)


def _in_backup_set(manifest_path):
    """True for a partition copy kept inside a backup set's own folder."""
    try:
        return (Path(manifest_path).parent.parent / BACKUP_SET_MANIFEST).is_file()
    except OSError:
        return False


def _var_backup_manifests():
    if not BACKUP_ROOT.is_dir() or BACKUP_ROOT.is_symlink():
        return []
    paths = [path for path in BACKUP_ROOT.rglob("backup-manifest.json")
             if not path.is_symlink() and not path.parent.is_symlink() and
             not _being_deleted(path)]
    return sorted(paths, key=lambda path: path.stat().st_mtime if path.exists() else 0,
                  reverse=True)


def saved_robot_name(device_tag):
    """Jibo's name from this robot's newest saved var backup, or None. A label only."""
    return _saved_robot_name(device_tag)


def backup_free_bytes():
    """Free space where backups are saved (the nearest existing folder)."""
    folder = Path(BACKUP_ROOT).expanduser()
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return shutil.disk_usage(folder).free


def _saved_robot_name(device_tag, limit=5):
    """The name in this robot's newest saved var backup. A label only, not identity."""
    if not _has_unique_device_tag(device_tag):
        return None
    tried = 0
    for manifest in _var_backup_manifests():
        try:
            if manifest.stat().st_size > 65536:
                continue
            record = json.loads(manifest.read_text())
            tags = set(record.get("device_tags", ())) | {record.get("device_tag")}
            if (record.get("kind") != "jibo-var-backup" or record.get("status") == "failed" or
                    device_tag not in tags):
                continue
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        tried += 1
        name = robot_name_from_var_image(
            manifest.parent / Path(str(record.get("image") or "var.img")).name)
        if name or tried >= limit:
            return name
    return None


def _backup_set_manifests():
    """``(path, layout)`` for named (robot/time) and older (backup-set-*) folders."""
    if not BACKUP_ROOT.is_dir() or BACKUP_ROOT.is_symlink():
        return []
    found = {}
    for pattern, layout in (("*/*/" + BACKUP_SET_MANIFEST, "named"),
                            ("backup-set-*/" + BACKUP_SET_MANIFEST, "older")):
        for path in sorted(BACKUP_ROOT.glob(pattern)):
            parents = (path, path.parent) + ((path.parent.parent,) if layout == "named" else ())
            if any(item.is_symlink() for item in parents) or _being_deleted(path) or \
                    any(item.name.startswith(".") for item in parents[1:]):
                continue
            found.setdefault(path, layout)
    return list(found.items())


def describe_backup_set(manifest, layout=None, _names=None):
    """Summarize one backup-set.json from local files only; never raises for bad files.

    ``state`` is ``complete`` (finished and every image file present at its
    recorded size), ``missing-files``, ``incomplete`` or ``unreadable``. Image
    hashes are checked again by restore_partitions before anything is written.
    """
    manifest = Path(manifest)
    names = {} if _names is None else _names
    entry = {"manifest": str(manifest), "folder": str(manifest.parent), "layout": layout,
             "device_tag": None, "robot_name": None, "robot_tag": "unknown-robot",
             "robot_label": "unknown-robot", "created_utc": None, "partitions": [],
             "total_bytes": 0, "every_partition": False, "status": None,
             "state": "unreadable", "restorable": False}
    try:
        if manifest.is_symlink() or not manifest.is_file() or \
                manifest.stat().st_size > 1024 * 1024:
            return entry
        record = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return entry
    if not isinstance(record, dict) or record.get("kind") != "jibo-partition-backup-set":
        return entry
    device_tag = record.get("device_tag") if isinstance(record.get("device_tag"), str) else None
    created = record.get("created_utc")
    entry.update(device_tag=device_tag, robot_tag=short_robot_tag(device_tag),
                 created_utc=created if isinstance(created, str) else None,
                 status=record.get("status") if isinstance(record.get("status"), str) else None)
    partitions = []
    for item in record.get("partitions") if isinstance(record.get("partitions"), list) else ():
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        size = item.get("size_bytes") if isinstance(item.get("size_bytes"), int) else 0
        image = Path(str(item.get("image") or ""))
        try:
            present = (bool(item.get("image")) and not image.is_symlink() and image.is_file()
                       and image.stat().st_size == size)
        except OSError:
            present = False
        partitions.append({"name": item["name"], "size_bytes": size, "image": str(image),
                           "present": present,
                           "backup_status": str(item.get("backup_status") or "")})
    entry["partitions"] = partitions
    entry["total_bytes"] = sum(item["size_bytes"] for item in partitions)
    entry["every_partition"] = record.get("every_partition") is True
    if entry["status"] == "complete" and partitions:
        entry["state"] = ("complete" if all(item["present"] for item in partitions)
                          else "missing-files")
    else:
        entry["state"] = "incomplete"
    entry["restorable"] = entry["state"] == "complete"
    name = _valid_robot_name(record.get("robot_name"))
    if name is None:
        var = next((item for item in partitions if item["name"] == "var" and item["present"]),
                   None)
        if var is not None:
            if var["image"] not in names:
                names[var["image"]] = robot_name_from_var_image(var["image"])
            name = names[var["image"]]
    if name is None and device_tag:
        if device_tag not in names:
            names[device_tag] = _saved_robot_name(device_tag)
        name = names[device_tag]
    entry["robot_name"] = name
    entry["robot_label"] = name or entry["robot_tag"]
    return entry


def saved_backups():
    """Backup sets and older standalone rollback images, newest first; local only."""
    if not BACKUP_ROOT.is_dir() or BACKUP_ROOT.is_symlink():
        return []
    names = {}
    entries = [describe_backup_set(path, layout, names) for path, layout in _backup_set_manifests()]
    # Original rollback images remain in place and are never deletable here.
    # They have no saved GPT extents, so never manufacture a restorable set.
    for pattern in ("var-backup-*/backup-manifest.json", "partition-backup-*/backup-manifest.json"):
        for manifest in BACKUP_ROOT.glob(pattern):
            try:
                if manifest.is_symlink() or manifest.parent.is_symlink() or manifest.stat().st_size > 65536:
                    continue
                record = json.loads(manifest.read_text())
                if not isinstance(record, dict) or record.get("kind") not in (
                        "jibo-var-backup", "jibo-partition-backup"):
                    continue
                item = _legacy_backup_partition(manifest, record)
                image = Path(item["image"])
                present = not image.is_symlink() and image.is_file() and image.stat().st_size == item["size_bytes"]
                tag = record.get("device_tag")
                name = robot_name_from_var_image(image) if item["name"] == "var" and present else None
                name = name or _saved_robot_name(tag)
                entries.append(dict(manifest=str(manifest), folder=str(manifest.parent),
                    layout="rollback", device_tag=tag, robot_name=name,
                    robot_tag=short_robot_tag(tag), robot_label=name or short_robot_tag(tag),
                    created_utc=record.get("created_utc") if isinstance(record.get("created_utc"), str) else None,
                    total_bytes=item["size_bytes"],
                    partitions=[dict(item, present=present)], every_partition=False,
                    status=record.get("status", "complete"),
                    state="rollback-image" if present and record.get("status") != "failed" else "missing-files",
                    restorable=False, restore_reason="This is a rollback image, not a partition backup set "
                    "with saved GPT extents. It is kept for the original recovery workflow."))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    entries.sort(key=lambda entry: (entry["created_utc"] or "", entry["manifest"]), reverse=True)
    return entries


def _legacy_backup_partition(manifest, record):
    """Describe an existing single-image manifest without rewriting its format."""
    if not isinstance(record.get("size_bytes"), int) or record["size_bytes"] <= 0:
        raise ValueError("Invalid backup size")
    return dict(name=record.get("partition", "var"),
                image=str(Path(manifest).parent / Path(str(record.get("image") or "var.img")).name),
                size_bytes=record.get("size_bytes", 0), sha256=record.get("sha256"))


def _backup_set_path(backup_set):
    manifest = Path(backup_set).expanduser()
    if manifest.is_dir() and not manifest.is_symlink():
        manifest /= BACKUP_SET_MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise DfuError("Select a backup-set.json manifest created by this toolkit.")
    return manifest


def check_backup_set_files(backup_set):
    """Hash every image a saved backup set lists and compare it with its saved SHA-256.

    Local files only: nothing is read from or written to the robot.
    """
    manifest = _backup_set_path(backup_set)
    try:
        record = json.loads(manifest.read_text())
    except (OSError, ValueError) as exc:
        raise DfuError("Could not read the selected backup-set manifest.") from exc
    if not isinstance(record, dict) or record.get("kind") not in (
            "jibo-partition-backup-set", "jibo-var-backup", "jibo-partition-backup"):
        raise DfuError("The selected file is not a backup set made by this toolkit.")
    entries = ([_legacy_backup_partition(manifest, record)] if record["kind"] != "jibo-partition-backup-set"
               else [entry for entry in record.get("partitions") or () if isinstance(entry, dict)])
    results = []
    for index, entry in enumerate(entries, 1):
        name = str(entry.get("name") or "?")
        image = Path(str(entry.get("image") or ""))
        print("Checking {} ({} of {})".format(name, index, len(entries)), flush=True)
        try:
            present = (bool(entry.get("image")) and not image.is_symlink() and image.is_file()
                       and image.stat().st_size == entry.get("size_bytes"))
            status = ("match" if present and _sha256_file(image) == entry.get("sha256") else
                      "mismatch" if present else "missing")
        except OSError:
            status = "missing"
        results.append({"name": name, "status": status})
    return {"status": "checked", "manifest": str(manifest), "partitions": results,
            "all_match": bool(results) and all(item["status"] == "match" for item in results)}


def _running_in_wsl():
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def open_folder(path):
    """Ask the desktop to show a local folder; True when a file browser was started.

    Under sudo the browser runs as the person who started the toolkit, not root.
    """
    folder = Path(path).expanduser()
    if folder.is_symlink() or not folder.is_dir():
        return False
    folder = folder.resolve()
    commands = []
    if sys.platform == "darwin":
        commands.append(["open", str(folder)])
    elif sys.platform.startswith("win"):
        commands.append(["explorer", str(folder)])
    else:
        if _running_in_wsl():
            explorer = shutil.which("explorer.exe") or "/mnt/c/Windows/explorer.exe"
            converter = shutil.which("wslpath")
            if converter and Path(explorer).exists():
                try:
                    windows = subprocess.run([converter, "-w", str(folder)], capture_output=True,
                                             text=True, timeout=5, check=True).stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    windows = ""
                if windows:
                    commands.append([explorer, windows])
        opener = shutil.which("xdg-open")
        if opener:
            commands.append([opener, str(folder)])
    owner = _invoking_user() if os.geteuid() == 0 else None
    for argv in commands:
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                   "stderr": subprocess.DEVNULL, "start_new_session": True}
        if owner:
            options.update(user=owner[0], group=owner[1],
                           env=dict(os.environ, HOME=str(owner[2])))
        try:
            subprocess.Popen(argv, **options)
            return True
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            continue
    return False


def _shared_by_newer_layout(path, keep):
    """True when a backup set other than ``keep`` still lists ``path``."""
    for other, _layout in _backup_set_manifests():
        if other == keep:
            continue
        try:
            record = json.loads(other.read_text())
            if any(isinstance(entry, dict) and entry.get("image") and
                   Path(entry["image"]).resolve() == Path(path).resolve()
                   for entry in record.get("partitions") or ()):
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def delete_backup_set(backup_set):
    """Delete one saved backup set from this computer. The robot is never contacted.

    Only a set made by the newer Backups menu (Jibo-Backups/<robot>/<time>/) can be
    deleted; its folder holds every file it lists. Older sets reuse shared copies
    that other backups and rollbacks rely on, so they are refused here. The caller
    must confirm first, naming the robot and the date. A set that is being written,
    restored or deleted elsewhere is refused.
    """
    manifest = _backup_set_path(backup_set)
    root = BACKUP_ROOT.resolve()
    folder = manifest.parent
    if BACKUP_ROOT.is_symlink() or folder.is_symlink() or folder.parent.is_symlink() or \
            folder.resolve().parent.parent != root:
        raise DfuError("Only backups made by the Backups menu can be deleted here. "
                       "Nothing was deleted.")
    try:
        record = json.loads(manifest.read_text())
    except (OSError, ValueError) as exc:
        raise DfuError("Could not read this backup. Nothing was deleted.") from exc
    if not isinstance(record, dict) or record.get("kind") != "jibo-partition-backup-set":
        raise DfuError("This folder is not a backup made by this toolkit. Nothing was deleted.")
    if record.get("status") == "reading":
        raise DfuError("This backup is still being saved. Nothing was deleted.")
    resolved = folder.resolve()
    allowed = {manifest.resolve()}
    for entry in record.get("partitions") or ():
        image = Path(str(entry.get("image") or "")) if isinstance(entry, dict) else None
        if image is not None and str(image) and resolved not in image.resolve().parents:
            raise DfuError("This backup lists files outside its own folder. Nothing was deleted.")
        if image is not None and _shared_by_newer_layout(image, manifest):
            raise DfuError("Another saved backup uses these files. Nothing was deleted.")
        if image is None or not entry.get("backup_manifest"):
            raise DfuError("This backup has invalid partition records. Nothing was deleted.")
        saved_manifest = Path(entry["backup_manifest"])
        if resolved not in saved_manifest.resolve().parents:
            raise DfuError("This backup lists files outside its own folder. Nothing was deleted.")
        allowed.update((image.resolve(), saved_manifest.resolve()))
    allowed_dirs = {parent for path in allowed for parent in path.parents if resolved in parent.parents}
    for path in folder.rglob("*"):
        if path.is_symlink() or (path.is_dir() and path.resolve() not in allowed_dirs) or \
                (not path.is_dir() and (not path.is_file() or path.resolve() not in allowed)):
            raise DfuError("This backup contains unrecorded files or folders. Nothing was deleted.")
    label = describe_backup_set(manifest)["robot_label"]
    holding = folder.parent / (DELETING_PREFIX + folder.name)
    if holding.exists() or holding.is_symlink():
        raise DfuError("An earlier delete of this backup did not finish. Remove " + str(holding) +
                       " by hand. Nothing else was deleted.")
    # The exclusive lock fails while a restore or backup holds this exact set.
    with _backup_set_lock(manifest, exclusive=True):
        os.rename(folder, holding)
    shutil.rmtree(holding)
    return {"status": "deleted", "folder": str(folder), "robot_label": label,
            "created_utc": record.get("created_utc")}


def restore_partitions(backup_set, partitions=None, port=None, dfu_util=None,
                       out=None, confirmation=None):
    """Restore selected entries from a checked backup set and verify each readback."""
    dfu_util = dfu_util or tool("dfu-util")
    supplied_manifest = Path(backup_set).expanduser()
    if supplied_manifest.is_symlink():
        raise DfuError("The backup-set manifest cannot be a symbolic link.")
    manifest = supplied_manifest.resolve()
    if manifest.is_dir():
        manifest /= "backup-set.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise DfuError("Select a backup-set.json manifest created by this toolkit.")
    # Hold the exact source even when it lives outside the default backup root.
    with _backup_set_lock(manifest):
        return _restore_partitions(manifest, partitions, port, dfu_util, out, confirmation)


def _restore_partitions(manifest, partitions, port, dfu_util, out, confirmation):
    try:
        source = json.loads(manifest.read_text())
    except (OSError, ValueError) as exc:
        raise DfuError("Could not read the selected backup-set manifest.") from exc
    if source.get("kind") != "jibo-partition-backup-set" or source.get("status") != "complete":
        raise DfuError("The selected backup set is incomplete or has an unsupported format.")
    port, device_tag, available = available_backup_partitions(port, dfu_util)
    if not _has_unique_device_tag(device_tag) or source.get("device_tag") != device_tag:
        raise DfuError("The backup set belongs to a different robot or its identity is unavailable.")
    entries = source.get("partitions")
    if not isinstance(entries, list) or not entries:
        raise DfuError("The backup set contains no partitions.")
    by_name = {entry.get("name"): entry for entry in entries if isinstance(entry, dict)}
    if len(by_name) != len(entries):
        raise DfuError("The backup set has duplicate or invalid partition entries.")
    selected = _selected_partitions(partitions if partitions is not None else tuple(by_name),
                                    by_name)
    _selected_partitions(selected, available)
    for name in selected:
        entry = by_name[name]
        extent = available[name]
        if any(entry.get(key) != extent[key] for key in ("first_lba", "last_lba", "size_bytes")):
            raise DfuError("The live GPT extent for {} differs from the backup set.".format(name))
        image = Path(entry.get("image", ""))
        if image.is_symlink() or not image.is_file() or image.stat().st_size != extent["size_bytes"]:
            raise DfuError("The saved {} image is missing or has the wrong size.".format(name))
        if _sha256_file(image) != entry.get("sha256"):
            raise DfuError("The saved {} image failed its SHA-256 check.".format(name))
        saved_manifest = Path(entry.get("backup_manifest", ""))
        if saved_manifest.is_symlink() or not saved_manifest.is_file():
            raise DfuError("The saved {} backup manifest is missing.".format(name))
        try:
            saved_record = json.loads(saved_manifest.read_text())
        except (OSError, ValueError) as exc:
            raise DfuError("The saved {} backup manifest cannot be read.".format(name)) from exc
        tags = set(saved_record.get("device_tags", ()))
        tags.add(saved_record.get("device_tag"))
        if (saved_record.get("kind") != ("jibo-var-backup" if name == "var" else
                                          "jibo-partition-backup") or
                saved_record.get("partition") != name or
                saved_record.get("sha256") != entry["sha256"] or
                saved_record.get("size_bytes") != extent["size_bytes"] or
                device_tag not in tags or
                (saved_manifest.parent / saved_record.get("image", "")).resolve() != image.resolve()):
            raise DfuError("The saved {} image does not match its backup manifest.".format(name))
        if name == "var":
            _ext4_uuid_from_image(image)
    plan = {"operation": "restore selected partitions", "usb_port": port,
            "source_manifest": str(manifest),
            "backup_robot": describe_backup_set(manifest)["robot_label"],
            "backup_created_utc": source.get("created_utc"),
            "partitions": [{"name": name, "bytes": available[name]["size_bytes"]}
                           for name in selected]}
    if confirmation is None:
        accepted = _confirm_file_write(None, plan)
    elif callable(confirmation):
        accepted = bool(confirmation(plan))
    else:
        accepted = bool(confirmation)
    if not accepted:
        return {"status": "cancelled", "message": "No partition was written."}
    directory = _new_operation_dir(out, "restore-set")
    record_path = directory / "restore-manifest.json"
    record = {"schema": 1, "kind": "jibo-partition-restore", "created_utc": _utc_now(),
              "device_tag": device_tag, "usb_port": port, "source_manifest": str(manifest),
              "selected": list(selected), "writes": [], "status": "ready"}
    _private_write(record_path, record)
    try:
        images = {name: (by_name[name]["image"], by_name[name]["size_bytes"], by_name[name]["sha256"])
                  for name in selected}
        _write_and_verify(images, available, port, dfu_util, directory, record_path, record, "Restoring ")
        return {"status": "verified", "partitions": list(selected),
                "operation_directory": str(directory), "manifest": str(record_path)}
    except Exception as exc:
        if record["status"] != "readback mismatch":
            record.update(status="failed", error=str(exc))
            _private_write(record_path, record)
        raise


def _write_and_verify(images, available, port, dfu_util, directory, record_path, record, label):
    """Write each {partition: (image, size, sha256)} in order and check it by reading it back.

    An image shorter than its partition writes and reads back only its own bytes;
    the rest of the partition is left as it was.
    """
    for name, (image, size, digest) in images.items():
        full = available[name]["size_bytes"]
        write = {"name": name, "size_bytes": size, "sha256": digest, "status": "write started"}
        record["writes"].append(write)
        record["status"] = "write started"
        _private_write(record_path, record)
        if name == "skills" and available[name]["transfer"] == "bounded chunks":
            names, listing = dfu_alternatives(dfu_util, port)
            chunks = _validate_skills_chunk_alternatives(full, names, listing)
            _write_skills_chunks(dfu_util, port, image, full, chunks,
                                 directory, record_path, record, write, transfer_size=size)
        else:
            run_with_progress(_partition_transfer_argv(dfu_util, port, name, "-D", image),
                              timeout=14400, label=label + name, download_size=size,
                              allow_progress_completion=True)
            with tempfile.TemporaryDirectory(prefix="restore-readback-", dir=directory) as temp:
                readback = Path(temp) / "partition.img"
                actual = (_upload_var(dfu_util, port, readback) if name == "var" and size == full else
                          _upload_partition(dfu_util, port, name, size, readback,
                                            None if size == full else full))
            write["readback_sha256"] = actual
            if actual != digest:
                write["status"] = "readback mismatch"
                record["status"] = "readback mismatch"
                _private_write(record_path, record)
                raise DfuError("{} did not match its readback. No further partitions were written.".format(name))
            write["status"] = "verified"
            _private_write(record_path, record)
    record["status"] = "verified"
    _private_write(record_path, record)


def flash_images(images, port=None, dfu_util=None, out=None, confirmation=None):
    """Write any .img files to the chosen partitions: {partition name: image path}.

    Each image must fit its live GPT partition and be whole 512-byte sectors. After the
    review, the chosen partitions are backed up into a new backup set, then each image is
    written and read back.
    """
    dfu_util = dfu_util or tool("dfu-util")
    port, device_tag, available = available_backup_partitions(port, dfu_util)
    selected = _selected_partitions(tuple(images), available)
    checked = {}
    for name in selected:
        image = Path(images[name]).expanduser()
        if image.is_symlink() or not image.is_file():
            raise DfuError("The image for {} is missing or is a symbolic link.".format(name))
        image = image.resolve()
        size, full = image.stat().st_size, available[name]["size_bytes"]
        if size <= 0 or size % SKILLS_SECTOR_SIZE:
            raise DfuError("{} is not a whole number of 512-byte sectors.".format(image.name))
        if size > full:
            raise DfuError("{} is {} bytes, larger than the {} partition ({} bytes).".format(
                image.name, size, name, full))
        checked[name] = (str(image), size, _sha256_file(image))
    plan = {"operation": "flash images to partitions", "usb_port": port,
            "readback_policy": "each partition is backed up first, then read back after writing",
            "partitions": [{"name": name, "bytes": checked[name][1],
                            "image": checked[name][0]} for name in selected]}
    if confirmation is None:
        accepted = _confirm_file_write(None, plan)
    elif callable(confirmation):
        accepted = bool(confirmation(plan))
    else:
        accepted = bool(confirmation)
    if not accepted:
        return {"status": "cancelled", "message": "No partition was written."}
    backup = backup_partitions(selected, port=port, dfu_util=dfu_util)
    if backup.get("status") != "backup complete":
        raise DfuError("The backup did not finish, so nothing was written.")
    directory = _new_operation_dir(out, "flash-images")
    record_path = directory / "flash-manifest.json"
    record = {"schema": 1, "kind": "jibo-partition-flash", "created_utc": _utc_now(),
              "device_tag": device_tag, "usb_port": port, "backup_manifest": backup["manifest"],
              "selected": list(selected), "writes": [], "status": "ready"}
    _private_write(record_path, record)
    try:
        _write_and_verify(checked, available, port, dfu_util, directory, record_path, record, "Flashing ")
    except Exception as exc:
        if record["status"] != "readback mismatch":
            record.update(status="failed", error=str(exc))
            _private_write(record_path, record)
        raise
    return {"status": "verified", "partitions": list(selected), "backup": backup["manifest"],
            "operation_directory": str(directory), "manifest": str(record_path)}


def _expected_skills_chunks(capacity):
    """Return the exact byte ranges for the loader's GPT-bounded skills alternatives."""
    try:
        capacity = int(capacity)
    except (TypeError, ValueError) as exc:
        raise DfuError("The GPT skills capacity is invalid.") from exc
    if capacity <= 0 or capacity % SKILLS_SECTOR_SIZE:
        raise DfuError("The GPT skills capacity is not a positive whole number of sectors.")
    count = (capacity + SKILLS_CHUNK_BYTES - 1) // SKILLS_CHUNK_BYTES
    chunks = []
    for index in range(count):
        offset = index * SKILLS_CHUNK_BYTES
        chunks.append({"name": "skills-{:03d}".format(index),
                       "offset_bytes": offset,
                       "size_bytes": min(SKILLS_CHUNK_BYTES, capacity - offset)})
    return chunks


def _dfu_alt_sizes(output):
    sizes = {}
    for line in output.splitlines():
        name = re.search(r'\bname\s*=\s*"([^"]+)"', line)
        size = re.search(r"\bsize\s*=\s*(\d+)", line)
        if name and size:
            sizes[name.group(1)] = int(size.group(1))
    return sizes


def _validate_skills_chunk_alternatives(capacity, names, output):
    """Require a complete, GPT-sized skills chunk map before an update can start."""
    found = [name for name in names if name.startswith("skills-")]
    if not found:
        return None
    if "skills" in names:
        raise DfuError("The DFU loader lists both a full skills alternative and skills chunks.")
    expected = _expected_skills_chunks(capacity)
    expected_names = [chunk["name"] for chunk in expected]
    if len(found) != len(set(found)) or set(found) != set(expected_names):
        missing = sorted(set(expected_names) - set(found))
        unexpected = sorted(set(found) - set(expected_names))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise DfuError("The skills chunk alternatives do not match the GPT skills capacity: " +
                       "; ".join(details) + ".")
    # dfu-util's standard -l output does not report raw-alternative capacities.
    # The loader binds each writable alias to an exact GPT slice; when a build
    # does report sizes, use them as an additional consistency check.
    sizes = _dfu_alt_sizes(output)
    for chunk in expected:
        reported = sizes.get(chunk["name"])
        if reported is not None and reported != chunk["size_bytes"]:
            raise DfuError("The DFU loader reports {} bytes for {}; GPT requires {} bytes.".format(
                reported, chunk["name"], chunk["size_bytes"]))
    return expected


def _copy_exact(source, destination, size, digest=None):
    """Copy exactly size bytes from source to a binary destination and update digest."""
    remaining = size
    while remaining:
        block = source.read(min(1024 * 1024, remaining))
        if not block:
            raise DfuError("A partition image ended before its expected byte range.")
        destination.write(block)
        if digest is not None:
            digest.update(block)
        remaining -= len(block)


def _copy_file_slice(source_path, offset, size, destination_path):
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    if source_path.is_symlink() or not source_path.is_file():
        raise DfuError("Prepared skills image is missing or is a symbolic link.")
    if offset < 0 or size <= 0 or offset + size > source_path.stat().st_size:
        raise DfuError("A skills chunk range falls outside the prepared image.")
    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as source, destination_path.open("wb") as destination:
            source.seek(offset)
            _copy_exact(source, destination, size, digest)
            destination.flush()
            os.fsync(destination.fileno())
        if destination_path.stat().st_size != size:
            raise DfuError("A prepared skills chunk has an unexpected byte length.")
    except (DfuError, OSError):
        destination_path.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def _upload_skills_partition(dfu_util, port, capacity, chunks, destination=None,
                             workdir=None):
    """Read bounded skills alternatives and optionally assemble one partition image."""
    destination = Path(destination) if destination is not None else None
    if destination is not None:
        destination.unlink(missing_ok=True)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    workdir = Path(workdir or (destination.parent if destination else tempfile.gettempdir()))
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    whole_hash = hashlib.sha256()
    chunk_records = []
    try:
        output_stream = destination.open("w+b") if destination is not None else None
        try:
            if output_stream is not None:
                output_stream.truncate(capacity)
            with tempfile.TemporaryDirectory(prefix="skills-chunk-", dir=workdir) as temporary:
                chunk_path = Path(temporary) / "partition.img"
                for index, chunk in enumerate(chunks, 1):
                    chunk_hash = _upload_partition(dfu_util, port, chunk["name"],
                                                   chunk["size_bytes"], chunk_path)
                    with chunk_path.open("rb") as source:
                        if output_stream is not None:
                            output_stream.seek(chunk["offset_bytes"])
                            _copy_exact(source, output_stream, chunk["size_bytes"], whole_hash)
                        else:
                            _copy_exact(source, _NullWriter(), chunk["size_bytes"], whole_hash)
                    chunk_records.append({"alternative": chunk["name"],
                                          "offset_bytes": chunk["offset_bytes"],
                                          "size_bytes": chunk["size_bytes"],
                                          "sha256": chunk_hash,
                                          "status": "read"})
                    print("Read skills chunk {}/{} ({}).".format(index, len(chunks), chunk["name"]))
            if output_stream is not None:
                output_stream.flush()
                os.fsync(output_stream.fileno())
        finally:
            if output_stream is not None:
                output_stream.close()
        if destination is not None:
            if destination.stat().st_size != capacity:
                raise DfuError("The assembled skills backup has an unexpected byte length.")
            destination.chmod(0o600)
            _chown_to_invoking_user(destination)
        return {"sha256": whole_hash.hexdigest(), "chunks": chunk_records}
    except (DfuError, OSError):
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise


def _upload_skills_prefix(dfu_util, port, transfer_size, chunks, workdir=None):
    """Hash only the written skills prefix through bounded chunk alternatives."""
    if transfer_size <= 0 or transfer_size % SKILLS_SECTOR_SIZE:
        raise DfuError("The skills readback prefix is not a positive whole number of sectors.")
    selected_chunks = []
    for chunk in chunks:
        if chunk["offset_bytes"] >= transfer_size:
            break
        selected_chunks.append({**chunk, "transfer_bytes": min(
            chunk["size_bytes"], transfer_size - chunk["offset_bytes"])})
    if not selected_chunks or sum(item["transfer_bytes"] for item in selected_chunks) != transfer_size:
        raise DfuError("The bounded skills alternatives do not cover the readback prefix.")
    workdir = Path(workdir or tempfile.gettempdir())
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="skills-prefix-readback-", dir=workdir) as temporary:
        destination = Path(temporary) / "chunk.img"
        for chunk in selected_chunks:
            _upload_partition(dfu_util, port, chunk["name"],
                              chunk["transfer_bytes"], destination, chunk["size_bytes"])
            with destination.open("rb") as stream:
                _copy_exact(stream, _NullWriter(), chunk["transfer_bytes"], digest)
    return digest.hexdigest()


class _NullWriter:
    def write(self, data):
        return len(data)


def _backup_skills_partition_once(dfu_util, port, device_tag, capacity, chunks, identity=None,
                                  out=None):
    existing = (_find_partition_backup(device_tag, "skills", capacity)
                if _has_unique_device_tag(device_tag) and out is None else None)
    if existing:
        try:
            _verify_or_bind_partition_identity(existing, identity)
            return existing
        except DfuError:
            existing = None
    space_root = BACKUP_ROOT.parent if out is None else Path(out).expanduser().resolve().parent
    if shutil.disk_usage(space_root).free < capacity + SKILLS_CHUNK_BYTES:
        raise DfuError("Not enough free space to save the original skills partition.")
    directory = _new_operation_dir(out, "partition-backup")
    image = directory / "partition.img"
    try:
        uploaded = _upload_skills_partition(dfu_util, port, capacity, chunks,
                                            destination=image, workdir=directory)
        if identity and _ext4_uuid_from_image(image) != identity.get("ext4_uuid"):
            raise DfuError("The downloaded skills image ext4 UUID does not match the live partition.")
        same_bytes = (_find_partition_backup_by_hash("skills", capacity, uploaded["sha256"])
                      if out is None else None)
        if same_bytes:
            image.unlink(missing_ok=True)
            shutil.rmtree(directory, ignore_errors=True)
            _bind_partition_backup_identity(same_bytes, device_tag, uploaded["sha256"], identity)
            same_bytes["status"] = "existing verified backup reused"
            return same_bytes
        manifest = directory / "backup-manifest.json"
        _private_write(manifest, {"schema": 1, "kind": "jibo-partition-backup",
                                  "created_utc": _utc_now(), "device_tag": device_tag,
                                  "device_tags": [device_tag] if _has_unique_device_tag(device_tag) else [],
                                  "usb_port": port, "partition": "skills",
                                  "size_bytes": capacity, "sha256": uploaded["sha256"],
                                  "image": image.name, "chunks": uploaded["chunks"],
                                  "partition_identity": identity})
        return {"image": str(image), "sha256": uploaded["sha256"],
                "manifest": str(manifest), "status": "backup complete"}
    except (DfuError, OSError):
        image.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass
        raise


def _write_skills_chunks(dfu_util, port, candidate, capacity, chunks,
                         operation_directory, record_path, record, write_entry,
                         verify_readback=True, transfer_size=None):
    candidate = Path(candidate)
    if transfer_size is None:
        transfer_size = capacity
    if candidate.is_symlink() or not candidate.is_file() or \
            candidate.stat().st_size != transfer_size or transfer_size <= 0 or \
            transfer_size > capacity or transfer_size % SKILLS_SECTOR_SIZE:
        raise DfuError("The prepared skills prefix does not match its 512-byte-aligned transfer size.")
    candidate_hash = _sha256_file(candidate)
    readback_hash = hashlib.sha256()
    selected_chunks = []
    for chunk in chunks:
        if chunk["offset_bytes"] >= transfer_size:
            break
        selected_chunks.append({**chunk, "transfer_bytes": min(
            chunk["size_bytes"], transfer_size - chunk["offset_bytes"])})
    if not selected_chunks or sum(item["transfer_bytes"] for item in selected_chunks) != transfer_size:
        raise DfuError("The bounded skills alternatives do not cover the compact source prefix.")
    write_entry["chunks"] = []
    write_entry["transfer_bytes"] = transfer_size
    write_entry["status"] = "writing bounded skills chunks"
    _private_write(record_path, record)
    try:
        with tempfile.TemporaryDirectory(prefix="skills-write-", dir=operation_directory) as temporary:
            temporary = Path(temporary)
            for index, chunk in enumerate(selected_chunks, 1):
                candidate_piece = temporary / "candidate.img"
                expected_hash = _copy_file_slice(candidate, chunk["offset_bytes"],
                                                 chunk["transfer_bytes"], candidate_piece)
                chunk_record = {"alternative": chunk["name"],
                                "offset_bytes": chunk["offset_bytes"],
                                "size_bytes": chunk["transfer_bytes"],
                                "candidate_sha256": expected_hash,
                                "status": "write started"}
                write_entry["chunks"].append(chunk_record)
                _private_write(record_path, record)
                run_with_progress(
                    _partition_transfer_argv(dfu_util, port, chunk["name"], "-D",
                                             candidate_piece),
                    timeout=14400,
                    download_size=chunk["transfer_bytes"],
                    allow_progress_completion=True,
                    label="Writing skills chunk {}/{} ({})".format(
                        index, len(selected_chunks), chunk["name"]))
                if verify_readback:
                    readback = temporary / "readback.img"
                    actual_hash = _upload_partition(dfu_util, port, chunk["name"],
                                                    chunk["transfer_bytes"], readback,
                                                    chunk["size_bytes"])
                    chunk_record["readback_sha256"] = actual_hash
                    if actual_hash != expected_hash:
                        chunk_record["status"] = "readback mismatch"
                        _private_write(record_path, record)
                        raise DfuError("{} did not match its readback. DFU was left active; retry the update from the selected package.".format(
                            chunk["name"]))
                    with readback.open("rb") as stream:
                        _copy_exact(stream, _NullWriter(), chunk["transfer_bytes"], readback_hash)
                chunk_record["status"] = "verified" if verify_readback else "transfer complete"
                _private_write(record_path, record)
        if verify_readback and readback_hash.hexdigest() != candidate_hash:
            raise DfuError("The assembled skills readback did not match the prepared image.")
        if verify_readback:
            write_entry["readback_sha256"] = readback_hash.hexdigest()
        write_entry["status"] = "verified" if verify_readback else "transfer complete"
        _private_write(record_path, record)
        return candidate_hash
    except (DfuError, OSError):
        write_entry["status"] = "failed"
        _private_write(record_path, record)
        raise


def _update_candidates(folder):
    """List local choices quickly; full archive validation follows selection."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return []
    return [path for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold())
            if not path.is_symlink() and
            (path.is_dir() or (path.is_file() and path.name.lower().endswith(updates.ARCHIVE_SUFFIXES)))]


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _package_content_sha256(package):
    """Bind an update record to the archive or extracted image set used."""
    source = Path(package.source)
    if source.is_file():
        return _sha256_file(source)
    digest = hashlib.sha256()
    for name, image in sorted(package.images.items()):
        if image is None or Path(image).is_symlink() or not Path(image).is_file():
            raise DfuError("The selected update package is missing " + name + ".")
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(_sha256_file(image)))
    if not package.images:
        raise DfuError("The selected extracted update package has no image files.")
    return digest.hexdigest()




_STOCK_13_RESIZE_COMMANDS = [
    '#!/bin/sh',
    'if [ -f /var/etc/first_boot_resize.done ]',
    'then',
    'echo "First boot filesystem configuration already complete"',
    'else',
    'echo "First boot filesystem configuration"',
    '/usr/sbin/resize2fs /dev/mmcblk0p1',
    '/bin/sync',
    '/usr/sbin/resize2fs /dev/mmcblk0p2',
    '/bin/umount /var',
    '/usr/sbin/e2fsck -f /dev/mmcblk0p5',
    '/usr/sbin/resize2fs /dev/mmcblk0p5',
    '/bin/umount /usr/local',
    '/usr/sbin/e2fsck -f /dev/mmcblk0p4',
    '/usr/sbin/resize2fs /dev/mmcblk0p4',
    '/bin/umount /opt',
    '/usr/sbin/e2fsck -f /dev/mmcblk0p6',
    '/usr/sbin/resize2fs /dev/mmcblk0p6',
    '/bin/mount -a',
    'touch /var/etc/first_boot_resize.done',
    'fi',
]








def _backup_manifest(directory, port, image_path, digest, operations=None, device_tag=None,
                     transport="USB DFU upload", usb_state="DFU (0955:701a)",
                     profile="Jibo RAM DFU loader", partition_identity=None):
    record = {"schema": 1, "kind": "jibo-var-backup", "created_utc": _utc_now(),
              "partition": "var", "size_bytes": EXPECTED_VAR_SIZE, "sha256": digest,
              "profile": profile, "transport": transport, "usb_state": usb_state,
              "usb_port": port, "device_tag": device_tag or "usb-identity-unavailable",
              "device_tags": [device_tag] if device_tag else [],
              "image": Path(image_path).name,
              "partition_identity": partition_identity,
              "operations": operations or ["read var partition"]}
    _private_write(Path(directory) / "backup-manifest.json", record)
    return record


def _has_unique_device_tag(device_tag):
    return bool(device_tag and device_tag.startswith("serial-sha256:") and
                device_tag != "serial-sha256:" + hashlib.sha256(b"UNKNOWN").hexdigest())


def _find_verified_backup(device_tag=None, expected_sha256=None):
    if device_tag is None and expected_sha256 is None:
        return None
    if not BACKUP_ROOT.is_dir():
        return None
    manifests = sorted(BACKUP_ROOT.rglob("backup-manifest.json"),
                       key=lambda path: path.stat().st_mtime if path.exists() else 0,
                       reverse=True)
    for manifest_path in manifests:
        if manifest_path.is_symlink() or _in_backup_set(manifest_path):
            # Saved backup sets can be deleted from the Backups menu, so automatic
            # rollback copies come only from the toolkit's own var-backup folders.
            continue
        try:
            record = json.loads(manifest_path.read_text())
            if record.get("kind") != "jibo-var-backup" or record.get("status") == "failed":
                continue
            recorded_tags = set(record.get("device_tags", ()))
            if record.get("device_tag"):
                recorded_tags.add(record["device_tag"])
            if device_tag is not None and device_tag not in recorded_tags:
                continue
            if expected_sha256 is not None and record.get("sha256") != expected_sha256:
                continue
            filename = Path(record.get("image", "var.img")).name
            image_path = manifest_path.parent / filename
            if image_path.is_symlink() or not image_path.is_file() or image_path.stat().st_size != EXPECTED_VAR_SIZE:
                continue
            digest = _sha256_file(image_path)
            if digest != record.get("sha256"):
                continue
            return {"image": image_path, "sha256": digest,
                    "created_utc": record.get("created_utc", "unknown"),
                    "manifest": manifest_path}
        except (OSError, ValueError, TypeError):
            continue
    return None


def _select_factory_calibration_backup(device_tag, preferred, stock_var):
    """Choose verified calibration from this robot, preferring its rollback backup."""
    candidates = [preferred]
    if _has_unique_device_tag(device_tag) and BACKUP_ROOT.is_dir():
        manifests = sorted(BACKUP_ROOT.rglob("backup-manifest.json"),
                           key=lambda path: path.stat().st_mtime if path.exists() else 0,
                           reverse=True)
        for manifest in manifests:
            if manifest.is_symlink() or manifest.parent.is_symlink() or \
                    _in_backup_set(manifest) or str(manifest) == str(preferred.get("manifest")):
                continue
            try:
                record = json.loads(manifest.read_text())
                tags = set(record.get("device_tags", ()))
                tags.add(record.get("device_tag"))
                if record.get("kind") != "jibo-var-backup" or device_tag not in tags:
                    continue
                image = manifest.parent / Path(record.get("image", "var.img")).name
                if image.is_symlink() or not image.is_file():
                    continue
                candidates.append({"image": str(image), "sha256": record.get("sha256"),
                                   "manifest": str(manifest)})
            except (OSError, ValueError, TypeError):
                continue
    for backup in candidates:
        try:
            if _has_unique_device_tag(device_tag):
                _verify_var_backup_for_robot(backup, device_tag)
            if images.factory_calibration_available(backup["image"], stock_var):
                return backup
        except (DfuError, OSError, ValueError, TypeError):
            continue
    raise DfuError("No verified var backup for this robot contains its identity and camera "
                   "calibration. No partition write was attempted.")


def _resolve_factory_fresh_var(device_tag, backup, stock, destination, port, dfu_util):
    """Prefer the robot's verified local set; remote measurement fallback needs local identity."""
    try:
        source = _select_factory_calibration_backup(device_tag, backup, stock)
    except DfuError:
        if not calibration_service.CALIBRATION_SHARING_AVAILABLE:
            raise DfuError("This Jibo's calibration was not found in a local backup. "
                           "Nothing was written to the robot.") from None
        from jibo_calibration_flow import active
        session = active()
        if session is None:
            raise  # Scripted/CLI write approval does not authorize a network lookup.
        from jibo_calibration_restore import local_identity, build_fresh_var
        _verify_var_backup_for_robot(backup, device_tag)
        local_identity(backup["image"])
        identity = calibration_identity(port, dfu_util)
        download = session.lookup_for_fresh_setup(identity)
        if download is None:
            raise DfuError("No calibration source confirmed. No partition write was attempted.")
        if calibration_identity(port, dfu_util) != identity:
            raise DfuError("Connected robot changed; no downloaded calibration was used.")
        candidate = build_fresh_var(stock, backup["image"], destination, download.files)
        return backup, candidate, download
    return source, images.build_factory_fresh_var(stock, source["image"], destination), None


def _bind_verified_backup_identity(backup, device_tag, expected_sha256):
    """Associate a stable loader identity only after a full-image hash match."""
    if not _has_unique_device_tag(device_tag):
        return
    manifest = Path(backup["manifest"])
    try:
        record = json.loads(manifest.read_text())
        if (record.get("kind") != "jibo-var-backup" or
                record.get("sha256") != expected_sha256 or
                _sha256_file(backup["image"]) != expected_sha256):
            raise DfuError("The saved var backup changed during identity verification.")
        tags = list(dict.fromkeys([*record.get("device_tags", ()),
                                   record.get("device_tag"), device_tag]))
        record["device_tags"] = [tag for tag in tags if tag]
        _private_write(manifest, record)
    except (OSError, ValueError, TypeError) as exc:
        raise DfuError("Could not record the verified robot identity on its var backup.") from exc


def _prepare_current_and_baseline(dfu_util, port, device_tag, directory):
    identity = _calibration_capture_identity(port, dfu_util)
    result = _prepare_current_and_baseline_without_event(dfu_util, port, device_tag, directory)
    _calibration_backup_event({"status": "existing verified backup reused" if not result["baseline_matches_current"] else "backup complete",
                               "image": str(result["baseline"]), "sha256": result["baseline_sha256"]},
                              identity, port, dfu_util)
    return result


def _prepare_current_and_baseline_without_event(dfu_util, port, device_tag, directory):
    current = Path(directory) / "current-var.img"
    current_hash = _upload_var(dfu_util, port, current)
    baseline = (_find_verified_backup(device_tag) if _has_unique_device_tag(device_tag) else
                _find_verified_backup(expected_sha256=current_hash))
    if baseline:
        return {"before": current, "before_sha256": current_hash,
                "baseline": baseline["image"], "baseline_sha256": baseline["sha256"],
                "baseline_matches_current": current_hash == baseline["sha256"]}

    baseline_directory = _new_operation_dir(prefix="var-backup")
    baseline_image = baseline_directory / "var.img"
    try:
        shutil.move(str(current), str(baseline_image))
        baseline_image.chmod(0o600)
        _backup_manifest(baseline_directory, port, baseline_image, current_hash,
                         ["initial automatic backup before first var write"], device_tag)
    except (OSError, DfuError) as exc:
        raise DfuError("Could not preserve the initial var backup in " + str(baseline_directory) + ": " + str(exc)) from exc
    return {"before": baseline_image, "before_sha256": current_hash,
            "baseline": baseline_image, "baseline_sha256": current_hash,
            "baseline_matches_current": True}


def _remove_if_temporary(path, directory, protected=()):
    path = Path(path).resolve()
    if path.parent == Path(directory).resolve() and path not in {Path(item).resolve() for item in protected}:
        path.unlink(missing_ok=True)


def _record_prewrite_failure(directory, operation, error, state=None):
    """Keep a small record of a stopped operation without retaining another image."""
    directory = Path(directory)
    record_path = directory / "write-manifest.json"
    if record_path.exists():
        return record_path
    record = {"schema": 1, "kind": "jibo-var-write", "created_utc": _utc_now(),
              "partition": "var", "status": "failed before write", "write_started": False,
              "operation": operation, "error": str(error),
              "usb_state": "DFU (0955:701a)", "operation_directory": str(directory)}
    if state:
        record.update(baseline_backup=str(state["baseline"]),
                      baseline_sha256=state["baseline_sha256"],
                      current_sha256=state["before_sha256"])
    _private_write(record_path, record)
    return record_path


def _raise_live_operation_error(directory, operation, state, error):
    manifest = Path(directory) / "write-manifest.json"
    try:
        status = json.loads(manifest.read_text()).get("status") if manifest.is_file() else None
    except (OSError, ValueError):
        status = "unknown"
    write_started = status in ("write started", "failed", "verification failed")
    if write_started:
        raise DfuError(str(error) + "\nBackup and operation files are preserved in " + str(directory)) from error

    if state:
        _remove_if_temporary(state["before"], directory, (state["baseline"],))
        _remove_if_temporary(Path(directory) / "edited-var.img", directory, (state["baseline"],))
    record_path = _record_prewrite_failure(directory, operation, error, state)
    details = str(error) + "\nNo partition write was attempted."
    if state:
        details += " The saved rollback backup remains at " + str(state["baseline"]) + "."
    details += " Operation record: " + str(record_path)
    raise DfuError(details) from error


def calibration_identity(port=None, dfu_util=None):
    """Read an exact, unanimous CID from the selected supported loader. Never normalize."""
    selected_port, _, _, output = _dfu_context(port, dfu_util or tool("dfu-util"), include_output=True)
    serials = re.findall(r'serial="([^"]*)"', output)
    if (not serials or len(set(serials)) != 1 or not re.fullmatch(r"[0-9a-f]{32}", serials[0]) or
            serials[0] == "0" * 32):
        raise DfuError("Database unavailable: this recovery loader has no supported hardware ID.")
    return {"cid_serial": serials[0], "usb_port": selected_port}


def calibration_partition_images(identity):
    """Only hash/size-verified partition backups already bound to this exact CID."""
    cid = identity.get("cid_serial")
    if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{32}", cid) or cid == "0" * 32:
        return {}
    tag = "serial-sha256:" + hashlib.sha256(cid.encode("ascii")).hexdigest()
    selected = {}
    if not BACKUP_ROOT.is_dir() or BACKUP_ROOT.is_symlink():
        return selected
    manifests = sorted(BACKUP_ROOT.glob("partition-backup-*/backup-manifest.json"),
                       key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for manifest in manifests:
        try:
            if manifest.is_symlink() or manifest.parent.is_symlink() or manifest.stat().st_size > 16384:
                continue
            record = json.loads(manifest.read_text(encoding="utf-8"))
            partition = record.get("partition")
            destination = "rootfs_image" if partition in ("rootfsA", "rootfsB", "rootfs") else "skills_image" if partition == "skills" else None
            tags = set(record.get("device_tags", ())) | {record.get("device_tag")}
            if (destination is None or destination in selected or record.get("kind") != "jibo-partition-backup" or
                    record.get("status") == "failed" or tag not in tags):
                continue
            image = manifest.parent / Path(record.get("image", "partition.img")).name
            if (image.is_symlink() or not image.is_file() or image.stat().st_size <= 0 or
                    image.stat().st_size != record.get("size_bytes") or _sha256_file(image) != record.get("sha256")):
                continue
            selected[destination] = str(image)
        except (OSError, ValueError, TypeError):
            continue
    return selected


def _calibration_capture_identity(port, dfu_util):
    if not calibration_service.CALIBRATION_SHARING_AVAILABLE:
        return None
    try:
        from jibo_calibration_flow import active
    except ImportError:
        return None  # Optional sharing must not break older offline packaged installs.
    session = active()
    if session is None or getattr(session, "seen", False):
        return None
    try:
        return calibration_identity(port, dfu_util)
    except Exception:
        session.outcome = "Not shared"
        session.detail = "Backup saved; calibration sharing unavailable (no supported hardware ID)."
        return None


def _calibration_backup_event(backup, identity, port, dfu_util):
    if not calibration_service.CALIBRATION_SHARING_AVAILABLE or identity is None:
        return
    try:
        if calibration_identity(port, dfu_util) != identity:
            return
        from jibo_calibration_flow import on_verified_var_backup_ready
        on_verified_var_backup_ready(backup, identity)
    except Exception:
        pass  # A sharing/UI/ID failure cannot invalidate a verified backup.


def backup_var(port=None, dfu_util=None, out=None, refresh=False, expected_identity=None):
    identity = _calibration_capture_identity(port, dfu_util)
    result = _backup_var_without_event(port, dfu_util, out, refresh, expected_identity)
    _calibration_backup_event(result, identity, port, dfu_util)
    return result


def _backup_var_without_event(port=None, dfu_util=None, out=None, refresh=False, expected_identity=None):
    dfu_util = dfu_util or tool("dfu-util")
    port, _, device_tag = _dfu_context(port, dfu_util)
    existing = (_find_verified_backup(device_tag)
                if out is None and _has_unique_device_tag(device_tag) else None)
    if existing and not refresh:
        backup = {"image": existing["image"], "sha256": existing["sha256"],
                  "manifest": existing["manifest"]}
        _verify_var_backup_for_robot(backup, device_tag, expected_identity)
        return {"status": "existing verified backup reused", "image": str(existing["image"]),
                "sha256": existing["sha256"], "manifest": str(existing["manifest"]),
                "created_utc": existing["created_utc"],
                "message": "Use --refresh to capture the currently connected var state."}
    directory = _new_operation_dir(out, "var-backup")
    image_path = directory / "var.img"
    try:
        digest = _upload_var(dfu_util, port, image_path)
        if expected_identity and _ext4_uuid_from_image(image_path) != expected_identity.get("ext4_uuid"):
            raise DfuError("The downloaded var image ext4 UUID does not match the live partition.")
        if out is None:
            if existing is None:
                existing = _find_verified_backup(expected_sha256=digest)
            if existing and existing["sha256"] == digest:
                image_path.unlink(missing_ok=True)
                directory.rmdir()
                _bind_verified_backup_identity(existing, device_tag, digest)
                _verify_var_backup_for_robot({"image": existing["image"],
                    "sha256": existing["sha256"], "manifest": existing["manifest"]},
                    device_tag, expected_identity)
                return {"status": "existing verified backup reused", "image": str(existing["image"]),
                        "sha256": digest, "manifest": str(existing["manifest"]),
                        "created_utc": existing["created_utc"]}
        record = _backup_manifest(directory, port, image_path, digest, device_tag=device_tag,
                                  partition_identity=expected_identity)
    except DfuError as exc:
        image_path.unlink(missing_ok=True)
        _private_write(directory / "backup-manifest.json", {
            "schema": 1, "kind": "jibo-var-backup", "created_utc": _utc_now(),
            "partition": "var", "status": "failed", "usb_state": "DFU (0955:701a)",
            "usb_port": port, "error": str(exc), "operation_directory": str(directory)})
        raise
    return {"status": "backup complete", "image": str(image_path), "size_bytes": EXPECTED_VAR_SIZE,
            "sha256": digest, "manifest": str(directory / "backup-manifest.json"),
            "operation_directory": str(directory), "profile": record["profile"]}


def _confirm_write(supplied=None, plan=None):
    if callable(supplied):
        try:
            return bool(supplied(plan))
        except Exception as exc:
            raise DfuError("The write confirmation screen could not be completed: " + str(exc)) from exc
    if supplied is not None:
        if supplied != WRITE_CONFIRMATION:
            raise DfuError("For a write, --confirm must be exactly 'WRITE VAR'.")
        return True
    prompt = "Type WRITE VAR to write the edited var partition: "
    try:
        if sys.stdin.isatty():
            response = input(prompt)
        else:
            with open("/dev/tty", "r+", encoding="utf-8") as terminal:
                terminal.write(prompt)
                terminal.flush()
                response = terminal.readline().rstrip("\r\n")
    except OSError as exc:
        raise DfuError("A typed confirmation is required. Re-run with --confirm 'WRITE VAR'.") from exc
    if response != WRITE_CONFIRMATION:
        return False
    return True


def _write_candidate(candidate, before, directory, port, dfu_util, confirmation=None,
                     operation="write var", baseline=None, before_hash=None,
                     baseline_hash=None):
    candidate = Path(candidate).expanduser().resolve()
    before = Path(before).expanduser().resolve()
    candidate_hash = _sha256_file(candidate)
    before_hash = before_hash or _sha256_file(before)
    baseline = Path(baseline or before).resolve()
    baseline_hash = baseline_hash or _sha256_file(baseline)
    record = {"schema": 1, "kind": "jibo-var-write", "created_utc": _utc_now(),
              "partition": "var", "size_bytes": EXPECTED_VAR_SIZE,
              "before_sha256": before_hash, "candidate_sha256": candidate_hash,
              "baseline_backup": str(baseline), "baseline_sha256": baseline_hash,
              "transport": "USB DFU", "usb_state": "DFU (0955:701a)", "usb_port": port,
              "operation": operation, "status": "prepared", "reset_after_write": False}
    record_path = Path(directory) / "write-manifest.json"
    if candidate.stat().st_size != EXPECTED_VAR_SIZE or before.stat().st_size != EXPECTED_VAR_SIZE:
        raise DfuError("Edited var image must be exactly 524288000 bytes.")
    if before_hash == candidate_hash:
        record.update(status="unchanged", readback_sha256=before_hash)
        _private_write(record_path, record)
        _remove_if_temporary(before, directory, (baseline,))
        _remove_if_temporary(candidate, directory, (baseline,))
        return {"status": "unchanged", "message": "The edited image matches the current var partition; no write was needed.",
                "backup": str(baseline), "operation_directory": str(directory), "sha256": candidate_hash}
    print("\nWrite plan: var partition, 524288000 bytes, USB port " + port)
    print("Change: " + operation)
    print("Original rollback backup: " + str(baseline))
    if not record["baseline_sha256"] == before_hash:
        print("The current var state differs from the original backup; no additional persistent backup will be made.")
    print("Current var SHA-256: " + before_hash)
    print("Edited image: " + str(candidate))
    print("Edited image SHA-256: " + candidate_hash)
    print("A successful write will be read back and compared. The robot will not be reset.")
    try:
        confirmed = _confirm_write(confirmation, record)
    except DfuError:
        record["status"] = "confirmation rejected"
        _private_write(record_path, record)
        _remove_if_temporary(before, directory, (baseline,))
        _remove_if_temporary(candidate, directory, (baseline,))
        raise
    if not confirmed:
        record["status"] = "cancelled"
        _private_write(record_path, record)
        _remove_if_temporary(before, directory, (baseline,))
        _remove_if_temporary(candidate, directory, (baseline,))
        return {"status": "cancelled", "message": "The robot was not changed.",
                "backup": str(baseline), "operation_directory": str(directory)}
    record["status"] = "write started"
    _private_write(record_path, record)
    try:
        run_with_progress(
            _partition_transfer_argv(dfu_util, port, "var", "-D", candidate),
            timeout=900, label="Writing the edited 500 MiB var partition over USB",
            download_size=EXPECTED_VAR_SIZE, allow_progress_completion=True)
        readback = Path(directory) / "readback-var.img"
        readback_hash = _upload_var(dfu_util, port, readback)
        record["readback_sha256"] = readback_hash
        record["status"] = "verified" if readback_hash == candidate_hash else "verification failed"
        _private_write(record_path, record)
        if readback_hash != candidate_hash:
            raise DfuError("Var write readback did not match the edited image. The robot was not reset. "
                           "Backup and readback are preserved in " + str(directory))
    except DfuError as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        _private_write(record_path, record)
        raise DfuError(str(exc) + "\nBackup and operation files are preserved in " + str(directory)) from exc
    _remove_if_temporary(readback, directory, (baseline,))
    _remove_if_temporary(before, directory, (baseline,))
    _remove_if_temporary(candidate, directory, (baseline,))
    return {"status": "verified", "message": "Var was written and read back successfully. The robot was left in DFU; it was not reset.",
            "backup": str(baseline), "readback_sha256": candidate_hash,
            "operation_directory": str(directory), "sha256": candidate_hash}


def write_var(image, port=None, dfu_util=None, out=None, confirmation=None):
    image = Path(image).expanduser().resolve()
    if not image.is_file():
        raise DfuError("Edited var image does not exist: " + str(image))
    if image.stat().st_size != EXPECTED_VAR_SIZE:
        raise DfuError("Edited var image must be exactly 524288000 bytes.")
    images.inspect_var(image)
    dfu_util = dfu_util or tool("dfu-util")
    port, _, device_tag = _dfu_context(port, dfu_util)
    directory = _new_operation_dir(out, "write-var")
    try:
        state = _prepare_current_and_baseline(dfu_util, port, device_tag, directory)
    except DfuError as exc:
        (directory / "current-var.img").unlink(missing_ok=True)
        _private_write(directory / "write-manifest.json", {
            "schema": 1, "kind": "jibo-var-write", "created_utc": _utc_now(),
            "status": "failed before write", "usb_state": "DFU (0955:701a)",
            "usb_port": port, "error": str(exc), "operation_directory": str(directory)})
        raise
    return _write_candidate(image, state["before"], directory, port, dfu_util, confirmation,
                            baseline=state["baseline"], before_hash=state["before_sha256"],
                            baseline_hash=state["baseline_sha256"])


def _saved_calibration_partitions(device_tag, names, capacities):
    """Enumerate verified LOCAL inputs for a flash, not the backup capture flow."""
    if not _has_unique_device_tag(device_tag) or not BACKUP_ROOT.is_dir():
        return
    for manifest in sorted(BACKUP_ROOT.glob("partition-backup-*/backup-manifest.json"), reverse=True):
        try:
            if manifest.is_symlink() or manifest.parent.is_symlink():
                continue
            record = json.loads(manifest.read_text())
            tags = set(record.get("device_tags", ()))
            tags.add(record.get("device_tag"))
            name = record.get("partition")
            if record.get("kind") != "jibo-partition-backup" or device_tag not in tags or \
                    name not in names or record.get("size_bytes") != capacities[name]:
                continue
            image = manifest.parent / "partition.img"
            if image.is_symlink() or not image.is_file() or image.stat().st_size != capacities[name]:
                continue
            digest = _sha256_file(image)
            if digest != record.get("sha256"):
                continue
            yield {"image": str(image), "sha256": digest, "manifest": str(manifest)}
        except (OSError, ValueError, TypeError, AttributeError):
            continue


def _prepare_partition_calibration(prepared, capacities, device_tag, workdir):
    """Use existing same-robot partition backups; do not capture or alter them."""
    groups = (("rootfs", "touchscreen", ("rootfsA", "rootfsB")),
              ("skills", "factory_record", ("skills",)))
    report = {"status": {}, "sources": {}, "files": []}
    for partition, key, names in groups:
        selected = None
        for backup in _saved_calibration_partitions(device_tag, names, capacities):
            if selected is None:
                selected = backup
            with tempfile.TemporaryDirectory(prefix="calibration-check-", dir=workdir) as temp:
                _spec, raw, _status, _metadata = images._calibration_item(backup["image"], key, temp)
            if raw is not None:
                selected = backup
                break
        for name in names:
            built = images.build_calibrated_partition(
                prepared[name], selected["image"] if selected else None,
                Path(workdir) / ("factory-calibrated-" + name + ".ext4"), partition)
            prepared[name] = Path(built["image"])
            report["status"].update(built["calibration_status"])
            for path in built["calibration_files"]:
                if path not in report["files"]:
                    report["files"].append(path)
        if selected:
            report["sources"][partition] = selected
    return report


def _confirm_update_plan(plan, confirmation):
    if callable(confirmation):
        return bool(confirmation(plan))
    if confirmation is None:
        return _ask_confirmation('Flash this package?', UPDATE_CONFIRMATION)
    if confirmation != UPDATE_CONFIRMATION:
        raise DfuError("For an update write, --confirm must be exactly 'FLASH UPDATE'.")
    return True


def _hosted_space_precheck(metadata, out, var_capacity):
    import jibo_flash_contract
    required = jibo_flash_contract.workspace_bytes(metadata, var_capacity)
    volumes = {}
    for selected, budget in ((Path(out).expanduser().absolute() if out else BACKUP_ROOT,
                               required), (BACKUP_ROOT, var_capacity + 1024**3)):
        if any(parent.is_symlink() for parent in (selected, *selected.parents)):
            raise DfuError('Hosted operation/backup paths cannot contain symlinks')
        existing = selected
        while not existing.exists():
            existing = existing.parent
        device = existing.stat().st_dev
        prior = volumes.get(device)
        if prior is None or budget > prior[1]:
            volumes[device] = (existing, budget)
    for path, budget in volumes.values():
        if shutil.disk_usage(path).free < budget:
            raise DfuError('Not enough free space for hosted private preparation/rollback: ' + str(budget) + ' bytes required')




def flash_update(package_path, var_policy=flash_policy.KEEP, port=None, dfu_util=None, out=None,
                 confirmation=None, dry_run=False, resume_from=None,
                 verify_readback=False, unsafe_without_calibration=False, local_work=None):
    """Install fresh userspace filesystems, optionally restoring this robot's calibration."""
    if resume_from is not None:
        raise DfuError("Interrupted flashes cannot currently resume. Keep the saved rollback backup; "
                       "select the package again for a new reviewed flash. No USB action was attempted.")
    if var_policy not in flash_policy.POLICIES:
        raise DfuError("Choose keep-calibration or without-calibration.")
    fresh_with_calibration = var_policy == flash_policy.KEEP
    if not fresh_with_calibration and not dry_run and unsafe_without_calibration is not True:
        raise DfuError("Flashing without calibration requires explicit unsafe confirmation.")
    local_work = local_work or _call_with_progress
    package = local_work(lambda: updates.validate_package(package_path), "Checking the image")
    phoenix = getattr(package, 'phoenix', None)
    dfu_util = dfu_util or tool("dfu-util")
    selected = select_device(devices(), port)
    if selected is None:
        raise DfuError("No Jibo RCM/DFU device detected. Connect the robot by USB first.")
    if selected["state"] == "rcm":
        raise DfuError("The robot is in RCM/APX. Enter DFU with ShofEL before installing an update.")
    port, names, device_tag, alt_output = _dfu_context(
        selected["port"], dfu_util, include_output=True)
    if phoenix and not _has_unique_device_tag(device_tag):
        raise DfuError('Hosted writes require the current loader with stable eMMC identity; no USB write attempted')
    partitions = list(UPDATE_ORDER)
    skills_aliases = [name for name in names if name.startswith("skills-")]
    if "skills" not in names and not skills_aliases:
        raise DfuError("This DFU loader does not expose the skills partition or its bounded skills-### alternatives.")
    missing = [name for name in partitions if name != "skills" and name not in names]
    if missing:
        raise DfuError("This DFU profile does not expose the required partitions: " + ", ".join(missing))
    capacities = _read_gpt_capacities(dfu_util, port, names)
    skills_chunks = _validate_skills_chunk_alternatives(
        capacities["skills"], names, alt_output)
    plan = {"package": str(package.source), "version": package.version,
            "usb_port": port, "var_policy": var_policy,
            "calibration_choice": flash_policy.KEEP_LABEL if fresh_with_calibration else flash_policy.WITHOUT_LABEL,
            "calibration_detail": flash_policy.KEEP_DETAIL if fresh_with_calibration else flash_policy.WITHOUT_DETAIL,
            "image_strategy": "compact-prefix-v1",
            "filesystem_resize": "Transfer compact image prefixes; target init expands the filesystems at first boot.",
            "rollback_backup": "var only; other partitions are restored from the package",
            "partitions": [{"name": name, "bytes": capacities[name]} for name in partitions],
            "skills_transfer": ("compact official image prefix through bounded DFU writes"
                                if skills_chunks else "compact official image prefix through its named DFU alternative"),
            "readback_policy": "source image prefix readback" if verify_readback else "DFU transfer completion",
            "status": "plan only" if dry_run else "awaiting confirmation"}
    if phoenix:
        plan['phoenix'] = {'schema': 1, 'id': phoenix['id'], 'profile': phoenix['profile'],
                           'strategy': phoenix['strategy'], 'base_release': phoenix['base_release'],
                           'components': phoenix['components'],
                           'metadata_sha256': phoenix['_metadata_sha256'],
                           'var_policy': var_policy, 'image_notice': flash_policy.PHOENIX_LINE,
                           'scope': 'Userspace filesystems only; no boot0/boot1, BCT, GPT, fuses or universal brick recovery'}
        _hosted_space_precheck(phoenix, out, capacities['var'])
    if dry_run:
        return plan
    print("\nFull-flash update plan:")
    print(json.dumps(plan, indent=2))
    if not _confirm_update_plan(plan, confirmation):
        return {**plan, 'status': 'cancelled'}

    directory = _new_operation_dir(out, "flash-update")
    record_path = directory / "update-manifest.json"
    record = {"schema": 1, "kind": "jibo-full-flash-update", "created_utc": _utc_now(),
              **plan, "status": "preparing", "backups": {}, "writes": []}
    record["package_sha256"] = local_work(lambda: _package_content_sha256(package), "Checking the image")
    _private_write(record_path, record)
    try:
        with tempfile.TemporaryDirectory(prefix="prepared-", dir=directory) as prepared_dir:
            compact_images = local_work(
                lambda: updates.prepare_compact_images(package, capacities, prepared_dir),
                "Preparing Jibo's software")
            prepared = dict(compact_images.images)
            missing_candidates = [name for name in partitions if name not in prepared]
            if missing_candidates:
                raise DfuError("The update package did not prepare: " + ", ".join(missing_candidates))
            transfer_sizes = {}
            for name in partitions:
                candidate_path = Path(prepared[name])
                if candidate_path.is_symlink() or not candidate_path.is_file():
                    raise DfuError("Prepared {} image is missing or is a symbolic link.".format(name))
                transfer_size = candidate_path.stat().st_size
                if transfer_size <= 0 or transfer_size > capacities[name] or \
                        transfer_size % SKILLS_SECTOR_SIZE:
                    raise DfuError("Prepared {} prefix does not fit the GPT partition or 512-byte DFU sectors."
                                   .format(name))
                transfer_sizes[name] = transfer_size
            candidate_hashes = local_work(
                lambda: {name: _sha256_file(prepared[name]) for name in partitions},
                "Preparing Jibo's software")
            if phoenix:
                signed = {member['role']: member for member in phoenix['members']}
                for name in partitions:
                    role = 'rootfs' if name in ('rootfsA', 'rootfsB') else name
                    if candidate_hashes[name] != signed[role]['sha256'] or transfer_sizes[name] != signed[role]['size']:
                        raise DfuError('Prepared hosted prefix differs from its authenticated member hash/size')
            record["status"] = "backing up var"
            _private_write(record_path, record)
            backup = backup_var(port, dfu_util, refresh=True)
            _verify_var_backup_for_robot(backup, device_tag)
            record["backups"]["var"] = backup
            _private_write(record_path, record)

            if fresh_with_calibration:
                record["partition_calibration"] = _prepare_partition_calibration(
                    prepared, capacities, device_tag, prepared_dir)
                for key, status in record["partition_calibration"]["status"].items():
                    print("Calibration {}: {}".format(key, status), flush=True)
                record["status"] = "preparing fresh var with calibration"
                _private_write(record_path, record)
                source, calibrated, remote_calibration = _resolve_factory_fresh_var(
                    device_tag, record["backups"]["var"], prepared["var"],
                    Path(prepared_dir) / "factory-fresh-var.ext4", port, dfu_util)
                prepared["var"] = Path(calibrated["image"])
                candidate_hashes["var"] = calibrated["sha256"]
                record["factory_calibration"] = {
                    "source_manifest": str(source["manifest"]),
                    "source_sha256": source["sha256"],
                    "files": calibrated["calibration_files"],
                    "identity_restored": True, "mode": "oobe",
                    "camera_matches_stock": calibrated.get("camera_matches_stock"),
                    "status": {**calibrated.get("calibration_status", {}),
                               **record["partition_calibration"]["status"]},
                    "removed_distortion_caches": calibrated.get("removed_distortion_caches", []),
                    "partition_files": record["partition_calibration"]["files"],
                    "candidate_sha256": calibrated["sha256"]}
                if remote_calibration is not None:
                    verify_readback = True
                    record["factory_calibration"]["remote_manifest"] = remote_calibration.manifest
                    record["readback_policy"] = "mandatory source image prefix readback (database calibration)"
                    resolved_plan = dict(plan, calibration_source="Community reviewed; not factory-authenticated",
                                         calibration_files=calibrated["calibration_files"],
                                         calibration_revision=remote_calibration.manifest["revision"],
                                         calibration_digest=remote_calibration.manifest["bundle_sha256"],
                                         identity_source="this robot's verified local rollback backup",
                                         readback_policy=record["readback_policy"])
                    if not callable(confirmation) or not confirmation(resolved_plan):
                        record["status"] = "cancelled"
                        _private_write(record_path, record)
                        return {"status": "cancelled", "message": "Resolved calibration write plan was not confirmed. No partition write was attempted."}
                    identity = calibration_identity(port, dfu_util)
                    from jibo_calibration_client import load_core
                    if load_core().robot_key_from_cid(identity["cid_serial"]) != remote_calibration.manifest["robot_key"]:
                        raise DfuError("Connected robot changed; no partition write was attempted.")
                _private_write(record_path, record)
                for key, status in calibrated.get("calibration_status", {}).items():
                    if key not in ("touchscreen", "factory_record"):
                        print("Calibration {}: {}".format(key, status), flush=True)
                if calibrated.get("camera_matches_stock"):
                    print("The selected var backup's camera files match this package's defaults; "
                          "the saved files and robot identity were copied into fresh var.", flush=True)

            # Signed package prefixes were checked BEFORE local calibration edits.
            # Freeze hashes of the actual personalized candidates for readback.
            candidate_hashes = local_work(
                lambda: {name: _sha256_file(prepared[name]) for name in partitions},
                "Preparing Jibo's software")
            for name in partitions:
                if Path(prepared[name]).stat().st_size != transfer_sizes[name]:
                    raise DfuError("Calibration changed the partition prefix length; no write attempted.")
            if phoenix:
                record['phoenix']['archive_sha256'] = record['package_sha256']
                record['phoenix']['device_tag'] = device_tag
                _private_write(record_path, record)
                current = select_device(devices(), port)
                if current is None or current['state'] != 'dfu':
                    raise DfuError('Robot disconnected or changed mode before hosted writes')
                rechecked = _dfu_context(port, dfu_util, include_output=True)
                if rechecked[2] != device_tag or _read_gpt_capacities(dfu_util, port, rechecked[1]) != capacities:
                    raise DfuError('Robot identity or GPT changed before first hosted write')

            def write_and_verify(name, candidate, digest, entry):
                entry["status"] = "write started"
                entry.pop("readback_sha256", None)
                record["status"] = "writing"
                _private_write(record_path, record)
                if name == "skills" and skills_chunks:
                    actual = _write_skills_chunks(
                        dfu_util, port, candidate, capacities[name], skills_chunks,
                        directory, record_path, record, entry, verify_readback,
                        transfer_size=transfer_sizes[name])
                else:
                    run_with_progress(
                        _partition_transfer_argv(dfu_util, port, name, "-D", candidate),
                        timeout=14400,
                        download_size=transfer_sizes[name],
                        allow_progress_completion=True,
                        label="Writing {} prefix ({} bytes)".format(name, transfer_sizes[name]))
                    actual = None
                    if verify_readback:
                        with tempfile.TemporaryDirectory(prefix="readback-", dir=directory) as readback_dir:
                            readback = Path(readback_dir) / "partition.img"
                            actual = _upload_partition(dfu_util, port, name,
                                                       transfer_sizes[name], readback,
                                                       capacities[name])
                if verify_readback:
                    entry["readback_sha256"] = actual
                    if actual != digest:
                        entry["status"] = "verification failed"
                        _private_write(record_path, record)
                        raise DfuError("{} did not match its readback. DFU was left active; retry the update from the selected package.".format(name))
                entry["status"] = "verified" if verify_readback else "transfer complete"
                _private_write(record_path, record)

            for name in partitions:
                candidate = prepared[name]
                digest = candidate_hashes[name]
                if _sha256_file(candidate) != digest:
                    raise DfuError("Prepared image changed before write; robot remains in DFU.")
                entry = {"partition": name, "candidate_sha256": digest,
                         "size_bytes": capacities[name], "transfer_bytes": transfer_sizes[name],
                         "status": "write started"}
                record["writes"].append(entry)
                write_and_verify(name, candidate, digest, entry)
        record["status"] = "verified; reset pending" if verify_readback else "transferred; reset pending"
        _private_write(record_path, record)
        try:
            # Without -a, dfu-util sees one DFU interface per alternate and refuses to detach.
            run([dfu_util, "-d", "0955:701a", "--path", port, "-a", MARKER, "-e", "-R"],
                timeout=60)
            record["status"] = "verified; reset requested" if verify_readback else "transferred; reset requested"
        except DfuError as exc:
            record["status"] = "verified; reset not confirmed" if verify_readback else "transferred; reset not confirmed"
            record["reset_error"] = str(exc)
        _private_write(record_path, record)
        return {"status": record["status"], "package": str(package.source),
                "var_policy": plan["var_policy"],
                "transferred_partitions": partitions,
                "verified_partitions": partitions if verify_readback else [],
                "manifest": str(record_path), "backups": record["backups"]}
    except (DfuError, updates.UpdateError, images.ImageError, OSError, calibration_service.ClientError) as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        _private_write(record_path, record)
        raise DfuError(str(exc) + "\nUpdate record: " + str(record_path)) from exc


def verify_update_write(manifest, partition, port=None, dfu_util=None):
    """Read back one direct DFU update target and compare it with its record."""
    if partition not in ("rootfsA", "rootfsB", "services", "skills", "var"):
        raise DfuError("Readback comparison supports rootfsA, rootfsB, services, skills, and var.")
    manifest = Path(manifest).expanduser()
    if manifest.is_symlink() or not manifest.is_file():
        raise DfuError("Update record is missing or is a symbolic link: " + str(manifest))
    try:
        record = json.loads(manifest.read_text())
    except (OSError, ValueError) as exc:
        raise DfuError("Could not read the update record: " + str(exc)) from exc
    if record.get("kind") != "jibo-full-flash-update":
        raise DfuError("The supplied file is not an update operation record.")
    entries = [item for item in record.get("writes", [])
               if isinstance(item, dict) and item.get("partition") == partition]
    if len(entries) != 1:
        raise DfuError("The update record does not contain exactly one write for " + partition + ".")
    expected_hash = entries[0].get("candidate_sha256")
    expected_capacity = entries[0].get("size_bytes")
    transfer_size = entries[0].get("transfer_bytes", expected_capacity)
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or \
            not isinstance(expected_capacity, int) or expected_capacity <= 0 or \
            not isinstance(transfer_size, int) or transfer_size <= 0 or \
            transfer_size > expected_capacity or transfer_size % SKILLS_SECTOR_SIZE:
        raise DfuError("The update record has an invalid size or hash for " + partition + ".")

    dfu_util = dfu_util or tool("dfu-util")
    port, names, device_tag, alt_output = _dfu_context(port, dfu_util, include_output=True)
    if partition not in names:
        if partition != "skills" or not any(name.startswith("skills-") for name in names):
            raise DfuError("The DFU loader does not expose " + partition + ".")
    capacities = _read_gpt_capacities(dfu_util, port, names)
    if capacities.get(partition) != expected_capacity:
        raise DfuError("The live " + partition + " size does not match the saved update record.")
    if partition == "skills" and "skills" not in names:
        chunks = _validate_skills_chunk_alternatives(expected_capacity, names, alt_output)
        actual_hash = _upload_skills_prefix(dfu_util, port, transfer_size, chunks)
    else:
        with tempfile.TemporaryDirectory(prefix="jibo-verify-update-") as temporary:
            actual_hash = _upload_partition(dfu_util, port, partition, transfer_size,
                                            Path(temporary) / "readback.img", expected_capacity)
    return {"status": "match" if actual_hash == expected_hash else "mismatch",
            "partition": partition, "size_bytes": transfer_size,
            "partition_capacity_bytes": expected_capacity,
            "expected_sha256": expected_hash, "readback_sha256": actual_hash,
            "temporary_readback_removed": True}


def _validate_file_path(path):
    try:
        encoded = path.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise DfuError("File paths must be valid UTF-8.") from exc
    if not encoded or len(encoded) > 255 or not path.startswith("/") or path == "/":
        raise DfuError("A file path must be an absolute path of 1 to 255 bytes inside the selected partition.")
    if any(byte < 0x20 or byte == 0x7f for byte in encoded):
        raise DfuError("File paths cannot contain NUL or control bytes.")
    components = encoded[1:].split(b"/")
    if any(not part or part in (b".", b"..") for part in components):
        raise DfuError("File paths cannot contain empty, '.' or '..' components.")
    return encoded


def _file_alt_name(partition):
    if partition not in FILE_LEVEL_PARTITIONS:
        raise DfuError("File operations support only: " + ", ".join(FILE_LEVEL_PARTITIONS))
    return "jibo-file-" + partition + "-in"


def _file_response_alt_name(partition):
    if partition not in FILE_LEVEL_PARTITIONS:
        raise DfuError("File operations support only: " + ", ".join(FILE_LEVEL_PARTITIONS))
    return "jibo-file-" + partition + "-out"


def _file_loader_context(port, dfu_util, partitions, require_identity):
    port, names, device_tag, listing = _dfu_context(port, dfu_util, include_output=True)
    if FILE_LEVEL_MARKER not in names:
        raise DfuError("The active DFU loader does not support file access. Load the toolkit's current RAM loader or use a full-partition workflow.")
    missing = [name for name in (_file_alt_name(partition) for partition in partitions)
               if name not in names]
    if missing:
        raise DfuError("The active loader does not expose file operations for: " + ", ".join(missing))
    missing = [name for name in (_file_response_alt_name(partition) for partition in partitions)
               if name not in names]
    if missing:
        raise DfuError("The active loader does not expose file responses for: " + ", ".join(missing))
    if require_identity and not _has_unique_device_tag(device_tag):
        raise DfuError("File writes require a stable eMMC identity from the candidate loader. The current UNKNOWN-serial "
                       "loader cannot safely reuse rollback baselines; use the full-partition workflow.")
    return port, names, device_tag, listing


def _live_var_file_available(port, dfu_util):
    _, names, _ = _dfu_context(port, dfu_util)
    return {FILE_LEVEL_MARKER, "jibo-file-var-in", "jibo-file-var-out"}.issubset(names)


def _file_request(path, operation, content=b"", request_id=None, precondition=None):
    path_bytes = _validate_file_path(path)
    if operation not in (FILE_RPC_READ, FILE_RPC_WRITE, FILE_RPC_STAT):
        raise DfuError("Unsupported file request operation.")
    if operation in (FILE_RPC_READ, FILE_RPC_STAT) and content:
        raise DfuError("A read or stat request cannot include file contents.")
    if operation == FILE_RPC_WRITE and not (0 < len(content) <= FILE_LEVEL_MAX_BYTES):
        raise DfuError("A replacement must contain 1 to {} bytes.".format(FILE_LEVEL_MAX_BYTES))
    if operation == FILE_RPC_WRITE and (precondition is None or
                                         len(precondition) != FILE_WRITE_PRECONDITION.size):
        raise DfuError("A file replacement requires a complete compare-and-write precondition.")
    if operation != FILE_RPC_WRITE and precondition is not None:
        raise DfuError("Only file replacements accept a compare-and-write precondition.")
    request_id = request_id or secrets.token_bytes(16)
    if len(request_id) != 16:
        raise DfuError("A file request ID must contain 16 bytes.")
    request = (FILE_RPC_HEADER.pack(FILE_RPC_MAGIC, operation, len(path_bytes), len(content), request_id) +
               path_bytes + (precondition or b"") + content)
    if len(request) > FILE_RPC_MAX_REQUEST:
        raise DfuError("The file mailbox request exceeds its protocol size limit.")
    return request, request_id


def _decode_file_response(response, request_id):
    if len(response) < FILE_RPC_RESPONSE_HEADER.size:
        raise DfuError("The file mailbox returned a truncated response.")
    magic, response_id, status, data_size, digest = FILE_RPC_RESPONSE_HEADER.unpack_from(response)
    data = response[FILE_RPC_RESPONSE_HEADER.size:]
    if magic != FILE_RPC_RESPONSE_MAGIC or response_id != request_id:
        raise DfuError("The file mailbox response does not match the current request.")
    if status != 0:
        raise FileRpcStatusError(status)
    if data_size != len(data) or data_size > FILE_LEVEL_MAX_BYTES:
        raise DfuError("The file mailbox response has an invalid byte count.")
    if hashlib.sha256(data).digest() != digest:
        raise DfuError("The file mailbox response failed its SHA-256 check.")
    return data


def _file_request_transfer(dfu_util, port, partition, request, directory, label, quiet=False):
    alt = _file_alt_name(partition)
    request_path = Path(directory) / "file-request.bin"
    request_path.write_bytes(request)
    request_path.chmod(0o600)
    _chown_to_invoking_user(request_path)
    try:
        run_with_progress([dfu_util, "-d", "0955:701a", "--path", port, "-a", alt,
                           "-D", str(request_path)],
                          timeout=120, label=label,
                          download_size=len(request), allow_progress_completion=True,
                          display=not quiet)
    finally:
        request_path.unlink(missing_ok=True)


def _read_partition_file_rpc(dfu_util, port, partition, path, workdir, quiet=False):
    """Ask the mailbox alt to select and upload one bounded existing file."""
    workdir = Path(workdir)
    request, request_id = _file_request(path, FILE_RPC_READ)
    _file_request_transfer(dfu_util, port, partition,
                          request, workdir,
                          "Selecting {} for read".format(path), quiet=quiet)
    return _upload_file_response(dfu_util, port, partition, request_id, workdir, path,
                                 quiet=quiet)


def _stat_partition_file_rpc(dfu_util, port, partition, path, workdir, quiet=False):
    request, request_id = _file_request(path, FILE_RPC_STAT)
    _file_request_transfer(dfu_util, port, partition, request, workdir,
                           "Selecting {} for stat".format(path), quiet=quiet)
    response = _upload_file_response(dfu_util, port, partition, request_id, workdir,
                                     "stat for {}".format(path), quiet=quiet)
    if len(response) != FILE_STAT_STRUCT.size:
        raise DfuError("The file mailbox returned invalid stat metadata.")
    (inode, size, allocated_bytes, uid, gid, mode, nlink,
     mapping_count, ext4_uuid) = FILE_STAT_STRUCT.unpack(response)
    if (not inode or not stat.S_ISREG(mode) or size > FILE_LEVEL_MAX_BYTES or
            allocated_bytes < size or not allocated_bytes or
            allocated_bytes > FILE_LEVEL_MAX_BYTES or
            nlink != 1 or mapping_count != 1 or len(ext4_uuid) != 16):
        raise DfuError("The target must be an existing bounded regular file with one "
                       "allocated block mapping and no hard links.")
    return {"inode": inode, "size_bytes": size, "allocated_bytes": allocated_bytes,
            "uid": uid, "gid": gid, "mode": mode, "nlink": nlink,
            "mapping_count": mapping_count, "ext4_uuid": ext4_uuid.hex()}


def _upload_file_response(dfu_util, port, partition, request_id, workdir, label, quiet=False):
    destination = Path(workdir) / "file-response.bin"
    destination.unlink(missing_ok=True)
    try:
        run_with_progress([dfu_util, "-d", "0955:701a", "--path", port,
                           "-a", _file_response_alt_name(partition), "-U", str(destination)],
                          timeout=120, label="Reading {} from {}".format(label, partition),
                          display=not quiet)
        if not destination.is_file() or destination.stat().st_size > (
                FILE_LEVEL_MAX_BYTES + FILE_RPC_RESPONSE_HEADER.size):
            raise DfuError("The file response is missing or exceeds the mailbox size limit.")
        destination.chmod(0o600)
        _chown_to_invoking_user(destination)
        return _decode_file_response(destination.read_bytes(), request_id)
    finally:
        destination.unlink(missing_ok=True)


def read_partition_file_live(path, partition="var", port=None, dfu_util=None):
    """Read one existing, bounded file through the DFU mailbox."""
    _validate_file_path(path)
    dfu_util = dfu_util or tool("dfu-util")
    port, _, _, _ = _file_loader_context(port, dfu_util, (partition,), False)
    with tempfile.TemporaryDirectory(prefix="jibo-file-read-", dir="/tmp") as temp:
        return _read_partition_file_rpc(dfu_util, port, partition, path, temp)


def stat_partition_file_live(path, partition="var", port=None, dfu_util=None):
    """Read current ownership and permissions without changing the partition."""
    _validate_file_path(path)
    dfu_util = dfu_util or tool("dfu-util")
    port, _, _, _ = _file_loader_context(port, dfu_util, (partition,), False)
    with tempfile.TemporaryDirectory(prefix="jibo-file-stat-", dir="/tmp") as temp:
        details = _stat_partition_file_rpc(dfu_util, port, partition, path, temp)
    return {"status": "stat", "partition": partition, "path": path,
            "permissions_octal": format(stat.S_IMODE(details["mode"]), "04o"),
            **details}


def _partition_file_backup(dfu_util, port, device_tag, partition, size, names,
                           alt_output, identity):
    if partition == "var":
        # backup-var already enforces byte-identical matching for an UNKNOWN serial.
        record = backup_var(port=port, dfu_util=dfu_util,
                            expected_identity=identity)
        backup = {"image": record["image"], "sha256": record["sha256"],
                  "manifest": record["manifest"], "status": record["status"]}
        _verify_var_backup_for_robot(backup, device_tag, identity)
        return backup
    if partition == "skills":
        if "skills" in names:
            return _backup_partition_once(dfu_util, port, device_tag, partition, size, identity)
        chunks = _validate_skills_chunk_alternatives(size, names, alt_output)
        if not chunks:
            raise DfuError("The loader cannot create a full skills rollback backup.")
        return _backup_skills_partition_once(dfu_util, port, device_tag, size, chunks, identity)
    if partition not in names:
        raise DfuError("The loader cannot create a full {} rollback backup; no bounded partition read is exposed."
                       .format(partition))
    return _backup_partition_once(dfu_util, port, device_tag, partition, size, identity)


class FileTransaction:
    """A reviewed batch of existing-file replacements with a reusable var baseline."""

    def __init__(self, partitions, port=None, dfu_util=None, out=None, guided=False):
        requested = tuple(dict.fromkeys(partitions))
        if not requested or any(partition not in FILE_LEVEL_PARTITIONS for partition in requested):
            raise DfuError("Declare one or more supported ext4 partitions: " +
                           ", ".join(FILE_LEVEL_PARTITIONS))
        self.partitions = requested
        self.port = port
        self.dfu_util = dfu_util or tool("dfu-util")
        self.out = out
        self.edits = []
        self._committed = False
        self.guided = guided

    def replace(self, partition, path, content, description=None):
        if self._committed:
            raise DfuError("This file transaction has already been committed.")
        if partition not in self.partitions:
            raise DfuError("Partition {} was not declared in this file transaction.".format(partition))
        _validate_file_path(path)
        if not isinstance(content, bytes) or not (0 < len(content) <= FILE_LEVEL_MAX_BYTES):
            raise DfuError("A replacement must contain 1 to {} bytes.".format(FILE_LEVEL_MAX_BYTES))
        self.edits.append({"partition": partition, "path": path, "content": content,
                           "description": description})

    def transform(self, partition, path, callback, description=None):
        """Build a replacement from the transaction's single preflight read."""
        if self._committed:
            raise DfuError("This file transaction has already been committed.")
        if partition not in self.partitions or not callable(callback):
            raise DfuError("A declared partition and a file transformer are required.")
        _validate_file_path(path)
        self.edits.append({"partition": partition, "path": path, "transform": callback,
                           "description": description})

    def commit(self, confirmation=None):
        if self._committed:
            raise DfuError("This file transaction has already been committed.")
        self._committed = True
        if not self.edits:
            raise DfuError("The file transaction has no replacements.")
        touched = tuple(dict.fromkeys(edit["partition"] for edit in self.edits))
        edit_keys = [(edit["partition"], edit["path"]) for edit in self.edits]
        if len(set(edit_keys)) != len(edit_keys):
            raise DfuError("A file transaction can replace each partition path only once.")
        port, names, device_tag, alt_output = _file_loader_context(
            self.port, self.dfu_util, touched, True)
        gpt_layout = _read_gpt_layout(self.dfu_util, port, names)
        capacities = {name: details["size_bytes"] for name, details in gpt_layout.items()}
        partition_identities = {}
        backups = {}
        directory = _new_operation_dir(self.out, "file-transaction")
        record_path = directory / "file-transaction.json"
        record = {"schema": 1, "kind": "jibo-file-transaction", "created_utc": _utc_now(),
                  "usb_state": "DFU (0955:701a)", "usb_port": port,
                  "device_tag": device_tag, "partitions": list(touched),
                  "backups": {}, "writes": [], "status": "preparing baselines"}
        _private_write(record_path, record)
        request_dir = Path(tempfile.mkdtemp(prefix="jibo-file-rpc-", dir="/tmp"))
        os.chmod(request_dir, 0o700)
        try:
            if self.guided:
                print("Checking the current files and their permissions…", flush=True)
            # Read the existing files and build the review plan before creating large
            # rollback images. No device writes occur during this phase.
            changes = []
            for edit in self.edits:
                before_stat = _stat_partition_file_rpc(self.dfu_util, port, edit["partition"],
                                                       edit["path"], request_dir,
                                                       quiet=self.guided)
                identity = partition_identities.get(edit["partition"])
                if identity is None:
                    extent = gpt_layout.get(edit["partition"])
                    if extent is None:
                        raise DfuError("The live GPT does not contain partition " + edit["partition"])
                    identity = {"device_tag": device_tag if _has_unique_device_tag(device_tag) else None,
                                "first_lba": extent["first_lba"],
                                "last_lba": extent["last_lba"],
                                "size_bytes": extent["size_bytes"],
                                "ext4_uuid": before_stat["ext4_uuid"]}
                    partition_identities[edit["partition"]] = identity
                elif identity["ext4_uuid"] != before_stat["ext4_uuid"]:
                    raise DfuError("The selected paths reported different ext4 UUIDs for {}."
                                   .format(edit["partition"]))
                current = _read_partition_file_rpc(self.dfu_util, port, edit["partition"],
                                                   edit["path"], request_dir,
                                                   quiet=self.guided)
                if len(current) != before_stat["size_bytes"]:
                    raise DfuError("{} changed size between stat and read; no write was attempted."
                                   .format(edit["path"]))
                if "transform" in edit:
                    edit["content"] = edit["transform"](current)
                if not isinstance(edit["content"], bytes) or not (0 < len(edit["content"]) <= FILE_LEVEL_MAX_BYTES):
                    raise DfuError("A replacement must contain 1 to {} bytes.".format(FILE_LEVEL_MAX_BYTES))
                if len(edit["content"]) > before_stat["allocated_bytes"]:
                    raise FileRpcStatusError(3, "{} needs {} bytes but has only {} bytes allocated."
                                             .format(edit["path"], len(edit["content"]),
                                                     before_stat["allocated_bytes"]))
                change = {"partition": edit["partition"], "path": edit["path"],
                          "description": edit.get("description"),
                          "before_size_bytes": len(current),
                          "candidate_size_bytes": len(edit["content"]),
                          "before_sha256": hashlib.sha256(current).hexdigest(),
                          "candidate_sha256": hashlib.sha256(edit["content"]).hexdigest(),
                          "before_metadata": before_stat}
                changes.append(change)
            if all(change["before_sha256"] == change["candidate_sha256"] for change in changes):
                record.update(status="already current", changes=changes)
                _private_write(record_path, record)
                return {"status": "already current", "operation_directory": str(directory),
                        "writes": [], "backups": {}}
            plan = {"operation": "replace existing files through DFU",
                    "usb_port": port, "device_tag": device_tag,
                    "partitions": [{"name": name, **partition_identities[name]} for name in touched],
                    "rollback_policy": "Save or reuse one var baseline if var is changed. Other partitions are backed up only on request.",
                    "changes": changes,
                    "status": "awaiting confirmation"}
            if not self.guided:
                print("\nFile write plan:")
                print(json.dumps(plan, indent=2))
                print("The loader updates only existing regular files in place. It does not create or delete files.")
            if confirmation is None:
                accepted = _confirm_file_write(None, plan)
            elif callable(confirmation):
                accepted = bool(confirmation(plan))
            else:
                accepted = _confirm_file_write(confirmation, plan)
            if not accepted:
                record.update(status="cancelled", changes=changes)
                _private_write(record_path, record)
                return {"status": "cancelled", "operation_directory": str(directory),
                        "backup_partitions": []}

            # Var holds robot-specific data. Other partitions are saved only
            # through an explicit partition-backup request.
            if self.guided and "var" in touched:
                print("Checking the rollback backup…", flush=True)
            for partition in touched:
                if partition != "var":
                    continue
                size = capacities.get(partition)
                if not size:
                    raise DfuError("The live GPT does not contain partition " + partition)
                backups[partition] = _partition_file_backup(
                    self.dfu_util, port, device_tag, partition, size, names, alt_output,
                    partition_identities[partition])
            record["backups"] = {name: {"image": item["image"], "sha256": item["sha256"],
                                        "manifest": item["manifest"], "status": item["status"]}
                                 for name, item in backups.items()}
            _private_write(record_path, record)

            record.update(status="write started", changes=changes)
            _private_write(record_path, record)
            for edit, change in zip(self.edits, changes):
                if change["before_sha256"] == change["candidate_sha256"]:
                    continue
                if self.guided:
                    print("Updating {}…".format(edit.get("description") or edit["path"]), flush=True)
                write_entry = {**change, "status": "write started"}
                record["writes"].append(write_entry)
                _private_write(record_path, record)
                before = change["before_metadata"]
                precondition = FILE_WRITE_PRECONDITION.pack(
                    before["inode"], before["size_bytes"], before["allocated_bytes"],
                    before["uid"], before["gid"], before["mode"],
                    bytes.fromhex(before["ext4_uuid"]),
                    bytes.fromhex(change["before_sha256"]))
                request, request_id = _file_request(edit["path"], FILE_RPC_WRITE,
                                                    edit["content"], precondition=precondition)
                _file_request_transfer(self.dfu_util, port, edit["partition"], request,
                                       request_dir, "Writing {} in {}".format(
                                           edit["path"], edit["partition"]), quiet=self.guided)
                ack = _upload_file_response(self.dfu_util, port, edit["partition"],
                                            request_id, request_dir,
                                            "write acknowledgement for {}".format(edit["path"]),
                                            quiet=self.guided)
                if ack:
                    raise DfuError("The file mailbox returned data with a write acknowledgement.")
                actual = _read_partition_file_rpc(self.dfu_util, port, edit["partition"],
                                                  edit["path"], request_dir,
                                                  quiet=self.guided)
                after_stat = _stat_partition_file_rpc(self.dfu_util, port, edit["partition"],
                                                      edit["path"], request_dir,
                                                      quiet=self.guided)
                actual_hash = hashlib.sha256(actual).hexdigest()
                write_entry["readback_sha256"] = actual_hash
                write_entry["after_metadata"] = after_stat
                unchanged_metadata = all(
                    after_stat[key] == change["before_metadata"][key]
                    for key in ("inode", "allocated_bytes", "uid", "gid", "mode",
                                "nlink", "mapping_count", "ext4_uuid"))
                if actual != edit["content"] or len(actual) != after_stat["size_bytes"] or not unchanged_metadata:
                    write_entry["status"] = "verification failed"
                    record["status"] = "verification failed"
                    _private_write(record_path, record)
                    backup_note = (" Rollback backup: " + backups[edit["partition"]]["image"]
                                   if edit["partition"] in backups else "")
                    raise DfuError("{} did not match its file-level readback. The robot remains in DFU; "
                                   "no additional file write was attempted.{}".format(
                                       edit["path"], backup_note))
                write_entry["status"] = "verified"
                _private_write(record_path, record)
            record["status"] = "verified"
            record["reset_after_write"] = False
            _private_write(record_path, record)
            return {"status": "verified", "operation_directory": str(directory),
                    "backups": record["backups"], "writes": record["writes"],
                    "robot_left_in_dfu": True, "reset_after_write": False}
        except Exception as exc:
            if record.get("status") not in ("cancelled", "verification failed", "verified"):
                started = any(write.get("status") in ("write started", "verified",
                                                        "verification failed")
                              for write in record.get("writes", []))
                record["status"] = "failed" if started else "failed before write"
                record["error"] = str(exc)
                _private_write(record_path, record)
            if isinstance(exc, (DfuError, images.ImageError)):
                raise
            raise DfuError(str(exc)) from exc
        finally:
            shutil.rmtree(request_dir, ignore_errors=True)


def _confirm_file_write(supplied=None, plan=None):
    if callable(supplied):
        try:
            return bool(supplied(plan))
        except Exception as exc:
            raise DfuError("The file write confirmation could not be completed: " + str(exc)) from exc
    if supplied is not None:
        return bool(supplied)
    try:
        if sys.stdin.isatty():
            response = input("Apply these file replacements? [y/N] ").strip().lower()
        else:
            with open("/dev/tty", "r+", encoding="utf-8") as terminal:
                terminal.write("Apply these file replacements? [y/N] ")
                terminal.flush()
                response = terminal.readline().strip().lower()
    except OSError as exc:
        raise DfuError("Use --yes or an interactive terminal confirmation before writing files.") from exc
    return response in ("y", "yes")


def write_partition_file_live(partition, path, content, port=None, dfu_util=None,
                              out=None, confirmation=None, guided=False):
    transaction = FileTransaction((partition,), port, dfu_util, out, guided=guided)
    transaction.replace(partition, path, content)
    return transaction.commit(confirmation)


def set_mode_file_live(mode, partition="var", port=None, dfu_util=None,
                       out=None, confirmation=None, guided=False):
    if mode not in images.MODE_VALUES:
        raise DfuError("Mode must be one of: " + ", ".join(images.MODE_VALUES))
    details = {}
    def change_mode(current):
        data = images._json_mode(current)
        details["previous_mode"] = data["mode"]
        if data["mode"] == mode:
            return current
        data["mode"] = mode
        return (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    transaction = FileTransaction((partition,), port, dfu_util, out, guided=guided)
    transaction.transform(partition, images.VAR_MODE_PATH, change_mode,
                          description="robot mode to " + mode)
    result = transaction.commit(confirmation)
    result.update(**details, mode=mode)
    return result


def configure_wifi_file_live(ssid, password=None, open_network=False, partition="var",
                             port=None, dfu_util=None, out=None, confirmation=None,
                             guided=False):
    try:
        details = {}
        startup_edits = (_wifi_startup_file_edits(port, dfu_util)
                         if partition == "var" else (False, False))
        def save_network(current):
            replacement, details["wifi_network_count"] = images.build_wifi_config(
                current, ssid, password, open_network, compact=True)
            return replacement
        transaction = FileTransaction((partition,), port, dfu_util, out, guided=guided)
        transaction.transform(partition, images.VAR_WIFI_PATH, save_network,
                              description="saved Wi-Fi network {!r}".format(ssid))
        if any(startup_edits):
            transaction.transform(partition, images.VAR_INTERFACES_PATH,
                                  lambda current: images.build_stock_wifi_startup(current)[0],
                                  description="Wi-Fi startup")
        result = transaction.commit(confirmation)
        result.update(details)
        result["ssid"] = ssid
        wrote_startup = any(
            write["path"] == images.VAR_INTERFACES_PATH for write in result.get("writes", ()))
        result["rtm2_wifi_startup_adjusted"] = wrote_startup and startup_edits[0]
        result["wifi_server_check_hook_added"] = wrote_startup and startup_edits[1]
        return result
    finally:
        password = None


def _wifi_startup_file_edits(port, dfu_util):
    """Check the current startup hooks before planning a fast Wi-Fi transaction."""
    try:
        current = read_partition_file_live(images.VAR_INTERFACES_PATH, "var", port, dfu_util)
    except FileRpcStatusError as exc:
        if exc.status == 2:
            return False, False
        raise
    _, radio_adjusted, server_hook_added = images.build_stock_wifi_startup(current)
    return radio_adjusted, server_hook_added


def set_mode_wifi_file_live(mode, ssid, password=None, open_network=False,
                            port=None, dfu_util=None, out=None,
                            confirmation=None, guided=False):
    """Change both files in one reviewed file transaction when supported."""
    if mode not in images.MODE_VALUES:
        raise DfuError("Mode must be one of: " + ", ".join(images.MODE_VALUES))
    details = {}
    def change_mode(current):
        data = images._json_mode(current)
        details["previous_mode"] = data["mode"]
        if data["mode"] == mode:
            return current
        data["mode"] = mode
        return (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    def save_network(current):
        replacement, details["wifi_network_count"] = images.build_wifi_config(
            current, ssid, password, open_network, compact=True)
        return replacement
    try:
        startup_edits = _wifi_startup_file_edits(port, dfu_util)
        transaction = FileTransaction(("var",), port, dfu_util, out, guided=guided)
        transaction.transform("var", images.VAR_MODE_PATH, change_mode,
                              description="robot mode to " + mode)
        transaction.transform("var", images.VAR_WIFI_PATH, save_network,
                              description="saved Wi-Fi network {!r}".format(ssid))
        if any(startup_edits):
            transaction.transform("var", images.VAR_INTERFACES_PATH,
                                  lambda current: images.build_stock_wifi_startup(current)[0],
                                  description="Wi-Fi startup")
        result = transaction.commit(confirmation)
        result.update(details, mode=mode, ssid=ssid)
        wrote_startup = any(
            write["path"] == images.VAR_INTERFACES_PATH for write in result.get("writes", ()))
        result["rtm2_wifi_startup_adjusted"] = wrote_startup and startup_edits[0]
        result["wifi_server_check_hook_added"] = wrote_startup and startup_edits[1]
        return result
    finally:
        password = None


def set_mode_live(mode, port=None, dfu_util=None, out=None, confirmation=None):
    if mode not in images.MODE_VALUES:
        raise DfuError("Mode must be one of: " + ", ".join(images.MODE_VALUES))
    dfu_util = dfu_util or tool("dfu-util")
    port, _, device_tag = _dfu_context(port, dfu_util)
    directory = _new_operation_dir(out, "set-mode")
    state = None
    candidate = directory / "edited-var.img"
    try:
        state = _prepare_current_and_baseline(dfu_util, port, device_tag, directory)
        before = state["before"]
        edit = images.edit_mode(before, candidate, mode)
        if edit.get("journal_replayed_on_temporary_copy"):
            print("Replayed the ext4 journal on the temporary working image before editing. The saved backup was not changed.")
        print("Mode change: " + edit["previous_mode"] + " → " + mode)
        result = _write_candidate(candidate, before, directory, port, dfu_util,
                                  confirmation, "set mode to " + mode,
                                  state["baseline"], state["before_sha256"], state["baseline_sha256"])
        result.update(current_mode=edit["previous_mode"], new_mode=mode,
                      journal_replayed_on_temporary_copy=edit.get("journal_replayed_on_temporary_copy", False))
        return result
    except (DfuError, images.ImageError) as exc:
        _raise_live_operation_error(directory, "set mode to " + mode, state, exc)


def _password_from_args(args):
    if getattr(args, "open_network", False):
        return None
    if getattr(args, "password_stdin", False):
        value = sys.stdin.readline()
        if value.endswith("\n"):
            value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
        return value
    while True:
        password = getpass.getpass("Wi-Fi password (hidden): ")
        repeated = getpass.getpass("Confirm Wi-Fi password (hidden): ")
        if password == repeated:
            return password
        print("Passwords do not match. Enter both again; no Wi-Fi change has been made.",
              file=sys.stderr)
        password = repeated = None


def configure_wifi_live(ssid, password=None, open_network=False, port=None,
                        dfu_util=None, out=None, confirmation=None):
    dfu_util = dfu_util or tool("dfu-util")
    port, _, device_tag = _dfu_context(port, dfu_util)
    directory = _new_operation_dir(out, "configure-wifi")
    state = None
    candidate = directory / "edited-var.img"
    try:
        state = _prepare_current_and_baseline(dfu_util, port, device_tag, directory)
        before = state["before"]
        edit = images.edit_wifi(before, candidate, ssid, password, open_network,
                                compact=True)
        print("Planned Wi-Fi change: save SSID " + ssid + "; a matching saved network is updated.")
        if edit["rtm2_wifi_startup_adjusted"]:
            print("The stock RTM2 startup hook was found; the later TI radio settings will be saved with Wi-Fi.")
        if edit["wifi_server_check_hook_added"]:
            print("Wi-Fi startup will bypass the retired Jibo server check and keep an original service backup.")
        operation = "save Wi-Fi network and adjust Wi-Fi startup"
        if not (edit["rtm2_wifi_startup_adjusted"] or edit["wifi_server_check_hook_added"]):
            operation = "save Wi-Fi network"
        result = _write_candidate(candidate, before, directory, port, dfu_util,
                                  confirmation, operation,
                                  state["baseline"], state["before_sha256"], state["baseline_sha256"])
        result["wifi_network_count"] = edit["network_count"]
        result["rtm2_wifi_startup_adjusted"] = edit["rtm2_wifi_startup_adjusted"]
        result["wifi_server_check_hook_added"] = edit["wifi_server_check_hook_added"]
        return result
    except (DfuError, images.ImageError) as exc:
        _raise_live_operation_error(directory, "save Wi-Fi network", state, exc)
    finally:
        password = None


def set_mode_wifi_live(mode, ssid, password=None, open_network=False,
                       port=None, dfu_util=None, out=None, confirmation=None,
                       network_status=None):
    """Apply both changes with one var read, one write, and one readback."""
    if mode not in images.MODE_VALUES:
        raise DfuError("Mode must be one of: " + ", ".join(images.MODE_VALUES))
    dfu_util = dfu_util or tool("dfu-util")
    port, _, device_tag = _dfu_context(port, dfu_util)
    directory = _new_operation_dir(out, "set-mode-wifi")
    state = None
    candidate = directory / "edited-var.img"
    try:
        state = _prepare_current_and_baseline(dfu_util, port, device_tag, directory)
        before = state["before"]
        edit_options = {} if network_status is None else {"network_status": network_status}
        edit = images.edit_mode_wifi(before, candidate, mode, ssid,
                                     password, open_network, **edit_options)
        if edit.get("journal_replayed_on_temporary_copy"):
            print("Replayed the ext4 journal on the temporary working image before editing. The saved backup was not changed.")
        print("Mode change: " + edit["previous_mode"] + " → " + mode)
        print("Planned Wi-Fi change: save SSID " + ssid + "; a matching saved network is updated.")
        if edit["rtm2_wifi_startup_adjusted"]:
            print("The stock RTM2 startup hook was found; the later TI radio settings will be saved with Wi-Fi.")
        if edit["wifi_server_check_hook_added"]:
            print("Wi-Fi startup will bypass the retired Jibo server check and keep an original service backup.")
        operation = "set mode, save Wi-Fi network, and adjust Wi-Fi startup"
        if not (edit["rtm2_wifi_startup_adjusted"] or edit["wifi_server_check_hook_added"]):
            operation = "set mode and save Wi-Fi network"
        if network_status is not None:
            operation += "; " + network_status + " boot-screen Wi-Fi/IP status (var-only)"
        result = _write_candidate(candidate, before, directory, port, dfu_util,
                                  confirmation, operation,
                                  state["baseline"], state["before_sha256"],
                                  state["baseline_sha256"])
        result.update(current_mode=edit["previous_mode"], new_mode=mode,
                      boot_network_status=network_status,
                      wifi_network_count=edit["network_count"], ssid=ssid,
                      rtm2_wifi_startup_adjusted=edit["rtm2_wifi_startup_adjusted"],
                      wifi_server_check_hook_added=edit["wifi_server_check_hook_added"],
                      journal_replayed_on_temporary_copy=edit.get(
                          "journal_replayed_on_temporary_copy", False))
        return result
    except (DfuError, images.ImageError) as exc:
        _raise_live_operation_error(directory, "set mode and save Wi-Fi network", state, exc)
    finally:
        password = None


def boot_network_status_live(action, port=None, dfu_util=None, out=None, confirmation=None):
    """Use the same backed-up, full-var hash-readback path; never writes services."""
    if action not in ("install", "remove"):
        raise DfuError("Boot network status action must be install or remove.")
    dfu_util = dfu_util or tool("dfu-util")
    port, _, device_tag = _dfu_context(port, dfu_util)
    directory = _new_operation_dir(out, "boot-network-status")
    state = None
    candidate = directory / "edited-var.img"
    operation = action + " boot-screen Wi-Fi/IP status (var-only)"
    try:
        state = _prepare_current_and_baseline(dfu_util, port, device_tag, directory)
        edit = images.edit_boot_network_status(state["before"], candidate, action)
        result = _write_candidate(candidate, state["before"], directory, port, dfu_util,
                                  confirmation, operation, state["baseline"],
                                  state["before_sha256"], state["baseline_sha256"])
        result.update(boot_network_status=action,
                      journal_replayed_on_temporary_copy=edit.get("journal_replayed_on_temporary_copy", False))
        return result
    except (DfuError, images.ImageError) as exc:
        _raise_live_operation_error(directory, operation, state, exc)


def _default_edit_path(image):
    image = Path(image).expanduser().resolve()
    return image.with_name(image.stem + "-edited" + (image.suffix or ".img"))


def _display_devices():
    found = devices()
    if not found:
        print("No Jibo recovery USB device detected.")
        print("Connect the robot by USB and put it into recovery mode, or check the USB connection.")
        return found
    for device in found:
        if device["state"] == "rcm":
            explanation = "RCM/APX recovery; no partition access yet"
        else:
            explanation = "DFU; partition access depends on the recovery profile"
        print("USB port {port}: {state} — {explanation}".format(explanation=explanation, **device))
    return found


def _ask_confirmation(prompt, phrase=WRITE_CONFIRMATION):
    print(prompt)
    return input("Type " + phrase + " to continue: ").strip() == phrase


def _add_device_arguments(parser):
    parser.add_argument("--port", help="USB bus-port path from detect, for example 1-2")


def _add_dfu_argument(parser):
    parser.add_argument("--dfu-util", help="Path to dfu-util; defaults to bundled tool or PATH")


def _add_operation_argument(parser):
    parser.add_argument("--operation-dir", help="New private directory for backup and operation files")


def _add_confirmation_argument(parser):
    parser.add_argument("--confirm", help=argparse.SUPPRESS)
    parser.add_argument("--yes", action="store_true", help="Apply the reviewed change without an interactive confirmation")


def _launch_menu():
    import jibo_tui
    result = jibo_tui.run(sys.modules[__name__])
    if result is None:
        print("The guided menu needs an interactive terminal. Use a CLI subcommand when running without one.",
              file=sys.stderr)
        return 2
    return result


def _launch_gui():
    """Open the graphical window; it falls back to the text menu if Tk is unavailable."""
    import jibo_gui_core
    return jibo_gui_core.launch(sys.modules[__name__], fallback=_launch_menu)


def flash_hosted_update(package_path, *, var_policy=flash_policy.KEEP, port=None,
                        dfu_util=None, out=None, confirmation=None,
                        dry_run=False, resume_from=None, unsafe_without_calibration=False, local_work=None,
                        verify_readback=False):
    """Authenticate the local hosted artifact before the reviewed DFU flow."""
    if resume_from is not None:
        raise DfuError("Interrupted flashes cannot currently resume. Keep the saved rollback backup.")
    if var_policy not in flash_policy.POLICIES:
        raise hosted.HostedError('incompatible', 'Unsupported calibration choice')
    path = hosted._safe_path(package_path)
    with hosted._lock(path.with_name(path.name + '.lock')):
        work = local_work or _call_with_progress
        checked = work(lambda: hosted.verify_local_image(path), "Checking the image")
        review = confirmation
        if callable(confirmation):
            def review(plan):
                details = dict(plan.get('phoenix') or {})
                details['archive_sha256'] = checked['archive_sha256']
                return confirmation({**plan, 'phoenix': details})
        return flash_update(path, var_policy, port=port, dfu_util=dfu_util, out=out,
                            confirmation=review, dry_run=dry_run,
                            unsafe_without_calibration=unsafe_without_calibration, local_work=local_work,
                            verify_readback=verify_readback)


def _hosted_cli_list(directory, offline):
    from dataclasses import asdict
    try:
        catalog = (hosted.cached_catalog(cache_dir=Path(directory) / '.hosted') if offline else
                   hosted.fetch_catalog(cache_dir=Path(directory) / '.hosted'))
    except hosted.HostedError as exc:
        if not offline or exc.code not in ('offline', 'expired'):
            raise
        catalog = hosted.Catalog((), None, '', True,
                                 exc.code + ': ' + str(exc))
    rows = []
    for image in catalog.images:
        row = asdict(image)
        row.pop('downloaded_path', None)
        rows.append({**row, 'source': 'hosted', 'recommended': image.is_latest,
                     'available': image.compatible and image.offered,
                     'reason': image.incompatible_reason, 'var_policies': hosted.hosted_var_policies(image)})
    return {'images': rows, 'latest_id': catalog.latest_id, 'from_cache': catalog.from_cache,
            'error': catalog.error}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--gui":
        argv[0] = "gui"
    if not argv:
        return _launch_menu()
    parser = argparse.ArgumentParser(description=__doc__ + " Run without arguments to open the menu.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("interactive", help="Open the guided text menu")
    sub.add_parser("gui", help="Open the graphical window (same actions as the text menu)")
    sub.add_parser("detect", help="Show matching USB devices as JSON")
    sub.add_parser("list", help="Show matching USB devices in plain language")
    shofel_entry = sub.add_parser("enter-dfu-shofel",
                                  help="Start the RAM DFU loader from RCM/APX through ShofEL")
    _add_device_arguments(shofel_entry)
    shofel_entry.add_argument("--shofel", help="Path to the launch-enabled ShofEL host and adjacent payloads")
    shofel_entry.add_argument("--loader", type=Path,
                              help="RAM DFU image (defaults to the packaged pinned loader)")
    _add_dfu_argument(shofel_entry)
    shofel_entry.add_argument("--timeout", type=float, default=120,
                              help="Seconds to wait for DFU after the loader starts")
    shofel_entry.add_argument("--confirm-meerkat-rev02", action="store_true", required=True,
                              help="Confirm the Meerkat Rev02 SDRAM profile for this robot")
    gpt_probe = sub.add_parser("probe-dfu-gpt",
                               help="Read and validate the live GPT marker through DFU without saving a backup")
    _add_device_arguments(gpt_probe)
    _add_dfu_argument(gpt_probe)
    backup = sub.add_parser("backup-var", help="Save a private var image and SHA-256 manifest")
    _add_device_arguments(backup)
    _add_dfu_argument(backup)
    backup.add_argument("--out", type=Path, help="New output directory; otherwise ~/Jibo-Backups")
    backup.add_argument("--refresh", action="store_true", help="Capture the current var state instead of reusing the saved baseline")
    available = sub.add_parser("list-partitions", help="List GPT partitions available for backup and restore")
    _add_device_arguments(available)
    _add_dfu_argument(available)
    backup_set = sub.add_parser("backup-partitions", help="Back up selected partitions or all supported partitions")
    backup_set.add_argument("--partition", action="append", default=[], help="Partition name; repeat to select several")
    backup_set.add_argument("--all", action="store_true", help="Select every supported GPT partition")
    backup_set.add_argument("--out", type=Path, help="New directory for the backup-set manifest")
    _add_device_arguments(backup_set)
    _add_dfu_argument(backup_set)
    restore_set = sub.add_parser("restore-partitions", help="Restore selected partitions from a backup set")
    restore_set.add_argument("backup_set", type=Path, help="backup-set.json or its directory")
    restore_set.add_argument("--partition", action="append", help="Partition name; default is every entry in the set")
    restore_set.add_argument("--out", type=Path, help="New directory for the restore operation record")
    restore_set.add_argument("--yes", action="store_true", help="Apply the reviewed restore without an interactive confirmation")
    _add_device_arguments(restore_set)
    _add_dfu_argument(restore_set)
    inspect = sub.add_parser("inspect-var", help="Inspect a local var image without displaying credentials")
    inspect.add_argument("image", type=Path)
    mode_edit = sub.add_parser("edit-mode", help="Create a new offline image with a changed Jibo mode")
    mode_edit.add_argument("image", type=Path)
    mode_edit.add_argument("--mode", required=True, choices=images.MODE_VALUES)
    mode_edit.add_argument("--out", type=Path)
    wifi_edit = sub.add_parser("edit-wifi", help="Add or update Wi-Fi in a local var image")
    wifi_edit.add_argument("image", type=Path)
    wifi_edit.add_argument("--ssid", required=True)
    wifi_edit.add_argument("--open-network", action="store_true")
    wifi_edit.add_argument("--password-stdin", action="store_true", help="Read the protected network password from stdin")
    wifi_edit.add_argument("--out", type=Path)
    mode_live = sub.add_parser("set-mode", help="Save or reuse a backup, change mode, and check the result")
    mode_live.add_argument("--mode", required=True, choices=images.MODE_VALUES)
    _add_device_arguments(mode_live)
    _add_dfu_argument(mode_live)
    _add_operation_argument(mode_live)
    _add_confirmation_argument(mode_live)
    wifi_live = sub.add_parser("configure-wifi", help="Save or reuse a backup, add or update Wi-Fi, and check the result")
    wifi_live.add_argument("--ssid", required=True)
    wifi_live.add_argument("--open-network", action="store_true")
    wifi_live.add_argument("--password-stdin", action="store_true", help="Read the protected network password from stdin")
    _add_device_arguments(wifi_live)
    _add_dfu_argument(wifi_live)
    _add_operation_argument(wifi_live)
    _add_confirmation_argument(wifi_live)
    combined = sub.add_parser("set-mode-wifi", help="Set mode, Wi-Fi and optional boot-screen status in one verified var write")
    combined.add_argument("--mode", required=True, choices=images.MODE_VALUES)
    combined.add_argument("--ssid", required=True)
    combined.add_argument("--open-network", action="store_true")
    combined.add_argument("--password-stdin", action="store_true")
    combined.add_argument("--boot-network-status", choices=("install", "keep", "remove"),
                          help="Default: install for developer modes, keep for other modes")
    _add_device_arguments(combined)
    _add_dfu_argument(combined)
    _add_operation_argument(combined)
    _add_confirmation_argument(combined)
    boot_status = sub.add_parser("boot-network-status", help="Install/remove the var-only boot-screen diagnostic")
    boot_status.add_argument("action", choices=("install", "remove"))
    _add_device_arguments(boot_status)
    _add_dfu_argument(boot_status)
    _add_operation_argument(boot_status)
    _add_confirmation_argument(boot_status)
    status_edit = sub.add_parser("edit-boot-network-status", help="Install/remove the diagnostic on a local var copy")
    status_edit.add_argument("image", type=Path)
    status_edit.add_argument("action", choices=("install", "remove"))
    status_edit.add_argument("--out", type=Path)
    read_file = sub.add_parser("read-partition-file",
                               help="Read one bounded file through DFU")
    read_file.add_argument("path", help="Absolute path inside the selected ext4 partition")
    read_file.add_argument("--partition", required=True, choices=FILE_LEVEL_PARTITIONS)
    read_file.add_argument("--out", type=Path, required=True, help="New private output file")
    _add_device_arguments(read_file)
    _add_dfu_argument(read_file)
    stat_file = sub.add_parser("stat-partition-file",
                               help="Inspect an existing file's owner, permissions, and inode through DFU")
    stat_file.add_argument("path", help="Absolute path inside the selected ext4 partition")
    stat_file.add_argument("--partition", required=True, choices=FILE_LEVEL_PARTITIONS)
    _add_device_arguments(stat_file)
    _add_dfu_argument(stat_file)
    write_file = sub.add_parser("write-partition-file",
                                help="Replace one existing file through DFU")
    write_file.add_argument("path", help="Absolute path inside the selected ext4 partition")
    write_file.add_argument("image", type=Path, help="Local replacement file")
    write_file.add_argument("--partition", required=True, choices=FILE_LEVEL_PARTITIONS)
    _add_device_arguments(write_file)
    _add_dfu_argument(write_file)
    _add_operation_argument(write_file)
    write_file.add_argument("--yes", action="store_true", help="Confirm the reviewed file replacement plan")
    mode_file = sub.add_parser("set-mode-file",
                               help="Set mode by changing its existing file")
    mode_file.add_argument("--mode", required=True, choices=images.MODE_VALUES)
    mode_file.add_argument("--partition", default="var", choices=FILE_LEVEL_PARTITIONS)
    _add_device_arguments(mode_file)
    _add_dfu_argument(mode_file)
    _add_operation_argument(mode_file)
    mode_file.add_argument("--yes", action="store_true", help="Confirm the reviewed file replacement plan")
    wifi_file = sub.add_parser("configure-wifi-file",
                               help="Add or update Wi-Fi in its existing file")
    wifi_file.add_argument("--ssid", required=True)
    wifi_file.add_argument("--partition", default="var", choices=FILE_LEVEL_PARTITIONS)
    wifi_file.add_argument("--open-network", action="store_true")
    wifi_file.add_argument("--password-stdin", action="store_true",
                           help="Read the protected network password from stdin")
    _add_device_arguments(wifi_file)
    _add_dfu_argument(wifi_file)
    _add_operation_argument(wifi_file)
    wifi_file.add_argument("--yes", action="store_true", help="Confirm the reviewed file replacement plan")
    write = sub.add_parser("write-var", help="Back up var, write an edited image, and verify its readback")
    write.add_argument("image", type=Path)
    _add_device_arguments(write)
    _add_dfu_argument(write)
    _add_operation_argument(write)
    _add_confirmation_argument(write)
    list_updates = sub.add_parser("list-updates", help="List official full-flash packages in an updates folder")
    list_updates.add_argument("--directory", type=Path, default=Path.cwd() / "updates")
    for command, help_text in (('list-hosted-updates', 'List signed hosted Phoenix offers (no USB)'),
                               ('flash-hosted-update', 'Destructive reviewed Phoenix flash with robot-specific var')):
        hosted_parser = sub.add_parser(command, help=help_text)
        hosted_parser.add_argument('--directory', type=Path, default=hosted.default_updates_dir())
        if command == 'list-hosted-updates':
            hosted_parser.add_argument('--offline', action='store_true', help='Use the last authenticated catalog without network requests')
        if command != 'list-hosted-updates':
            hosted_parser.add_argument('--image', required=True, help='Exact immutable image id or latest (latest requires an unexpired catalog)')
        if command == 'flash-hosted-update':
            var_group = hosted_parser.add_mutually_exclusive_group()
            var_group.add_argument('--keep-calibration', action='store_true', help=flash_policy.KEEP_LABEL + ' (default)')
            var_group.add_argument('--without-calibration', action='store_true', help=flash_policy.WITHOUT_LABEL + '; requires --confirm FLASH UPDATE')
            hosted_parser.add_argument('--confirm', help='Exactly FLASH UPDATE; otherwise a cancel-default review is required')
            hosted_parser.add_argument('--verify-readback', action='store_true', help='Read back each written image and compare its checksum')
            hosted_parser.add_argument('--resume-from', type=Path)
            _add_device_arguments(hosted_parser)
            _add_dfu_argument(hosted_parser)
            _add_operation_argument(hosted_parser)
    flash = sub.add_parser("flash-update", help="Back up and install an official full-flash package")
    flash.add_argument("package", type=Path, help="Extracted package directory or official .tar.bz2 archive")
    policy = flash.add_mutually_exclusive_group()
    policy.add_argument("--keep-calibration", action="store_true", help=flash_policy.KEEP_LABEL + " (default)")
    policy.add_argument("--without-calibration", action="store_true", help=flash_policy.WITHOUT_LABEL + "; requires --confirm FLASH UPDATE")
    _add_device_arguments(flash)
    _add_dfu_argument(flash)
    _add_operation_argument(flash)
    flash.add_argument("--dry-run", action="store_true", help="Verify package and live partition sizes without writing")
    flash_confirmation = flash.add_mutually_exclusive_group()
    flash_confirmation.add_argument("--confirm", help="Exactly FLASH UPDATE, for scripted writes")
    flash_confirmation.add_argument("--yes", action="store_true",
                                    help="Confirm the reviewed update plan for scripted writes")
    flash.add_argument("--resume-from", type=Path,
                       help="Interrupted flashes cannot currently resume; see the saved rollback record")
    flash.add_argument("--verify-readback", action="store_true",
                       help="Read back each transferred package-image prefix and compare its SHA-256")
    verify = sub.add_parser("verify-update-write",
                            help="Read back one update partition and compare it with a saved operation record")
    verify.add_argument("manifest", type=Path, help="Saved update-manifest.json from a flash attempt")
    verify.add_argument("--partition", required=True,
                        choices=("rootfsA", "rootfsB", "services", "skills", "var"))
    _add_device_arguments(verify)
    _add_dfu_argument(verify)
    args = parser.parse_args(argv)
    try:
        if args.command == "interactive":
            return _launch_menu()
        if args.command == "gui":
            return _launch_gui()
        if args.command == "detect":
            result = devices()
        elif args.command == "list":
            _display_devices()
            return 0
        elif args.command == "enter-dfu-shofel":
            result = enter_shofel_dfu(args.port, args.shofel, args.loader,
                                      args.dfu_util, args.timeout,
                                      args.confirm_meerkat_rev02)
        elif args.command == "probe-dfu-gpt":
            result = probe_dfu_gpt(args.port, args.dfu_util)
        elif args.command == "backup-var":
            result = backup_var(args.port, tool("dfu-util", args.dfu_util), args.out, args.refresh)
        elif args.command == "list-partitions":
            port, _, available = available_backup_partitions(args.port, tool("dfu-util", args.dfu_util))
            result = {"port": port, "partitions": available}
        elif args.command == "backup-partitions":
            dfu_util = tool("dfu-util", args.dfu_util)
            if args.all:
                if args.partition:
                    raise DfuError("Choose --all or --partition, not both.")
                _, _, available = available_backup_partitions(args.port, dfu_util)
                selected = tuple(available)
            else:
                selected = args.partition
            result = backup_partitions(selected, args.port, dfu_util, args.out)
        elif args.command == "restore-partitions":
            result = restore_partitions(args.backup_set, args.partition, args.port,
                                        tool("dfu-util", args.dfu_util), args.out,
                                        True if args.yes else None)
        elif args.command == "inspect-var":
            result = images.inspect_var(args.image)
        elif args.command == "edit-mode":
            output = args.out or _default_edit_path(args.image)
            result = images.edit_mode(args.image, output, args.mode)
        elif args.command == "edit-wifi":
            password = _password_from_args(args)
            result = images.edit_wifi(args.image, args.out or _default_edit_path(args.image),
                                      args.ssid, password, args.open_network)
            password = None
        elif args.command == "set-mode":
            dfu_util = tool("dfu-util", args.dfu_util)
            confirmed = "WRITE VAR" if args.yes else args.confirm
            if _live_var_file_available(args.port, dfu_util):
                confirmed = None if confirmed is None else confirmed == "WRITE VAR"
                result = set_mode_file_live(args.mode, port=args.port, dfu_util=dfu_util,
                                            out=args.operation_dir, confirmation=confirmed)
            else:
                result = set_mode_live(args.mode, args.port, dfu_util,
                                       args.operation_dir, confirmed)
        elif args.command == "set-mode-wifi":
            password = _password_from_args(args)
            action = args.boot_network_status
            if action is None:
                action = "install" if args.mode in ("developer", "int-developer") else "keep"
            result = set_mode_wifi_live(
                args.mode, args.ssid, password, args.open_network,
                port=args.port, dfu_util=tool("dfu-util", args.dfu_util),
                out=args.operation_dir, confirmation="WRITE VAR" if args.yes else args.confirm,
                network_status=None if action == "keep" else action)
            password = None
        elif args.command == "boot-network-status":
            result = boot_network_status_live(
                args.action, port=args.port, dfu_util=tool("dfu-util", args.dfu_util),
                out=args.operation_dir, confirmation="WRITE VAR" if args.yes else args.confirm)
        elif args.command == "edit-boot-network-status":
            result = images.edit_boot_network_status(
                args.image, args.out or _default_edit_path(args.image), args.action)
        elif args.command == "configure-wifi":
            password = _password_from_args(args)
            dfu_util = tool("dfu-util", args.dfu_util)
            confirmed = "WRITE VAR" if args.yes else args.confirm
            if _live_var_file_available(args.port, dfu_util):
                confirmed = None if confirmed is None else confirmed == "WRITE VAR"
                result = configure_wifi_file_live(args.ssid, password, args.open_network,
                                                  port=args.port, dfu_util=dfu_util,
                                                  out=args.operation_dir, confirmation=confirmed)
            else:
                result = configure_wifi_live(args.ssid, password, args.open_network, args.port,
                                             dfu_util, args.operation_dir, confirmed)
            password = None
        elif args.command == "read-partition-file":
            payload = read_partition_file_live(args.path, args.partition, args.port,
                                               tool("dfu-util", args.dfu_util))
            output = args.out.expanduser().resolve()
            if output.exists() or output.is_symlink():
                raise DfuError("Output already exists; choose a new path: " + str(output))
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            output.write_bytes(payload)
            output.chmod(0o600)
            _chown_to_invoking_user(output)
            result = {"status": "read", "partition": args.partition, "path": args.path,
                      "output": str(output), "size_bytes": len(payload),
                      "sha256": hashlib.sha256(payload).hexdigest()}
        elif args.command == "stat-partition-file":
            result = stat_partition_file_live(args.path, args.partition, args.port,
                                              tool("dfu-util", args.dfu_util))
        elif args.command == "write-partition-file":
            source_arg = args.image.expanduser()
            if source_arg.is_symlink():
                raise DfuError("Replacement file is a symbolic link: " + str(source_arg))
            source = source_arg.resolve()
            if not source.is_file():
                raise DfuError("Replacement file does not exist or is a symbolic link: " + str(source))
            if not (0 < source.stat().st_size <= FILE_LEVEL_MAX_BYTES):
                raise DfuError("Replacement files must contain 1 to {} bytes.".format(
                    FILE_LEVEL_MAX_BYTES))
            result = write_partition_file_live(
                args.partition, args.path, source.read_bytes(), args.port,
                tool("dfu-util", args.dfu_util), args.operation_dir,
                True if args.yes else None)
        elif args.command == "set-mode-file":
            result = set_mode_file_live(args.mode, args.partition, args.port,
                                        tool("dfu-util", args.dfu_util),
                                        args.operation_dir, True if args.yes else None)
        elif args.command == "configure-wifi-file":
            password = _password_from_args(args)
            result = configure_wifi_file_live(
                args.ssid, password, args.open_network, args.partition, args.port,
                tool("dfu-util", args.dfu_util), args.operation_dir,
                True if args.yes else None)
            password = None
        elif args.command == 'list-hosted-updates':
            result = _hosted_cli_list(args.directory, args.offline)
        elif args.command == 'flash-hosted-update':
            if args.without_calibration and args.confirm != UPDATE_CONFIRMATION:
                raise DfuError('Without calibration requires --without-calibration and --confirm exactly FLASH UPDATE.')
            if args.resume_from is not None:
                raise DfuError("Interrupted flashes cannot currently resume. Keep the saved rollback backup.")
            from jibo_tui import download_for_flash
            with hosted.flash_download_session(args.image, dest_dir=args.directory) as session:
                try:
                    path = download_for_flash(hosted, args.image, dest_dir=session.directory)
                except hosted.HostedError as exc:
                    session.interrupted = exc.code in ('canceled', 'offline', 'http', 'rate_limited', 'download-stopped')
                    raise
                session.complete = True
                result = flash_hosted_update(path, port=args.port, dfu_util=args.dfu_util,
                    out=args.operation_dir, confirmation=args.confirm,
                    var_policy=flash_policy.WITHOUT if args.without_calibration else flash_policy.KEEP,
                    unsafe_without_calibration=args.without_calibration, verify_readback=args.verify_readback)
        elif args.command == "list-updates":
            result = [{"path": str(path), "name": path.name}
                      for path in _update_candidates(args.directory)]
        elif args.command == "flash-update":
            if args.without_calibration and not args.dry_run and args.confirm != UPDATE_CONFIRMATION:
                raise DfuError('Without calibration requires --without-calibration and --confirm exactly FLASH UPDATE; --yes is not enough.')
            result = flash_update(args.package,
                                  flash_policy.WITHOUT if args.without_calibration else flash_policy.KEEP,
                                  args.port, tool("dfu-util", args.dfu_util), args.operation_dir,
                                  UPDATE_CONFIRMATION if args.yes else args.confirm,
                                  args.dry_run, args.resume_from, args.verify_readback,
                                  unsafe_without_calibration=args.without_calibration)
        elif args.command == "verify-update-write":
            result = verify_update_write(args.manifest, args.partition, args.port,
                                         tool("dfu-util", args.dfu_util))
        else:
            result = write_var(args.image, args.port, tool("dfu-util", args.dfu_util),
                               args.operation_dir, "WRITE VAR" if args.yes else args.confirm)
        print(json.dumps(result, indent=2, default=str))
        return 1 if isinstance(result, dict) and result.get("status") == "mismatch" else 0
    except hosted.HostedError as exc:
        print('Hosted error [' + exc.code + ']: ' + str(exc), file=sys.stderr)
        return 130 if exc.code == 'canceled' else 3 if exc.code == 'dependency' else 1
    except (DfuError, images.ImageError, updates.UpdateError, OSError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1


DFU_SETTLE_SECONDS = 25.0
_DFU_LISTING_DEADLINE = ContextVar("dfu_listing_deadline", default=None)


def _settled_dfu_alternatives(dfu_util, port, deadline=None):
    """Wait for USB access and the Jibo loader after device re-enumeration."""
    stop = time.monotonic() + DFU_SETTLE_SECONDS
    if deadline is not None:
        stop = min(stop, deadline)
    names, output, last_error = [], "", None
    seen = []
    while time.monotonic() < stop:
        try:
            token = _DFU_LISTING_DEADLINE.set(stop)
            try:
                names, output = dfu_alternatives(dfu_util, port)
            finally:
                _DFU_LISTING_DEADLINE.reset(token)
            seen = list(dict.fromkeys(seen + names))
            last_error = None
            if MARKER in names and "var" in names:
                return names, output
        except DfuError as exc:
            names = []
            last_error = str(exc)
        time.sleep(min(0.25, max(0, stop - time.monotonic())))
    if not names:
        detail = last_error or _transfer_error_detail(output)
        raise DfuError("DFU appeared on USB port " + port +
                       ", but USB access is not ready. Check USB forwarding and permissions." +
                       (" Last listing: " + detail if detail else ""))
    missing = [name for name in (MARKER, "var") if name not in names]
    raise DfuError("DFU on USB port " + port + " is not Jibo's recovery loader "
                   "or its recovery profile is incomplete; missing: " + ", ".join(missing) +
                   ". Alternatives seen: " + ", ".join(seen) + ". No partition operation was attempted.")


if __name__ == "__main__":
    raise SystemExit(main())
