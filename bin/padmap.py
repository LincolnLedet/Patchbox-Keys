#!/usr/bin/env python3
"""
padmap — dual keyboard instrument router + web UI (:8080)

AKAI MPK (top pads 104-107)  → controls Akai keyboard sound
CASIO keyboard (bottom pads 100-103) → controls Casio keyboard sound
Both keyboards play simultaneously through independent instrument paths.

JACK topology:
  system:midi_capture (MPK)   → padmap_akai:padmap_akai
  system:midi_capture (CASIO) → padmap_casio:padmap_casio
  padmap_piano:padmap_piano   → Pianoteq:midi_in
  padmap_organ:padmap_organ   → setBfree:control
  padmap_zyn:padmap_zyn       → zynaddsubfx:midi_input
"""

import json, os, socket, struct, subprocess, threading, time, glob
from http.server import HTTPServer, BaseHTTPRequestHandler
import rtmidi

CONFIG_DIR  = os.path.expanduser("~/.config/pianoteq-pad")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
PIANOTEQ_ADDR = ("127.0.0.1", 8999)
ZYN_PORT_FILE = "/tmp/zyn-osc-port"
ZYN_BANK_DIR  = "/usr/share/zynaddsubfx/banks"

AKAI_PADS  = set(range(104, 108))   # top row  → Akai keyboard sound
CASIO_PADS = set(range(100, 104))   # bottom row → Casio keyboard sound
PAD_NOTES  = AKAI_PADS | CASIO_PADS

# FluidR3 GM sampled instruments (fluidr3.service). "preset" = GM program number,
# selected by sending a Program Change to fluidr3. Focused on the strings family.
FLUID_INSTRUMENTS = [
    ("Violin",            40, "solo strings"),
    ("Viola",             41, "solo strings"),
    ("Cello",             42, "solo strings"),
    ("String Ensemble",   48, "orchestral"),
    ("Slow Strings",      49, "orchestral"),
    ("Tremolo Strings",   44, "orchestral"),
    ("Pizzicato Strings", 45, "plucked"),
    ("Orchestral Harp",   46, "plucked"),
]

# Akai knobs → per-keyboard volume. Each sends CC7 (channel volume) to whichever
# engine that keyboard currently plays, so it works for Pianoteq/setBfree/Zyn alike.
# bristol ARP Odyssey (bristol.service). "preset" = memory number, selected by
# Program Change. The real ARP Odyssey model — Herbie's Head Hunters lead.
BRISTOL_MEMORIES = [
    ("ARP Odyssey 1",  1),
    ("ARP Odyssey 11", 11),
    ("ARP Odyssey 12", 12),
    ("ARP Odyssey 13", 13),
    ("ARP Odyssey 14", 14),
    ("ARP Odyssey 15", 15),
    ("ARP Odyssey 21", 21),
    ("ARP Odyssey 88", 88),
]

CC_VOL_AKAI  = 1   # top knob  → Akai (upper) volume
CC_VOL_CASIO = 5   # 2nd knob  → Casio (lower) volume

DEFAULT_CONFIG = {
    "akai_pad":  "107",
    "casio_pad": "100",
    "akai_vol":  100,
    "casio_vol": 100,
    # Per-preset level trim, percent (100 = unity). Keyed by "type:preset" so the
    # trim follows the SOUND, not the pad slot. Final level = dial × gain/100.
    "preset_gains": {},
    "pads": {
        # Akai top row — controls what Akai keyboard plays
        "107": {"type": "pianoteq", "preset": "",                  "name": "Piano"},
        "104": {"type": "pianoteq", "preset": "MKII Stage EP",     "name": "Rhodes"},
        "105": {"type": "pianoteq", "preset": "W2 Basic Stereo",   "name": "Wurlitzer"},
        "106": {"type": "pianoteq", "preset": "Clavinet D6 Stage", "name": "Clavinet"},
        # Casio bottom row — controls what Casio keyboard plays
        "100": {"type": "setbfree", "preset": "",                  "name": "Organ"},
        "101": {"type": None,       "preset": "",                  "name": ""},
        "102": {"type": None,       "preset": "",                  "name": ""},
        "103": {"type": None,       "preset": "",                  "name": ""},
    },
}

config = {}
_lock  = threading.Lock()


def load_config():
    global config
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(CONFIG_PATH) as f:
            loaded = json.load(f)
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["pads"].update(loaded.get("pads", {}))
        config["preset_gains"].update(loaded.get("preset_gains", {}))
        for key in ("akai_pad", "casio_pad", "akai_vol", "casio_vol"):
            if key in loaded:
                config[key] = loaded[key]
    except (FileNotFoundError, json.JSONDecodeError):
        config = json.loads(json.dumps(DEFAULT_CONFIG))


def save_config():
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ── Pianoteq JSON-RPC ────────────────────────────────────────────────────────

def pianoteq_rpc(method, params=None):
    body = json.dumps({
        "jsonrpc": "2.0", "method": method,
        "params": params or {}, "id": 1,
    }).encode()
    req = (
        b"POST /jsonrpc HTTP/1.0\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    try:
        with socket.create_connection(PIANOTEQ_ADDR, timeout=3) as s:
            s.sendall(req)
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.split(b"\r\n\r\n", 1)[1])
    except Exception as e:
        return {"error": str(e)}


