# Our own Wine build, and why each patch exists

Companion to `edge.md` (which explains how the fingerprint was made to match).
This file covers the **build** and the **memory work**: what we compile, what we
changed in Wine's source, what each change measured, and what failed.

Wine is LGPL and actively developed; we do **not** rewrite it. We build the exact
version we validated and patch the few places we measured. Everything here is
outside the browser, which is never modified.

---

## 1. Building

`tools/wine-build/build-wine.sh [deps|source|configure|make|install|all]`

- **Version:** Wine **11.18** + wine-staging 11.18 (`patchinstall.py --all`) - the
  same version as the stock `/opt/wine-staging` we validated at 63/63, so the
  result is comparable.
- **Low-RAM mode:** `-j2`, `-O2 -g0` (no debug info), `--enable-archs=x86_64`,
  `nice -n 15`. Peak usage stayed near **1 GB**; a full build took **28 min**
  compile + 2 min install on 8 threads. Incremental rebuilds take **9-45 s**.
- **Installs to `/opt/wine-ours`**, leaving `/opt/wine-staging` untouched so any
  measurement can be repeated against stock.
- **Feature parity matters:** the first configure silently dropped FFmpeg,
  Wayland, OpenCL, SANE, gphoto, PCSC, Samba and ODBC. The dependency list now
  includes them, and `install` diffs our module list against stock (identical).

Gotchas found:
- `patchinstall.py` needs `autoreconf` (autoconf/automake), otherwise staging
  fails with `FileNotFoundError: 'autoreconf'`.
- `wineboot -u` hangs in this lab (with or without fonts). New builds would
  trigger it, so each prefix is pinned with `echo disable > .update-timestamp`.
- `make install` writes **new inodes**. A workspace whose tree was hard-linked
  earlier silently keeps running the old build - `ws-use-ours.sh` now stops that
  workspace's wineserver, re-links, and `cmp`s the binary to prove it updated.

Workspaces select the build with `WINE_TREE=<tree>`; `ws-use-ours.sh <ws>` makes a
hard-linked clone of `/opt/wine-ours` per workspace (near-zero disk) with its own
`wined3d.dll` carrying that workspace's GPU name (stored in `ws.conf` as
`GPU_NAME_FROM/TO`, quoted with `%q` - card names contain parentheses).

---

## 2. Why Edge is heavy under Wine

Same binaries, same page, same flags:

| | native Windows | under Wine (before the work) |
|---|---|---|
| Edge | 344 MB, 5 processes | ~4.5-6.5 GB |
| Chrome | 403 MB, 6 processes | ~6 GB |

So Edge really is lighter than Chrome; the multiplier is Wine's. Reading a live
renderer's memory (`tools/relay/inv-blocks.py`, `inv-pe.py`) found two causes:

1. **A private copy of `msedge.dll` in every process.** The DLL is **335 MB** with
   `FileAlignment 0x200`. Wine can only `mmap` a PE image whose sections are
   page-aligned; otherwise `map_file_into_view()` falls back to `pread()` into
   anonymous memory - one full copy per process (a 270 MB block of x86 code
   bytes, `0x48 0x89 0x0f`, in each renderer). Windows shares one copy.
2. **~368 MB of resident all-zero pages** per renderer (6142 of 6144 sampled
   pages entirely zero). What writes them is still not pinned down.

A third, earlier theory - Wine's per-page protection table for Chromium's ~1.5 TB
of reservations - was **not** confirmed; it was cited before it was tested.

---

## 3. What we changed

### Patch 0001 - aligned image cache  ✅ kept
`dlls/ntdll/unix/virtual.c`. The first process that maps such an image writes a
page-aligned, pre-relocated copy of its sections into
`/var/tmp/wine-image-cache/`; every process then `mmap`s that file
copy-on-write. Pages live once in the page cache and are shared by all processes
mapping it at the same address; pages a process writes become private, as on
Windows. `WINE_IMAGE_CACHE=0` disables it for A/B runs.

Measured on one workspace: **2713 MB → 1018 MB**, and **321 MB** with KSM as well.

