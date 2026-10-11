# Omarchy rehearsal VM (Hyper-V on DARKNESS)

Purpose: install stock Omarchy 4 unattended into a throwaway Hyper-V VM, then add the local `[aces]` repo and install
the ACES packages over it, so the overlay (VISION §2.1: stock ISO plus one bootstrap), the `aces-omarchy` hooks and the
`aces-fabric` plugin are exercised before anyone touches the real dual boot (VISION §9). It stands in for the libvirt VM
named in the day-30 acceptance line (VISION §11.3). Everything large lives under `D:\HyperV\aces-rehearsal`
(`iso\`, `cidata\`, `vm\`, `serve\`, `ssh\`, `reports\`); C: has about 41 GB free.

## Step order

Run from `C:\Users\shedm\aces-os\rehearsal` in Windows PowerShell 5.1 (CurrentUser policy here is RemoteSigned, so
local scripts run). Every script except `validate.ps1` takes `-WhatIf`.

| # | Step | Elevation | Expected time | Tested here |
|---|---|---|---|---|
| 1 | `.\get-iso.ps1` downloads the newest ISO linked on omarchy.org (today `omarchy-4.0.4.iso`, 6,185,304,064 bytes) and its `.sha256`, verifies, skips a valid copy | none | measured 2026-10-09: 3.5 min for the download (about 28 MB/s) plus the hash | ran for real; the ISO verified against the published sha256 on the first try. First attempt died on curl's progress meter (fixed, see the run notes) |
| 2 | `.\make-cidata.ps1` builds the cidata drive | VHDX (default): **elevated** (Mount-VHD failed here unelevated: "A required privilege is not held by the client", 0x80070522; New-VHD worked). ISO (`-Iso`, and the automatic fallback when not elevated): none | ISO 0.7 s (measured) | ISO built with `-Iso` and accepted by the real installer (no wizard appeared). VHDX path: `-WhatIf` only |
| 3 | `.\new-vm.ps1` creates the VM (skips an existing one) | none for a member of Hyper-V Administrators: New-VM, Set-VMFirmware, Add-VMDvdDrive and Checkpoint-VM all ran unelevated here | seconds | `-Start` created and booted the VM; `Get-VMFirmware` showed Secure Boot off, boot order disk then DVD |
| 4 | `Start-VM aces-rehearsal; vmconnect.exe localhost aces-rehearsal` (or `.\vm-shot.ps1` for a PNG of the console without vmconnect) | none | measured: power-on to "Installing Omarchy" 18 s, to the Omarchy login screen under 2 min (the ISO installs from its bundled package mirror) | ran unattended; watched with `vm-shot.ps1` (Hyper-V WMI thumbnails), no vmconnect needed |
| 5 | `Checkpoint-VM -Name aces-rehearsal -SnapshotName omarchy-clean` once the VM has rebooted into Omarchy | none | seconds | taken; a second checkpoint `aces-installed` was taken after step 7 |
| 6 | `.\serve-repo.ps1` serves `repo\x86_64` on `http://192.168.1.171:8930` | none (this python.exe already has inbound Allow rules, TCP and UDP, Private and Public; the HA-External vNIC is Public) | about 1 s | served the signed repo (16 packages, key published); the VM fetched `aces.db` through the Windows firewall without any rule change |
| 7 | `.\validate.ps1 -VmIp <vm address> -Install` copies `aces-install.sh` in and runs it in a login shell over `ssh -t` (sudo asks for the rehearsal password up to three times unless the VM has the unattended sudo rule below), then fetches the report | none | measured: `omarchy update -y` plus `aces-os` with all its dependencies in 75 s on this LAN (the VM clock, which is on Brisbane time, says 13:15:11 to 13:16:26) | third attempt passed 13/13 (the first two found the sudo and `OMARCHY_PATH` problems, both fixed); report in `reports/2026-10-09-aces-rehearsal-report.txt` |
| 8 | `.\validate.ps1` again any time: fetches `~/aces-rehearsal-report.txt`, exit 0 all PASS, 1 any FAIL, 2 no VM or no report | none | seconds | exit 0 on the passing run, 1 on the stopped ones |

Without SSH, step 7 can be done in the VM console instead: log in as `aces`, open a terminal with Super+Return (vmconnect
passes the Windows key only in full screen unless Hyper-V Settings > Keyboard says otherwise), and run the line
`serve-repo.ps1` prints: `curl -fsSLo ~/aces-install.sh http://192.168.1.171:8930/aces-install.sh && bash ~/aces-install.sh http://192.168.1.171:8930`.
`validate.ps1` then finds the VM's address in the server's access log.

## What the kit is built on (read 2026-10-09)

- **ISO:** the omacom/omarchy-iso README says the link is on omarchy.org and a `.sha256` sits beside every ISO. The
  page links `https://iso.omarchy.org/omarchy-4.0.4.iso` (Last-Modified 15 Sep 2026); its sidecar is `sha256sum`
  format: `ddeded2758c48318d201dfdac905ecb28f570441883f0c052ea3cd5d05acf92d  omarchy-4.0.4.iso`.
- **cidata format:** read from omarchy-iso at commit `8b17699` (8 Oct 2026): `configs/airootfs/root/configurator`
  (writes the two JSON files), `omarchy-cidata-load` (copies `user_configuration.json`, `user_credentials.json` and the
  optional `user_full_name.txt`, `user_email_address.txt`, `user_encrypt_installation.txt`, `authorized_keys`,
  `tailscale_authkey`, `defer-provisioning` from any filesystem labelled `cidata` or `CIDATA`), the orchestrator, and
  `test/integration.d/base-test.sh` (the project's own unattended cidata build). `cidata/*.json` are the configurator's
  output shape with this VM's values: user `aces`, host `aces-rehearsal`, `Australia/Brisbane`, `us` keyboard, no
  `disk_encryption` block (an encrypted install would stop at the LUKS prompt), kernel `linux-omarchy`, `/dev/sda` wiped
  with the configurator's layout for an 80 GiB disk (1 MiB gap, 2 GiB ESP, btrfs to 1 MiB before the end). The loader
  has not changed since 9 Aug; the `input_method` keys are newer than 4.0.4 and ignored by older ISOs.
- **No post-install hook exists in that format.** The orchestrator installs only Omarchy's own package list; archinstall's
  `packages` and `custom_commands` keys are not acted on, and nothing on the drive is executed. So the ACES step is the
  SSH or console step above. This answers the question in VISION Appendix C: the cidata flow takes no extra packages;
  they need an installer patch or a post-install step like this one.
- **Secure Boot off:** omarchy-iso `plans/consumer-secure-boot.md` (state August 2026) says the ISO boots an unsigned
  GRUB and kernel, so "Secure Boot must be disabled today"; the README's Proxmox example enrols no keys.
- **Boot order disk first, not DVD first** (a deliberate departure from the brief): the Omarchy manual says the disk goes
  first on purpose, so the empty disk falls through to the ISO once and the installed system boots from disk. With the
  DVD first, every reboot would boot the ISO, find cidata again and reinstall. `-DvdFirst` exists to watch one install.
- **Hyper-V drivers:** `linux-omarchy` 7.2.8 (omacom/omarchy-pkgs HEAD) builds `HYPERV_STORAGE`, `HYPERV_NET`,
  `DRM_HYPERV`, `HYPERV_KEYBOARD` and `HID_HYPERV_MOUSE` as modules. The live ISO boots `linux-t2` (not checked).
  Omarchy's manual lists no Hyper-V guide; users report earlier releases (2.x to 3.x) working (omarchy discussion #445)
  with the console stuck at 1024x768 unless `Set-VMVideo` sets a size (new-vm.ps1 does) and with no audio. Not verified.
- **Omarchy guards pacman:** `00-omarchy-update-guard.hook` aborts a direct `pacman -Syu` ("omarchy update" is the path),
  and `omarchy refresh pacman` rewrites `/etc/pacman.conf`, then runs `pre-refresh-pacman` hooks. So `aces-install.sh`
  writes `/etc/pacman.d/aces.conf`, includes it above `[omarchy]` (Omarchy's own sample hook pattern) and installs a
  `pre-refresh-pacman.d/aces-repo` hook that restores the include; it runs `omarchy update -y` and then
  `pacman -S --needed aces-os`. `ACES_UPDATE=none` is the brief's literal `pacman -Sy aces-os` (a partial upgrade).

## Credentials (throwaway, this VM only)

User `aces`, password **`aces-rehearsal-only`** (also root's), stored as a SHA-512 crypt hash in
`cidata/user_credentials.json`. Never reuse it, and never put this VM on an untrusted network. SSH logs in with this
workstation's existing public key `%USERPROFILE%\.ssh\id_ed25519.pub`: `make-cidata.ps1` appends it to the committed
comment-only `cidata/authorized_keys` on the drive it builds (pass `-PublicKey` for another; private keys are refused).
The install enables `sshd` and opens port 22 in ufw only because `authorized_keys` is present.

## Signed and unsigned repo

`serve-repo.ps1` publishes `keys\aces-pkgs.pub.asc` (and the in-VM line carries `ACES_KEY_FPR` from `keys\FINGERPRINT`)
only when every `*.pkg.tar.zst` has a `.sig`; then `aces-install.sh` checks the fingerprint, runs `pacman-key --init`
(idempotent), `--add`, `--lsign-key` and uses `SigLevel = Required DatabaseOptional`. Otherwise no key is served and it
uses `Optional TrustAll`. On 2026-10-09 the key existed but 0 of 14 packages were signed, so the server ran unsigned.
The secret key and `keys\gnupg` are never copied; the server refuses to start if such a file appears in its web root.

## Tests run here (2026-10-09) and results

- PowerShell parser: all five scripts parse with no errors. `bash -n post-install/aces-install.sh`: OK.
- `cidata/*.json` load with Python's `json`; the partition table ends exactly 1 MiB before 80 GiB.
- cidata ISO (IMAPI2, 61,440 bytes) in an Arch container: `blkid` LABEL=cidata TYPE=iso9660; the real
  `omarchy-cidata-load` exited 0 and copied the three files byte-identical; `authorized_keys` yields 1 ed25519 key; the
  password hash re-derives with `openssl passwd -6`.
- `serve-repo.ps1`: `aces.db` served (sha256 equal to `aces.db.tar.zst`) on 127.0.0.1 and 192.168.1.171; a plain
  `http.server` on `repo\` answers 404 for `aces.db`, because `repo-add` leaves `aces.db`/`aces.files` as WSL symlinks
  (reparse tag 0xA000001D) that Windows programs cannot open; hence the web root of real copies. The key and the secret
  key path answer 404. Re-run while up re-syncs; `-Stop` stops it.
- `aces-install.sh` end to end in an Arch container against the served repo (`ACES_UPDATE=none`, `--offline` doctor,
  `aces-base aces-creative aces-consoles aces-omarchy`, an `omarchy` stub that runs upstream `omarchy-plugin-validate`,
  an Omarchy-shaped `pacman.conf`): 10 PASS, 1 FAIL; a second run was idempotent; pacman's repo order became aces,
  omarchy, core, extra; the pre-refresh hook put a removed include back.
- Signed mode with a throwaway key (not the ACES key): a wrong `ACES_KEY_FPR` stops before pacman is touched; the right
  one imports the key and installs the signed package; an unsigned package is refused ("failed retrieving file
  '...pkg.tar.zst.sig' ... 404").
- Still not tested: the VHDX cidata path, oscdimg (not installed), `-DvdFirst`.

## The real run (2026-10-09, afternoon)

Omarchy 4.0.4 installed itself from the cidata ISO with no wizard (power-on to "Installing Omarchy" in 18 s, login
screen in under 2 min: the ISO installs from its own package mirror). SSH as `aces` worked keyless; the installer puts
`authorized_keys` in the user's home only, enables `sshd` and opens 22 in ufw (omarchy-iso `configure_ssh_access`), so
root has no key and sudo needs the rehearsal password. `omarchy update -y` found only two packages to upgrade. The
ACES step then passed **13 PASS, 0 FAIL** on the third attempt (`reports/2026-10-09-aces-rehearsal-report.txt`): all
16 packages installed from the signed repo with `SigLevel = Required DatabaseOptional`, `aces doctor` reached every
rig endpoint from the VM, `aces-omarchy-setup` linked the hooks and the skills into `~/.claude`, `~/.codex`,
`~/.pi/agent` and `~/.agents` and copied the plugin, and **`omarchy plugin validate` passed on the real Omarchy host for
both the copied and the packaged plugin** (the symlink failure predicted below is gone since `aces-omarchy` copies the
plugin). `aces fabric refresh` from the VM read the rig's `sim-lab-4` mode and five lanes; `aces-gendesign selftest` and
`aces-voxel fea --self-test` passed in the engineering venv (Python 3.14.7). A second checkpoint, `aces-installed`,
was taken afterwards. Three things the VM found, all fixed in this kit:

- `get-iso.ps1` died on the first stderr line of curl's progress meter: under `$ErrorActionPreference = 'Stop'`,
  PowerShell 5.1 turns native stderr into terminating error records whenever stderr is not a console. The download now
  runs under `Continue`, drops the meter when stderr is redirected, and lets the exit code decide.
- `aces-install.sh` opened with `sudo -v`, which prompts even on a NOPASSWD host when any other rule for the user (the
  stock `%wheel` line) lacks NOPASSWD (sudoers `verifypw=all`); with nobody at the keyboard the run stalls. It now
  tries `sudo -n true` first.
- `omarchy update -y` aborted with `/usr/bin/omarchy-update-dev: line 7: OMARCHY_PATH: unbound variable`: a plain ssh
  command on Arch's bash reads no rc file, so the environment Omarchy's `~/.bashrc` sets (via
  `/usr/share/omarchy/default/bash/env-bootstrap`) was missing. `validate.ps1` now runs the script in a login shell
  (`bash -lc`), and `aces-install.sh` sources `env-bootstrap` itself when `OMARCHY_PATH` is unset, so the console path
  and any other caller work too.

Unattended sudo for this throwaway VM (what the passing run used; the `omarchy-clean` checkpoint predates it): as
`aces`, once, with the rehearsal password: `sudo tee /etc/sudoers.d/90-aces-rehearsal` with the two lines
`aces ALL=(ALL) NOPASSWD: ALL` and `Defaults:aces verifypw = any`, then `sudo chmod 440` it. Without that rule step 7
needs a person to type the password at the `ssh -t` prompts, as the table says.

A fourth finding came from opening the console: a copied plugin is only *discovered*; the bar shows a widget once its id
is in a bar section of `~/.config/omarchy/shell.json` (`omarchy plugin enable` asks the running shell to write that
entry and answers "omarchy-shell is not running" over SSH). `aces-omarchy` 0.1.0-4's setup script now does the
placement itself, and on this VM the entry was added by hand before the first desktop login.

Verified later the same evening: Hyprland and omarchy-shell do come up on Hyper-V's synthetic display. The desktop user
signed in at the console, the bar rendered with the `aces-fabric` widget on its right section, and `vm-shot.ps1`
captured it (the automated console sign-in attempt before that failed PAM authentication and is not retried: the
password is typed by a person). Before that, `omarchy plugin validate` and `qmllint` against Omarchy's own `qs.Commons`
and `qs.Ui` modules were the only checks. `qmllint` found two real defects in the plugin, fixed in `aces-omarchy`
0.1.0-3: `Service.qml` declared a `state` property, shadowing Item's built-in one, and the `Process.exited` handler
named a parameter type the shell cannot resolve. The remaining warning classes (`unqualified`, `missing-property` on the
untyped `bar` object) occur in Omarchy's first-party plugins in the same numbers.

## The golden path on the VM (2026-10-10, after midnight)

After the four golden-path packages were rebuilt and `serve-repo.ps1` re-synced the web root (checked byte for byte
against `repo\x86_64`), the VM upgraded aces-base 0.1.0-3, aces-engineering 0.1.0-2, aces-voxel 0.1.0-2 and
aces-omarchy 0.1.0-5 from the signed LAN repo with `pacman -Sy` and ran `aces golden run --demo` from a login shell
over SSH, as a user would: **PASS, verified-desktop, exit 0, 23.7 s** (25 s with interpreter start) on 4 vCPUs with no
GPU, run `20261009T144006Z-b3ac99`, bundle `070c439672c8`, simlab ledger entry 1, aces ledger entry 2. The same seed
gives the same winner and the same numbers as the workstation and the clean container (volfrac 0.23, rmin 11.55 mm,
0.177 kg, vm_p99 54.47 MPa against 60, umax 0.0312 mm against 0.035; the 2 mm check ran on the CPU, +4.4 % on vm_p99).
`aces golden verify latest` passed its ten checks (the eleventh, `resolve`, is opt-in), `aces-evidence verify` was ok,
`aces doctor` showed the golden row as ok with that run, and the linked agent skill carried the new text. The run
directory was copied to `D:\HyperV\aces-rehearsal\reports\golden-20261010-013959\` (report, renders, evidence). The
`aces-install.sh` kit script runs the same two commands as steps 12 and 13 on a fresh install.

## Rig run from the VM

The acceptance runs of the golden path on the real rig (build spec section 24, steps 7 to 9) end on this VM: the
same packages a customer machine installs, with the independent check on the rig's GPUs and the conforming check on
its CPUs. Before them, on DARKNESS, the operator has shipped the `aces-golden:0.1` image, deployed `rig-golden-1`,
validated the registry rows (tag `rig-golden-2`) and made the DARKNESS lane run (steps 1 to 6); the integrated packages
are built and signed, and `.\serve-repo.ps1` has re-synced the web root from `repo\x86_64` (check it byte for byte, as
above). The VM is a LAN host on HA-External, so it reaches the rig directly.

The simlab token, when the rig's simlab-mcp asks for one: put `ACES_SIMLAB_TOKEN=...` in `~/.config/aces/simlab.env` on
the VM and `chmod 600` it (a group- or world-readable file gets a warning). A missing or wrong token is refused before
anything is submitted (exit 2, HTTP 401).

Step 7, the lane run, from a login shell over SSH (`ssh aces@192.168.1.195`):

```bash
sudo pacman -Sy aces-engineering calculix-ccx-bin aces-omarchy aces-os
aces fabric refresh
aces golden run --demo --lane sim/pool --json --progress lines
aces golden verify latest
aces-evidence verify
aces ledger verify
aces golden show latest --bench-out /tmp/bench-vm.md
```

Copy the row from `/tmp/bench-vm.md` into `docs/BENCHMARKS.md`. Record the grade exactly as it comes out (the target
is `rig-verified`); the verdict line names where the check ran (`check 2 mm on the rig (GPU n)`) and the conforming
check's host.

Step 8, desktop only (no lane): the conforming check runs on the VM itself as `m001`.

```bash
aces golden run --demo
aces golden verify latest
```

Record the grade honestly: the constraint cap may give `preview-grade`.

Step 9, the offline refusal: a lane that cannot be used must stop before anything is computed.

```bash
aces golden run --demo --lane sim/none --json; echo "exit $?"
```

It must print exactly one JSON document, exit 2, and leave no new run directory under `~/Work/golden-demo/golden/`.

Afterwards (steps 10 and 11): the rig gate is ok, `rig_capacity` shows the pool free again, no `simlab-golden-*`
container is left, and the `docs/PACKAGES.md` table "Golden path on the rig" and the BENCHMARKS rows are filled from
these runs' records. If the grade is not `rig-verified` because of a band, the cap or a validation, no band is changed.

`calculix-ccx-bin` 2.23-1 builds at the integration: it brings the GCC 7 Fortran runtime the official binary needs
(see `docs/PACKAGES.md`), and in a clean container it installed, `ccx -v` named 2.23 and the demo's conforming check
ran. If the `pacman` line of step 7 still stops at `calculix-ccx-bin`, or the desktop conforming check of step 8 says
`ccx-not-found`, record that as it comes, and do not work around it on the VM.

## Known problems to expect

- With the VHDX cidata the VM has two SCSI disks; the config names `/dev/sda`, which should be the OS disk at LUN 0, but
  Linux does not guarantee sd names. The ISO variant leaves one disk and no ambiguity.
- On HA-External the VM is a LAN host: `aces doctor` makes read-only GETs to the rig, and the ACES post-boot and
  post-update hooks run `aces fabric refresh`, which also POSTs a tool call to rig-state `:8010`. Use
  `ACES_DOCTOR_ARGS=--offline` and point `~/.config/aces/aces.toml` elsewhere if the rig must not be contacted.
- The VM's disks (`D:\HyperV\aces-rehearsal\vm`) sit on a USB SSD (Samsung T7 Shield). On the evening of 2026-10-09 the
  drive stalled for a moment (System log, provider `disk`, event 153 "IO operation ... was retried", four in one second;
  three more the day before), the Hyper-V worker passed the failed I/O into the guest, and the guest's btrfs root went
  read-only: `pacman -Q` listed nothing, `sudo` asked for a password (its drop-in could not be read), binaries returned
  "Input/output error", and `/sys/fs/btrfs/*/devinfo/1/error_stats` showed 125 write and 101 read errors with no
  corruption or generation errors. VMMS logged 16400/16420 "cannot access the data folder" and Hyper-V showed the VM as
  RunningCritical. The repair was `Stop-VM aces-rehearsal -Force` (the guest went Off in seconds) and `Start-VM`: the
  root remounted read-write at the last committed transaction with every package, the sudo drop-in, `shell.json` and
  the agent configuration intact. Do not restore a checkpoint for this. The `serve-repo.ps1` http.server wedged in the
  same stall (connections accepted, empty replies) and needed `-Stop` and a restart. Disable USB selective suspend or
  the drive's power-off in Windows power management, or move the VM to an internal disk, to stop it recurring.
- Hyprland and omarchy-shell on Hyper-V's synthetic display: verified on 2026-10-09 (see above); the SSH path does not
  need them.

## Reset

- Re-run the ACES overlay on a clean Omarchy: `Restore-VMCheckpoint -VMName aces-rehearsal -Name omarchy-clean -Confirm:$false`.
- Start over: `Stop-VM aces-rehearsal -TurnOff; Remove-VM aces-rehearsal -Force; Remove-Item -Recurse D:\HyperV\aces-rehearsal\vm, D:\HyperV\aces-rehearsal\ssh`
  then steps 3 to 5, and step 2 first if `cidata/` changed (the ISO stays). Rebuild the cidata drive only while no
  running VM has it attached.
- Stop serving: `.\serve-repo.ps1 -Stop`. Remove everything: delete `D:\HyperV\aces-rehearsal`.

## aces-cad on the VM

The operator's acceptance of the local text-to-CAD package (`aces-cad` 0.1.0-1, build spec section 29) runs on this
VM: the same signed packages a customer machine installs, the real Omarchy kernel (Landlock decides strict mode), and
an agent session at the console. Results are written here and into the aces-cad section of `docs/PACKAGES.md`; copy
`cad-doctor.json`, the run and pull JSON, the renders, the drawing and the bundle to `rehearsal/reports/`.

**Run on 2026-10-10, 10:15 to 10:35 UTC**, by the operator from DARKNESS over ssh, against the packages `bin/build`
made from main 61ac8a4 and served from the LAN repo. Every step passed; step 9 ran with Codex as the MCP client, not
Claude Code (the reason is in that step). Records, renders, the drawing, the bundles, the agent loop's event stream and
the cancel test are in `reports/2026-10-10-aces-cad-vm/`. Results, step by step:

1. `pacman -S aces-cad` installed aces-cad 0.1.0-1 and upgraded aces-os to 0.1.0-6; installed afterwards: aces-cad
   0.1.0-1, aces-os 0.1.0-6, aces-engineering 0.1.0-5, aces-base 0.1.0-3, calculix-ccx-bin 2.23-1 (aces-omarchy 0.1.0-6
   and aces-voxel 0.1.0-2 from the morning's upgrade). The clean-container install tests on DARKNESS the same day:
   the default set resolves aces-os to 510 packages, the aces-base + aces-engineering + aces-cad set to 522, and the
   aces-cad block (doctor, plate, inspect, render, drawing, pull with its ledger entry, socket exit 5, loop exit 4) and
   the 114 unit tests passed (430 s).
2. Kernel `7.2.5-3-omarchy`, lsm `lockdown,capability,landlock,yama,bpf`. Doctor: verdict ready, mode strict, seccomp
   active (106 BPF instructions, TSYNC), Landlock active at ABI 10 (the TCP rules and the signal and abstract-socket
   scoping in force, no skipped roots), 14 of 14 checks pass: socket AF_INET and AF_UNIX EACCES, fork and posix_spawn
   EPERM, kill(parent) EPERM, the planted secret and `/proc/<parent>/environ` EACCES, a write and a symlink outside
   the job directory EACCES, a write inside it and a thread allowed, the audit hook's PermissionError on
   `socket.socket()`, the planted secret in no output, no file written outside. Python 3.14.7, build123d 0.11.1, OCP
   7.9.3.1, numpy 2.4.6, scipy 1.17.1, scikit-image 0.26.0, trimesh 5.1.0, matplotlib 3.11.1, ezdxf 1.4.4, lib3mf
   2.5.0; the thickness check reports the embree ray backend.
3. Selftest: 13 checks PASS (project, doctor, plate, parameter override, inspect, render, drawing, pull, reproducible
   pull, hostile socket, loop, write outside and OCCT export outside), 37.8 s wall. `/usr/bin/time` is not on the VM
   (the first attempt failed with exit 127); shell timing was used throughout.
4. `aces project new cad-vm --class sovereign`: created.
5. Plate run `20261010T101628Z-0ffb`: 2.4 s wall, volume 11607.300918301276 mm³ (exact), 60 × 40 × 5 mm, counts
   1/1/7/15/10, valid, sandbox strict and confined.
6. Inspect `20261010T101631Z-eca3` (2.4 s wall, 1.6 s of it imports, 503 MB peak RSS): mass 0.030991 kg at 2670 kg/m³
   (AlSi10Mg, the golden demo's nominal value); validity ok (BRepCheck valid, closed, 0 free edges, no
   self-intersection, manifold); one simple through hole `h1`, Ø10.0, axis +Z, entering at (30.0, 20.0, 5.0), depth
   5.0 mm; thickness min 5.0 mm (268 rays, 268 hits, p01 = p50 = 5.0).
7. Render 3.5 s: iso, front and cutaway at 1024 × 800 (43358, 19815 and 34830 bytes; 268 triangles at 0.0723 mm).
   Drawing 3.1 s: third-angle, 1:1 mm, front, top, right and iso, dimension texts 60, 5, 40, Ø10 THRU, 30, 20, as
   DXF, SVG and PNG. Checked by eye on the PNG (the report copy): the six values read as listed, the Ø is clean, and
   the hole's hidden edges in the front and right views are grey. Checked in the DXF with ezdxf: every DIMENSION
   entity carries a `dimlfac` override of 1.0 and its rendered block text is one of the six values; the header
   `$DIMLFAC` is 100 (ezdxf's default for its EZDXF dimstyle, which these entities override), so a viewer that ignores
   per-entity overrides would show 6000 where one that honours them (ezdxf's own rendering, the SVG and the PNG) shows
   60.
8. Pull: bundle `cad/local/20261010T101736Z-plate` (7 files), geometry_sha256
   `806c9be7a86c8ae60308ee75d9a910f371d5f5aae6fe6517fa5ce6aa5fd1742f`; `check_bundle.py`: all files match; `aces
   ledger verify` ok (6 entries); the `cad.pull` entry carries `class: sovereign`; `aces project show cad-vm` lists
   `cad/local/20261010T101736Z-plate/part.step`. Run and pull again (`20261010T101741Z-plate`): the same
   geometry_sha256, equal to the CI and Windows values, so the plate's STEP hash is now equal on Windows, Arch (the
   container) and the VM.
9. Agent loop: Codex 0.133.0 (gpt-5.5, the operator's ChatGPT login) on DARKNESS, with `aces-cad` configured as
   `ssh aces@<vm> env ACES_CAD_PROJECT=cad-vm aces-cad-mcp`, asked to make the plate, check it, show it, draw it, pull
   it and note a lesson, with the aces-cad tools only. Seven tool calls in order, each right at the first try
   (`agent-loop-codex/events.jsonl`): cad_start; cad_run (run `20261010T103142Z-09a4`, the agent's own BuildPart
   script, volume 11607.300918301273 mm³); cad_inspect (hole h1 Ø10 through, thickness min 5.0 mm); cad_render (iso);
   cad_drawing (60, 5, 40, Ø10 THRU, 30, 20); cad_pull (bundle `20261010T103215Z-agent-plate`, geometry_sha256
   `4213f419d026…`, different from the plate's because the run name is written into the STEP); cad_note (one lesson,
   now in `cad/NOTES.md`). 102 s wall including the model. Images: the server sent `image/png` content with the render
   (58,484 base64 characters) and the drawing (201,896), but Codex passed only the text to the model, which reported
   "rendered image itself reached me: no". Claude Code was not the client: the CLI's stored claude.ai login on
   DARKNESS had expired ("OAuth session expired and could not be refreshed"), the user settings route the CLI to a
   third-party endpoint, and logging in again is the operator's to do; whether Claude Code shows the images to the
   model is still open. Esc, with a plain stdio client (`records/vm-cad-cancel-test.json`): `notifications/cancelled`
   during a 100 s run killed the job, the worker was gone 0.13 s later, no reply was sent for the cancelled id, `ping`
   and a following `cad_runs` answered (the cancelled run is kept with status error), and the server exited 0 when
   stdin closed. A client that dies mid-run: the server's EOF grace lets the running job go on for 10 s, then kills it
   (gone at 10.7 s, by design).
10. Hostile, as `aces`, from `/usr/share/aces/cad/tests/scripts/`: socket exit 5 (sandbox: `socket.getaddrinfo` is not
    available); loop `--timeout 5` exit 4 (timeout), and with `--timeout 3` no worker left 1 s later; memory exit 4
    (kind memory, RLIMIT_AS); the planted secret (a labelled fake value) exit 5 (sandbox: reading the file outside the
    allowed roots is refused) and the marker appears nowhere in the output; OCCT export outside exit 1, kind script,
    "Failed to write STEP file", and `~/escape.step` does not exist (Landlock refuses the C++ write; OCCT reports a
    write failure rather than a sandbox error, as the sandbox section says); kill parent exit 5.
11. 60 s loop: load 0.41 → 0.58, the worker at nice 10 on one core (98.9 %, 516 MB RSS), a foreground Python loop ran
    in 0.01 s; measured from an ssh shell, not at the Hyprland console.
12. Golden hand-off: `disc_domain.py` run `20261010T102204Z-09d1` (Ø100 × 40 mm, 314159.27 mm³), bundle
    `20261010T102209Z-disc`; `aces golden run --spec /tmp/rm.json --step <bundle>/part.step --project cad-gold --new`:
    exit 0 after 50 s, PASS, grade verified-desktop (run `20261010T102210Z-bc7391`): winner volfrac 0.23, rmin 11.55
    mm, mass 0.177 kg, vm_p99 54.47 ≤ 60 MPa, umax 0.0312 ≤ 0.035 mm; the conforming tet10 check at 2 mm on the VM
    (38,618 C3D10, 187,011 DOF, 29.5 s) agrees (vm_p99 −5.7 %, mass +0.0 %); `aces golden verify`: 11 PASS, rig and
    resolve INFO (not run); `aces ledger verify` ok (10 entries). The bench row is in `docs/BENCHMARKS.md`.
13. This section, the aces-cad section of `docs/PACKAGES.md`, and `reports/2026-10-10-aces-cad-vm/`.

The step list as planned:

1. On DARKNESS: `bash bin/build aces-cad` (and `bash bin/build aces-os`), then `.\serve-repo.ps1` (check the web root
   byte for byte against `repo\x86_64`). On the VM: `sudo pacman -Sy aces-cad`, then `sudo pacman -S aces-os`.
   aces-os 0.1.0-6 depends on `calculix-ccx-bin` (golden-rig's, which builds now and ships the Fortran runtime it
   needs) and on `aces-cad`; in clean containers on 2026-10-10 it resolved to 510 packages after the default
   install-test set and 522 after aces-base, aces-engineering and aces-cad. Record the installed versions
   (`pacman -Q aces-cad aces-engineering aces-base calculix-ccx-bin aces-os`).
2. `uname -r`, `cat /sys/kernel/security/lsm`, `aces-cad doctor --json > ~/cad-doctor.json`. Expect verdict `ready`,
   mode `strict`, seccomp active (106 instructions, TSYNC), Landlock active with its ABI recorded (4 or more adds the
   TCP rules, 6 or more the signal and abstract-socket scoping), every check passing, and Python 3.14.7, build123d
   0.11.1, OCP 7.9.3.1, ezdxf 1.4.4, lib3mf 2.5.0, trimesh 5.1.0 and the embree ray backend. If it is not ready, record
   the lsm line and stop: strict mode needs Landlock, and no agent code runs without it.
3. `aces-cad selftest`: every check PASS; record the wall time.
4. `aces project new cad-vm --class sovereign`.
5. `aces-cad run /usr/share/aces/cad/tests/scripts/aces_plate.py --project cad-vm --name plate`: 11607.300918301276 mm3
   within 1e-6, 60 x 40 x 5 mm, counts 1/1/7/15/10, valid; record the wall time.
6. `aces-cad inspect --project cad-vm --check mass,validity,holes,thickness --material AlSi10Mg`: one through Ø10 hole
   entering at (30, 20, 5), thickness min 5.0 within 0.01, mass 0.0310 kg, validity ok.
7. `aces-cad render --project cad-vm --view iso,front,cutaway` and `aces-cad drawing --project cad-vm`. Open the
   drawing's SVG in a browser and its DXF in a DXF viewer: the dimensions read 60, 40, 5, 30, 20 and Ø10 THRU (never
   6000), hidden lines are grey and dashed, and the Ø sign is clean.
8. `aces-cad pull --project cad-vm --json > ~/cad-pull.json`, then
   `python3 /usr/share/aces/cad/tests/check_bundle.py ~/cad-pull.json`, `aces ledger verify`,
   `aces ledger show --kind cad.pull --json` (the entry carries `class: sovereign`) and `aces project show cad-vm` (it
   lists `cad/local/<bundle>/part.step`). Run and pull again: the same `geometry_sha256`. Compare it with the CI value
   and record whether they are equal (cross-OS equality is not verified yet).
9. Agent: `aces-omarchy-setup` (the skill is linked as `~/.claude/skills/aces-cad`); add the `aces-cad` entry of
   `/usr/share/aces/cad/mcp.json.example` to Claude Code with `ACES_CAD_PROJECT=cad-vm`; `/mcp` lists 8 tools. Ask
   "make a 60 x 40 x 5 mm plate with a 10 mm hole in the middle, check it, show it, draw it, pull it" and record the
   tool calls, whether the render images reached the model, the total wall time and the bundle. Press Esc during a long
   run: `pgrep -f aces_cad/worker` must be empty within 3 s.
10. Hostile, as the desktop user, from `/usr/share/aces/cad/tests/scripts/`:
    - `aces-cad run hostile_socket.py --project cad-vm` exits 5;
    - `aces-cad run hostile_loop.py --project cad-vm --timeout 5` exits 4, and `pgrep -f aces_cad/worker` is empty;
    - `aces-cad run hostile_memory_list.py --project cad-vm` exits 4 with kind memory;
    - plant `~/.config/aces/onshape.env` (any marker value), then
      `aces-cad run hostile_read_secret.py --project cad-vm --param secret="$HOME/.config/aces/onshape.env"` exits 5;
    - `aces-cad run hostile_occt_export_outside.py --project cad-vm --param target="$HOME/escape.step"` fails, and
      `~/escape.step` does not exist;
    - `aces-cad run hostile_kill_parent.py --project cad-vm` exits 5.
11. During a 60 s loop (`hostile_loop.py --timeout 60`) the desktop stays responsive (2 threads, nice 10); note
    anything else.
12. Optional golden hand-off: run `disc_domain.py` and pull it; `aces golden spec --demo root-mount --out /tmp/rm.json`;
    `aces golden run --spec /tmp/rm.json --step <bundle>/part.step --project cad-gold --new`. Record the exit code and
    the grade (expected like the built-in demo domain; unverified until run).
13. Write the results here and into `docs/PACKAGES.md`, and copy the files listed above to `rehearsal/reports/`.

`post-install/aces-install.sh` runs `aces-cad doctor` and `aces-cad selftest` on a fresh install when aces-cad is
installed (steps 2 and 3 in short form).

## aces-console on the VM (2026-10-11)

The single ACES console (`aces-console` 0.1.0-2, with aces-base 0.1.0-4, aces-omarchy 0.1.0-7, aces-os 0.1.0-7),
built signed on DARKNESS and served by `serve-repo.ps1`. Script and outputs: `reports/2026-10-11-aces-console-vm/`.

1. `yes | sudo pacman -Sy aces-base aces-console aces-omarchy aces-os`: pacman asked to remove the conflicting
   `aces-consoles`; the answer was yes. Afterwards `aces-console 0.1.0-2`, no `aces-consoles`, one launcher entry
   (`/usr/share/applications/aces.desktop`), no GRID CONTROL, SIMLAB or MLflow launcher entries, the `[console]` table.
2. `aces-console start` from an ssh shell: health with all six modules installed, static tree from the package, bound to
   127.0.0.1:8070 only, pid and log under `~/.local/state/aces/console`.
3. Live reads from the VM: rig status, Mission Control agents/runs/missions/chats (through the rig's `/gc` read proxy:
   the VM holds no GRID CONTROL token), the 17 simlab-mcp tools, SIMLAB experiments, MLflow experiments, the registry,
   the VM's three projects, 16+ CAD runs, the golden runs, the desktop ledger verifying, decisions, red team, model
   staging; the three replaced consoles listed but not openable.
4. Human-only writes: a POST without the human headers 403, from another Origin 403, a write tool without its typed
   name 400, a read tool (`sim_whoami`) answering; the ledger stayed at 11 entries and `aces ledger verify` passed.
5. From DARKNESS through `ssh -L 8070:127.0.0.1:8070`: `verify_ui.py` and `verify_records.py` against the installed
   console PASS at 1440x900 and 390x844 (all eight tabs by keyboard, 0 console errors, no overflow).

Result: 33 PASS / 0 FAIL, both harnesses PASS. Not done: opening the console window inside the VM's own desktop
session. The VM sits at the Omarchy login screen (`vm-console-login-screen.png`) and its password is not typed by an
agent; after logging in, the "ACES" entry of the app launcher (or `aces-console open`) opens it. Writes to GRID
CONTROL (chats, missions, the switch, auto-switch, red team) need `~/.config/aces/gridcontrol.env` on the VM.

### Golden runs and CAD actions from the console (aces-console 0.1.0-4, 2026-10-11)

`reports/2026-10-11-aces-console-vm/vm-jobs-accept.sh` (6 PASS / 0 FAIL): through the console's own API, as its pages
do, a golden run of the built-in demo with `--lane none` (computed on the VM only) finished as
`20261011T122641Z-b3ac99` (pass, verified-desktop, 50.3 s, 48 progress lines; `aces golden verify` exit 0), and
`aces-cad` inspect and render ran on cad-vm's latest run. The ledger went from 13 to 17 entries and verifies. 0.1.0-3
refused the `lane` option; 0.1.0-4 fixes it. Both UI harnesses pass against the installed console.

