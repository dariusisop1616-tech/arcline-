# Stock Edge under Wine that matches native Windows Edge

How a **stock, unmodified Windows Edge 154.0.4258.37**, running under Wine 11.18 in
WSL2, was made to report the same fingerprint as native Edge on the same PC — and how
its WebGL identity is set to any card. Measured 2026-09-28.

**Rule:** the browser is never modified. No extensions, no JS injection, no CDP, no
patched binaries. Every change is in Wine, the DLLs underneath the browser, the
registry, or the process environment.

---

## 1. Result

`tools/fingerprint-probe.html` (63 signals) was run in native Windows Edge and in Wine
Edge — the same build on both sides — and the outputs diffed.

| stage | identical | different |
|---|---|---|
| software renderer (llvmpipe) + fake GPU identity | 29 | 35 |
| real GPU (Direct3D 11 on the RTX) | 48 | 15 |
| + host fonts, Microsoft DirectWrite, ClearType | 52 | 11 |
| + RAM, languages, screen | 57 | 6 |
| + exact GPU name, taskbar size | 60 | 3 |
| + 48 kHz audio | 61 | 2 |
| **+ camera device** | **63** | **0** |

Measurement tooling: `tools/relay/collect.py` serves the probe over http and collects
results (one collector reachable from both sides — a `file://` page cannot POST to
`localhost`). `fp-diff.sh` / `diff-one.sh` produce the diff.

---

## 2. The layers, one by one

### 2.1 GPU — Edge on the real graphics card

WSL2 exposes the Windows GPU through `/dev/dxg` (GPU paravirtualization). Mesa's
**`d3d12` Gallium driver** runs OpenGL on top of it, so `glxinfo` on the virtual display
reports `D3D12 (NVIDIA GeForce RTX 3060 Ti)`, hardware accelerated. Ubuntu's Mesa has no
Vulkan-on-D3D12 driver, so the GPU is reached through OpenGL.

Chain:

```
Edge (ANGLE, Direct3D 11 backend — the Windows default)
  -> Wine d3d11 / dxgi -> wined3d (renderer = gl)
  -> Mesa d3d12 -> /dev/dxg -> Windows GPU driver -> the real card
```

| setting | value |
|---|---|
| `HKCU\Software\Wine\Direct3D\renderer` | `gl` |
| environment | `GALLIUM_DRIVER=d3d12`, no `LIBGL_ALWAYS_SOFTWARE` |
| Edge flags | `--use-angle=d3d11 --in-process-gpu --disable-gpu-watchdog` |

**`--in-process-gpu` is the key.** With Chromium's normal separate GPU process, the GPU
process crashed on every start (`exit_code 0x80000003` = STATUS_BREAKPOINT, then
`Failed to create shared context for virtualization`). A separate GPU process must share
textures with other processes through D3D11 shared handles, which Wine's d3d11 does not
provide. Running the GPU code inside the browser process avoids shared handles entirely;
with it there were zero crashes.

**Pixels are byte-identical to native.** The same test program (`tools/relay/d3d11_test.cpp`,
`d3d11_hard.cpp`, shader bytecode precompiled with `fxc` and embedded) was run natively
and under Wine:

| probe | native Windows | Wine -> real GPU | Wine -> software |
|---|---|---|---|
| shader math (sin/cos/exp2/log2/rsqrt) | `9d262fa2` | **`9d262fa2`** | `fe9f3ca3` |
| mipmaps + 16× anisotropic + blending | `5f4c9dc5` | **`5f4c9dc5`** | `a1109dc5` |

The translation layers do not change the bytes because the same hardware units do the
math, filtering and rounding. This is vendor-neutral: Mesa's d3d12 driver talks to
whatever Windows GPU driver is installed (NVIDIA, AMD, Intel).

This fixed the whole WebGL group at once: `MAX_VIEWPORT_DIMS` 32767, `ALIASED_LINE_WIDTH`
1-1, extension list and counts, precision, WebGL render/toDataURL hashes, and WebGPU
(vendor nvidia, architecture ampere). Those values are the Direct3D 11 backend's own
answers, which is why a name-only spoof on another backend can never be consistent.

### 2.2 Canvas text — Microsoft's text engine

Tested against native Edge: running native Edge with `--disable-gpu` changes **every**
canvas 2D hash, so canvas 2D is GPU-rendered on Windows. But letters are first turned
into small bitmaps by **DirectWrite** on the CPU, and only then composited by the GPU.
So text needs both the real GPU and Microsoft's rasterizer.

`tools/relay/split.html` hashes each canvas component separately:

| component | Wine's DirectWrite | Microsoft DirectWrite, grayscale | Microsoft DirectWrite + ClearType |
|---|---|---|---|
| gradient (GPU only) | match | match | match |
| Arial 15 px text | differs | differs (less ink) | **match** |
| Times New Roman 17 px | differs | differs | **match** |
| single glyph, 15 and 40 px | differs | differs | **match** |
| emoji 15 and 40 px | differs | **match** | **match** |

