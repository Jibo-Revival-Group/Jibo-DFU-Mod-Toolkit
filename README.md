# Jibo DFU Mod Toolkit

A guided graphical toolkit for Jibo USB recovery, backups, mode and Wi-Fi settings, and official update packages. Start with the GUI on **Windows through WSL 2 + WSLg + usbipd-win** or **Linux**; a native **macOS** launcher is also available. The original text menu and scripting commands remain available.

## Download

Get the single file for your computer from [GitHub Releases](https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/releases). Release builds are unsigned.

| Computer | Download | Start |
| --- | --- | --- |
| Windows x64 | `Jibo-DFU-Windows-x64.exe` | Double-click. The launcher uses your standard Ubuntu 22.04+ or asks to set up its own private Linux, with no Linux account/password prompts. It guides WSL 2 + WSLg / usbipd-win setup if missing, then opens the same GUI. |
| Linux x86_64 | `Jibo-DFU-Linux-x86_64.AppImage` | Mark executable (`chmod +x`), then double-click with a polkit agent, or run from a terminal for sudo. Approve the USB administrator prompt. Requires a graphical desktop, glibc 2.35+ (Ubuntu 22.04+) and `pkexec` or `sudo`. |
| Mac Apple silicon | `Jibo-DFU-macOS-arm64.dmg` | Open, drag **Jibo DFU Mod Toolkit** to Applications, then open the app. |
| Mac Intel | `Jibo-DFU-macOS-x86_64.dmg` | Open, drag the app to Applications, then open it. |

**Windows SmartScreen:** click **More info → Run anyway**. **macOS first-open block:** open **System Settings → Privacy & Security → Open Anyway**, then confirm Open.

Linux and Mac downloads include Python/Tk and all toolkit tools: no clone, compiler, Git, Homebrew or internet needed for first launch. Windows includes the prebuilt Linux payload. It reuses its own **Jibo-DFU-Toolkit** Linux, or a standard **Ubuntu 22.04+** WSL 2 distribution with a normal user and WSLg. It never automatically uses Linux belonging to NVIDIA Workbench, Docker, Podman, Rancher or another tool. If no Ubuntu matches, it asks once to download a verified official Ubuntu image (about 345 MB) and create its own private Linux; no Linux username or password is needed. Internet and a reboot may be needed to install missing Windows/WSL prerequisites. Existing source launchers remain available below. Downloading firmware updates still needs internet. The downloadable Mac apps require macOS 15 or later; Apple silicon USB operation remains experimental.

**Windows setup window:** the EXE is a Windows GUI application: no command
window or Terminal tab is created, even at startup. The setup window shows five
named stages, the current step and elapsed time. The Ubuntu download shows received
MB, total MB and a percentage; other steps show activity, not a made-up percentage.
Questions appear inside that same window. **Set up the toolkit’s Linux?** offers
**Set it up** / **Not now**; the Windows Linux-support and USB-support questions
offer **Install** / **Not now**. The safe default is **Not now**. Closing a question
also counts as **Not now**: no approval is given and setup stops there. While waiting
for you the window stays on top, restores itself, flashes its taskbar button when
unfocused and sounds a reminder about once a minute.
Windows may still request UAC approval; **Waiting for you** explains how to find
the flashing shield icon or installer. Waiting for your answer or a Windows prompt
is not a setup stall. A stall or failure explains what happened and what to try,
with **Copy details** and **Open log folder** for support. Where more time is safe,
choose **Keep waiting** or **Stop setup**; closing that decision stops setup. Run
`Jibo-DFU-Windows-x64.exe -ShowConsole` only when troubleshooting; this can be
combined with `-Distro NAME`. Source launchers keep their terminal.

The Windows download also checks the Linux display's current mode before opening
the GUI. If Windows reports **WARN:COPY MODE** (a known WSL bug when the toolkit's
Linux starts again while other Linux apps keep WSL running), setup shows
**Fixing a Windows display problem** and restarts the display of the toolkit's own
private Linux once; that takes a few seconds and touches no other Linux app. If
that does not work, or you chose your own distribution with `-Distro`, setup stops
visibly instead of opening an invisible window: save your other Linux work, run
`wsl --shutdown` in PowerShell (this closes all Linux apps), then open the toolkit
again, and restart Windows only if it still happens. Do not remove or reinstall
its private Linux. A Linux
mapped-window signal alone is not success: setup also checks this launch's Windows
window for visibility and responsiveness. Robot operations stay unavailable until
that check succeeds. If it cannot confirm the window within two minutes, it closes
the unopened toolkit and shows recovery instructions with **Copy details**.

Phoenix image lists and downloads use per-user toolkit storage, not the folder you started from: `${XDG_DATA_HOME:-~/.local/share}/jibo-dfu/updates` on Linux/WSL, and `~/Library/Application Support/jibo-dfu/updates` on macOS. Official packages and previously completed Phoenix downloads in `./updates` remain discoverable. The one-step Phoenix install removes its owned download after the flash attempt; robot backups are never part of that cleanup. Folder failures name the affected folder; HTTP failures include the status and a Cloudflare note when detected.

