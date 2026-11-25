#!/usr/bin/env python3
"""Offline, secret-conscious editing helpers for Jibo var images."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile


MODE_VALUES = ("normal", "int-developer", "developer", "oobe")
VAR_MODE_PATH = "/jibo/mode.json"
VAR_WIFI_PATH = "/etc/wpa_supplicant.conf"
VAR_INTERFACES_PATH = "/etc/network/interfaces"
FACTORY_CAMERA_PATHS = (
    "/jibo/lps/CameraModelParamsL.json",
    "/jibo/lps/CameraModelParamsR.json",
    "/jibo/lps/InterCameraTransform.json",
)
FACTORY_IDENTITY_PATH = "/jibo/identity.json"


class ImageError(Exception):
    pass


def _chown_to_invoking_user(path):
    if getattr(os, "geteuid", lambda: 1)() == 0 and os.environ.get("SUDO_UID"):
        try:
            uid = int(os.environ["SUDO_UID"])
            gid = int(os.environ.get("SUDO_GID", "-1"))
            os.chown(path, uid, gid)
        except (ValueError, OSError):
            pass


def _host_path(path):
    """A local path as debugfs sees it. Windows packagers (BEep) bundle a Cygwin debugfs:
    give it the short (space-free) name with forward slashes."""
    text = str(path)
    if sys.platform != "win32":
        return text
    import ctypes
    target = Path(text)
    probe = target if target.exists() else target.parent
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.kernel32.GetShortPathNameW(str(probe), buffer, len(buffer)):
        text = buffer.value if probe == target else str(Path(buffer.value) / target.name)
    return text.replace("\\", "/")


def _debugfs_arg(path):
    """A local path inside a debugfs command, quoted so folders with spaces work."""
    return '"' + _host_path(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_debugfs(image, command, writable=False, allow_missing=False):
    executable = shutil.which("debugfs")
    if not executable:
        raise ImageError("debugfs is required for offline ext4 image editing (install e2fsprogs).")
    argv = [executable]
    if writable:
        argv.append("-w")
    argv.extend(("-R", command, _host_path(image)))
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImageError("Could not run debugfs: " + str(exc)) from exc
    output = result.stdout + result.stderr
    missing = re.search(r"File not found|ext2_lookup", output, re.IGNORECASE)
    if allow_missing and missing:
        return output
    if result.returncode or re.search(
            r"(?:File not found|Command not found|Could not|Permission denied|Invalid argument)",
            output, re.IGNORECASE):
        raise ImageError("Could not safely edit the ext4 image using debugfs.")
    return output


def _check_ext4(image):
    executable = shutil.which("e2fsck")
    if not executable:
        raise ImageError("e2fsck is required to check edited images (install e2fsprogs).")
    try:
        result = subprocess.run([executable, "-f", "-n", _host_path(image)], capture_output=True,
                                text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImageError("Could not check the ext4 image: " + str(exc)) from exc
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        if len(detail) > 1600:
            detail = "…\n" + detail[-1600:]
        message = ("The ext4 image did not pass its read-only filesystem check "
                   "(e2fsck exit code {}). It was not accepted.".format(result.returncode))
        if detail:
            message += "\ne2fsck report:\n" + detail
        raise ImageError(message)


def _replay_journal_on_copy(image):
    """Let e2fsck perform only its automatic fixes on a disposable image."""
    executable = shutil.which("e2fsck")
    if not executable:
        raise ImageError("e2fsck is required to prepare ext4 images (install e2fsprogs).")
    try:
        result = subprocess.run([executable, "-f", "-p", _host_path(image)], capture_output=True,
                                text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImageError("Could not check the working ext4 copy: " + str(exc)) from exc
    if result.returncode not in (0, 1):
        detail = (result.stdout + result.stderr).strip()[-1600:]
        raise ImageError("The working ext4 copy needs repairs beyond automatic journal replay. "
                         "The original image was not changed.\n" + detail)
    _check_ext4(image)
    return result.returncode == 1


def _extract(image, image_path, destination, optional=False):
    destination = Path(destination)
    if destination.exists():
        destination.unlink()
    output = _run_debugfs(image, "dump " + image_path + " " + _debugfs_arg(destination), allow_missing=optional)
    if not destination.is_file():
        if optional and re.search(r"File not found|ext2_lookup", output, re.IGNORECASE):
            return None
        raise ImageError("Expected file was not found in the var image: " + image_path)
    return destination.read_bytes()


def _json_mode(raw):
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageError("/jibo/mode.json is not valid UTF-8 JSON.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("mode"), str):
        raise ImageError("/jibo/mode.json must be a JSON object with a string 'mode' field.")
    return data


def _file_metadata(image, image_path):
    output = _run_debugfs(image, "stat " + image_path)
    mode = re.search(r"\bMode:\s+(?:0)?([0-7]{3,4})\b", output)
    file_type = re.search(r"\bType:\s+([A-Za-z]+)\b", output)
    uid = re.search(r"\bUser:\s*(\d+)\b", output)
    gid = re.search(r"\bGroup:\s*(\d+)\b", output)
    if not (mode and file_type and uid and gid):
        raise ImageError("Could not read original file permissions from the var image.")
    if file_type.group(1).lower() != "regular":
        raise ImageError("Only regular files can be replaced in a var image.")
    # debugfs's Mode display contains permission bits only. set_inode_field needs
    # the ext4 regular-file type bits as well, or it creates an invalid inode.
    full_mode = 0o100000 | int(mode.group(1), 8)
    return {"mode": format(full_mode, "07o"),
            "uid": uid.group(1), "gid": gid.group(1)}


def _replace_file(image, image_path, content, workdir):
    metadata = _file_metadata(image, image_path)
    replacement = Path(workdir) / "replacement"
    replacement.write_bytes(content)
    replacement.chmod(0o600)
    _run_debugfs(image, "rm " + image_path, writable=True)
    _run_debugfs(image, "write " + _debugfs_arg(replacement) + " " + image_path, writable=True)
    for field in ("mode", "uid", "gid"):
        _run_debugfs(image, "set_inode_field " + image_path + " " + field + " " + metadata[field],
                     writable=True)
    verified = _extract(image, image_path, Path(workdir) / "verified", optional=False)
    if verified != content:
        raise ImageError("The edited file did not match its offline readback.")


def _put_factory_file(image, image_path, content, workdir, mode, source_metadata=None):
    """Replace a stock file or add a missing per-robot file on a disposable image."""
    existing = _extract(image, image_path, Path(workdir) / "existing", optional=True)
    if existing is not None:
        metadata = _file_metadata(image, image_path)
        _run_debugfs(image, "rm " + image_path, writable=True)
    else:
        metadata = {"mode": format(0o100000 | mode, "07o"), "uid": "0", "gid": "0"}
    if source_metadata is not None:
        metadata = source_metadata
    replacement = Path(workdir) / "replacement"
    replacement.write_bytes(content)
    replacement.chmod(0o600)
    _run_debugfs(image, "write " + _debugfs_arg(replacement) + " " + image_path, writable=True)
    for field in ("mode", "uid", "gid"):
        _run_debugfs(image, "set_inode_field " + image_path + " " + field + " " + metadata[field],
                     writable=True)
    if _extract(image, image_path, Path(workdir) / "verified") != content:
        raise ImageError("The factory file did not match its offline readback: " + image_path)


def _factory_inputs(backup, stock):
    """Read identity and camera files from one robot's verified var."""
    with tempfile.TemporaryDirectory(prefix="jibo-factory-input-") as temporary:
        raw = {}
        parsed = {}
        for index, path in enumerate((FACTORY_IDENTITY_PATH, *FACTORY_CAMERA_PATHS)):
            content = _extract(backup, path, Path(temporary) / str(index))
            if not content or len(content) > 65536:
                raise ImageError("Missing or oversized factory file in the var backup: " + path)
            try:
                value = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ImageError("The var backup has invalid factory JSON at " + path) from exc
            if not isinstance(value, dict):
                raise ImageError("The var backup has invalid factory data at " + path)
            raw[path] = content
            parsed[path] = value
        identity = parsed[FACTORY_IDENTITY_PATH]
        name = identity.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+){2,3}", name) or \
                not all(isinstance(identity.get(key), str) and identity[key]
                        for key in ("serial_number", "cpuid", "wifi_mac")):
            raise ImageError("The var backup does not contain a complete robot identity.")
        defaults = []
        for index, path in enumerate(FACTORY_CAMERA_PATHS):
            content = _extract(stock, path, Path(temporary) / ("default-" + str(index)))
            try:
                value = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ImageError("The package has invalid default calibration at " + path) from exc
            if not isinstance(value, dict) or not value or not set(value).issubset(parsed[path]):
                raise ImageError("The saved camera calibration does not match the package schema.")
            defaults.append(value)
        camera_matches_stock = all(parsed[path] == default
                                   for path, default in zip(FACTORY_CAMERA_PATHS, defaults))
        return raw, name, camera_matches_stock