### Patch 0002 - stable image base  ❌ reverted (crashed Edge)
`server/mapping.c`: make `assign_map_address()` return 0 so every process maps an
image at its PE header base, giving one cache file for all workspaces.
**It crashed Edge** with a page fault at `0x1700616D8`: returning 0 means "use the
preferred address", but when that address is taken Wine maps the image elsewhere
and then skips relocation, so the process runs code relocated for the wrong base.
Left in the source behind a renamed, off-by-default variable as a record.

### Patch 0003 - key the cache on the actual address  ✅ kept
The cache was keyed on a *predicted* address (`map_addr`, else the header base).
0002 exposed that as unsound. It is now keyed on `view->base`, the address the
image really occupies, and the relocation delta uses the same value. Correct in
every case.

### Patch 0004 - base-independent cache  ❌ reverted (slower)
Store the image unrelocated, one cache file per DLL regardless of address, and
let Wine's normal relocation run in each process. Correct, and workspaces did
share one file - but relocation dirties the pages it touches, so memory got
**worse**: 526 MB + 980 MB for two workspaces.

### Patch 0005 - back to the per-address cache  ✅ kept
Reverts 0004. Sharing a file only pays off if relocation can be skipped, and
skipping it is only correct when every process agrees on the address.

**Current set: 0001 + 0003 + 0005** (0002 disabled, 0004 reverted).

---

## 4. Memory results

One workspace = display + audio + Wine + one Edge on a real page.

| configuration | 1st workspace | 2nd workspace |
|---|---|---|
| stock behaviour (cache off, KSM off) | 2713 MB | - |
| + patch 0001 | 1018 MB | - |
| + KSM | 321 MB | - |
| patches 0001+0002+0003 (unsafe) | 187 MB | +170 MB |
| patches 0001+0004 (safe, worse) | 526 MB | +980 MB |
| **current set (safe)** | **~450-540 MB** | **~370-385 MB** |

Numbers move between runs (page cache, settle time), so treat them as a range.

**KSM** (Kernel Samepage Merging) is enabled per workspace: our `LD_PRELOAD` shim
calls `PR_SET_MEMORY_MERGE`, and `/sys/kernel/mm/ksm` runs with `use_zero_pages`.
On the unpatched build it merged 1401 MB and collapsed 614 MB of zero pages, at
**0.3 % CPU** at 1000 pages / 200 ms (2 % at the aggressive test rate). It matters
less now that the image is file-backed, since KSM only merges anonymous memory.
Note: kernel threads such as `ksmd` are invisible inside the WSL PID namespace -
measure their CPU from `/proc/stat`.

**Host changes:** WSL swap moved to `D:\wsl\swap.vhdx` (16 GB) and the distro disk
to `D:\wsl\ubuntu`, taking C: from 1.1 GB to ~25 GB free.

### Still open
- **Cross-workspace sharing.** Each workspace still caches its own copy of
  `msedge.dll`, because wineserver randomises the address per server. The fix is a
  deterministic allocator that reserves the same address in every workspace -
  0002's shortcut is not it.
- **The zero pages.** ~368 MB per renderer, cause not yet identified.
- **Process count.** Edge runs 9-13 processes under Wine; the policies in
  `tools/relay/edge-policies.sh` remove several background services.

---

## 5. Things the browser would otherwise reveal

- **Window manager title bar.** fluxbox drew its own title bar above the browser,
  which Windows never does. `tools/relay/windows-look.sh` writes an apps rule
  (`[Deco] {NONE}`), giving a 1920x1040 window at 0,0 with `availHeight` unchanged.
- **"Unsupported command-line flag: --no-sandbox".** Real Edge never shows that
  yellow bar. Removed with Edge's own policy
  `CommandLineFlagSecurityWarningsEnabled=0`, together with policies that disable
  the Copilot sidebar, shopping, rewards, background mode and telemetry
  (`tools/relay/edge-policies.sh`).
- **Desktop looks.** `tools/relay/windows-taskbar.sh` restyles the taskbar to a
  flat dark Windows-like bar with a clock, keeping the reserved height exact.

---

## 6. Input: is it real?