| setting | value |
|---|---|
| `system32\dwrite.dll` | copied from the host's `C:\Windows\System32\dwrite.dll` |
| DLL override | `WINEDLLOVERRIDES=...;dwrite=n` |
| `HKCU\Control Panel\Desktop` | `FontSmoothing=2`, `FontSmoothingType=2` (ClearType), `FontSmoothingGamma=0`, `FontSmoothingOrientation=1` — the host's values; Wine defaulted to type 1 (grayscale) |

### 2.3 Fonts

| step | why |
|---|---|
| copy the host's `C:\Windows\Fonts` files into the prefix's `C:\windows\Fonts` | the prefix had 12 families, Windows 45 |
| **import the host's `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts` key** (`reg export` on Windows, `wine regedit /S` in the prefix) | DirectWrite — what Chromium uses — enumerates fonts from this key. Copying files alone left the browser at 12 families even though GDI saw them |
| drop Wine's font cache (`HKCU\Software\Wine\Fonts`) once | Wine trusts a cached font list |
| `FONTCONFIG_FILE` pointing to a config that lists only the prefix font folder | hides distro-only families. Only **Ubuntu** was a real leak — the host itself has DejaVu Sans, Liberation Serif and Roboto |

Font metrics (`textMetrics`) matched as soon as the right typefaces were present.

### 2.4 RAM (`navigator.deviceMemory`)

Chromium derives it from Windows' physical-memory figure, which Wine takes from Linux
`sysinfo()`. A small `LD_PRELOAD` shim (`/root/winelab/mem/memshim_env.so`) reports
`FAKE_MEM_KB` instead. Bind-mounting a fake `/proc/meminfo` does **not** work — Wine
does not read it for this.

### 2.5 Languages and locale

- `LANG=fr_FR.UTF-8` for the Wine process (locale generated with `locale-gen`) — gives
  Edge's French UI and the Windows locale.