def factory_calibration_available(backup, stock):
    """Whether this var backup can supply a factory-like fresh stock image."""
    try:
        _factory_inputs(backup, stock)
    except ImageError:
        return False
    return True


def _calibration_item(backup, key, workdir):
    # Lazy import avoids a cycle: the pure extractor uses our debugfs helpers.
    import jibo_calibration as calibration
    spec = next(item for item in calibration.ITEMS if item.key == key)
    try:
        raw, status = calibration._read_item(backup, spec, Path(workdir) / key)
    except calibration.CalibrationError as exc:
        raise ImageError("Could not inspect saved calibration: " + key) from exc
    metadata = _file_metadata(backup, spec.image_path) if raw is not None else None
    return spec, raw, status, metadata


def _put_calibration_item(candidate, spec, raw, metadata, workdir):
    import jibo_calibration as calibration
    # No symlink traversal, including parents in the incoming package image.
    parents = list(reversed(PurePosixPath(spec.image_path).parents))
    for parent in parents:
        stat = calibration._image_stat(candidate, str(parent))
        if stat is None:
            _run_debugfs(candidate, "mkdir " + str(parent), writable=True)
            for field, value in (("mode", "040755"), ("uid", "0"), ("gid", "0")):
                _run_debugfs(candidate, "set_inode_field {} {} {}".format(parent, field, value), writable=True)
        elif stat[0] != "directory":
            raise ImageError("Calibration destination parent is not a directory.")
    stat = calibration._image_stat(candidate, spec.image_path)
    if stat is not None and stat[0] != "regular":
        raise ImageError("Calibration destination is not a regular file.")
    _put_factory_file(candidate, spec.image_path, raw, workdir, 0o644, source_metadata=metadata)
    if _file_metadata(candidate, spec.image_path) != metadata:
        raise ImageError("Restored calibration permissions did not match the backup.")