**Remove the toolkit's private Linux:** run `wsl --unregister Jibo-DFU-Toolkit` in PowerShell. This deletes **all files inside that Linux**, including any backups saved there; copy files you need to Windows first. It does not remove your other WSL distributions. Advanced users can explicitly choose another compatible distro with `Jibo-DFU-Windows-x64.exe -Distro NAME`; automatic selection never offers tool distributions.

If Linux reports a FUSE error, run `./Jibo-DFU-Linux-x86_64.AppImage --appimage-extract-and-run` from a terminal. Verify downloads against the release's `SHA256SUMS`; license notices and complete corresponding sources accompany each binary and the release. **Administrator setup approval is not permission to write a robot**: the GUI keeps every existing review and confirmation. See [release and troubleshooting notes](docs/release.md).

## Start the GUI from source

Clone or download this repository, extract it if needed, and keep the launchers in the toolkit folder. Complete the linked platform setup before launching:

| Platform | Double-click in the toolkit folder | Launch from a terminal | Prerequisites |
| --- | --- | --- | --- |
| **Windows x64** | `run-gui.cmd` | `.\run.ps1 -Gui -Distro Ubuntu-22.04` | [WSL 2, Ubuntu, WSLg and usbipd-win](#windows-with-wsl) |
| **macOS Intel / Apple silicon** | `Jibo DFU Toolkit.command` | `./run.sh --gui` | [Native Python with matching Tk, Homebrew/MacPorts tools](#macos); Intel tested, Apple silicon **experimental** |
| **Linux x86_64** | `Jibo DFU Toolkit.desktop` | `./run.sh --gui` | [Python/Tk, a graphical desktop and USB administrator access](#linux-quick-start) |

Linux file managers may require **Allow Launching** or **Trust** for the `.desktop` file. On macOS, Finder may require **Open** for the `.command` file; it is not a signed/notarized app. First launch may need internet access to build the host tools and may open a terminal for setup or authorization. **Host setup authorization is not consent to write a robot**: each device change has its own review.

### GUI screenshots

![DFU-ready action screen](docs/images/gui/ready.png)

*DFU-ready action screen. USB readiness and available operations are shown together.*

<details>
<summary>Mode/Wi-Fi setup, safety review and progress screenshots</summary>

![Mode selection before Wi-Fi setup](docs/images/gui/mode-wifi.png)

*Mode/Wi-Fi setup. Select the mode before entering the network settings; nothing is written at this step.*

![Mode and Wi-Fi write review](docs/images/gui/review.png)

*Write review. Check the target, hashes and rollback backup; Cancel is the default and Confirm requires explicit approval.*

![Var backup progress](docs/images/gui/progress.png)

*Backup progress. Follow the current stage, percentage and operation log.*

</details>

### USB workflow and safety

1. Put one robot in **RCM/APX** (`0955:7740`); see [Put Jibo in RCM mode](#put-jibo-in-rcm-mode). The toolkit uses ShofEL to load its recovery program into RAM.
2. The robot reappears in **DFU** (`0955:701a`). All partition reads and writes use DFU. ShofEL is used only for the RCM-to-DFU entry step.

The GUI enables DFU entry in RCM and partition actions only after the recovery loader is ready in DFU. It shares the text menu's device operations, validation, backups and per-write review, including the ON-by-default boot-network checkbox for developer mode + Wi-Fi setup. It shows USB state and progress, refuses duplicate starts, and does not allow window closure during a transfer. **Keep USB connected and wait for completion**; forcibly killing the app can still interrupt a transfer. Read the [hardware limitations](#hardware-results-and-remaining-work) and [boot-screen diagnostic caveats](#boot-screen-wi-fiip-diagnostic-usb-only-setup) before proceeding.

On Linux, the `.pyz` package is generated locally and reused on later runs; on macOS, the launcher runs the Python source directly. See [GUI setup, safety and verification](docs/gui.md) for details.

### Text menu (alternate)

For the original text menu, run `./run.sh` on Linux/macOS or `.\run.ps1 -Distro Ubuntu-22.04` on Windows. The launchers without a GUI flag still open the text menu. Both interfaces use the same backend and safety checks. See [using the toolkit](#using-the-toolkit) and [manual build and scripting](#manual-build-and-scripting).

## Compatibility

**Any stock Jibo that has never been modified is supported with 100% certainty.** I have personally tested every stock firmware version and all stock files. Windows, Linux and Intel Macs have also been tested; Apple silicon Macs are experimental.

If your robot was previously changed by another mod system, it will most likely work with no changes, but that cannot be guaranteed: there is no way to foresee changes someone else made. If it doesn't work, press **Open a support ticket** in the **#help** channel on the [Phoenix Discord](https://discord.gg/FyDVYQ6FfV). I'm happy to help.

## Put Jibo in RCM mode

Every USB step starts with Jibo in RCM mode, which the toolkit shows as **RCM/APX** (`0955:7740`).

<img src="assets/help/rcm-buttons.png" width="360" height="360" alt="Jibo's round button panel with two small buttons circled in red: RESET (upper) and RCM (lower)">

*The round button panel on the back of Jibo's head, under the hatch. RESET is the upper small button; RCM is the lower one.*

1. Fully shut Jibo down.
2. Open the hatch on the back of his head to reach the round button panel in the photo.
3. Connect the USB cable from your computer to Jibo's micro-USB port under the same hatch.
4. Press and hold the RCM button, the lower of the two small buttons circled in the photo. Keep holding it while you turn Jibo back on with his power button, then let go of the RCM button.
5. In RCM mode, the red light on his forehead stays on and you don't hear his startup chime. If he chimes or starts normally, shut him down and try again.
6. The toolkit then shows RCM/APX detected. Choose Enter DFU with ShofEL (RAM loader).

The GUI shows these steps and this photo under **How do I put Jibo in RCM mode?** until Jibo reaches DFU. In the text menu, press `h`.

## Quick path: stock Jibo to int-developer

If you are here to mod a stock robot, use the guided menu to change its existing installation to `int-developer` mode. **If the robot has not joined your Wi-Fi yet, configure Wi-Fi in the toolkit too:** changing the mode alone does not give it a network connection.

1. Start the GUI with `./run.sh --gui` on Linux or macOS, or `.\run.ps1 -Gui -Distro Ubuntu-22.04` from PowerShell on a Windows PC with WSL 2 + WSLg. The double-click entries above open the same window. Connect one robot by USB and [put it in RCM mode](#put-jibo-in-rcm-mode).
2. Choose **Enter DFU with ShofEL (RAM loader)**. Wait for the menu to show the robot in DFU. The included Meerkat Rev02 entry selects RAM slot 2 or 3 from the hardware strap; slot 3 has only offline checks so far. See [hardware coverage](#hardware-results-and-remaining-work) for other revisions.
3. If the robot already has your Wi-Fi saved, choose **More tools and local images → Set robot mode** and select **int-developer**. If it has never connected to your Wi-Fi, choose **Set mode and configure Wi-Fi**, select **int-developer**, then enter your Wi-Fi name and password twice. That action applies both settings together. If a saved Wi-Fi password is wrong, choose **More tools and local images → Add or update a Wi-Fi network** and enter the same SSID with the corrected password.
4. Review and confirm the change. The toolkit saves or reuses a `var` rollback backup and checks the result. After a fresh RTM flash, the stock Wi-Fi and network startup files can be too large for the quick editor; if the menu offers a full `var` transfer, choose it and leave the robot connected until readback finishes. Setting Wi-Fi also prepares startup to skip the retired Jibo server check that can disconnect an otherwise working network. On RTM2, it applies the later TI radio settings too. These changes take effect when the robot restarts; flashing alone does not make them.
5. When the toolkit reports the change verified, restart the robot normally.

## Linux quick start

On an x86_64 Linux system (including WSL 2), clone the repository and run the launcher:

```sh
git clone https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit.git
cd Jibo-DFU-Mod-Toolkit
./run.sh --gui
```

The launcher checks the local `dist/jibo-dfu-linux-x86_64.pyz` before opening it. If it is missing, stale, or contains a ShofEL helper that cannot start DFU, the launcher asks for `sudo` authorization in the same terminal, installs missing build packages on supported distributions, builds the launch-enabled ShofEL helper, replaces the `.pyz`, and opens the GUI. It also installs the system Tk dependency on supported distributions; a working graphical display is required. You do not need to install ShofEL separately. Prefer `./run.sh --gui` so the launcher requests authorization and preserves the required desktop connection variables. USB access requires administrator privileges. Backups are saved under the invoking owner's `~/Jibo-Backups`.

When `./run.sh` is started inside WSL, it also tries to use Windows PowerShell and usbipd-win to attach a connected Jibo and reattach it when it changes from APX to DFU. If Windows interop or usbipd-win is unavailable, attach it manually. Use `JIBO_MANUAL_USB=1 ./run.sh --gui` to skip automatic USB handoff.

The first build needs an internet connection for ShofEL and package installation. The launcher's default loader is the included `assets/loader.bin`; `JIBO_LOADER=/path/to/loader.bin ./run.sh --gui` selects a different local copy of the same pinned image. The U-Boot source and notices are in `vendor/jibo-ram-dfu-v1-source.tar.gz`; the file access changes are in `firmware/file-level.patch` and `firmware/cid-serial.patch`, and the faster transfer changes are in `firmware/dfu-queue.patch`. An existing package can be run directly with `sudo python3 dist/jibo-dfu-linux-x86_64.pyz`.

## Windows with WSL

Use an x64 Windows PC with WSL 2 and Ubuntu. For a first-time setup, open **PowerShell as Administrator** and install WSL with Ubuntu 22.04 LTS:

```powershell
wsl --install -d Ubuntu-22.04
```

Restart Windows if prompted, then open **Ubuntu 22.04** once to create its Linux username and password. In PowerShell, update WSL and check that Ubuntu shows `VERSION 2`:

```powershell
wsl --update
wsl --list --verbose
```

If Ubuntu shows `VERSION 1`, run `wsl --set-version Ubuntu-22.04 2`. Install [usbipd-win](https://github.com/dorssel/usbipd-win) from PowerShell with Windows Package Manager, then reopen PowerShell so `usbipd` is on your path:

```powershell
winget install --interactive --exact dorssel.usbipd-win
usbipd list
```

If `winget` is unavailable, install the `.msi` from the [usbipd-win releases](https://github.com/dorssel/usbipd-win/releases). [Microsoft's WSL instructions](https://learn.microsoft.com/windows/wsl/install), [Ubuntu's WSL guide](https://ubuntu.com/wsl/docs/latest/howto/install-ubuntu-wsl2/), and [Microsoft's USB connection guide](https://learn.microsoft.com/windows/wsl/connect-usb) cover the setup and USB prerequisites.

Open PowerShell in the downloaded or cloned toolkit folder and run:

```powershell
.\run.ps1 -Gui -Distro Ubuntu-22.04
```

The launcher selects that WSL 2 distribution, shares and attaches a connected Jibo USB device, then runs `run.sh --gui` inside WSL. WSLg must be available to display the window; if it is missing, run `wsl --update` and `wsl --shutdown`, then reopen the launcher. The robot can change from APX (`0955:7740`) to DFU (`0955:701a`); the launcher watches both USB identities while the menu is open. Sharing a new USB identity may show a Windows administrator prompt. Run `.\run.ps1 -Gui -Distro Ubuntu-22.04 -ManualUsb` if you prefer to attach from another PowerShell window with `usbipd bind --busid <BUSID>` (administrator) and `usbipd attach --wsl --busid <BUSID>`. The Linux `./run.sh --gui` also works directly inside WSL with manual USB attachment and WSLg. If PowerShell's script policy blocks the launcher, use `powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Gui -Distro Ubuntu-22.04` for that invocation.

### Windows troubleshooting

If setup stops, use **Copy details**, press **Open a support ticket** in the **#help** channel on the
[Phoenix Discord](https://discord.gg/FyDVYQ6FfV), and paste that short summary into your ticket. It includes the build, Windows/WSL
versions, the step that failed, the error and recent log lines. Review it before
sharing; do not post passwords or Wi-Fi settings. **Open log folder** opens the
full setup log. You can also paste `%LOCALAPPDATA%\Jibo-DFU-Toolkit\logs` into File
Explorer. Each launch has a `setup-<UTC time>-<id>.log`; if local app data is not
writable, the launcher uses `%TEMP%\Jibo-DFU-Toolkit\logs` instead.

For **Starting Linux**, **Unpacking the toolkit**, **Checking the toolkit** or
**Opening the toolkit**, close other WSL work first, run `wsl --shutdown` in
PowerShell, then reopen the EXE. This stops all your WSL Linux sessions. Restart
Windows if it still fails or a WSL install requested a reboot. For a stopped
Ubuntu download, check your internet connection. For **Connecting USB**, look
for a Windows administrator approval prompt and check the USB cable.
`Jibo-DFU-Windows-x64.exe -ShowConsole` still provides console diagnostics.

If `run-gui.cmd` says the only Linux is **Jibo-DFU-Toolkit**, use
**Jibo-DFU-Windows-x64.exe** instead. That private Linux intentionally has no
Linux password or sudo access; do not try to invent a password or run the source
launcher as Windows administrator. For source builds, install your own Ubuntu
with a normal sudo-enabled user and select it with
`.\run.ps1 -Gui -Distro Ubuntu-22.04`.

## macOS

Install [Homebrew](https://brew.sh) and native tools with a matching Python/Tk pair. This example explicitly selects Python 3.12:

```sh
brew install python@3.12 python-tk@3.12 dfu-util libusb e2fsprogs
git clone https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit.git
cd Jibo-DFU-Mod-Toolkit
JIBO_PYTHON="$(brew --prefix python@3.12)/bin/python3.12" ./run.sh --gui
```

If you already use another Python 3.10+ version, install its matching `python-tk@X.Y` package and use `JIBO_PYTHON` to select that interpreter. A Finder launch must also resolve a Python with matching Tk; the terminal command above is the explicit path when several Python versions are installed.

The launcher does not install packages itself: it checks the tools above before opening the interface and prints the exact install command if one is missing. Installing Homebrew also installs the Xcode Command Line Tools (`cc`, `make`, `git`) that the first launch uses to build the ShofEL entry helper from the pinned in-repo payloads, so no ARM cross-compiler is needed. That helper is downloaded, built once on the first launch (internet required), and reused afterwards. No `sudo` is needed on a Mac, and backups are saved under the invoking owner's `~/Jibo-Backups`. On Apple silicon, open a native Terminal session with Rosetta disabled.

For older Intel Macs that Homebrew does not support, install the same tools with [MacPorts](https://www.macports.org/install.php). Without Homebrew the launcher finds MacPorts in `/opt/local` on its own, so `Jibo DFU Toolkit.command` works by double-click:

```sh
sudo port install python312 py312-tkinter tk dfu-util libusb e2fsprogs
./run.sh --gui
```

Install the `tk` port as well as `py312-tkinter`: without it the window fails with "Can't find a usable tk.tcl". The downloadable Mac app needs macOS 15 or later; on older macOS use this source version.

See [macOS setup and verification](docs/macos.md) for architecture notes, tool selection, `JIBO_*` overrides, and the current hardware-verification results.

## Manual build and scripting

The launcher builds the package automatically; these are the equivalent manual steps when you need to inspect or customize the build. You need Python 3, `dfu-util`, `libusb-1.0`, GCC, Make, and an ARM bare-metal toolchain. The toolkit does not include or need a production signing key for ShofEL entry.

```sh
python3 scripts/build_file_level_entry.py \
  --loader assets/loader.bin --out .build/file-level-entry
```

This helper checks the loader manifest, clones the pinned ShofEL revision, applies the matching entry patch, and tests the resulting host tool and RAM stage. A matching local build is reused.

Package the included loader, ShofEL entry files, DFU tool, and Python UI. The packager checks the loader against its pinned size and SHA-256 and rejects a ShofEL host built without DFU launch support. It does not include signed RCM artifacts or a signing key.

```sh
python3 scripts/package.py \
  --loader assets/loader.bin \
  --shofel2 .build/file-level-entry/shofel2_t124 \
  --intermezzo .build/file-level-entry/intermezzo.bin \
  --dfu-stage .build/file-level-entry/dfu_stage2.bin \
  --dfu-util "$(command -v dfu-util)" \
  --out dist/jibo-dfu-linux-x86_64.pyz
sudo python3 dist/jibo-dfu-linux-x86_64.pyz
```

To rebuild the loader itself, extract `vendor/jibo-ram-dfu-v1-source.tar.gz` and follow [the loader build notes](docs/loader-build.md) with `--file-level-candidate`. That separate build needs the matching Buildroot host toolchain. To run the Python source directly instead of a `.pyz`, place the matching image at `./loader.bin`, place `shofel2_t124`, `intermezzo.bin`, and `dfu_stage2.bin` together in `./tools/`, then run `sudo python3 jibo_dfu.py`.

## Using the toolkit

See [RTM images and factory calibration](docs/rtm-images-and-calibration.md)
for the archived RTM packages and the factory's calibration steps.

### Phoenix images

With Jibo in DFU, open **Install a Phoenix image or an official update**.
The Phoenix image is featured first in the same list as your local
packages, marked **— recommended**. Choosing it gets the image from jibo.io,
checks it, then continues through calibration handling, the Cancel-default
review and writing without returning to the menu -- one flow with no
separate download step. The fetched image
and its working files are removed when the attempt ends, including a cancelled
review or a failed write. Robot backups and your own packages are kept. Only an
interrupted download stays so the same image can resume next time. Connection
interruptions retry automatically up to six attempts without starting a second
flash. You can cancel the retry wait; untrusted images are never retried.
After downloading, **Checking the image** and **Preparing Jibo's software** show
moving progress. The image is checked in one forward pass and unpacked once,
without repeating those checks for an unchanged file. Like official packages,
Phoenix writes finish when Jibo accepts each transfer; no automatic read-back
follows. The fresh var rollback copy is still taken before writing. The CLI's
`flash-update --verify-readback` remains available for an extra check.
See [Phoenix images from jibo.io](docs/hosted-flash-images.md) for the two
calibration choices and what an install does not fix.

With the robot in RCM/APX ([how to put Jibo in RCM mode](#put-jibo-in-rcm-mode)), choose **Enter DFU with ShofEL** first. The toolkit waits up to 25 seconds for the new DFU connection to become accessible, within the entry timeout, before checking the recovery loader. Both interfaces refresh robot state and available actions automatically when USB changes. In the GUI, use **Refresh USB**, F5 or Ctrl+R; in the TUI, `r` refreshes manually. The current loader uses Meerkat Rev02 RAM slots 2 and 3; only slot 2 has been confirmed on hardware. Select it for a robot whose hardware profile matches. Once DFU is active, both interfaces offer:

| Action | What it does |
| --- | --- |
| Enter DFU with ShofEL (RAM loader) | Reviews and starts the RAM loader from RCM/APX; disabled once DFU is active. |
| Set mode and configure Wi-Fi | Changes the settings in one reviewed file transaction when the fast writer supports the files. If it does not, one full `var` read, edit, write, and readback handles the settings and needed Wi-Fi startup changes. |
| Install a Phoenix image or an official update | Selects an official or Phoenix package and offers **Keep this Jibo's calibration (recommended)** (default) or **Flash without calibration (unsafe)**. Both take a fresh verified var rollback copy before writing. Unsafe requires an additional Cancel-default confirmation. Image installation behavior is unchanged. |
| Backups | Back up every exposed partition or chosen partitions, restore from a partition backup set, and manage saved backups by robot name, date, partitions and size. The reusable **Save or check a var backup** action is here too. |
| More tools and local images | Set mode or Wi-Fi separately, check the partition layout, write an edited `var` image, remove boot-screen status, or inspect/edit a local image. Local image edits do not change the robot. |

The GPT check reads the complete `jibo-dfu-v1` alternate. Its final short transfer resets the loader's read cursor, so another check or an update can run without restarting DFU.

**Transfer speed:** Whole-partition reads and writes go through `jibo_dfu_pipeline.py`, which talks to Linux usbfs directly and keeps DFU requests queued instead of waiting a USB round trip for every block. The included loader uses 32 KiB blocks and accepts queued writes; it lists the read-only `jibo-dfu-queue-v1` alternate to say so. With an older loader, reads are still queued, but writes send one block at a time. Over WSL with usbipd-win, a 500 MiB `var` read took 52.0 s, against 115 s with dfu-util 0.9. A readback-checked `var` write took 76.4 s, against about 2 MiB/s with dfu-util. Set `JIBO_DFU_TRANSFER=dfu-util` to use dfu-util for these transfers. Small file-level requests always use dfu-util.

**Calibration choices:** Keep copies camera L/R and alignment plus local identity into fresh var; supported IMU and microphone files are restored when present. Touchscreen calibration and the factory record are restored only from existing, verified same-robot rootfs/skills partition backups. Existing standalone rollback images are kept; named partition backup sets are managed separately under **Backups**. Without calibration uses the image's generic measurements and identity (which may be absent); camera, motion, hearing and setup may not work properly. Restore a valid local identity before setup if the image has none. The full current var backup is retained locally for recovery.

The included loader supports bounded changes to existing files. It reads the target file once before the change, then checks the file and its ownership and permissions after writing. The first `var` change saves a rollback image; later `var` changes reuse any verified `var` image for the same robot, even if the live filesystem has changed. Other partitions are backed up only when selected. The file writer currently handles only existing regular files of at most 4 KiB in one allocated block, mapped by one extent or one legacy direct pointer. Stock RTM Wi-Fi configs may occupy four 1 KiB blocks because of comments, and stock network startup files occupy two. A full-`var` Wi-Fi edit removes comment-only and blank lines from these stock files while keeping their active settings. That can make later file edits fit in one block. When a mode or Wi-Fi file uses an unsupported layout, or `var` has an unclean ext4 journal, the menu offers the slower full-`var` edit as an explicit choice, with cancel selected by default. The journal check protects direct writes and remains in force after a robot boot until Linux cleanly unmounts `var` or the full transfer replays the journal on a local copy. Full image writes and update packages still use partition transfers. See [the file-level protocol](firmware/file-level-protocol.md). An earlier direct mode-file write passed content and metadata readback.

**Backups:** Open **Backups → Back up every partition** or **Back up specific partitions**. The checklist shows each size and the selected total. Each run reads a fresh, self-contained set under `Jibo-Backups/<robot name>/<UTC date-time>/`; the name comes from the saved var image, with a short device-tag fallback when unavailable. **Stop after this partition** finishes the active read, keeps saved files and skips the remaining partitions. **Restore partitions** checks the serial-hash robot identity, live GPT extents, image sizes, SHA-256 and individual manifests before a Cancel-default review, then reads every restored partition back. Another robot's backup is refused even if its displayed name matches. **Saved backups** shows details, opens the folder, checks local files, or explicitly deletes one new self-contained set after a Cancel-default review. Old backup sets and standalone rollback images are listed in place and cannot be deleted here. Standalone images lack saved GPT extents and are not offered as partition-set restores. These are the GPT partitions exposed for complete DFU transfer, not the raw GPT, gaps, or eMMC Boot0/Boot1. See [backup management and safety](docs/backups.md).

**Flash .img files:** **Backups → Flash .img files to partitions** writes your own image files to the partitions you check, one image per partition. Each image must be whole 512-byte sectors and no larger than its partition; a smaller image replaces only its own bytes. After a Cancel-default review, the toolkit saves a new backup set of those partitions, writes each image, and reads it back to verify it. Restore that backup set to undo.

Jibo's archived `PlatformTeam/system-manager` starts `wpa_supplicant` using `/var/etc/wpa_supplicant.conf`. The archived `jiborobot/jibo-wifi` saves networks with `SAVE_CONFIG`; its separate `/var/etc/networks.conf` stores optional static IP settings for those SSIDs, not a second place for Wi-Fi passwords. This menu adds a DHCP Wi-Fi network to the supplicant file or replaces a saved network with the same SSID, while preserving other saved networks. It does not configure static IP addresses or test association while the robot is in DFU. Interactive Wi-Fi password entry asks for the password twice; scripts using `--password-stdin` still provide one line.

On a live RTM2 robot, Wi-Fi associated, received an IP address, and then disconnected after its Wi-Fi service failed to reach `api.jibo.com`. The service treats that server failure as a failed connection and disconnects locally. When you set Wi-Fi on a stock RTM2 or RTM3 installation, the toolkit adds a startup command that disables only this obsolete server reachability check before the service starts. It keeps the original service bundle at `/usr/local/bin/jibo-ssm/lib/skills-service-manager.js.jibo-dfu-original` and leaves the service's access-point and IP checks in place. The command applies once at boot if the expected setting is present; it has no effect on an unknown service bundle. This was verified on the live RTM2 robot and across a restart.

Stock RTM2 starts the TI `wlan0` supplicant directly. The later RTM3 image disables TI radio power saving before starting it. When you set Wi-Fi, the toolkit also applies those two startup commands if it finds the original RTM2 hook in `var`. The tested RTM2 disconnect occurred with those radio settings already in place, so they did not cause that disconnect. If Wi-Fi still fails after restarting, router security or radio compatibility may be a separate issue. The toolkit saves WPA-PSK credentials and can use the WPA2 side of a mixed WPA2/WPA3 network; WPA3-only operation has not been verified.

For advanced file inspection, `stat-partition-file /jibo/mode.json --partition var` reads the file's UID, GID, permissions, and inode. `set-mode` and `configure-wifi` use the quick file path when the active loader advertises it; `set-mode-file` and `configure-wifi-file` require that path explicitly.

For scripts, the corresponding commands are:

```sh
sudo python3 dist/jibo-dfu-linux-x86_64.pyz enter-dfu-shofel --port 1-1 --confirm-meerkat-rev02
sudo python3 dist/jibo-dfu-linux-x86_64.pyz backup-var --port 1-1
sudo python3 dist/jibo-dfu-linux-x86_64.pyz list-partitions --port 1-1
sudo python3 dist/jibo-dfu-linux-x86_64.pyz backup-partitions --all --port 1-1
sudo python3 dist/jibo-dfu-linux-x86_64.pyz restore-partitions /path/to/backup-set.json --partition var --port 1-1 --yes
sudo python3 dist/jibo-dfu-linux-x86_64.pyz inspect-var /path/to/var.img
sudo python3 dist/jibo-dfu-linux-x86_64.pyz list-updates
```

Use `--out /path/to/new/directory` with `backup-var` to force a fresh read for comparison. The default reuses one verified baseline for the same robot; `--refresh` reads the current state and discards the new copy if it is byte-identical to the saved baseline. Use `--partition NAME` more than once with `backup-partitions` or `restore-partitions` to pick several partitions. The update workflow always reads and verifies the current `var` before writing, even if an older backup already exists. Full readback of update images is optional with `--verify-readback`; it checks only the stock image prefixes that were sent, before first-boot expansion.

If an update stops mid-flash, leave the robot in DFU and keep its saved rollback backup and update record. Interrupted flashes cannot currently resume: `--resume-from` refuses without a USB action. Select the same package for a new reviewed flash instead. Older same-robot calibration backups remain available if a partial write damaged the current var. Add `--verify-readback` to compare every new transfer before reset.

### Flash commands

```sh
# Default: fresh setup with this Jibo's calibration and name
sudo python3 dist/jibo-dfu-linux-x86_64.pyz flash-update /path/to/package.tar.bz2 --keep-calibration --yes
# Unsafe: the dedicated flag AND the typed confirmation are required; --yes is not enough
sudo python3 dist/jibo-dfu-linux-x86_64.pyz flash-update /path/to/package.tar.bz2 --without-calibration --confirm 'FLASH UPDATE'
```

Phoenix image reviews state: **This image connects Jibo to Phoenix (jibo.io). After flashing he starts at the setup screens; finish setup at jibo.io.** This line is shown only for authenticated Phoenix images. [Hosted image details](docs/hosted-flash-images.md).

## Boot-screen Wi-Fi/IP diagnostic (USB-only setup)

In **Set mode and configure Wi-Fi**, developer and int-developer modes default
**Show Wi-Fi status and IP on the boot screen** to ON. Select **Leave the
current boot-screen setting unchanged** to retain the previous fast file-only
path. Install or removal intentionally uses the existing full-var backup,
confirmation, write and SHA-256 readback transaction, with mode and Wi-Fi in the
same candidate image. The guided confirmation discloses the larger transfer.
It does not require networking, cloud access, SSH or an OTA on the robot.

The assets live under `/var/jibo/phoenix/network-status/`; one guarded `lo`
`post-up` line in `/var/etc/network/interfaces` binds the matching renderer over
the read-only SSM startup-view file **in memory only**, before X11/SSM starts.
The services partition is never rewritten or remounted read-write. Unknown/custom
SSM hashes safely keep the stock screen. The diagnostic is suppressed outside
developer modes and shows only current Wi-Fi association and a usable wlan0 IPv4
address, not saved credentials. Renderer polling has timeouts and stops when the
startup window loses ownership.

Removal is under **More tools → Remove boot-screen Wi-Fi/IP status** or in the
developer-mode setup selector. It removes only the toolkit-owned files and
loopback line; mode, saved Wi-Fi, identity and calibration are retained. Reboot
after removal; an existing bind mount lasts for the current boot.

### What Jibo's screen shows

The Wi-Fi/IP line appears only in **developer** or **int-developer** mode with **Show Wi-Fi status and IP on the boot screen** enabled (ON by default in the GUI's **Set mode and configure Wi-Fi** developer-mode setup). After restarting Jibo normally:

| On Jibo's screen | What it means and what to do |
| --- | --- |
| `Wi-Fi: checking...` | Jibo is starting up and checking Wi-Fi. Wait for the status to update. |
| `Wi-Fi: connected to <network name> · IP <address>` | Wi-Fi is connected and Jibo has an IP address. This is the IP address the Phoenix console's **Robots → Add a Jibo** command asks for. |
| `Wi-Fi: connected, waiting for address` | Jibo has joined the network, but the router has not given him an IP address yet. Wait a minute; if it stays here, check the router and its DHCP settings. |
| `Wi-Fi: not connected` | Jibo is not connected to Wi-Fi. Check the network name and password, and make sure Jibo is within range of the router. Re-run **Set mode and configure Wi-Fi** or **Add or update a Wi-Fi network** with the correct name and password, then restart Jibo. |
| `Wi-Fi: status unavailable` | The Wi-Fi status could not be read. Restart Jibo once; if it still appears, open a support ticket in **#help** on the [Phoenix Discord](https://discord.gg/FyDVYQ6FfV). |

If the line is missing, check the mode and enable the option in **Set mode and configure Wi-Fi**, then restart. To remove it, choose **More tools → Remove boot-screen Wi-Fi/IP status**, then restart Jibo. Unchecking the GUI checkbox leaves the current setting unchanged; it does not remove an installed status line.

Equivalent CLI operations (explicit confirmation is still required):

```bash
# Defaults to install for developer modes; password is prompted securely.
python3 jibo_dfu.py set-mode-wifi --mode int-developer --ssid 'Your network'
# Use --boot-network-status keep to opt out, or remove to remove in the same edit.
python3 jibo_dfu.py boot-network-status remove
# Offline edit of a saved var copy; the source image is never modified:
python3 jibo_dfu.py edit-boot-network-status backup-var.img install --out candidate-var.img
```

`assets/boot-network-status/manifest.json` pins four stock hashes to three
renderer assets: legacy Electron DOM, early X11 without ScreenScheduler, and
current X11. Offline release checks cover the 40 candidate packages found in the
archive's production/stable/production-OTA/SQA indexes through 13.0.0, including
additional beta/EFT candidates. This is **not hardware boot or DFU evidence**;
real first-boot/USB installation remains a release gate. For a new/custom hash,
review its source and init/layout before adding a pin; never bypass the hash guard.

The hook suppresses the overlay during pending or malformed OTA work state and
runs a local monitor to release its owned bind before services replacement.
**Real services-OTA timing is not verified.** Until tested on hardware, remove
this diagnostic through DFU and reboot before a services OTA, verifying there is
no startup-view child bind. The monitor is not a synchronous updater handshake.

## Hardware results and remaining work

On one robot, ShofEL started the RAM loader and the robot entered DFU on the same USB port. Two consecutive 17 KiB GPT marker reads in one DFU session validated the partition layout. A full DFU read of `var` took 116.9 seconds and produced exactly 524,288,000 bytes with SHA-256 `4a58631e0c6eb0559bef7d2827676a1bce7965886f2887afbdc65dedce18416b`. That matched the same robot's earlier read-only ShofEL `var` backup byte for byte. The temporary comparison copy was removed; neither validation wrote eMMC. An older full eMMC dump from June has different `var` contents, so it is not the matching reference for this test.

A mode change to `int-developer` has also been confirmed on a connected Jibo. The owner has verified the Windows/WSL USB helper through RCM-to-DFU reattachment, other USB state changes, unplug/replug, and power-off/power-on. On another robot, a compact 13.0.0 package transfer with preserved `var` completed and the robot booted. The first-boot resize tag was written after the script's `set -e` resize sequence. Live SSH inspection found rootfsA, rootfsB, and services ext4 block counts exactly equal to their GPT partition sizes; skills occupies all but 2,560 bytes of its 10,991,139,328-byte partition. `var` retained `int-developer` and its original 500 MiB size. Selectable GPT partition backup and restore are implemented. A raw full eMMC user-area backup and restore are not implemented. The toolkit does not enable SSH.

The official 5.4.2 and 13.0.0 flash packages each include one `u-boot-flasher.bin` and one base `meerkat_rev02.bct` (plus its signed copy). Their base BCT files are byte-identical, but `bct_dump` shows **two distinct DDR3 configurations**, in RAM slots 2 and 3; two EMC timing fields differ. Jibo's `flash-dfu.sh` passes the BCT and flasher separately to `tegrarcm`, supplying those RAM configurations through the BCT. [Jibo's archived flashing instructions](https://pvindex.org/confluence/display/ENG/Building+a+single+step+flash+system+for+Jibo) also prescribe different build configurations for EVT and DVT2, with an explicit Meerkat Rev02 BCT selection for DVT2. One bundled flasher binary therefore does not establish that every board revision uses the same RAM entry path.

The toolkit's ShofEL entry now selects the official Meerkat Rev02 **RAM slot 2 or 3** profile from the robot's RAM strap and rejects other codes before initializing RAM. The prior slot 2 entry passed live DFU checks on hardware. The new two-slot build passes compilation and offline profile checks, but has not yet been run on a robot with slot 3. [Jibo's board history](https://pvindex.org/confluence/display/ENG/Boards,+Revisions+and+History) identifies shipping JB1014 mainboard revisions, while its [board ID table](https://pvindex.org/confluence/display/ENG/Mainboard+(JB1014)+Board+ID+Resistor+Definitions) describes separate board ID straps; neither source maps those revisions to Tegra `RAM_CODE`. The distribution of slots 2 and 3 among robots is therefore unknown. Earlier EVT hardware also had a different flashing configuration, so coverage of every robot still needs evidence and a live check on a slot 3 unit.

## License

The Jibo DFU Mod Toolkit is free software under the [GNU General Public License v3.0](LICENSE) (GPL-3.0-only). You may use, study, change and share it. If you distribute it or a modified version, including inside another project, you must keep the copyright and license notices, release your version under the GPL-3.0, and publish its complete source.

U-Boot, ShofEL2 and the stock robot files the toolkit modifies keep their own licenses; [NOTICE.md](NOTICE.md) lists which parts they cover.
