# similar-to-gologin — full project summary

**Date:** 2026-09-26
**Machine:** Windows 10 Pro 19045, i3-10105F, RTX 3060 Ti, 8 GB RAM
**Goal:** an antidetect product where the user installs a **stock, unpatched browser** — we spoof
the *environment underneath it*, never the browser itself.

---

## 0. The thesis (why this project exists)

GoLogin patches Chromium (Orbita). That means the browser is modified, and JS-level
detection (DataDome-class: `Function.prototype.toString`, Workers, iframes, timing) can
find the patch. We rejected that.

The user's framing, in their own words:

> "the browser call the api, we will become the api its simple"

> "instead of putting [the brain] back on human head and wiring all nerves, we build a
> human head with plugs similar to all original nerves and plug it there"

So: **the browser is native and untouched. We replace what it talks to.** Everything below
is measured, not theorized.

---

## 1. Approach 1 — Antidetect Windows VM (GPU partitioning)

Full Windows 10 guest per profile, on Hyper-V, with the real GPU passed through.

### Proven

| Claim | Result |
|---|---|
| GPU partitioning on Win10 19045 (not just Win11/Server) | **works** — `ValidPartitionCounts = 32` |
| Guest WebGL string vs bare metal | **byte-identical**: `ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti (0x00002489) Direct3D11 vs_5_0 ps_5_0, D3D11)` |
| Capability hashes match host | `extHash 298b24e9`, `precisionHash bc7aed29`, `MAX_TEXTURE_SIZE 16384`, `renderHash 120d1956` |
| Virtualization visible at WebGL layer? | **no** — guest's PCI device is `VEN_1414&DEV_008E` (Microsoft paravirt), yet ANGLE→DXGI reports NVIDIA's real `0x2489` |

### The problem this exposed

Without a GPU partition, everything collapses to one fingerprint:

| | renderer | canvas |
|---|---|---|
| host bare metal | `NVIDIA ... RTX 3060 Ti (0x2489) D3D11` | `13631a9d` |
| guest, no partition | `Microsoft Basic Render Driver (0x8C)` | `fe094356` |
| guest, SwiftShader | `SwiftShader Device (0xC0DE)` | `d8c276e6` |
| **host** `--disable-gpu` | `Microsoft Basic Render Driver (0x8C)` | **`fe094356`** |

A GPU-less **guest** and a GPU-less **host** produce the *same* canvas hash — software
rasterization is deterministic across machines. Every profile without a partition is
identical to every other one and to millions of other VMs. **GPU partitioning is mandatory.**

### The wall we hit

- `--use-angle` gives 4 distinct identities on bare metal (d3d11/gl/vulkan/swiftshader),
  but **inside a partition guest only `d3d11` works** — `gl`/`vulkan` fall back to
  `Microsoft Basic Render Driver` (no native ICD exists for the paravirt device).
- **Canvas 2D does not follow WebGL onto the GPU.** All these gave `fe094356` unchanged:
  `--enable-accelerated-2d-canvas --ignore-gpu-blocklist`,
  `+ --enable-gpu-rasterization --enable-zero-copy`,
  `+ --disable-software-rasterizer`.
  → This was the open problem: every profile on every host shares one canvas hash.
- Registry rename of `HardwareInformation.*` **failed**: PnP rebuilds those values from the
  INF on every boot, and DXGI reads through GPU-PV from the host anyway.
- `--gpu-testing-gl-renderer` / `--gpu-testing-vendor-id` are **ignored** by WebGL
  (byte-identical output with and without). Dead end.
- App-directory `dxgi.dll` redirection **failed**: Chromium calls
  `SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32)`; app-dir is ignored.

**Cost:** ~4 GB/profile, one Windows license/profile, plus a System32-hardening fight.

---

## 2. Approach 2 — Wine "synthetic Windows"

No Windows at all: stock **Windows** Chrome/Edge running under Wine in WSL2.

### Proven

```
userAgent            : Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...
platform             : Win32          <- the browser believes it is on Windows
webgl RENDERER       : ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (0xC0DE)), SwiftShader)
canvas2d hash        : 41d5bfb9
fonts.count          : 4              <- Wine ships almost no fonts
```

- **The illusion holds.** The browser knows the world only through Wine's APIs, and Wine
  says "Windows". No browser patch, no integrity fight — Wine's DLLs are *ours*.