def load_preset_async(preset_name):
    if preset_name:
        threading.Thread(
            target=pianoteq_rpc,
            args=("loadPreset",),
            kwargs={"params": {"name": preset_name}},
            daemon=True,
        ).start()


def get_preset_catalog():
    catalog = []

    # setBfree organ
    catalog.append({"name": "Hammond Organ", "bank": "Organ", "itype": "setbfree"})

    # FluidR3 GM sampled instruments — "preset" is the GM program number (as str).
    for name, prog, label in FLUID_INSTRUMENTS:
        catalog.append({"name": name, "bank": f"FluidR3 GM · {label}",
                        "itype": "fluid", "preset": str(prog)})

    # bristol ARP Odyssey memories — "preset" is the memory number (Program Change)
    for name, mem in BRISTOL_MEMORIES:
        catalog.append({"name": name, "bank": "bristol · ARP Odyssey",
                        "itype": "bristol", "preset": str(mem)})

    # ZynAddSubFX banks (show only key banks to keep list manageable)
    SHOW_BANKS = ["Strings", "Choir and Voice", "Pads", "Brass", "Guitar", "Misc"]
    for bank in SHOW_BANKS:
        bank_dir = os.path.join(ZYN_BANK_DIR, bank)
        if not os.path.isdir(bank_dir):
            continue
        for xiz in sorted(glob.glob(os.path.join(bank_dir, "*.xiz")))[:20]:
            name = os.path.basename(xiz)
            name = name[5:].replace(".xiz", "") if len(name) > 5 and name[:4].isdigit() else name.replace(".xiz", "")
            catalog.append({"name": name.strip(), "bank": f"Zyn / {bank}", "itype": "zyn", "path": xiz})

    # Pianoteq presets — Pianoteq names them by model code (W2, MKII, …), so tag
    # each with a real-instrument label that's shown AND searchable.
    # Only include LICENSED presets (license_status == "ok"); the rest are
    # demo-locked and won't actually play, which just clutters the picker.
    for p in pianoteq_rpc("getListOfPresets").get("result") or []:
        if isinstance(p, dict):
            if p.get("license_status") not in (None, "ok"):
                continue
            name = p.get("name", "")
        else:
            name = p
        catalog.append({"name": name, "bank": _pianoteq_label(name), "itype": "pianoteq"})

    return catalog


# Preset-name prefix → friendly instrument label (searchable + displayed).
# Longest prefixes first so "NY Steinway" wins over "Steinway".
PIANOTEQ_LABELS = [
    ("MKII",         "Rhodes Mark II · electric piano"),
    ("MKI",          "Rhodes Mark I · electric piano"),
    ("W2",           "Wurlitzer 200A · electric piano"),
    ("W1",           "Wurlitzer 120 · electric piano"),
    ("Pianet",       "Hohner Pianet · electric piano"),
    ("Electra",      "Electra Piano · electric piano"),
    ("Clavinet",     "Clavinet"),
    ("NY Steinway",  "Steinway D (New York) · grand piano"),
    ("HB Steinway",  "Steinway D (Hamburg) · grand piano"),
    ("Steinway",     "Steinway · grand piano"),
    ("C. Bechstein", "Bechstein · grand piano"),
    ("Bösendorfer",  "Bösendorfer · grand piano"),
    ("Blüthner",     "Blüthner · grand piano"),
    ("Grotrian",     "Grotrian · grand piano"),
    ("Petrof",       "Petrof · grand piano"),
    ("Shigeru",      "Shigeru Kawai · grand piano"),
    ("Steingraeber", "Steingraeber · grand piano"),
    ("Grand",        "grand piano"),
    ("Concert Harp", "Harp"),
    ("Celtic",       "Celtic Harp"),
    ("Vibraphone",   "Vibraphone · mallets"),
    ("Marimba",      "Marimba · mallets"),
    ("Xylophone",    "Xylophone · mallets"),
    ("Glockenspiel", "Glockenspiel · mallets"),
    ("Celesta",      "Celesta · mallets"),
    ("Kalimba",      "Kalimba"),
    ("Steel",        "Steel Drum"),
    ("Spacedrum",    "Spacedrum"),
    ("Tank",         "Tank Drum"),
    ("Hand",         "Handpan"),
    ("Toy",          "Toy Piano"),
]


def _pianoteq_label(name):
    for prefix, label in PIANOTEQ_LABELS:
        if name.startswith(prefix):
            return label
    return ""


# ── ZynAddSubFX OSC control ──────────────────────────────────────────────────

def _osc_str(s):
    """Encode an OSC string: one null terminator, then pad to a 4-byte boundary."""
    b = (s.encode() if isinstance(s, str) else s) + b'\x00'
    return b + b'\x00' * ((4 - len(b) % 4) % 4)


