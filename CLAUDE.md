# patchOS — Pisound Sound Pedal

This machine is a **Raspberry Pi guitar/sound pedal** built on a **Pisound HAT**
(Blokas) running **PatchBox OS**. It is a headless, single-purpose audio device:
audio + MIDI in, real-time DSP, audio out — controlled mostly by the Pisound's
single hardware button.

## Hardware

- **Board:** Raspberry Pi, `aarch64`, hostname `patchbox`
- **Audio HAT:** Pisound (`card 3`, ALSA id `pisound`) — stereo analog audio I/O +
  DIN MIDI in/out + a physical button and knob
- Onboard audio (`vc4hdmi0/1`, `Headphones`) exists but is **not** the audio path;
  the Pisound is the pedal's sound card.
- User `patch` is in the `audio`, `jack`, `gpio`, `i2c`, `spi`, `input` groups.

## OS & audio stack

- **OS:** PatchBox OS on Debian 12 (bookworm), kernel `6.6.20+rpt-rpi-v8`
- **Audio:** JACK is the central server (`jack.service`), with PipeWire bridging.
  ALSA underneath. Default: **48 kHz**, **128-sample** blocks, **2 periods**
  (~5.3 ms latency). Buffer is set in **`/etc/jackdrc`** (`-p 128 -n 2`), which
  `jack.service` execs directly — edit there (sudo) to change latency. 128 is
  the stable sweet spot for Pianoteq; 64 causes xruns under polyphonic play.
  After editing jackdrc, restart: `jack` → `pianoteq9` → `setbfree` → `padmap`.
  Watch for xruns with `journalctl -u jack.service | grep -i xrun`.
- Check the JACK server with `jack_control status`; JACK MIDI with `jack_lsp -c`.

## Patchbox modules

PatchBox runs one "module" at a time, selected via the `patchbox` CLI.
Installed/imported modules: **puredata**, **modep**, **orac**.
Current active module: **none** (`/etc/patchbox-active-module` is empty).
Manage with `patchbox module list` / `patchbox module activate <name>`.

## The Pisound button (primary UI)

Button actions are configured in **`/usr/local/etc/pisound.conf`**, mapping
click-counts and hold-durations to scripts in
`/usr/local/pisound/scripts/pisound-btn/`. Current bindings:

- **1 click** → start Pure Data   - **2 clicks** → stop Pure Data
- **hold 1s** → toggle Bluetooth discoverable
- **hold 3s** → toggle WiFi hotspot   - **hold 5s** → shutdown

Daemon: `pisound-btn.service`. Backups in `/usr/local/pisound-ctl-backups/`.

## Active synth stack (set up 2026-07-04)

FluidSynth was replaced with **Pianoteq 9** (physical-modeling piano) +
**setBfree** (Hammond B3 organ). Three systemd services manage everything.

### Pianoteq 9 (`pianoteq9.service`)

- **Binary:** `/opt/Pianoteq 9/Pianoteq 9`
- **Mode:** headless JACK client + JSON-RPC API server
- **Service flags:** `--headless --serve 127.0.0.1:8999` (no `--preset` — padmap
  applies the saved preset on startup via RPC)
- **Audio:** JACK client → `system:playback_1/2` (Pisound output)
- **MIDI:** JACK MIDI port `Pianoteq:midi_in` ← `padmap_piano:padmap_piano`
- **JSON-RPC:** `POST http://127.0.0.1:8999/jsonrpc`; key methods:
  `loadPreset(name)`, `getListOfPresets()`, `getInfo()`, `setParameters()`
- **Prefs:** `~/.config/Modartt/Pianoteq92.prefs` — `voices=24`,
  `voices_thresh=70` (shed early to avoid xruns under heavy chords)
- **stdin trick:** `sleep infinity | Pianoteq 9 --headless …` keeps stdin pipe
  open; without it Pianoteq exits immediately on EOF from the terminal.
- Restart: `sudo systemctl restart pianoteq9`

### setBfree Hammond organ (`setbfree.service`)

