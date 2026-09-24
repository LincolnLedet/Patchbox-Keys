# Patchbox-Keys

A headless **Raspberry Pi keyboard/sound workstation** built on a
[Pisound](https://blokas.io/pisound/) HAT running
[Patchbox OS](https://blokas.io/patchbox-os/). Plug in one or more MIDI
keyboards, route each to a different software instrument (LV2 plugins, Pianoteq,
samplers, vintage-synth emulations), and control everything from a phone-friendly
web UI — no screen or mouse needed.

Runs on a Raspberry Pi 4 (aarch64) with the Pisound as the sound card. JACK is
the real-time audio core (48 kHz, 128-sample buffer).

## What it does

- **Plug-and-play MIDI routing.** A single controller (`padmap.py`) intercepts
  every keyboard over JACK MIDI and routes each to whichever instrument its
  currently-selected pad points at. Keyboards are re-connected automatically when
  plugged/unplugged.
- **Multiple sound engines**, each an independent instrument:
  | Engine | Sound | Selected by |
  |--------|-------|-------------|
  | Pianoteq 9 | Physical-modeled pianos / EPs / clav | JSON-RPC preset name |
  | setBfree | Hammond B3 organ | (routing only) |
  | FluidR3 GM (fluidsynth) | Sampled strings / orchestral | GM Program Change |
  | bristol | ARP Odyssey vintage-synth lead | patch memory number |
  | ZynAddSubFX | Subtractive-synth patches | `.xiz` file |
- **Web control panel** at `http://patchbox.local:8080` — assign sounds to pads,
  set per-preset volume trim, browse the catalog. Served inline by `padmap.py`
  (no separate web server).
- **Hardware controls:** Akai/MPK pads switch presets; two knobs set upper/lower
  volume; triple-tapping an upper pad arms a 10-second fade-in.
- **WiFi hotspot fallback** so the web UI stays reachable in the field with no
  router.

## Repository layout

```
bin/
  padmap.py              # THE controller: MIDI router + web UI (HTML/CSS/JS
                         # are embedded inline as the PAGE string) + JACK
                         # connection watcher. This one file is the app.
scripts/
  bristol-start.sh       # launches bristol under a virtual X display (Xvfb)
  wifi-fallback.sh       # brings up the AP hotspot if no known WiFi at boot
systemd/
  padmap.service         # runs padmap.py
  pianoteq9.service      # Pianoteq headless + JSON-RPC
  setbfree.service       # Hammond organ (jalv LV2 host)
  fluidr3.service        # FluidR3 GM sampler
  bristol.service        # ARP Odyssey emulation
  zyn.service            # ZynAddSubFX
  wifi-fallback.service  # oneshot hotspot fallback
config/
  jackdrc                # JACK buffer settings (-p 128 -n 2)
  config.example.json    # example pad/volume state (live copy is gitignored)
CLAUDE.md                # full engineering notes: every engine, gotcha, and
                         # tuning decision, with rationale
```

### Where the web UI lives

There is **no separate HTML file**. The entire control panel (markup, CSS, and
JavaScript) is an inline string, `PAGE = r"""..."""`, inside
[`bin/padmap.py`](bin/padmap.py), served at `GET /`.

## Install (on a Patchbox OS device)

Paths in the service files assume the device layout below. Adjust if yours differs.

```bash
# controller
install -Dm755 bin/padmap.py ~/.local/bin/padmap.py

# launcher scripts
sudo install -Dm755 scripts/bristol-start.sh /usr/local/bin/bristol-start.sh
sudo install -Dm755 scripts/wifi-fallback.sh /usr/local/bin/wifi-fallback.sh

# systemd units
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jack padmap pianoteq9 setbfree fluidr3 bristol zyn wifi-fallback

# JACK buffer
sudo cp config/jackdrc /etc/jackdrc      # then: sudo systemctl restart jack

# starting pad/volume state
mkdir -p ~/.config/pianoteq-pad
cp config/config.example.json ~/.config/pianoteq-pad/config.json
```

**Not included / device-specific** (you supply these): the Pianoteq 9 binary and
license (commercial), the FluidR3 GM soundfont, bristol/ZynAddSubFX packages, and
the `pb-hotspot` NetworkManager profile (set its password with
`nmcli con modify pb-hotspot wifi-sec.psk '<yourpw>'`). The hotspot password is
intentionally **not** stored in this repo.

## Notes

- This is a **live audio appliance**, not a portable library — the service files
  hard-code device paths. Treat it as a documented backup + reference for
  rebuilding the device, and read `CLAUDE.md` for the reasoning behind every
  non-obvious choice (xrun tuning, async volume races, MIDI routing).