def _osc_message(path, *args):
    tags = ','
    vals = b''
    for a in args:
        if isinstance(a, str):
            tags += 's'
            vals += _osc_str(a)
        elif isinstance(a, bool):        # bool must precede int (bool is an int)
            tags += 'T' if a else 'F'
        elif isinstance(a, int):
            tags += 'i'
            vals += struct.pack('>i', a)
        elif isinstance(a, float):
            tags += 'f'
            vals += struct.pack('>f', a)
    return _osc_str(path) + _osc_str(tags) + vals


def zyn_load(xiz_path):
    # ZynAddSubFX 3.x OSC: /load_xiz <int part> <string path> (part 0 = channel 1).
    try:
        port = int(open(ZYN_PORT_FILE).read().strip())
        msg = _osc_message('/load_xiz', 0, xiz_path)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.sendto(msg, ('127.0.0.1', port))
        s.close()
    except Exception:
        pass


# ── JACK MIDI ports ──────────────────────────────────────────────────────────

JACK = rtmidi.API_UNIX_JACK

akai_in   = rtmidi.MidiIn(JACK,  "padmap_akai")
casio_in  = rtmidi.MidiIn(JACK,  "padmap_casio")
piano_out = rtmidi.MidiOut(JACK, "padmap_piano")
organ_out = rtmidi.MidiOut(JACK, "padmap_organ")
zyn_out   = rtmidi.MidiOut(JACK, "padmap_zyn")
fluid_out = rtmidi.MidiOut(JACK, "padmap_fluid")
brist_out = rtmidi.MidiOut(JACK, "padmap_bristol")


def jrun(*args):
    subprocess.run(list(args), capture_output=True)