def _distortion_caches(image):
    output = _run_debugfs(image, "ls -p /jibo/lps")
    paths = []
    for line in output.splitlines():
        fields = line.split("/")
        if len(fields) >= 7 and fields[5].startswith("distortionPrecomputeFile_"):
            paths.append("/jibo/lps/" + fields[5])
    return paths


def _remove_distortion_caches(candidate):
    import jibo_calibration as calibration
    paths = _distortion_caches(candidate)
    for path in paths:
        if any(char in path for char in "\n\r\x00"):
            raise ImageError("Unsupported distortion cache filename.")
        stat = calibration._image_stat(candidate, path)
        if stat is None or stat[0] not in ("regular", "symlink"):
            raise ImageError("A distortion cache is not a regular file or symlink.")
        quoted = '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
        _run_debugfs(candidate, "rm " + quoted, writable=True)
    if _distortion_caches(candidate):
        raise ImageError("Distortion caches survived removal from the candidate.")
    return paths


def build_factory_fresh_var(stock, backup, destination):
    """Make stock var fresh for OOBE while retaining this robot's factory files."""
    stock, backup, destination = Path(stock), Path(backup), Path(destination)
    if stock.is_symlink() or backup.is_symlink() or destination.exists() or destination.is_symlink():
        raise ImageError("Factory var inputs must be regular files and the output must be new.")
    raw, name, camera_matches_stock = _factory_inputs(backup, stock)
    restored = list(FACTORY_CAMERA_PATHS)
    status = {"left": "restored", "right": "restored", "inter_camera": "restored",
              "touchscreen": "not preserved (no backup of that partition)",
              "factory_record": "not preserved (no backup of that partition)"}
    try:
        shutil.copyfile(stock, destination)
        destination.chmod(0o600)
        with tempfile.TemporaryDirectory(prefix="jibo-factory-edit-") as temporary:
            for path in FACTORY_CAMERA_PATHS:
                _put_factory_file(destination, path, raw[path], temporary, 0o644)
            for key in ("imu", "audio"):
                spec, content, item_status, metadata = _calibration_item(backup, key, temporary)
                status[key] = item_status
                if content is not None:
                    _put_calibration_item(destination, spec, content, metadata, temporary)
                    status[key] = "restored"
                    restored.append(spec.image_path)
            removed_caches = _remove_distortion_caches(destination)
            _put_factory_file(destination, FACTORY_IDENTITY_PATH,
                              raw[FACTORY_IDENTITY_PATH], temporary, 0o600)
            _put_factory_file(destination, "/jibo/mode.json", b'{"mode":"oobe"}',
                              temporary, 0o600)
            _put_factory_file(destination, "/etc/hostname", (name + "\n").encode("ascii"),
                              temporary, 0o644)
            hosts = ("127.0.0.1\tlocalhost\n127.0.1.1\t" + name + "\n").encode("ascii")
            _put_factory_file(destination, "/etc/hosts", hosts, temporary, 0o644)
        _check_ext4(destination)
        if destination.stat().st_size != stock.stat().st_size:
            raise ImageError("The calibrated var image changed length.")
        return {"image": str(destination), "calibration_files": restored,
                "calibration_status": status, "removed_distortion_caches": removed_caches,
                "identity_restored": True, "mode": "oobe",
                "camera_matches_stock": camera_matches_stock,
                "sha256": sha256_file(destination)}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def build_calibrated_partition(stock, backup, destination, partition):
    """Preserve a conditional item in a NEW rootfs/skills copy, when backed up.

    The caller must bind the backup to the connected robot before calling. No
    file is invented when the partition backup or supported item is absent.
    """
    keys = {"rootfs": "touchscreen", "skills": "factory_record"}
    if partition not in keys:
        raise ImageError("Conditional calibration is supported only for rootfs and skills.")
    stock, destination = Path(stock), Path(destination)
    if stock.is_symlink() or not stock.is_file() or destination.exists() or destination.is_symlink():
        raise ImageError("Calibration inputs must be regular files and the output must be new.")
    key = keys[partition]
    status = "not preserved (no backup of that partition)"
    raw = None
    if backup is not None:
        backup = Path(backup)
        if backup.is_symlink() or not backup.is_file():
            raise ImageError("The calibration backup must be a regular partition image.")
    try:
        with tempfile.TemporaryDirectory(prefix="jibo-partition-calibration-") as temporary:
            if backup is not None:
                spec, raw, status, metadata = _calibration_item(backup, key, temporary)
            if raw is not None:
                shutil.copyfile(stock, destination)
                destination.chmod(0o600)
                _put_calibration_item(destination, spec, raw, metadata, temporary)
                _check_ext4(destination)
                if destination.stat().st_size != stock.stat().st_size:
                    raise ImageError("The calibrated partition image changed length.")
                status = "restored"
        selected = destination if raw is not None else stock
        return {"image": str(selected), "sha256": sha256_file(selected),
                "calibration_files": [spec.image_path] if raw is not None else [],
                "calibration_status": {key: status}}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _copy_for_edit(source, destination):
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if not source.is_file():
        raise ImageError("Var image does not exist: " + str(source))
    if source == destination:
        raise ImageError("Choose a new output path; the source backup is kept unchanged.")
    if destination.exists():
        raise ImageError("Output already exists; choose a new filename: " + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise ImageError("Could not create a working copy of the var image: " + str(exc)) from exc
    destination.chmod(0o600)
    _chown_to_invoking_user(destination)
    try:
        repaired = _replay_journal_on_copy(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return source, destination, repaired


def inspect_var(image):
    image = Path(image).expanduser().resolve()
    if not image.is_file():
        raise ImageError("Var image does not exist: " + str(image))
    with tempfile.TemporaryDirectory(prefix="jibo-inspect-") as temp:
        working = Path(temp) / "var.img"
        shutil.copyfile(image, working)
        repaired = _replay_journal_on_copy(working)
        mode_raw = _extract(working, VAR_MODE_PATH, Path(temp) / "mode.json")
        mode = _json_mode(mode_raw)["mode"]
        wifi_raw = _extract(working, VAR_WIFI_PATH, Path(temp) / "wifi.conf", optional=True)
    try:
        networks = _network_blocks(wifi_raw.decode("utf-8")) if wifi_raw is not None else []
    except UnicodeDecodeError as exc:
        raise ImageError("The Wi-Fi config is not valid UTF-8; no content was displayed.") from exc
    return {"mode": mode, "wifi_configured": wifi_raw is not None,
            "wifi_network_count": len(networks), "journal_replayed_on_temporary_copy": repaired}


def edit_mode(source, destination, mode):
    if mode not in MODE_VALUES:
        raise ImageError("Mode must be one of: " + ", ".join(MODE_VALUES))
    source, destination, repaired = _copy_for_edit(source, destination)
    try:
        with tempfile.TemporaryDirectory(prefix="jibo-mode-") as temp:
            current_raw = _extract(destination, VAR_MODE_PATH, Path(temp) / "mode.json")
            data = _json_mode(current_raw)
            previous = data["mode"]
            data["mode"] = mode
            replacement = (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
            _replace_file(destination, VAR_MODE_PATH, replacement, temp)
            _replay_journal_on_copy(destination)
            final = _json_mode(_extract(destination, VAR_MODE_PATH, Path(temp) / "final.json"))
            if final["mode"] != mode:
                raise ImageError("The edited mode did not survive image readback.")
        return {"image": str(destination), "previous_mode": previous,
                "mode": mode, "sha256": sha256_file(destination),
                "journal_replayed_on_temporary_copy": repaired}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _network_block_spans(text):
    """Return balanced network={...} positions without interpreting secrets."""
    spans = []
    depth = 0
    start = None
    quoted = False
    escaped = False
    comment = False
    for index, char in enumerate(text):
        if comment:
            if char == "\n":
                comment = False
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "#":
            comment = True
        elif char == "{":
            if depth == 0:
                before = text[:index]
                match = re.search(r"\bnetwork\s*=\s*$", before)
                if match:
                    start = match.start()
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise ImageError("Wi-Fi configuration has unmatched braces.")
            if depth == 0 and start is not None:
                spans.append((start, index + 1))
                start = None
    if quoted or depth != 0:
        raise ImageError("Wi-Fi configuration has an unfinished string or network block.")
    return spans


def _network_blocks(text):
    return [text[start:end] for start, end in _network_block_spans(text)]


def _decode_ssid(value):
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
        result = bytearray()
        index = 0
        while index < len(value):
            char = value[index]
            if char != "\\":
                result.extend(char.encode("utf-8"))
                index += 1
                continue
            index += 1
            if index >= len(value):
                return None
            escaped = value[index]
            if escaped == "x" and index + 2 < len(value):
                try:
                    result.append(int(value[index + 1:index + 3], 16))
                except ValueError:
                    return None
                index += 3
            elif escaped in ('\\', '"'):
                result.extend(escaped.encode("utf-8"))
                index += 1
            else:
                result.extend(escaped.encode("utf-8"))
                index += 1
        return bytes(result)
    if re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,32}", value):
        return bytes.fromhex(value)
    return None


def _block_ssid(block):
    match = re.search(r"^\s*ssid\s*=\s*(.*)$", block, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip()
    if value.startswith('"'):
        escaped = False
        closing = None
        for index in range(1, len(value)):
            if escaped:
                escaped = False
            elif value[index] == "\\":
                escaped = True
            elif value[index] == '"':
                closing = index
                break
        value = value[:closing + 1] if closing is not None else value
    else:
        value = value.split("#", 1)[0].strip()
    return _decode_ssid(value)


def build_wifi_config(existing, ssid, password=None, open_network=False,
                      compact=False):
    try:
        ssid_bytes = ssid.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise ImageError("SSID must be valid UTF-8 text.") from exc
    if not 1 <= len(ssid_bytes) <= 32 or b"\x00" in ssid_bytes:
        raise ImageError("SSID must be 1 to 32 UTF-8 bytes.")
    try:
        text = existing.decode("utf-8") if existing is not None else "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\nupdate_config=1\n"
    except UnicodeDecodeError as exc:
        raise ImageError("The existing Wi-Fi config is not valid UTF-8; it was left unchanged.") from exc
    if compact:
        # The stock file is mostly documentation and occupies four 1 KiB
        # blocks. Keep active directives and networks so later file-RPC edits
        # can use its one-block writer when the filesystem is clean.
        text = "\n".join(line for line in text.splitlines()
                         if line.strip() and not line.lstrip().startswith("#")) + "\n"
    spans = _network_block_spans(text)
    if open_network:
        if password not in (None, ""):
            raise ImageError("An open network cannot have a password.")
        settings = "    key_mgmt=NONE\n"
    else:
        if not isinstance(password, str):
            raise ImageError("A Wi-Fi password is required for a protected network.")
        try:
            password_bytes = password.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ImageError("Password must be valid UTF-8 text.") from exc
        if not 8 <= len(password_bytes) <= 63 or any(byte < 0x20 or byte > 0x7e for byte in password_bytes):
            raise ImageError("WPA password must be 8 to 63 printable ASCII characters.")
        psk = hashlib.pbkdf2_hmac("sha1", password_bytes, ssid_bytes, 4096, dklen=32).hex()
        settings = "    key_mgmt=WPA-PSK\n    psk=" + psk + "\n"
    network = "network={\n    ssid=" + ssid_bytes.hex() + "\n" + settings + "}"
    matched = 0
    parts = []
    cursor = 0
    for start, end in spans:
        block = text[start:end]
        parts.append(text[cursor:start])
        if _block_ssid(block) == ssid_bytes:
            parts.append(network)
            matched += 1
        else:
            parts.append(block)
        cursor = end
    parts.append(text[cursor:])
    if matched:
        result_text = "".join(parts)
        count = len(spans)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        result_text = text + network + "\n"
        count = len(spans) + 1
    result = result_text.encode("utf-8")
    verified_blocks = _network_blocks(result_text)
    if (len(verified_blocks) != count or
            sum(_block_ssid(block) == ssid_bytes for block in verified_blocks)
            != (matched or 1)):
        raise ImageError("Generated Wi-Fi configuration failed structural validation.")
    return result, count


_RTM2_WLAN_START = "post-up wpa_supplicant -B -i wlan0 -c /var/etc/wpa_supplicant.conf"
_RTM3_WLAN_START = "post-up wireless-startup"
_RTM2_WIFI_SLEEP_AUTH = "/sys/kernel/debug/ieee80211/phy0/wlcore/sleep_auth"
_WIFI_SERVER_CHECK_HOOK = (
    "post-up /bin/sh -c 'f=/usr/local/bin/jibo-ssm/lib/skills-service-manager.js; "
    "[ \"$(grep -Fc \"const testJiboServer = true;\" \"$f\")\" = 1 ] && "
    "sed -i.jibo-dfu-original \"s/const testJiboServer = true;/const testJiboServer = false;/\" \"$f\" "
    "|| true'")


def build_rtm2_wifi_startup(existing, compact=False):
    """Adjust the stock RTM2 hook, keeping other network interfaces intact."""
    try:
        text = existing.decode("utf-8")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise ImageError("The network interfaces file is not valid UTF-8.") from exc
    lines = text.splitlines(keepends=True)
    in_wlan0 = False
    start_index = None
    rtm3_start = False
    has_power_save = False
    has_sleep_auth = False
    for index, line in enumerate(lines):
        active = line.split("#", 1)[0].strip()
        if re.match(r"^(?:iface|auto|allow-\S+)\s+", active):
            in_wlan0 = bool(re.fullmatch(r"iface\s+wlan0\s+inet\s+dhcp", active))
        if not in_wlan0:
            continue
        if active == _RTM2_WLAN_START:
            if start_index is not None:
                raise ImageError("The RTM2 wlan0 startup appears more than once; no change was made.")
            start_index = index
        if active == _RTM3_WLAN_START:
            rtm3_start = True
        has_power_save |= "iw wlan0 set power_save off" in active
        has_sleep_auth |= _RTM2_WIFI_SLEEP_AUTH in active
    changed = False
    if start_index is not None:
        if has_power_save != has_sleep_auth:
            raise ImageError("The wlan0 startup has a partial power-save change; review it manually.")
        if not has_power_save:
            original = lines[start_index]
            indent = original[:len(original) - len(original.lstrip())]
            newline = "\r\n" if original.endswith("\r\n") else "\n"
            commands = (
                indent + "post-up /usr/sbin/iw wlan0 set power_save off || true" + newline,
                indent + "post-up /bin/sh -c 'echo 0 > " + _RTM2_WIFI_SLEEP_AUTH + "' || true" + newline,
            )
            lines[start_index:start_index] = commands
            changed = True
    if compact and (start_index is not None or rtm3_start):
        # The stock file is two 1 KiB blocks of mostly comments. Preserve its
        # active directives so later file-RPC reads and edits fit one block.
        lines = [line for line in lines if line.strip() and
                 not line.lstrip().startswith("#")]
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
    return "".join(lines).encode("utf-8"), changed


def build_wifi_server_check_hook(existing):
    """Make stock Wi-Fi startup skip the defunct cloud check before SSM starts."""
    try:
        text = existing.decode("utf-8")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise ImageError("The network interfaces file is not valid UTF-8.") from exc
    lines = text.splitlines(keepends=True)
    in_wlan0 = False
    start_index = None
    hook_count = 0
    for index, line in enumerate(lines):
        active = line.split("#", 1)[0].strip()
        if re.match(r"^(?:iface|auto|allow-\S+)\s+", active):
            in_wlan0 = bool(re.fullmatch(r"iface\s+wlan0\s+inet\s+dhcp", active))
        if not in_wlan0:
            continue
        if active in (_RTM2_WLAN_START, _RTM3_WLAN_START):
            if start_index is not None:
                raise ImageError("The stock wlan0 startup appears more than once; no change was made.")
            start_index = index
        if "testJiboServer = true;" in active and "skills-service-manager.js" in active:
            hook_count += 1
    if hook_count > 1:
        raise ImageError("The Wi-Fi server-check hook appears more than once; no change was made.")
    if start_index is None or hook_count:
        return existing, False
    original = lines[start_index]
    indent = original[:len(original) - len(original.lstrip())]
    newline = "\r\n" if original.endswith("\r\n") else "\n"
    lines.insert(start_index + 1, indent + _WIFI_SERVER_CHECK_HOOK + newline)
    return "".join(lines).encode("utf-8"), True


def build_stock_wifi_startup(existing, compact=False):
    """Apply stock Wi-Fi startup fixes and report each change separately."""
    replacement, radio_adjusted = build_rtm2_wifi_startup(existing, compact=compact)
    replacement, server_hook_added = build_wifi_server_check_hook(replacement)
    return replacement, radio_adjusted, server_hook_added


BOOT_NETWORK_STATUS_DIR = "/jibo/phoenix/network-status"
BOOT_NETWORK_STATUS_HOOK = (
    "post-up /bin/sh /var" + BOOT_NETWORK_STATUS_DIR + "/boot-network-status.sh || true")


def build_boot_network_status_startup(existing, action):
    """Add/remove only our exact loopback hook; refuse ambiguous boot layouts."""
    if action not in ("install", "remove"):
        raise ImageError("Boot network status action must be install or remove.")
    try:
        text = existing.decode("utf-8")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise ImageError("The network interfaces file is not valid UTF-8.") from exc
    if "\x00" in text or "\\\n" in text or "\\\r\n" in text:
        raise ImageError("Unsupported network interfaces continuation or NUL.")
    lines = text.splitlines(keepends=True)
    loopbacks = []
    auto_lo = 0
    in_lo = False
    hooks = []
    for index, line in enumerate(lines):
        active = line.split("#", 1)[0].strip()
        parts = active.split()
        if not parts:
            continue
        if parts[0] in ("source", "source-directory", "mapping"):
            raise ImageError("External or mapped interfaces require manual review.")
        if parts[0] in ("auto", "allow-auto"):
            auto_lo += parts[1:].count("lo")
        if parts[0] in ("iface", "auto") or parts[0].startswith("allow-"):
            in_lo = parts == ["iface", "lo", "inet", "loopback"]
        if parts[0] == "iface" and len(parts) > 1 and parts[1] == "lo":
            if not in_lo or not line.endswith(("\n", "\r\n")):
                raise ImageError("Unsupported loopback interface layout.")
            loopbacks.append(index)
        if "/var" + BOOT_NETWORK_STATUS_DIR in active or "boot-network-status.sh" in active:
            if active != BOOT_NETWORK_STATUS_HOOK or not in_lo:
                raise ImageError("Unknown or misplaced boot network status hook; no change was made.")
            hooks.append(index)
    if len(loopbacks) != 1 or auto_lo != 1 or len(hooks) > 1:
        raise ImageError("Expected one automatic IPv4 loopback and at most one status hook.")
    if action == "remove":
        if hooks:
            del lines[hooks[0]]
    elif not hooks:
        index = loopbacks[0]
        newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
        lines.insert(index + 1, "\t" + BOOT_NETWORK_STATUS_HOOK + newline)
    return "".join(lines).encode("utf-8")


def boot_network_status_assets():
    """Load bundled (also pyz-extracted) assets and reject a damaged bundle."""
    directory = Path(__file__).resolve().parent / "assets/boot-network-status"
    try:
        raw = (directory / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if manifest["schema"] != 1 or manifest["target"] != "/usr/local/bin/jibo-ssm/startup/startup-view.js":
            raise ValueError("unsupported manifest")
        files = manifest["files_sha256"]
        if set(files) != {"startup-view.js", "startup-view-x11-early.js", "startup-view-dom.js",
                          "boot-network-status.sh", "ota-safe-overlay.js"}:
            raise ValueError("unexpected assets")
        assets = {name: (directory / name).read_bytes() for name in files}
        for name, content in assets.items():
            if not content or len(content) > 262144 or hashlib.sha256(content).hexdigest() != files[name]:
                raise ValueError("asset checksum mismatch")
        for variant in manifest["variants"]:
            if (not re.fullmatch(r"[0-9a-f]{64}", variant["stock_sha256"]) or
                    variant["patched_sha256"] != files[variant["asset"]] or
                    variant["stock_sha256"].encode() not in assets["boot-network-status.sh"] or
                    variant["patched_sha256"].encode() not in assets["boot-network-status.sh"]):
                raise ValueError("invalid variant pin")
        assets["manifest.json"] = raw
        return assets
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ImageError("The bundled boot network status assets are missing or invalid.") from exc


def _boot_status_directory(image, path):
    output = _run_debugfs(image, "stat " + path, allow_missing=True)
    if re.search(r"File not found|ext2_lookup", output, re.IGNORECASE):
        _run_debugfs(image, "mkdir " + path, writable=True)
        for field, value in (("mode", "040755"), ("uid", "0"), ("gid", "0")):
            _run_debugfs(image, "set_inode_field " + path + " " + field + " " + value, writable=True)
    elif not re.search(r"\bType:\s+directory\b", output):
        raise ImageError("Boot network status parent is not a directory: " + path)
    if path == BOOT_NETWORK_STATUS_DIR:
        for field, value in (("mode", "040755"), ("uid", "0"), ("gid", "0")):
            _run_debugfs(image, "set_inode_field " + path + " " + field + " " + value, writable=True)


def _apply_boot_network_status(image, temp, action, mode):
    if action not in ("install", "remove"):
        raise ImageError("Boot network status action must be install or remove.")
    if action == "install" and mode not in ("developer", "int-developer"):
        raise ImageError("Boot network status can only be installed in a developer mode.")
    work = Path(temp)
    original = _extract(image, VAR_INTERFACES_PATH, work / "status-interfaces")
    replacement = build_boot_network_status_startup(original, action)
    if action == "install":
        assets = boot_network_status_assets()
        for path in ("/jibo", "/jibo/phoenix", BOOT_NETWORK_STATUS_DIR):
            _boot_status_directory(image, path)
        for name, content in assets.items():
            path = BOOT_NETWORK_STATUS_DIR + "/" + name
            _put_factory_file(image, path, content, temp, 0o644)
            for field, value in (("mode", "0100644"), ("uid", "0"), ("gid", "0")):
                _run_debugfs(image, "set_inode_field " + path + " " + field + " " + value, writable=True)
    else:
        for path in (BOOT_NETWORK_STATUS_DIR + "/startup-view.js",
                     BOOT_NETWORK_STATUS_DIR + "/startup-view-x11-early.js",
                     BOOT_NETWORK_STATUS_DIR + "/startup-view-dom.js",
                     BOOT_NETWORK_STATUS_DIR + "/boot-network-status.sh",
                     BOOT_NETWORK_STATUS_DIR + "/ota-safe-overlay.js",
                     BOOT_NETWORK_STATUS_DIR + "/manifest.json",
                     BOOT_NETWORK_STATUS_DIR + "/boot-network-status.log"):
            content = _extract(image, path, work / "remove-check", optional=True)
            if content is not None:
                _file_metadata(image, path)
                _run_debugfs(image, "rm " + path, writable=True)
            if _extract(image, path, work / "removed-check", optional=True) is not None:
                raise ImageError("Boot network status removal did not survive readback.")
    if replacement != original:
        _replace_file(image, VAR_INTERFACES_PATH, replacement, temp)
    return replacement


def _verify_boot_network_status(image, temp, action):
    if action == "install":
        for name, content in boot_network_status_assets().items():
            path = BOOT_NETWORK_STATUS_DIR + "/" + name
            if (_extract(image, path, Path(temp) / "status-checked") != content or
                    _file_metadata(image, path) != {"mode": "0100644", "uid": "0", "gid": "0"}):
                raise ImageError("Boot network status content or permissions did not survive readback.")


def _adjust_stock_wifi_startup(image, temp, compact=False):
    """Adjust stock wlan0 startup while a Wi-Fi edit is already in progress."""
    work = Path(temp)
    original = _extract(image, VAR_INTERFACES_PATH, work / "interfaces", optional=True)
    if original is None:
        return False, False, None
    replacement, adjusted, hook_added = build_stock_wifi_startup(original, compact=compact)
    if replacement != original:
        _replace_file(image, VAR_INTERFACES_PATH, replacement, temp)
        if _extract(image, VAR_INTERFACES_PATH, work / "final-interfaces") != replacement:
            raise ImageError("The Wi-Fi startup edit did not survive image readback.")
    return adjusted, hook_added, replacement


def edit_wifi(source, destination, ssid, password=None, open_network=False,
              compact=False):
    source, destination, repaired = _copy_for_edit(source, destination)
    try:
        with tempfile.TemporaryDirectory(prefix="jibo-wifi-") as temp:
            existing = _extract(destination, VAR_WIFI_PATH, Path(temp) / "wifi.conf", optional=True)
            replacement, count = build_wifi_config(existing, ssid, password,
                                                   open_network, compact=compact)
            if existing is None:
                local = Path(temp) / "replacement"
                local.write_bytes(replacement)
                local.chmod(0o600)
                _run_debugfs(destination, "write " + _debugfs_arg(local) + " " + VAR_WIFI_PATH, writable=True)
                _run_debugfs(destination, "set_inode_field " + VAR_WIFI_PATH + " mode 0644", writable=True)
                _run_debugfs(destination, "set_inode_field " + VAR_WIFI_PATH + " uid 0", writable=True)
                _run_debugfs(destination, "set_inode_field " + VAR_WIFI_PATH + " gid 0", writable=True)
            else:
                _replace_file(destination, VAR_WIFI_PATH, replacement, temp)
            startup_adjusted, hook_added, startup_expected = _adjust_stock_wifi_startup(
                destination, temp, compact=compact)
            _replay_journal_on_copy(destination)
            verified = _extract(destination, VAR_WIFI_PATH, Path(temp) / "verified")
            if verified != replacement:
                raise ImageError("The edited Wi-Fi file did not survive image readback.")
            if (startup_expected is not None and
                    _extract(destination, VAR_INTERFACES_PATH,
                             Path(temp) / "checked-interfaces") != startup_expected):
                raise ImageError("The network startup file did not survive image readback.")
        return {"image": str(destination), "network_count": count,
                "rtm2_wifi_startup_adjusted": startup_adjusted,
                "wifi_server_check_hook_added": hook_added,
                "sha256": sha256_file(destination),
                "journal_replayed_on_temporary_copy": repaired}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def edit_mode_wifi(source, destination, mode, ssid, password=None,
                   open_network=False, network_status=None):
    """Apply mode and Wi-Fi to one disposable var copy and check both files."""
    if mode not in MODE_VALUES:
        raise ImageError("Mode must be one of: " + ", ".join(MODE_VALUES))
    source, destination, repaired = _copy_for_edit(source, destination)
    try:
        with tempfile.TemporaryDirectory(prefix="jibo-mode-wifi-") as temp:
            work = Path(temp)
            mode_data = _json_mode(_extract(destination, VAR_MODE_PATH, work / "mode.json"))
            previous = mode_data["mode"]
            mode_data["mode"] = mode
            mode_bytes = (json.dumps(mode_data, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
            wifi_before = _extract(destination, VAR_WIFI_PATH, work / "wifi.conf", optional=True)
            wifi_bytes, count = build_wifi_config(wifi_before, ssid, password,
                                                  open_network, compact=True)
            _replace_file(destination, VAR_MODE_PATH, mode_bytes, temp)
            if wifi_before is None:
                _put_factory_file(destination, VAR_WIFI_PATH, wifi_bytes, temp, 0o644)
            else:
                _replace_file(destination, VAR_WIFI_PATH, wifi_bytes, temp)
            startup_adjusted, hook_added, startup_expected = _adjust_stock_wifi_startup(
                destination, temp, compact=True)
            if network_status is not None:
                startup_expected = _apply_boot_network_status(destination, temp, network_status, mode)
            _replay_journal_on_copy(destination)
            if network_status is not None:
                _verify_boot_network_status(destination, temp, network_status)
            final_mode = _json_mode(_extract(destination, VAR_MODE_PATH, work / "final-mode"))
            final_wifi = _extract(destination, VAR_WIFI_PATH, work / "final-wifi")
            if final_mode["mode"] != mode or final_wifi != wifi_bytes:
                raise ImageError("The combined mode and Wi-Fi edit did not survive image readback.")
            if (startup_expected is not None and
                    _extract(destination, VAR_INTERFACES_PATH,
                             work / "checked-interfaces") != startup_expected):
                raise ImageError("The network startup file did not survive image readback.")
        return {"image": str(destination), "previous_mode": previous,
                "mode": mode, "network_count": count,
                "boot_network_status": network_status,
                "rtm2_wifi_startup_adjusted": startup_adjusted,
                "wifi_server_check_hook_added": hook_added,
                "sha256": sha256_file(destination),
                "journal_replayed_on_temporary_copy": repaired}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def edit_boot_network_status(source, destination, action):
    """Install/remove without changing mode or saved networks on one var copy."""
    source, destination, repaired = _copy_for_edit(source, destination)
    try:
        with tempfile.TemporaryDirectory(prefix="jibo-boot-status-") as temp:
            mode = _json_mode(_extract(destination, VAR_MODE_PATH, Path(temp) / "mode"))["mode"]
            expected = _apply_boot_network_status(destination, temp, action, mode)
            _replay_journal_on_copy(destination)
            _verify_boot_network_status(destination, temp, action)
            if _extract(destination, VAR_INTERFACES_PATH, Path(temp) / "interfaces-final") != expected:
                raise ImageError("The boot network status hook did not survive readback.")
        return {"image": str(destination), "boot_network_status": action,
                "sha256": sha256_file(destination),
                "journal_replayed_on_temporary_copy": repaired}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