`tools/relay/mouse.html` measures what a site can see, driven along an identical
circular path natively and in a workspace (`mouse-native.ps1`, `mouse-wine.sh`).

| signal | native Windows | workspace | same |
|---|---|---|---|
| untrusted events | 0 | 0 | yes |
| clicks trusted | 2 / 2 | 2 / 2 | yes |
| pointerType / pressure / width | mouse / 0 / 1 | mouse / 0 / 1 | yes |
| fractional coordinates | 0 | 0 | yes |
| missing `movementX/Y` | 0 | 0 | yes |
| `maxTouchPoints`, `pointer:fine`, `hover`, dpr | 0, true, true, 1 | same | yes |
| event rate | 41.7 Hz | 51.1 Hz | differs |
| largest gap between events | 2314 ms | 43 ms | differs |

Every *kind* of property matches; only the rhythm differs, and it differed
between runs on the same machine (the native run stalled 2.3 s). Clicks arrive as
`isTrusted=true` with correct client/screen coordinates and `document.hasFocus()`.

**Isolation:** with two workspaces open, moving ws1's pointer produced 49 events
in ws1 and **none** in ws2; each display has its own pointer. (ws1's counter rose
by 9 while ws2 was being moved - most likely its own queued events arriving late,
since the reverse direction showed nothing at all.)

**Caveat worth knowing:** over VNC/RDP the event *rate* comes from the remote
protocol, which coalesces motion, not from Wine. Sites that profile mouse
dynamics see the protocol's rhythm.

---

## 6b. GPU memory, scaling, and the file dialog

### Dedicated VRAM per workspace: effectively zero

A Windows RDP session is reported to cost ~1 GB of VRAM each. Measured here:

| step | dedicated VRAM |
|---|---|
| no workspace | 1094 MiB |
| one workspace live on a WebGL page | 1093 MiB |
| two workspaces | 1085 MiB |

To prove the measurement is not simply blind to WSL, `vram_alloc.exe` allocates
40 textures of 2048x2048 (**640 MB**) and clears each one (a texture that is only
created, never touched, is not committed - the first version of this test
measured nothing for that reason):

| same binary | dedicated VRAM delta |
|---|---|
| run natively on Windows | **+651 MiB** (the tool works) |
| run inside a workspace | **+11 MiB** |

Windows' own per-process GPU counters (dedicated and shared) did not move for the
workspace run either. So the GPU work really does not consume dedicated VRAM the
way a native app or an RDP session does; the screen itself is a software
framebuffer in ordinary RAM. Where WSL accounts that memory is not fully
established - worth revisiting if VRAM ever becomes the limit.

### A second browser in the same workspace saves nothing

| | cost |
|---|---|
| second browser inside one workspace (own profile) | **+440 MB** |
| second workspace (own display, audio, prefix) | **+385 MB** |

The browser dominates; the workspace wrapper is ~150 MB. Separate workspaces cost
the same and are properly isolated, so there is no reason to share one.

### File upload behaves exactly like Windows

Clicking a file input opens a real Open dialog, and the site receives:

```
name test.txt   size 26   type "text/plain"
lastModified 2026-01-02T03:04:05.000Z
isTrusted true   File/Blob instances correct
```

Every field a site can read - including the MIME type, which comes from the
registry, and the timestamp - matches what Windows would report
(`tools/relay/upload.html`, `upload-wine.sh`).

## 7. Any browser, not just Edge

The host's installed **Opera GX 136** (Chromium 152) ran under Wine unmodified and
inherited the whole environment:

| | Opera GX in a workspace | native Windows Edge |
|---|---|---|
| canvas2d toDataURL | `13631a9d` | `13631a9d` |
| canvas2d gradient | `a85aab38` | `a85aab38` |
| webgl renderHash | `120d1956` | `120d1956` |
| GPU string | RTX 3060 Ti, Direct3D11 | identical |

It also picked up the workspace's RAM (8), 1920x1080 screen and 48 kHz audio on
its own. The fingerprint comes from the **environment**, not the browser, so users
can bring their own Chromium-based browser. Opera is heavier: 13 processes,
3.5-4.9 GB before the patches were measured against it.