- **Cost ~1 GB** vs 4 GB. No license (Wine is not Windows).
- `fonts.count = 4` is both a tell (too few) and the lever — the font set is entirely ours.
- Canvas hash `41d5bfb9` differs from Windows SwiftShader `d8c276e6` → the environment
  shifts the software-render hash.

**Not working OOTB:** real GPU through Wine (`--use-angle=gl` → `eglInitialize OpenGL
failed EGL_NOT_INITIALIZED`). Needs wined3d/vkd3d + Mesa dozen + Vulkan ICD. Deferred.

### The two approaches have distinct roles

| | Approach 1: VM + GPU-P | Approach 2: Wine |
|---|---|---|
| WebGL realism | REAL hardware | software renderer we own |
| Per-profile pixel control | limited to real HW output | **ARBITRARY** |
| License | one per profile | none |
| Cost/profile | ~4 GB | ~1 GB |
| Integrity fight | yes | none |
| Best for | borrowed realism | **full control / Task 1 + Task 2** |

---

## 3. The decisive result — "the driver socket"

The user's Task 1 + Task 2 (rename GPU; feed canvas from *outside* the browser), and their
rejection of the JS-override path, converged on one question:

> Can a **native, unmodified** browser be made to return our engine's pixels with **no**
> JS override, extension, CDP injection, or browser patch?

**Answer: yes — by replacing the graphics driver it renders through.**

Native Linux Chrome 154, headless, same probe, same payload. Only the driver changed:

| Renderer | `toDataURL` hash | External / replaceable? |
|---|---|---|
| Chrome's **bundled SwiftShader** | `46730522` | **NO** — compiled into the browser |
| **Mesa llvmpipe** (`--use-gl=angle --use-angle=gl`) | `33e11e69` | YES |
| **Mesa lavapipe** (`--use-angle=vulkan`) | `49972c82` | YES |

Renderer string on the Vulkan run proves Chrome left its own renderer:
`ANGLE (Mesa, Vulkan 1.4.335 (llvmpipe (LLVM 21.1.8 256 bits)...))`

### The critical requirement

**Chrome uses its OWN bundled SwiftShader by default when no usable GPU is present.**
In that mode no external driver is in the path and swapping Mesa does nothing — this is why
four different `GALLIUM_DRIVER` settings all produced the identical `46730522`.

The browser must be forced onto an external driver:
- `--use-angle=vulkan`, or
- `--use-gl=angle --use-angle=gl`

**That flag is the difference between the socket existing and not existing.**

### Why this beats the JS-override path

| | JS override (extension / CDP) | Engine as driver |
|---|---|---|
| DataDome-class detection | **yes** (toString, Workers, iframes) | **nothing to detect** |
| JS prototypes modified | yes | **none** |
| Arbitrary page content | only captured payloads | **any content** |
| Precomputing unknown payloads | required — unsolvable | **not required — the engine IS the rasterizer** |

**Architecture:**
```
Windows browser (native, unpatched)   <- Windows-ness: platform Win32, Windows UA
        | Windows graphics APIs
        v
Wine                                   <- proven: browser reports Win32
        | Linux GL / Vulkan
        v
OUR DRIVER (engine as Mesa/Gallium driver or Vulkan ICD)
        v
   RTX 3060 Ti pixels
```

### Engineering ahead
1. **Port engine JS → C.** `fingerprint.mjs` warns `Math.sin/cos/exp/log2` are V8-defined
   (V8 ships its own fdlibm port). Port fdlibm exactly or bytes drift.
2. **Fork a working driver** (llvmpipe / lavapipe); replace its rasterization with the
   engine's measured units — do not write a driver from scratch.
3. **Keep canvas GPU-accelerated**, else Skia rasterizes inside the browser and never
   reaches the driver.
4. The engine's existing units (texture filter, 26 blend modes, attribute interpolator,
   output converter, MSAA 4× resolve, shader math) **are** rasterizer components — natural fit.

---

## 4. The other Claude's engine (`pc antidtect saas/gputocpu.md`)

Byte-exact CPU replay of GPU output — proven independently:

- `verify-text`: **0 wrong of 2,448,000** pixels
- Engine FNV `89687606` == real GPU FNV `89687606`
- Speed: cold **1.36 ms** vs browser 0.80 ms; cached **0.0008 ms**; 736/sec
- **Native unpatched Edge on SwiftShader returned the 3060 Ti's exact pixels** with the
  extension: `getImageData` FNV `89687606` (== real GPU), `toDataURL` FNV `7b63fdde`
  (== real GPU, 5966 chars) vs `69958e75`/`5918` without.