- Profile `Default/Preferences` preset with
  `intl.accept_languages` = `fr,fr-FR,en,en-GB,en-US` (the host's list).
- Do **not** pass `--lang=fr` / `--start-maximized`: that pair makes Edge exit silently
  under Wine (found by testing one change at a time).

### 2.6 Screen

- Virtual display (Xvfb) at the target size, e.g. 1920×1080.
- A bottom bar reserving exactly 40 px so `availHeight` = 1040 like the Windows taskbar.
  The window manager (fluxbox) adds a 1 px border on each side, so it is configured with
  height 38.

### 2.7 Audio

`AudioContext.sampleRate` follows the audio server Wine connects to. WSLg's shared server
runs at 44.1 kHz; Windows reports 48 kHz. Each workspace runs its own PulseAudio
(`tools/relay/setup-audio.sh`) with a 48 kHz "Speakers" sink and a "Microphone" source,
selected with `PULSE_SERVER`.

### 2.8 Camera

The host's only video device was **OBS Virtual Camera**, a DirectShow filter. The same
`obs-virtualcam-module64.dll` was copied to the same path in the prefix and registered
with `wine regsvr32`, as OBS's installer does (`tools/relay/setup-camera.sh`). Before
permission is granted, `enumerateDevices` only reveals which device kinds exist, so one
registered video source is enough.

---

## 3. How the WebGL info is changed

ANGLE on Direct3D 11 builds the renderer string from what DXGI reports about the adapter:

```
ANGLE (<vendor from VendorId>, <Description> (0x<DeviceId, 8 hex digits>) Direct3D11 vs_5_0 ps_5_0, D3D11)
```

The vendor word comes from the vendor ID (0x10DE -> NVIDIA, 0x1002 -> AMD, 0x8086 ->
Intel). `UNMASKED_VENDOR_WEBGL` becomes `Google Inc. (<vendor>)`. So three things have
to be set: vendor ID, device ID, description.

| part | where it comes from | how it is set |
|---|---|---|
| vendor ID, device ID | Wine registry overrides | `HKCU\Software\Wine\Direct3D`: `VideoPciVendorID`, `VideoPciDeviceID` (REG_DWORD) |
| description (card name) | Wine's built-in GPU table **inside `wined3d.dll`**, looked up by those IDs | rename the entry in place with `tools/relay/set_gpu_name.py` |

Details that matter:

- **Which file:** on the OpenGL path the live table is in `wined3d.dll`. The same strings
  also exist in `win32u.so`, but that copy was not used here (tested by renaming one file
  at a time).
- **Commas:** Chromium removes commas from the description. Wine's entry for 0x2489 is
  `NVIDIA GeForce RTX 3060 Ti (GA104, Low Hash Rate)`, which appeared as
  `... (GA104 Low Hash Rate)`. It was renamed to `NVIDIA GeForce RTX 3060 Ti`.
- **Length:** the new name is written into the old one's slot and padded with zero bytes,
  so it must not be longer than the existing entry (about 50 characters — enough for any
  real card name). The script writes to a temp file and swaps it in atomically.
- **Unknown device IDs:** if the device ID is not in Wine's table, Wine falls back to a
  default card for that vendor and changes the device ID too (an unknown NVIDIA ID became
  `GTX 470 (0x06CD)`). Use an ID that exists in the table, then rename its entry if needed.
- **Per workspace:** each workspace has its own hard-linked copy of the Wine install with
  its own real `wined3d.dll`, so each workspace can have its own name. `WINEDLLPATH` is
  not honoured for this DLL, and loading the prefix's copy with `wined3d=n` breaks
  Direct3D.
- **Verification without the browser:** `d3d11_test.exe` prints the DXGI adapter name and
  ID in a few seconds (`tools/relay/ws-verify.sh`).

Verified in three workspaces:

| workspace | registry IDs | name | resulting WebGL renderer |
|---|---|---|---|
| ws1 | 0x10DE / 0x2489 | renamed to `NVIDIA GeForce RTX 3060 Ti` | `ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti (0x00002489) Direct3D11 vs_5_0 ps_5_0, D3D11)` — identical to native |
| ws2 | 0x10DE / 0x1B06 | table entry as-is | `ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 Ti (0x00001B06) Direct3D11 vs_5_0 ps_5_0, D3D11)` |
| ws3 | 0x1002 / 0x7590 | table entry as-is | `ANGLE (AMD, AMD Radeon RX 9060 XT (0x00007590) Direct3D11 vs_5_0 ps_5_0, D3D11)` |

**Limit:** the name changes, the pixels do not. All three workspaces rendered the same
test image hash (`9d262fa2`) because they all draw on the same RTX. A workspace that
claims a different card while drawing RTX pixels is inconsistent to any service that
keeps per-card hash databases. Different pixels per workspace need the CPU engine
(option 2), not this method.

*Superseded approach:* before the real-GPU path worked, a wrapper Vulkan driver
(`tools/vulkan-layer/rx580_icd.c`) rewrote the device properties on the software renderer.
It could set any name and IDs, but the string came out in Vulkan format
(`ANGLE (NVIDIA, Vulkan 1.3.303 (...))`), and limits/extensions were the software
renderer's, not a real Windows card's.

---

## 4. Running it

| file (in `tools/relay/`) | purpose |
|---|---|
| `edge-rtx.sh probe <label>` / `open <url>` | single-lab launcher with every fix; `probe` runs the fingerprint diff |
| `ws-setup.sh` | builds workspaces from the validated prefix: hard-linked prefix and Wine clones, per-workspace IDs, name, screen, RAM, fonts config |
| `ws-start.sh <ws>` | starts one workspace: display, noVNC, audio, Edge |
| `ws-all.sh` / `ws-keep.sh` | start all / supervise running workspaces |
| `ws-stop.sh <ws...>` | stops workspaces completely |
| `ws-status.sh`, `ws-verify.sh`, `ws-procs.sh` | status, GPU identity check, memory per workspace |
| `set_gpu_name.py` | renames a GPU table entry |
| `setup-audio.sh`, `setup-camera.sh` | per-workspace audio server, OBS camera registration |

Edge flags used:

```
--no-sandbox --no-first-run --no-default-browser-check --disable-extensions
--disable-sync --disable-component-update --renderer-process-limit=1
--in-process-gpu --use-angle=d3d11 --disable-gpu-watchdog
--window-position=0,0 --window-size=<W>,<H minus taskbar> --user-data-dir=C:\ws
```

Workspace access: noVNC at `http://localhost:6080/6081/6082/vnc.html`. Each workspace
has its own display, so each user gets an independent mouse and keyboard.

---

## 5. Open problems

- **RAM.** One Edge under Wine measured about 6.4 GB (RAM + swap) across 22 processes;
  renderers hold 750–920 MB each and reserve ~1.5 TB of address space. Three workspaces
  on this 12 GB PC thrashed, and Edge's network service crashed in two of them. The cause
  of the per-process overhead is not established yet: an early "Wine page-table" theory
  is doubtful because compressed RAM only achieved 1.5×. Swap was moved to D: (16 GB)
  and compressed RAM (zram) added; reduction tests are next.
- **Signals the probe does not cover yet:** WebRTC (fingerprint.com's TURN host failed to
  resolve inside Wine), client-hint Windows version, speech voices, permissions.
- **Licensing:** `dwrite.dll`, Windows fonts and the OBS module are copied from the user's
  own Windows install. That is fine on their own machine; shipping those files to
  customers is not permitted.
- **One shared GPU:** real-GPU workspaces on one PC share one pixel fingerprint (section 3).
