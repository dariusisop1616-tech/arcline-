# SaaS: shipping, updates, packaging, and protection

A plan for turning the working lab into a product. Nothing here is built yet;
this is the design to build against. It assumes the two verified modes:
**real-GPU** (byte-identical to native Windows) and **engine** (CDP injection of
captured pixels), both running on our patched Wine.

---

## 1. Two ways to sell it

| | **A. Hosted (server-side)** | **B. Installed (customer PC)** |
|---|---|---|
| where workspaces run | your servers | the customer's Windows |
| customer connects by | browser / RDP | local app |
| GPU for real-GPU mode | your server's GPU | the customer's own GPU |
| protection | total - nothing ships | strong but not absolute (see §5) |
| cost to you | your hardware + bandwidth | almost none |
| best for | most customers, teams | customers who need their own real GPU fingerprint |

Recommendation: **lead with A**, offer B for the real-GPU-on-own-card case.
The rest of this plan covers both; §5 is mostly about B, since A has little to
protect on the client.

---

## 2. What actually ships (the combined installable)

For mode B, one signed installer that contains everything, so the customer never
assembles parts:

```
installer.exe
 ├─ enables WSL2 + the vGPU bits if missing (one-time, needs a reboot)
 ├─ drops a single distro image  (our Ubuntu, pre-built)
 │    └─ /opt/wine-ours          our patched Wine
 │    └─ /opt/lab                display stack, audio, scripts (read-only)
 │    └─ /opt/agent              the licensing/agent binary (closed source)
 ├─ a thin Windows launcher      (tray app: login, start/stop workspaces, open viewer)
 └─ registers an auto-update service
```