**Two measured gaps:** shared ±1 interpolator plane constant; anisotropic filtering above 1×.

---

## 5. What is still open

| # | Item | State |
|---|---|---|
| 1 | Canvas 2D on the real GPU in a partition guest | open (Approach 1) |
| 2 | Real GPU through Wine | deferred |
| 3 | Engine ported to C, packaged as a driver | next engineering step |
| 4 | Per-profile launch at scale (N profiles, cost, orchestration) | not started |
| 5 | Timing: answer cold renders at ~0.8 ms | open — pacing helper exists |
| 6 | A **visible, interactive** Linux display for testing | **done** — see §7 |
| 7 | Stock *Windows* Chrome/Edge visible under Wine | blocked — see §8 |
| 8 | WebGL reporting a chosen card (AMD RX 580) | **done** — see §10 |

---

## 6. Infrastructure used

- **Hyper-V lab** — `tools/host/build-hyperv-lab.ps1`, `autounattend.xml`, `attach-gpu.ps1`
  (Gen 2, GPU-PV, `GuestControlledCacheTypes`, MMIO reservation, 2.6 GB driver staging)
- **Guest tooling** — `tools/guest/run-probe.ps1` (GUI-subsystem binary → must use
  `Start-Process -RedirectStandardOutput`), `apply-profile.ps1`
- **DXGI proxy** — `tools/gpu-descriptor/dxgi_proxy.cpp` patches adapter vtable slots
  8/10/11/18. Note: `.def` forwarders **do not link** with this toolchain; use
  `#pragma comment(linker, "/export:Name=dxgi_orig.Name,@N")`.
- **Probe** — `tools/fingerprint-probe.html`, 40+ signals, `?post=` POST-back collector
  (because a GUI-subsystem chrome.exe writes nothing to a pipe)
- **Wine** — `tools/wine/run-wine-probe.sh`, `linux_socket_test.sh`

### Environment gotchas worth keeping
- WSL2's VirtualMachinePlatform holds VT-x → VirtualBox falls back to NEM and crawls.
  Enable `Microsoft-Hyper-V-All` **before** rebooting.
- **WSLg renders windows blank** on this machine (`[WARN:COPY MODE]` — window created,
  never painted). Even `xeyes` is white. Working around it with **Xvfb + x11vnc + noVNC**.
- `x11vnc` refuses to start when `WAYLAND_DISPLAY` is set (`Wayland display server
  detected ... Exiting`) → must `unset WAYLAND_DISPLAY XDG_RUNTIME_DIR PULSE_SERVER`.
- A background task exiting tears down the WSL session and all its windows → hold it open.

---

## 7. A visible, interactive display — working

WSLg on this machine creates windows but **never paints** them (`[WARN:COPY MODE]`; even
`xeyes` renders white). Replaced with a real X server, confirmed live at
`http://localhost:6080/vnc.html`:

```
Xvfb :9 -screen 0 1440x900x24 -ac
fluxbox
x11vnc -display :9 -forever -shared -nopw -rfbport 5901   # requires WAYLAND_DISPLAY unset
websockify --web=/usr/share/novnc 6080 localhost:5901
```

**Working and navigable:** wallpaper, fluxbox taskbar with live clock, workspace
switching, real window frames. A Wine `notepad` maps and paints with full Windows chrome.

**And a browser, rendering through our driver:**

```
ANGLE (Mesa, Vulkan 1.4.335 (llvmpipe (LLVM 21.1.8 256 bits) (0x00000000)), llvmpipe)
```

Native Linux Chrome for Testing 154 with `--use-angle=vulkan` +
`VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.json` — window `IsViewable` at
1360x820, page loaded (title set by its own JS). Chrome is on the **external Mesa
driver, not its bundled SwiftShader**. That is the replaceable-driver socket, live and
visible. Only the Windows-ness has to come from Wine.

## 8. The Wine gap — SOLVED by Wine 11: stock Windows Edge maps and paints

Under Wine 10.0, Chrome 154 and Edge 153 **created a correctly-sized window but never
mapped it** (`Map State: IsUnMapped`, `_NET_CLIENT_LIST` empty). Notepad mapped fine, so
it was Chromium's windowing specifically, and no flag in a full sweep fixed it
(d3d11 crashed, gl wanted `WGL_NV_DX_interop2`, swiftshader/vulkan/disable-gpu all
unmapped, processes died 7→3→1).