- **Engine:** jalv (LV2 host) running `http://gareus.org/oss/lv2/b_synth`
- **LV2_PATH:** includes `/var/modep/lv2`
- **Audio:** JACK ports `setBfree:outL/R` → `system:playback_1/2`
- **MIDI:** JACK MIDI port `setBfree:control` ← `padmap_organ:padmap_organ`
- **RT priority:** jalv started with `chrt --rr 79` for real-time scheduling
- **Note:** jalv also needs `sleep infinity |` to keep stdin open
- Restart: `sudo systemctl restart setbfree`

### FluidR3 GM sampler (`fluidr3.service`)

Sampled orchestral instruments — added for a realistic **violin** (ZynAddSubFX
only has thin synth violins).

- **Engine:** `fluidsynth -is -a jack -m jack` with `/usr/share/sounds/sf2/FluidR3_GM.sf2`
- **JACK ports:** MIDI in `fluidr3:midi_00`; audio `fluidr3:left/right`
  (auto-connected to `system:playback` via `audio.jack.autoconnect=1`)
- **MIDI is JACK-only** (`-m jack`) — it deliberately does NOT open an ALSA-seq
  port, so keyboards can't auto-grab it (the old FluidSynth bug). See
  [[project_fluidsynth_disabled]].
- **Instruments** exposed in the web UI catalog are the strings family, selected
  by **GM Program Change** (the pad's `preset` is the program number):
  Violin 40, Viola 41, Cello 42, Tremolo 44, Pizzicato 45, Harp 46,
  String Ensemble 48, Slow Strings 49.
- **`synth.cpu-cores=1` is important** — `cpu-cores=2` spawns non-RT worker
  threads that caused ~4× more xruns (50 vs 11 per 12 s). Polyphony 32.
- Volume: responds to **CC7** like setBfree/Zyn.
- Restart: `sudo systemctl restart fluidr3`

### bristol ARP Odyssey (`bristol.service`)

Vintage synth emulation for the Herbie Hancock / Head Hunters lead. bristol models
the **actual ARP Odyssey** (it also has `-mini` Mini Moog, `-rhodes`, `-b3`,
`-arp2600`, `-prophet` — effectively Herbie's whole rig).

- **Launcher:** `/usr/local/bin/bristol-start.sh` → `startBristol -odyssey -jack
  -register bristolody -voices 6 -gain 2`
- **Needs a virtual X display.** bristol's engine only instantiates an emulation
  when its GUI (`brighton`) connects over TCP, so the script runs **Xvfb on :99**.
  Engine-only (`-gui`, meaning *don't* start the GUI) registers no JACK ports.
  Both idle cheaply — measured ~4–6 xruns/15 s, same as baseline.
- **JACK ports:** `bristolody:out_left/out_right`, `bristolody:midi_in`
- **No audio auto-connect.** Unlike zyn/fluidr3, bristol does NOT wire its own
  output to `system:playback` — without that it plays silently. Both the start
  script and padmap's connection watcher now maintain
  `bristolody:out_left/right → system:playback_1/2`.
- **Patches:** 8 factory memories (1, 11–15, 21, 88) in
  `/usr/share/bristol/memory/odyssey/`, selected by **Program Change** — the
  pad's `preset` is the memory number.
- **It is HOT.** Even at `-gain 2` the patches clip at unity, so the bristol
  presets are seeded to a **42 % per-preset gain** (see `preset_gains`).
- **Gotcha:** loading a memory is **asynchronous and resets the patch volume**, so
  a Program Change would wipe our CC7. `activate_pad` therefore re-applies the
  level ~1.8 s after the program change. Without that it clips (or goes silent).
- Volume: responds to **CC7**.
- Restart: `sudo systemctl restart bristol`

### padmap — pad switcher + web UI (`padmap.service`)

The central controller: intercepts MIDI from all keyboards, routes to the active
instrument, and serves the web control panel.

- **Script:** `/home/patch/.local/bin/padmap.py` (python3-rtmidi, JACK API)
- **Config:** `~/.config/pianoteq-pad/config.json` (atomic write on every change)
- **Web UI:** `http://patchbox.local:8080` / `http://192.168.0.59:8080` /
  `http://172.24.1.1:8080` (hotspot)

**JACK MIDI topology** (pure JACK, no ALSA-seq routing):
```
system:midi_capture_1..6  ──►  padmap_in:padmap_in
                               (padmap.py processes MIDI)
padmap_piano:padmap_piano  ──►  Pianoteq:midi_in
padmap_organ:padmap_organ  ──►  setBfree:control
```
A background thread checks connections every 60 s and calls `jack_connect`
only when a connection is actually missing (avoids the xruns that batching
26 blind jack calls every 15s caused). Uses `rtmidi.API_UNIX_JACK` so the
virtual ports appear directly in JACK (not ALSA-seq).

**Pad layout** (MPK pads remapped to notes 100–107 via SysEx):
| Row | Notes | Default |
|-----|-------|---------|
| Top (pads 5–8) | 104 105 106 107 | Rhodes / Wurlitzer / Clavinet / Steinway |
| Bottom (pads 1–4) | 100 101 102 103 | Organ / — / — / — |

On a pad press: if switching instrument type (piano↔organ), sends all-notes-off
to the outgoing instrument; then calls `loadPreset` via JSON-RPC (Pianoteq) or
just reroutes MIDI (setBfree — no preset concept needed).

**Volume knobs (both live on the Akai/MPK):**
- **Top knob = CC1** → Akai (upper) volume
- **2nd knob = CC5** → Casio (lower) volume

Each knob sets the volume of whichever engine that keyboard currently plays.
Pianoteq ignores CC7 (its MIDI map is "Minimalistic"), so its volume is set via
JSON-RPC `setParameters` on the `volume` param (normalized `cc/127`); setBfree and
Zyn take **CC7** channel volume directly. Levels are stored per keyboard
(`akai_vol`/`casio_vol` in config.json), re-applied on pad switch and at startup,
and persisted **debounced** (2 s after the knob stops — never per-tick, to avoid
the SD-write storm that causes xruns).

**Per-preset level trim:** each assigned pad has a level slider (0–200 %, default
100) in the web UI. It is stored in `preset_gains` keyed by **`type:preset`** —
i.e. against the *sound*, not the pad slot — so moving an instrument to a
different pad carries its trim with it. Final level = **dial × gain/100**, so the
Akai/Casio knobs still scale everything on top. `POST /api/gain {pad, gain}`.

**Volume must be re-asserted after a preset switch.** Some engines reset their own
volume when a preset finishes loading, and that load is **asynchronous** —
measured: **Pianoteq clobbers it ~0.25 s after `loadPreset` returns** (the preset
carries its own stored volume, e.g. 0.727), and **bristol ~1.5 s** after a memory
change. Setting the level once at switch time loses that race, so a pad would
sometimes play at the patch's own (loud) level — and pressing the same pad again
"fixed" it. `_reapply_volume_later()` therefore re-sends the level at
**0.35 / 0.9 / 1.8 / 3.2 s** after every switch, guarded by a generation counter
so a newer switch — or a triple-tap fade — supersedes it. Zyn and FluidR3 do
*not* reset (CC7 survives), but they get the same treatment harmlessly.

**Triple-tap fade-in (upper pads):** triple-tapping an upper pad (≤0.6 s between
taps) activates that preset *and* arms a fade — the Akai keyboard is silenced
immediately and, from the first note played, ramps 0 → dial volume over
**10 s** (`FADE_SECONDS`). A single tap is unaffected (plays at full volume). The
fade is cancelled by turning the Akai volume dial, switching presets, or another
triple-tap. Implemented with a generation counter so a stale fade thread stops
when superseded.

**HTTP API:** `GET /api/state`, `GET /api/catalog`;
`POST /api/activate {pad}`, `POST /api/assign {pad, type, preset, name}`.

Restart: `sudo systemctl restart padmap`

## WiFi hotspot fallback (`wifi-fallback.service`)

If the Pi can't join a known WiFi network at boot, it starts its own access point
so the padmap web UI stays reachable in the field (no router needed).

- **Script:** `/usr/local/bin/wifi-fallback.sh` (oneshot, `After=NetworkManager`)
- **Logic:** wait up to 45 s for `wlan0` to connect to a saved network; if it
  doesn't, run `nmcli con up pb-hotspot`. If WiFi *does* connect, it does nothing.
- **Hotspot:** NetworkManager profile `pb-hotspot` — SSID **`patchbox`**,
  password set in the NM profile (not in this repo), AP mode,
  `ipv4.method=shared` at **172.24.1.1/24**
  (NM runs its own dnsmasq for DHCP).
- **Web UI on the hotspot:** `http://172.24.1.1:8080` (padmap binds `0.0.0.0`).
- Once the hotspot is up it stays up until reboot (it won't yank you off mid-edit
  if home WiFi reappears). The Pisound button 3 s hold still toggles it manually.
- Test the decision without switching networks:
  `sudo TIMEOUT=5 DRYRUN=1 /usr/local/bin/wifi-fallback.sh`
- Note: NetworkManager is the live network stack. The legacy
  `pisound-btn/enable_wifi_hotspot.sh` uses hostapd/dhcpcd (both inactive) — use
  `nmcli con up pb-hotspot` instead.

## MIDI routing (amidiminder)

amidiminder's role is now minimal — JACK MIDI routing handles everything.
Rules in `/etc/amidiminder.rules`:
- Block keyboards from ALSA-seq auto-connect (JACK is the active path)
- Block generic `RtMidi*` client names from auto-connecting

## MPK pad note remapping (one-time setup, persists in flash)

MPK pad notes were remapped via SysEx from factory defaults (44–51) to 100–107
so they sit above any keyboard note (MPK keys top out at 84 with OCT+). Written
to MPK flash **program 1**; retained across power cycles as long as program 1 is
selected. To re-apply: `python3 /tmp/mpk_remap.py`.

## Installed sound software

Pure Data (`pd`), SuperCollider, Audacity, Pianoteq 9 — desktop launchers in
`~/Desktop/*.desktop`. Pure Data settings: `~/.pdsettings` (JACK in/out, 48 kHz,
128-block, `-alsamidi -rt`). No user `.pd` patches on disk yet.

## MODEP — disabled

MODEP (modep-mod-host, modep-mod-ui, modep-amidithru, modep-browsepy) was
consuming ~88% CPU via a rogue `mod-host` JACK client, causing constant xruns.
All four modep services are now **stopped and disabled**. Do not re-enable them
while the Pianoteq+setBfree stack is active.

## RT audio tuning (applied 2026-07-05)

To eliminate xrun static in Pianoteq grand piano playback:
- **SD card write pressure** was causing periodic kernel IRQ latency spikes →
  xruns every 2–3 s. Fixed by:
  - `vm.dirty_writeback_centisecs=3000` / `vm.dirty_expire_centisecs=3000`
    in `/etc/sysctl.d/99-audio-rt.conf`
  - `journald Storage=volatile` in
    `/etc/systemd/journald.conf.d/99-audio-rt.conf` (logs in RAM only;
    cleared on reboot — check `journalctl -u jack.service` won't persist)
- **jalv** (setBfree) started with `chrt --rr 79` for real-time scheduling
- **Pianoteq voices** reduced to 16, voices_thresh to 55

## Working on this device — notes

- This is **not a git repo** and is a live audio appliance. Changes to
  `/usr/local/etc/pisound.conf`, the button scripts, or JACK settings affect the
  pedal's real-time behavior — back up before editing (see backups dir above).
- Editing system files under `/usr/local/` and `/boot/firmware/` needs `sudo`.
- Pisound tooling lives in `/usr/local/pisound/` (`pisound-btn`, `pisound-config`,
  scripts) and `/usr/local/pisound-ctl/`. `pisound-config` is the setup TUI.
- After changing `pisound.conf`, restart: `sudo systemctl restart pisound-btn`.
- Pisound docs/README: `/usr/local/pisound/README.md`.