The distro image is built once by us (not assembled on the customer's machine),
version-stamped and hashed. The customer's data (profiles, cookies) lives in a
separate encrypted volume, never inside the image, so updates can replace the
image without touching their data.

**Building the image** (our side, reproducible):
- a build script exports a tarball: patched Wine + lab + agent + a *bare* Wine
  prefix (no fonts/dwrite - those are added per-profile at first run from the
  customer's own Windows, to stay within Microsoft's license on their machine).
- `wsl --import` that tarball on the customer machine. It is just a filesystem;
  no per-customer compilation.

---

## 3. Shipping updates

Three independent update channels, so a fix to one part does not force a full
re-download:

| channel | contents | how | rollback |
|---|---|---|---|
| **agent** | the licensing/agent binary | tiny, signed, checked at every launch | keep previous, revert on hash mismatch |
| **runtime** | patched Wine + lab image | a squashfs layer, mounted read-only over the base | atomic: mount new, swap symlink, keep old layer |
| **profiles/rules** | GPU tables, fingerprint recipes, engine data | fetched from your server per session, never on disk | server-side, instant |

Design rules:
- **The image is read-only and layered.** An update is a new layer, applied
  atomically (build beside the live one, then flip a symlink). If it fails to
  start, flip back. This is how Proton/Steam and Android ship runtime updates.
- **Never break the fingerprint on update.** Every runtime update must re-pass
  the probe (63/63) in CI on our side before it is published. Pin the Wine
  version; a silent Wine change can move a signal.
- **Version negotiation.** The agent tells the server its runtime + agent
  versions; the server decides what profile data to send and can refuse to serve
  an outdated or tampered runtime.
- **Staged rollout.** Publish to a canary ring first, watch for probe failures
  and crashes reported by agents, then widen.

---

## 4. What we protect vs what we cannot

Be honest about the line, because the whole protection design depends on it.

| asset | protectable? | how |
|---|---|---|
| fingerprint profiles (the GPU/font/screen combinations) | **yes** | live only on your server; sent per session, encrypted, expiring |
| the engine (captured GPU data + replay) | **yes** | same - server-side, per-session, never fully on the client |
| licensing / who may run it | **yes** | short-lived tokens tied to account + machine, revocable |
| orchestration, dashboard, your own code | **yes** | closed source, server-side for mode A |
| our Wine *patches* | **no (legally)** | Wine is LGPL; if shipped, the Wine changes must be available. Not your moat anyway - a few hundred lines. |
| anything decrypted in the client's RAM | **no (ultimately)** | a customer who is admin of their own Windows can dump the WSL VM's memory from outside; nothing inside can stop it |

The last row is the hard limit (same for Orbita, same for all DRM). The strategy
is therefore: **make cracking expensive, and make what a cracker gets worthless.**

---

## 5. The protection system (mode B)

Layers, outermost first. Each one raises the cost; together they stop everyone
short of a determined reverse-engineer, and §5.6 makes even that person's prize
small.

### 5.1 Sealed, signed runtime
The distro image and the agent are signed. The agent verifies the image's hash
before starting anything; a modified image does not run. Binaries stripped and
obfuscated.

### 5.2 License + online-only
- The agent authenticates to your server and receives a **short-lived token**
  (minutes), tied to the account and a machine fingerprint.
- **Online-only:** the runtime periodically re-checks in; if it cannot, it stops.
  This kills offline cracking (nothing decrypts without the server) and lets you
  **revoke** a leaked copy or shared account within minutes.
- Rate/geo/device-count checks server-side flag and auto-revoke abuse.

### 5.3 Encrypted, per-session payloads
- Profile and engine data are encrypted and signed, delivered per session,
  decrypted **only in memory**, and expire.
- Keys are per session and per profile, issued by the server, never stored.
- The runtime takes its settings (GPU name, fonts, RAM, screen, language) **from
  the agent over a local channel, not from files or registry a user could read**.
  (This changes how the GPU-name patch should be written - see §6.)

### 5.4 Pinned, doubly-encrypted channel
TLS with certificate pinning, plus a second encryption layer inside it, so a
proxy tool (mitmproxy/Fiddler) sees nothing useful. Casual sniffing is dead.

### 5.5 Anti-tamper on the client
Anti-debugging (detect tracers, timing checks), integrity checks that stop the
workspace on tampering, no secrets in static strings. Stops ordinary reverse
engineering; slows the serious kind.

### 5.6 Make the prize worthless (defense in depth)
Since a determined admin can still read memory, ensure a memory dump yields
little:
- **Per session, expiring:** a dump works for one profile, this session only.
- **Server-side crown jewels:** the full profile DB and engine stay on your
  server; a session holds only what it needs.
- **Traceable payloads:** watermark delivered data per account, so a leak names
  the leaker and gets them banned.

### What each attacker gets

| attacker | result |
|---|---|
| copies the files to another PC | won't run (no license, hash check) |
| opens files for your profiles/engine | only encrypted data |
| shares their account | revoked server-side within minutes |
| sniffs the network | pinned + double-encrypted: nothing useful |
| attaches a debugger | anti-debug stops the workspace |
| dumps the WSL VM memory as admin | gets **one** profile's data, watermarked, expiring |

Mode A removes the entire right-hand column: nothing runs on their machine.

---

## 6. One decision to make now

The protection design (§5.3) wants the runtime to take its per-profile settings
**from the agent over a local socket**, not from registry values or files a user
can read. That is also the clean way to set the WebGL identity we discussed.

So the next Wine patch should:
- read GPU vendor / device / name (and later the engine's mode) from an
  environment or a local handshake the agent controls, **not** from
  `wined3d.dll` bytes or public registry keys.
- become the single seam where both the fingerprint identity and, later, the
  engine plug in per session.

Building it that way now means the same patch serves today's GPU-naming and
tomorrow's protected, engine-fed product, with no rework.

---

## 7. Build order (suggested)

1. **Agent v0:** login, token, launch a workspace with settings passed in. No
   encryption yet - just move settings out of files into the agent's hands.
2. **Wine settings patch (§6):** identity from the agent, not from disk.
3. **Hosted mode (A):** put the whole stack on one server, sell access. This is
   shippable earliest and needs the least protection work.
4. **Packaging (§2) + update channels (§3)** for mode B.
5. **Protection layers (§5)** hardened in, online-only first, then encryption,
   then anti-tamper.
6. **Engine mode** wired through the same agent seam.

Legal cleanup (fonts/dwrite licensing, the LGPL Wine-source offer, browser
terms) is deferred until revenue justifies it, per your call - noted so it is not
forgotten, not because it blocks building.