**Wine 11.18 staging (WineHQ repo) maps Chromium.** Real Windows Edge 154.0.4258.37
runs VISIBLE and interactive on the Xvfb display: fluxbox manages
`about:blank - Profile 1 - Microsoft Edge`, the window paints with full Edge chrome,
and BrowserLeaks loaded inside it reporting **Windows UA (`Windows NT 10.0; Win64; x64
… Edg/154.0.0.0`)** — the synthetic-Windows illusion, live and clickable.
Evidence: `results/vm-display/edge-live.png`, launcher `tools/vm/start-edge-lab.sh`.

Known remaining limits (both expected, both separate work items):
- **WebGL is unavailable** in the visible Edge — the GPU process crash-loops under Wine
  (`exit_code=0x80000003`, `Failed to create shared context for virtualization`) and
  Chromium falls back to `--use-gl=disabled`. This is the deferred "real GPU through
  Wine" item, not a windowing regression. Headless Wine Chromium still produces the
  full fingerprint (`platform: Win32`, `canvas2d 41d5bfb9`).
- Edge's renderer under Wine reserves ~**1.6 TB of virtual memory**; with the old
  default VM size the WSL service itself died. Host RAM is actually 12 GB (not 8);
  `.wslconfig` = 6 GB VM + 4 GB swap absorbs the spike (peak seen: 5.7 GB at map time).

Lab ops, learned by breaking them:
- WSLg renders blank on this machine (`[WARN:COPY MODE]`) — Xvfb + x11vnc + noVNC
  instead, `http://localhost:6080/vnc.html`. `x11vnc` needs `WAYLAND_DISPLAY` unset.
- Every lab daemon must be `setsid nohup` — a plain `&` child dies with its driver
  script, and a dead Xvfb leaves a stale `/tmp/.X11-unix` socket that blocks the next
  Xvfb (clean it, then verify with `xdpyinfo`).
- `wsl.exe` mangles inline quoted commands from Git Bash — pass a script file or a bare
  binary (`wsl -d Ubuntu -- sleep 28800` is the keepalive).

## 9. Bottom line

Two viable architectures were built and measured on real hardware:

1. **VM + GPU partition** — real GPU, invisible at the WebGL layer, but ~4 GB/profile and
   canvas 2D stays on the CPU.
2. **Wine synthetic Windows** — ~1 GB, no license, browser believes it's on Windows, and
   *every* DLL in the render path is ours.

The decisive finding ties them together: **a native, unpatched browser will render through
whatever driver you give it, as long as you force it off its bundled SwiftShader.** That is
the socket the engine plugs into — no extension, no override, nothing in-page to detect.

## 10. The socket demonstrated — WebGL reports an AMD RX 580

The thesis was tested end to end: a native, unpatched Chrome for Testing 154, rendering
through external Mesa, now reports

```
ANGLE (AMD, Vulkan 1.4.335 (AMD Radeon RX 580 Series (RADV POLARIS10) (0x000067DF)), radv)
```

as its WebGL renderer, with no GL errors — the context really works. Three identity fields
had to agree (vendorID `0x1002`, deviceID `0x67DF`, deviceName, plus `radv` from
`VkPhysicalDeviceDriverProperties`), which is why a single string patch would not do.
Limits are deliberately left at the real driver's values.

Two things had to be true, and both are now known:

1. **Force Chrome off its bundled SwiftShader.** By default it uses a renderer compiled
   into the binary, where nothing on disk is in the path. `--use-angle=vulkan` moves it
   onto external Mesa — the file we can replace.
2. **Answer the driver query from below.** Done with an `LD_PRELOAD` interposer on
   `vkGetPhysicalDeviceProperties`/`…2`. The Vulkan *layer* route is correct in principle
   but closed here: this loader records its own library path for the layer and then
   deadlocks re-entering itself (a stub layer that returns NULL for everything hangs it
   identically), so it is a loader bug and not something in our code.

One pitfall is worth keeping: `dlsym(RTLD_NEXT, …)` resolved fine in a standalone probe
(which links libvulkan) and **returned NULL inside Chrome** (which `dlopen`s it), leaving
`VkPhysicalDeviceProperties` uninitialised — ANGLE read a garbage `apiVersion` and rejected
the device with "Requires a minimum Vulkan device version of 1.1". The rename appeared to
have broken Vulkan; it had broken the call. Resolve through `RTLD_NEXT` → `RTLD_DEFAULT` →
an explicit `dlopen("libvulkan.so.1")`, rejecting our own symbol.

Full detail, evidence, and repro: `results/RX580-METHOD.md`.