def _jack_connections():
    try:
        out = subprocess.run(['jack_lsp', '-c'], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return set()
    pairs, src = set(), None
    for line in out.splitlines():
        if line.startswith(' '):
            if src:
                pairs.add((src, line.strip()))
        else:
            src = line.strip()
    return pairs


def _jack_ports():
    """Set of all ports that currently exist in the JACK graph."""
    try:
        out = subprocess.run(['jack_lsp'], capture_output=True, text=True, timeout=3).stdout
        return {l.strip() for l in out.splitlines() if l.strip()}
    except Exception:
        return set()


def _find_capture_port(keyword):
    """Return system:midi_capture_N whose JACK alias contains keyword."""
    try:
        out = subprocess.run(['jack_lsp', '-A'], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    cur = None
    for line in out.splitlines():
        if not line.startswith(' '):
            stripped = line.strip()
            cur = stripped if stripped.startswith('system:midi_capture_') else None
        elif cur and keyword.lower() in line.lower():
            return cur
    return None


def maintain_connections():
    time.sleep(2)
    while True:
        try:
            active = _jack_connections()   # (src, dst) currently connected
            ports  = _jack_ports()         # all ports that exist right now

            wanted = [
                (_find_capture_port('MPK'),   "padmap_akai:padmap_akai"),
                (_find_capture_port('CASIO'), "padmap_casio:padmap_casio"),
                ("padmap_piano:padmap_piano", "Pianoteq:midi_in"),
                ("padmap_organ:padmap_organ", "setBfree:control"),
                ("padmap_zyn:padmap_zyn",     "zynaddsubfx:midi_input"),
                ("padmap_fluid:padmap_fluid", "fluidr3:midi_00"),
                ("padmap_bristol:padmap_bristol", "bristolody:midi_in"),
                # bristol has no audio auto-connect, so wire its output too
                ("bristolody:out_left",  "system:playback_1"),
                ("bristolody:out_right", "system:playback_2"),
            ]
            # Connect only when BOTH endpoints exist and the link is missing.
            # In steady state every link exists → zero jack_connect calls (no
            # graph-lock churn), so a short poll interval is safe.
            for src, dst in wanted:
                if src and dst in ports and src in ports and (src, dst) not in active:
                    jrun("jack_connect", src, dst)
                    print(f"connected {src} → {dst}", flush=True)
        except Exception:
            pass
        time.sleep(8)


# ── Instrument routing ───────────────────────────────────────────────────────

def _get_type(keyboard):
    with _lock:
        pad_key = "akai_pad" if keyboard == "akai" else "casio_pad"
        return config["pads"].get(config[pad_key], {}).get("type")


def _engine_out(itype):
    return {"setbfree": organ_out, "pianoteq": piano_out,
            "zyn": zyn_out, "fluid": fluid_out, "bristol": brist_out}.get(itype)


def send_to(keyboard, msg):
    out = _engine_out(_get_type(keyboard))
    if out:
        out.send_message(msg)


def all_notes_off(itype):
    out = _engine_out(itype)
    if out:
        for ch in range(16):
            out.send_message([0xB0 | ch, 123, 0])


# Volume is persisted debounced — knob sweeps fire hundreds of CCs and we must
# not write the config file on every tick (that SD-write storm causes xruns).
_vol_timer = None

def _persist_soon():
    global _vol_timer
    if _vol_timer:
        _vol_timer.cancel()
    _vol_timer = threading.Timer(2.0, lambda: (_lock.acquire(), save_config(), _lock.release()))
    _vol_timer.daemon = True
    _vol_timer.start()


def _preset_key(pad):
    """Identity of the SOUND (not the pad slot) — so a per-preset gain follows the
    instrument when you move it to a different pad."""
    t = pad.get("type") or ""
    p = pad.get("preset") or pad.get("name") or ""
    return f"{t}:{p}"


def _pad_gain(pad):
    """Per-preset level trim, percent (100 = unity). Caller must hold no lock."""
    with _lock:
        return int(config.get("preset_gains", {}).get(_preset_key(pad), 100))


def _emit_for(keyboard):
    """Push dial volume × preset gain to this keyboard's engine."""
    with _lock:
        dial  = int(config.get("akai_vol" if keyboard == "akai" else "casio_vol", 100))
        pad   = config["pads"].get(config["akai_pad" if keyboard == "akai" else "casio_pad"], {})
        gain  = int(config.get("preset_gains", {}).get(_preset_key(pad), 100))
        itype = pad.get("type")
    _emit_volume(itype, dial * gain / 100.0)


def _emit_volume(itype, val):
    """Apply a 0-127 volume to one engine. Pianoteq ignores CC7 (Minimalistic MIDI
    map), so it's set via JSON-RPC; setBfree/Zyn take CC7 channel volume."""
    val = max(0, min(127, int(val)))
    if itype == "pianoteq":
        threading.Thread(
            target=pianoteq_rpc, args=("setParameters",),
            kwargs={"params": {"list": [{"id": "volume", "normalized_value": val / 127.0}]}},
            daemon=True,
        ).start()
    else:
        out = _engine_out(itype)
        if out:
            out.send_message([0xB0, 7, val])   # CC7 channel volume


def set_volume(keyboard, val):
    """Knob moved — apply dial × preset gain to this keyboard's engine."""
    val = max(0, min(127, int(val)))
    _cancel_fade(keyboard)   # turning the dial takes manual control
    with _lock:
        config["akai_vol" if keyboard == "akai" else "casio_vol"] = val
    _emit_for(keyboard)
    _persist_soon()


def apply_volume(keyboard, itype=None):
    """Re-apply this keyboard's level — used on pad switch / startup / gain change."""
    _emit_for(keyboard)


# Some engines reset their own volume when a preset finishes loading, and that
# load is ASYNCHRONOUS — Pianoteq clobbers it ~0.25 s after loadPreset returns,
# bristol ~1.5 s after a memory change. Setting the level once at switch time
# therefore loses the race and the patch plays at its own (often loud) level.
# Re-assert our level a few times after a switch so it always wins.
_vol_gen = {}

def _reapply_volume_later(keyboard, delays=(0.35, 0.9, 1.8, 3.2)):
    gen = _vol_gen.get(keyboard, 0) + 1
    _vol_gen[keyboard] = gen
    def worker():
        t0 = time.monotonic()
        for d in delays:
            rem = d - (time.monotonic() - t0)
            if rem > 0:
                time.sleep(rem)
            # a newer switch, or a fade taking over, supersedes this schedule
            if _vol_gen.get(keyboard) != gen or _fade_state(keyboard)["armed"]:
                return
            _emit_for(keyboard)
    threading.Thread(target=worker, daemon=True).start()


# ── Triple-tap fade-in ────────────────────────────────────────────────────────
# Triple-tap an upper pad → activate that preset AND arm a fade: the keyboard is
# silenced now and, from the first note played, ramps 0 → dial volume over 10 s.
FADE_SECONDS = 10.0
TRIPLE_TAP_WINDOW = 0.6   # max seconds between taps to count as part of a triple-tap
_tap = {"pad": None, "count": 0, "last": 0.0}
_fade = {}   # keyboard → {"armed": bool, "gen": int}

def _fade_state(keyboard):
    return _fade.setdefault(keyboard, {"armed": False, "gen": 0})

def _cancel_fade(keyboard):
    st = _fade_state(keyboard)
    st["armed"] = False
    st["gen"] += 1   # invalidates any in-flight fade worker

def arm_fade(keyboard):
    _vol_gen[keyboard] = _vol_gen.get(keyboard, 0) + 1   # cancel pending re-applies
    _cancel_fade(keyboard)
    _fade_state(keyboard)["armed"] = True
    _emit_volume(_get_type(keyboard), 0)   # silent until first note
    print(f"fade armed for {keyboard} (plays in over {int(FADE_SECONDS)}s)", flush=True)

def _fade_worker(keyboard, gen, itype):
    with _lock:
        dial = int(config.get("akai_vol" if keyboard == "akai" else "casio_vol", 100))
        pad  = config["pads"].get(config["akai_pad" if keyboard == "akai" else "casio_pad"], {})
        gn   = int(config.get("preset_gains", {}).get(_preset_key(pad), 100))
    target = dial * gn / 100.0   # fade up to the same level a normal note would hit
    steps = 50
    for i in range(1, steps + 1):
        if _fade_state(keyboard)["gen"] != gen:
            return   # superseded by another fade / cancel
        _emit_volume(itype, target * i / steps)
        time.sleep(FADE_SECONDS / steps)

def trigger_fade(keyboard):
    _vol_gen[keyboard] = _vol_gen.get(keyboard, 0) + 1   # cancel pending re-applies
    st = _fade_state(keyboard)
    st["armed"] = False
    st["gen"] += 1
    gen = st["gen"]
    threading.Thread(target=_fade_worker, args=(keyboard, gen, _get_type(keyboard)), daemon=True).start()


def activate_pad(keyboard, note_str):
    """Switch the active sound for one keyboard."""
    with _lock:
        pad_key  = "akai_pad" if keyboard == "akai" else "casio_pad"
        old_type = config["pads"].get(config[pad_key], {}).get("type")
        new_pad  = config["pads"].get(note_str, {})
        new_type = new_pad.get("type")
        if not new_type:
            return
        config[pad_key] = note_str
        save_config()

    if old_type and old_type != new_type:
        all_notes_off(old_type)

    if new_type == "pianoteq":
        load_preset_async(new_pad.get("preset", ""))
    elif new_type == "zyn" and new_pad.get("preset"):
        threading.Thread(target=zyn_load, args=(new_pad["preset"],), daemon=True).start()
    elif new_type == "fluid" and new_pad.get("preset"):
        try:
            fluid_out.send_message([0xC0, int(new_pad["preset"]) & 0x7F])  # GM program change
        except (ValueError, TypeError):
            pass
    elif new_type == "bristol" and new_pad.get("preset"):
        try:
            brist_out.send_message([0xC0, int(new_pad["preset"]) & 0x7F])  # memory select
        except (ValueError, TypeError):
            pass

    _cancel_fade(keyboard)             # a preset switch clears any pending fade
    apply_volume(keyboard, new_type)   # carry this keyboard's volume to the new engine
    _reapply_volume_later(keyboard)    # ...and re-assert it after the preset loads

    print(f"→ {keyboard}: {new_pad.get('name','?')} ({new_type})", flush=True)


def make_callback(keyboard):
    def on_midi(message, _):
        msg = message[0]
        if not msg:
            return
        status = msg[0] & 0xF0
        ch     = msg[0] & 0x0F
        d1     = msg[1] if len(msg) > 1 else 0
        d2     = msg[2] if len(msg) > 2 else 0

        # Pad presses (only from Akai — it has the pads)
        if keyboard == "akai" and status == 0x90 and d1 in PAD_NOTES and d2 > 0:
            target_kb = "akai" if d1 in AKAI_PADS else "casio"
            activate_pad(target_kb, str(d1))
            # Triple-tap an upper pad → arm a fade-in for the Akai keyboard
            if d1 in AKAI_PADS:
                now = time.monotonic()
                if _tap["pad"] == d1 and (now - _tap["last"]) <= TRIPLE_TAP_WINDOW:
                    _tap["count"] += 1
                else:
                    _tap["count"] = 1
                _tap["pad"], _tap["last"] = d1, now
                if _tap["count"] >= 3 and _get_type("akai"):
                    _tap["count"] = 0
                    arm_fade("akai")
            return

        # Volume knobs (both physical knobs live on the Akai/MPK)
        if keyboard == "akai" and status == 0xB0:
            if d1 == CC_VOL_AKAI:
                set_volume("akai", d2)
                return
            if d1 == CC_VOL_CASIO:
                set_volume("casio", d2)
                return

        # First note after a triple-tap → start the fade-in
        if keyboard == "akai" and status == 0x90 and d2 > 0 and _fade_state("akai")["armed"]:
            trigger_fade("akai")

        send_to(keyboard, msg)

    return on_midi


# ── Web UI ───────────────────────────────────────────────────────────────────

PAGE = r"""<!doctype html><html><head>
<title>Pedal Control</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#111;color:#eee;font-family:system-ui,sans-serif;padding:16px;max-width:500px;margin:0 auto}
h1{font-size:1.1em;color:#aaa;margin-bottom:14px}
.kblabel{font-size:.65em;font-weight:700;letter-spacing:.12em;color:#555;
         text-transform:uppercase;margin-bottom:6px;padding-left:2px}
.kblabel span{font-size:.9em;color:#444;font-weight:400;margin-left:8px;text-transform:none;letter-spacing:0}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
.pad{background:#232323;border-radius:10px;padding:10px 6px 8px;text-align:center;
     cursor:pointer;min-height:96px;position:relative;border:2px solid transparent;
     user-select:none;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:3px}
/* per-preset level trim */
.lvl{width:100%;margin-top:5px;display:flex;flex-direction:column;align-items:center;gap:1px}
.lvl input[type=range]{width:92%;height:3px;-webkit-appearance:none;appearance:none;
  background:#3a3a3a;border-radius:2px;outline:none;cursor:pointer}
.lvl input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
  width:11px;height:11px;border-radius:50%;background:#8a8a8a;cursor:pointer}
.lvl input[type=range]::-moz-range-thumb{width:11px;height:11px;border:none;
  border-radius:50%;background:#8a8a8a;cursor:pointer}
.pad.active .lvl input[type=range]::-webkit-slider-thumb{background:#6c6}
.pad.active .lvl input[type=range]::-moz-range-thumb{background:#6c6}
.lvlval{font-size:.55em;color:#666;font-variant-numeric:tabular-nums}
.pad.active .lvlval{color:#6c6}
.pad:active{opacity:.7}
.pad.active{background:#1a3a1a;border-color:#4caf50}
.pad.empty{background:#191919;border:2px dashed #333;color:#666}
.pad.empty:hover{border-color:#4caf50;color:#8c8}
.pad.empty .plus{font-size:1.5em;line-height:1;color:#555;font-weight:300}
.pad.empty:hover .plus{color:#6c6}
.pname{font-size:.82em;font-weight:600;line-height:1.2}
.psub{font-size:.62em;color:#777;line-height:1.3;word-break:break-word}
.pad.active .psub{color:#6c6}
.edit{position:absolute;top:3px;right:5px;font-size:.8em;color:#888;cursor:pointer;padding:3px}
.edit:hover{color:#fff}
.divider{border:none;border-top:1px solid #222;margin:6px 0 12px}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:50;align-items:flex-end}
.overlay.open{display:flex}
.sheet{background:#1c1c1c;border-radius:16px 16px 0 0;width:100%;padding:16px;
       max-height:88vh;display:flex;flex-direction:column;gap:10px}
.sheet h2{font-size:.95em;color:#bbb}
input{background:#272727;border:1px solid #383838;color:#eee;padding:8px 10px;
      border-radius:8px;width:100%;font-size:.9em;outline:none}
input:focus{border-color:#555}
.plist{overflow-y:auto;flex:1;border-radius:8px;background:#1e1e1e}
.pitem{padding:10px 12px;border-bottom:1px solid #252525;cursor:pointer;font-size:.85em;
       display:flex;justify-content:space-between;align-items:center}
.pitem:hover,.pitem:active{background:#2a2a2a}
.bank{color:#4a4a4a;font-size:.8em;margin-left:8px;flex-shrink:0}
.pitem.organ{color:#f5a623}
.pitem.zyn{color:#60b8d4}
.pitem.fluid{color:#d98a3d}
.pitem.bristol{color:#c98ae0}
.row{display:flex;gap:8px;margin-top:2px}
.btn{flex:1;background:#272727;border:none;color:#bbb;padding:10px;border-radius:8px;cursor:pointer;font-size:.88em}
.btn:hover{background:#333}
.clr{color:#e66;background:#2c1818}
.clr:hover{background:#3a1f1f}
.swsection{margin-bottom:20px}
.swhead{display:flex;justify-content:space-between;align-items:center;
        padding:8px 10px;background:#1c1c1c;border-radius:8px;cursor:pointer;
        font-size:.82em;color:#888;user-select:none}
.swhead:hover{background:#242424}
.swhead b{color:#bbb}
.swbody{display:none;padding-top:8px;display:flex;flex-direction:column;gap:10px}
.swbody.open{display:flex}
.swgroup{font-size:.7em;color:#555;margin-bottom:4px;padding-left:2px}
.swchips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:#222;border:1px solid #333;border-radius:6px;padding:5px 9px;
      font-size:.75em;cursor:pointer;color:#aaa;white-space:nowrap}
.chip:hover{background:#2d2d2d;border-color:#444;color:#ddd}
.chip.active{background:#1a3a1a;border-color:#4caf50;color:#6c6}
</style></head><body>
<h1>Pedal Control</h1>

<div class="kblabel">Akai <span>top pads</span></div>
<div class="grid" id="akai-grid"></div>

<hr class="divider">

<div class="kblabel">Casio <span>bottom pads</span></div>
<div class="grid" id="casio-grid"></div>

<div class="swsection">
  <div class="swhead" onclick="toggleSW()">
    <b>Steinway D</b> <span id="swarrow">▾</span>
  </div>
  <div class="swbody" id="swbody">
    <div>
      <div class="swgroup">NY STEINWAY D</div>
      <div class="swchips" id="ny-chips"></div>
    </div>
    <div>
      <div class="swgroup">HB STEINWAY D</div>
      <div class="swchips" id="hb-chips"></div>
    </div>
  </div>
</div>

<div class="overlay" id="overlay">
  <div class="sheet">
    <h2 id="stitle">Assign Pad</h2>
    <input id="search" placeholder="Search presets…" oninput="filter()">
    <div class="plist" id="plist"></div>
    <div class="row">
      <button class="btn clr" onclick="clearPad()">Clear</button>
      <button class="btn" onclick="closeSheet()">Cancel</button>
    </div>
  </div>
</div>

<script>
let state={},catalog=[],editNote=null;
const $=s=>document.querySelector(s);
const esc=s=>s.replace(/\\/g,'\\\\').replace(/'/g,"\\'");

async function refresh(){
  try{state=await fetch('/api/state').then(r=>r.json());render();}catch{}
}

function typeLabel(p){
  if(!p||!p.type) return '—';
  if(p.type==='setbfree') return 'Organ';
  if(p.type==='zyn') return p.name||'Zyn';
  if(p.type==='fluid') return p.name||'Sampled';
  if(p.type==='bristol') return p.name||'Odyssey';
  return p.preset||p.name||'—';
}

function renderGrid(gridId, notes, activePad){
  $(gridId).innerHTML=notes.map(n=>{
    const p=(state.pads||{})[n]||{};
    const active=activePad===n;
    const empty=!p.type;
    const cls='pad'+(active?' active':'')+(empty?' empty':'');
    // Empty pad → tapping opens the editor (nothing to activate yet).
    const onTap=empty?`openEdit('${n}')`:`tap('${n}')`;
    if(empty){
      return `<div class="${cls}" onclick="${onTap}">
        <div class="plus">+</div>
        <div class="psub">Add sound</div>
      </div>`;
    }
    const g=padGain(p);
    return `<div class="${cls}" onclick="${onTap}">
      <span class="edit" onclick="event.stopPropagation();openEdit('${n}')">&#9998;</span>
      <div class="pname">${p.name||('Pad '+(parseInt(n)-99))}</div>
      <div class="psub">${typeLabel(p)}</div>
      <div class="lvl" onclick="event.stopPropagation()">
        <input type="range" min="0" max="200" step="5" value="${g}"
               aria-label="Level for ${p.name||'pad'}"
               oninput="lvlLabel(this)" onchange="setGain('${n}',this.value)">
        <span class="lvlval">${g}%</span>
      </div>
    </div>`;
  }).join('');
}

// The level trim belongs to the SOUND, so it follows the instrument between pads.
function presetKey(p){ return (p.type||'')+':'+(p.preset||p.name||''); }
function padGain(p){ const v=(state.preset_gains||{})[presetKey(p)]; return v===undefined?100:v; }
function lvlLabel(el){ el.parentNode.querySelector('.lvlval').textContent=el.value+'%'; }
async function setGain(n,val){
  await fetch('/api/gain',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({pad:n,gain:+val})});
  refresh();
}

function render(){
  renderGrid('#akai-grid',  ['104','105','106','107'], state.akai_pad);
  renderGrid('#casio-grid', ['100','101','102','103'], state.casio_pad);
}

async function tap(n){
  await fetch('/api/activate',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({pad:n})});
  refresh();
}

async function openEdit(n){
  editNote=n;
  const num=parseInt(n);
  const kb=num>=104?'Akai':'Casio';
  $('#stitle').textContent=`Assign — ${kb} pad ${num>=104?num-103:num-99}`;
  $('#search').value='';
  if(!catalog.length) catalog=await fetch('/api/catalog').then(r=>r.json());
  filter();
  $('#overlay').classList.add('open');
}

function filter(){
  const q=$('#search').value.toLowerCase();
  $('#plist').innerHTML=catalog
    .filter(p=>(p.name+' '+(p.bank||'')).toLowerCase().includes(q))
    .slice(0,200)
    .map(p=>{
      const cls='pitem'+(p.itype==='setbfree'?' organ':p.itype==='zyn'?' zyn':p.itype==='fluid'?' fluid':p.itype==='bristol'?' bristol':'');
      // preset value stored per engine: pianoteq→name, zyn→file path, fluid→GM program
      const preset=p.itype==='pianoteq'?p.name:p.itype==='zyn'?(p.path||''):p.itype==='fluid'?(p.preset||''):p.itype==='bristol'?(p.preset||''):'';
      return `<div class="${cls}" onclick="assign('${p.itype}','${esc(p.name)}','${esc(preset)}')">
        <span>${p.name}</span><span class="bank">${p.bank||''}</span>
      </div>`;
    }).join('');
}

async function assign(itype,name,preset){
  await fetch('/api/assign',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pad:editNote,type:itype,preset,name})});
  catalog=[];closeSheet();refresh();
}

async function clearPad(){
  await fetch('/api/assign',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pad:editNote,type:null,preset:'',name:''})});
  closeSheet();refresh();
}

function closeSheet(){$('#overlay').classList.remove('open');editNote=null;}
$('#overlay').addEventListener('click',e=>{if(e.target===$('#overlay'))closeSheet();});

let swOpen=localStorage.getItem('sw-open')!=='false';
function toggleSW(){swOpen=!swOpen;localStorage.setItem('sw-open',swOpen);applySwOpen();}
function applySwOpen(){
  $('#swbody').classList.toggle('open',swOpen);
  $('#swarrow').textContent=swOpen?'▴':'▾';
}
async function renderSW(){
  if(!catalog.length) catalog=await fetch('/api/catalog').then(r=>r.json());
  const akaiPad=(state.pads||{})[state.akai_pad||''];
  const activePreset=akaiPad?.preset||'';
  const renderChips=(id,prefix)=>{
    const el=$('#'+id);
    el.innerHTML=catalog
      .filter(p=>p.itype==='pianoteq'&&p.name.startsWith(prefix))
      .map(p=>{
        const label=p.name.slice(prefix.length).trim()||p.name;
        return `<div class="chip${p.name===activePreset?' active':''}"
          onclick="loadVariant('${esc(p.name)}')">${label}</div>`;
      }).join('');
  };
  renderChips('ny-chips','NY Steinway D ');
  renderChips('hb-chips','HB Steinway D ');
  applySwOpen();
}
async function loadVariant(preset){
  await fetch('/api/variant',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({preset})});
  await refresh();
}

refresh();
renderSW();
setInterval(()=>{refresh().then(renderSW);},6000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self._send(200, 'text/html', PAGE.encode())
        elif self.path == '/api/state':
            with _lock:
                data = json.dumps(config).encode()
            self._send(200, 'application/json', data)
        elif self.path == '/api/catalog':
            self._send(200, 'application/json',
                       json.dumps(get_preset_catalog()).encode())
        else:
            self._send(404, 'text/plain', b'Not found')

    def do_POST(self):
        n    = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(n)) if n else {}

        if self.path == '/api/activate':
            note = body.get('pad', '')
            kb = "akai" if int(note or 0) in AKAI_PADS else "casio"
            activate_pad(kb, note)
            self._send(200, 'application/json', b'{}')

        elif self.path == '/api/assign':
            with _lock:
                note = body.get('pad', '')
                if note in config['pads']:
                    config['pads'][note].update({
                        'type':   body.get('type'),
                        'preset': body.get('preset', ''),
                        'name':   body.get('name', ''),
                    })
                    save_config()
            self._send(200, 'application/json', b'{}')

        elif self.path == '/api/gain':
            # Per-preset level trim. Stored against the sound's identity so it
            # follows the instrument if you move it to another pad.
            note = body.get('pad', '')
            gain = max(0, min(200, int(body.get('gain', 100))))
            with _lock:
                pad = config['pads'].get(note)
                if pad and pad.get('type'):
                    config.setdefault('preset_gains', {})[_preset_key(pad)] = gain
                    save_config()
                    kb = "akai" if int(note or 0) in AKAI_PADS else "casio"
                    active = config["akai_pad" if kb == "akai" else "casio_pad"] == note
                else:
                    kb, active = None, False
            if active:
                apply_volume(kb)   # hear the change immediately
            self._send(200, 'application/json', b'{}')

        elif self.path == '/api/variant':
            preset = body.get('preset', '')
            if preset:
                sw_prefixes = ('NY Steinway', 'HB Steinway')
                with _lock:
                    target = next(
                        (n for n, p in config['pads'].items()
                         if any(p.get('preset', '').startswith(pfx) for pfx in sw_prefixes)),
                        '107'
                    )
                    config['pads'].setdefault(target, {})['preset'] = preset
                    config['pads'][target]['type'] = 'pianoteq'
                    save_config()
                activate_pad("akai", target)
            self._send(200, 'application/json', b'{}')

        else:
            self._send(404, 'text/plain', b'Not found')

    def _send(self, code, ct, body):
        self.send_response(code)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    load_config()

    akai_in.open_virtual_port("padmap_akai")
    casio_in.open_virtual_port("padmap_casio")
    piano_out.open_virtual_port("padmap_piano")
    organ_out.open_virtual_port("padmap_organ")
    zyn_out.open_virtual_port("padmap_zyn")
    fluid_out.open_virtual_port("padmap_fluid")
    brist_out.open_virtual_port("padmap_bristol")

    akai_in.ignore_types(sysex=False, timing=False, active_sense=True)
    casio_in.ignore_types(sysex=False, timing=False, active_sense=True)
    akai_in.set_callback(make_callback("akai"))
    casio_in.set_callback(make_callback("casio"))

    threading.Thread(target=maintain_connections, daemon=True).start()

    def _apply_pad_preset(pad):
        t, p = pad.get("type"), pad.get("preset")
        if t == "pianoteq" and p:
            load_preset_async(p)
        elif t == "zyn" and p:
            zyn_load(p)
        elif t == "fluid" and p:
            try:
                fluid_out.send_message([0xC0, int(p) & 0x7F])
            except (ValueError, TypeError):
                pass
        elif t == "bristol" and p:
            try:
                brist_out.send_message([0xC0, int(p) & 0x7F])
            except (ValueError, TypeError):
                pass

    def startup_preset():
        time.sleep(6)
        with _lock:
            apad = config["pads"].get(config["akai_pad"], {})
            cpad = config["pads"].get(config["casio_pad"], {})
        # Load whatever each keyboard has active, whichever engine it is.
        _apply_pad_preset(apad)
        _apply_pad_preset(cpad)
        time.sleep(1)   # let engines load before setting volume
        apply_volume("akai",  apad.get("type"))
        apply_volume("casio", cpad.get("type"))
    threading.Thread(target=startup_preset, daemon=True).start()

    threading.Thread(
        target=lambda: HTTPServer(("", 8080), Handler).serve_forever(),
        daemon=True,
    ).start()
    print("padmap: web UI → http://patchbox.local:8080", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for port in (akai_in, casio_in, piano_out, organ_out, zyn_out, fluid_out, brist_out):
            port.close_port()


if __name__ == "__main__":
    main()
