"""
Radio sounds, synthesized here: no audio files, no rights to clear. Themes:
- classic: the original beeps, for comparison
- blade: Blade Runner style, warm detuned analog synths (CS-80), echo and reverb, sonar pings
- cyber: Cyberpunk style, digital, FM, bitcrush, glitch
- radio: military radio, squelch, roger beep, Morse signatures
- walkie: real professional radio, squelch tails, roger beep, five-tone selective calls
- mission: the Quindar tones of the Apollo missions (2525 Hz at the start of a transmission, 2475 Hz at the end, 250 ms)
- objects: physical, discreet sounds: wood, glass, little bell, marimba (damped partials)
- neon: Blade Runner and Cyberpunk together, bolder: saturated CS-80 brass, short echoes, digital accents
- cinema: interfaces of serious sci-fi films (Blade Runner 2049, Dune): only soft waves, fifths and fourths,
  a breath of air, a hall; no bitcrush, square waves, FM or arpeggios (they sounded like a Game Boy)
- booth: the on-board chimes of airliners, struck metal bars

Each event is a function returning 16-bit mono 48 kHz PCM (what pymumble wants).
Most sounds sit between 300 and 3400 Hz: they must stay audible on headphones in call mode
(the Bluetooth "hands-free" profile, as used by Mumla), which cuts lows and highs. They are short and quieter than the voice.
"""
import math

import numpy as np

SR = 48000
EVENTS = ("heard", "noise", "error", "working", "heartbeat", "end", "new", "open", "close", "waiting")
THEMES = ("classic", "blade", "cyber", "radio", "walkie", "mission", "objects", "neon", "cinema", "booth")
NATO = ("alpha", "bravo", "charlie", "delta", "echo")
MORSE = {"a": ".-", "b": "-...", "c": "-.-.", "d": "-..", "e": ".", "f": "..-.", "g": "--.", "h": "....",
         "i": "..", "j": ".---", "k": "-.-", "l": ".-..", "m": "--", "n": "-.", "o": "---", "p": ".--.",
         "q": "--.-", "r": ".-.", "s": "...", "t": "-", "u": "..-", "v": "...-", "w": ".--", "x": "-..-",
         "y": "-.--", "z": "--.."}


# --- building blocks -------------------------------------------------------------------


# Names used before the translation, still accepted in .env (SOUND_THEME, VOICE_FX, BED)
THEME_ALIASES = {"classico": "classic", "ricetrasmittente": "walkie", "missione": "mission", "oggetti": "objects",
                 "cabina": "booth"}
LEVEL_ALIASES = {"leggero": "light", "medio": "medium", "forte": "strong", "vuoto": ""}
BED_ALIASES = {"sala": "hall", "canale": "channel", "respiro": "breath"}


def canonical_theme(name):
    name = (name or "").strip().lower()
    return THEME_ALIASES.get(name, name)


def samples(ms):
    return max(1, int(SR * ms / 1000))


def silence(ms):
    return np.zeros(samples(ms))


def envelope(n, attack=5, release=30):
    """Rises over attack ms, falls over release ms."""
    e = np.ones(n)
    a = min(n, samples(attack))
    r = min(n - a, samples(release))
    if a:
        e[:a] = np.linspace(0, 1, a)
    if r:
        e[n - r:] = np.linspace(1, 0, r) ** 1.5
    return e


def decay(n, tau_ms):
    return np.exp(-np.arange(n) / samples(tau_ms))


def oscillator(freq, n, shape="sine"):
    """freq can be a number or an array of length n (glide, vibrato)."""
    f = np.full(n, float(freq)) if np.ndim(freq) == 0 else np.asarray(freq, float)[:n]
    phase = np.cumsum(f) / SR
    if shape == "sine":
        return np.sin(2 * np.pi * phase)
    fraction = phase % 1.0
    if shape == "saw":
        return 2 * fraction - 1
    if shape == "square":
        return np.where(fraction < 0.5, 1.0, -1.0)
    return 1 - 4 * np.abs(fraction - 0.5)   # triangle


def glide(f0, f1, n):
    return np.geomspace(f0, f1, n)


def biquad(x, kind, f0, q=0.707):
    """Two-pole filter (RBJ): lp, hp, bp. Python loop: acceptable for sounds only a few seconds long."""
    w0 = 2 * math.pi * f0 / SR
    cosw, alpha = math.cos(w0), math.sin(w0) / (2 * q)
    if kind == "lp":
        b0, b1, b2 = (1 - cosw) / 2, 1 - cosw, (1 - cosw) / 2
    elif kind == "hp":
        b0, b1, b2 = (1 + cosw) / 2, -(1 + cosw), (1 + cosw) / 2
    else:
        b0, b1, b2 = alpha, 0.0, -alpha
    a0, a1, a2 = 1 + alpha, -2 * cosw, 1 - alpha
    b0, b1, b2, a1, a2 = b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0
    y = [0.0] * len(x)
    x1 = x2 = y1 = y2 = 0.0
    for i, xi in enumerate(np.asarray(x, float).tolist()):
        yi = b0 * xi + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, xi, y1, yi
        y[i] = yi
    return np.array(y)


def band(x, low=300, high=3400):
    """The telephone band."""
    return biquad(biquad(x, "hp", low), "lp", high)


def _recursive(y, d, g):
    """y[n] += g * y[n - d], in blocks of length d: each block depends only on the previous one."""
    for start in range(d, len(y), d):
        end = min(start + d, len(y))
        y[start:end] += g * y[start - d:end - d]
    return y


def echo(x, delay_ms, feedback, mix=0.4, tail_ms=None):
    d = samples(delay_ms)
    tail = samples(tail_ms) if tail_ms else int(d * math.log(0.01) / math.log(feedback))
    dry = np.concatenate([x, np.zeros(tail)])
    delayed = np.concatenate([np.zeros(d), dry[:-d]])   # x delayed by d
    wet = _recursive(delayed.copy(), d, feedback)
    return dry + mix * wet


def reverb(x, mix=0.3, tail_ms=600, length=0.78):
    """Schroeder: four parallel combs and two allpass filters."""
    dry = np.concatenate([x, np.zeros(samples(tail_ms))])
    wet = np.zeros_like(dry)
    for ms in (29.7, 37.1, 41.1, 43.7):
        wet += _recursive(dry.copy(), samples(ms), length)
    wet /= 4
    for ms, g in ((5.0, 0.7), (1.7, 0.7)):
        d = samples(ms)
        delayed = np.concatenate([np.zeros(d), wet[:-d]])
        wet = _recursive(-g * wet + delayed, d, g)
    return (1 - mix) * dry + mix * wet


def bitcrush(x, bit=6, hold=1):
    if hold > 1:
        x = x[(np.arange(len(x)) // hold) * hold]
    levels = 2 ** (bit - 1)
    return np.round(x * levels) / levels


def white_noise(n, seed=7):
    return np.random.default_rng(seed).uniform(-1, 1, n)


def in_sequence(*parts):
    return np.concatenate([np.asarray(p, float) for p in parts if len(p)])


def together(*parts):
    """Sums sounds of different lengths."""
    n = max(len(p) for p in parts)
    out = np.zeros(n)
    for p in parts:
        out[:len(p)] += p
    return out


def pcm(x, volume=0.25):
    """From float to 16-bit PCM, with the peak at volume."""
    x = np.asarray(x, float)
    peak = np.max(np.abs(x)) if len(x) else 0
    if peak > 0:
        x = x / peak * volume
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def from_pcm(data):
    return np.frombuffer(data, dtype="<i2").astype(float) / 32767


# --- instruments ------------------------------------------------------------------

def cs80(freq, ms, cutoff=2400, attack=10, release=60, detune=7, vibrato=6):
    """Synthetic brass in the style of the Yamaha CS-80: three detuned sawtooth waves, slow vibrato, low-pass."""
    n = samples(ms)
    base = np.full(n, float(freq)) if np.ndim(freq) == 0 else np.asarray(freq, float)
    lfo = 1 + (vibrato / 1200) * np.log(2) * np.sin(2 * np.pi * 5.5 * np.arange(n) / SR)
    voice = sum(oscillator(base * lfo * 2 ** (c / 1200), n, "saw") for c in (-detune, 0, detune)) / 3
    return biquad(voice, "lp", cutoff, q=0.9) * envelope(n, attack, release)


def chord(freqs, ms, **kw):
    return sum(cs80(f, ms, **kw) for f in freqs) / len(freqs)


def fm(carrier, ms, ratio=1.5, index=2.0, attack=2, release=20):
    n = samples(ms)
    p = np.full(n, float(carrier)) if np.ndim(carrier) == 0 else np.asarray(carrier, float)
    modulator = index * np.sin(2 * np.pi * np.cumsum(p * ratio) / SR)
    return np.sin(2 * np.pi * np.cumsum(p) / SR + modulator) * envelope(n, attack, release)


def note(freq, ms, shape="sine", attack=3, release=20):
    n = samples(ms)
    return oscillator(freq, n, shape) * envelope(n, attack, release)


def squelch(ms, center=1600, tau=None, seed=3):
    n = samples(ms)
    r = biquad(white_noise(n, seed), "bp", center, q=0.6)
    return r * decay(n, tau or ms / 2.5)


def glitch(ms, steps=10, seed=11):
    """Random pieces: crushed noise, silence, square waves at random frequencies."""
    rng = np.random.default_rng(seed)
    length = ms / steps
    parts = []
    for i in range(steps):
        choice = rng.integers(0, 3)
        if choice == 0:
            p = bitcrush(white_noise(samples(length), int(rng.integers(0, 999))), 4, 6)
        elif choice == 1:
            p = silence(length)
        else:
            p = note(float(rng.uniform(300, 1800)), length, "square", 1, 3)
        parts.append(p * (1 - i / steps))
    return in_sequence(*parts)


# --- themes ---------------------------------------------------------------------

def _classic(event):
    if event == "heard":
        return pcm(note(880, 120, attack=10, release=30), 0.25)
    if event == "noise":
        return pcm(in_sequence(note(440, 90), silence(60), note(370, 140)), 0.2)
    if event == "error":
        return pcm(in_sequence(note(330, 150), silence(80), note(330, 150)), 0.25)
    if event == "working":
        return pcm(in_sequence(silence(300), note(520, 40), silence(60), note(520, 40)), 0.12)
    if event == "heartbeat":
        return pcm(in_sequence(note(660, 80), silence(80), note(660, 80)), 0.25)
    return b""   # today the other events have no sound


def _blade(event):
    if event == "heard":
        n = samples(110)
        return pcm(echo(cs80(glide(740, 988, n), 110, cutoff=2600, attack=8, release=60), 140, 0.3, 0.35), 0.25)
    if event == "noise":
        n = samples(260)
        return pcm(echo(note(glide(660, 330, n), 260, "tri", 5, 120), 200, 0.45, 0.5), 0.18)
    if event == "error":
        return pcm(reverb(chord((294, 311), 380, cutoff=1600, attack=40, release=200), 0.3, 500), 0.3)
    if event == "working":
        n = samples(30)
        ping = oscillator(1400, n) * decay(n, 12)
        return pcm(echo(ping, 170, 0.45, 0.6), 0.10)
    if event == "heartbeat":
        p = note(990, 40, attack=3, release=30)
        return pcm(reverb(in_sequence(p, silence(90), p), 0.25, 300), 0.15)
    if event == "end":
        return pcm(reverb(chord((440, 523, 659), 350, cutoff=1800, attack=60, release=280), 0.5, 600), 0.12)
    if event == "new":
        beeps = [note(f, 35, "tri", 2, 10) for f in (1568, 2093, 1760, 2349)]
        seq = in_sequence(*[in_sequence(b, silence(15)) for b in beeps])
        return pcm(echo(seq, 120, 0.25, 0.3), 0.18)
    if event in ("open", "close"):
        n = samples(700)
        f0, f1 = (147, 294) if event == "open" else (294, 147)
        engine = cs80(glide(f0, f1, n), 700, cutoff=1500, attack=300, release=300, detune=12)
        puff = band(white_noise(n), 1500, 4000) * (np.linspace(0, 1, n) if event == "open" else np.linspace(1, 0, n))
        return pcm(reverb(engine + 0.25 * puff * envelope(n, 100, 200), 0.35, 700), 0.22)
    if event == "waiting":
        return pcm(echo(in_sequence(cs80(523, 120, cutoff=2200), silence(30), cs80(622, 160, cutoff=2200)), 150, 0.3, 0.3), 0.2)
    return b""


def _cyber(event):
    if event == "heard":
        n = samples(45)
        c = bitcrush(fm(glide(900, 1800, n), 45, 1.5, 1.6), 6, 3)
        tail = c[-samples(15):]
        return pcm(in_sequence(c, tail * 0.6, tail * 0.3), 0.22)
    if event == "noise":
        return pcm(band(glitch(250), 300, 3400), 0.18)
    if event == "error":
        n = samples(320)
        hum = np.tanh(3 * (oscillator(110, n, "square") + oscillator(116, n, "square")))
        gate = (np.arange(n) // samples(40)) % 2 == 0
        return pcm(band(hum * gate * envelope(n, 5, 60), 200, 3000), 0.28)
    if event == "working":
        n = samples(8)
        click = biquad(white_noise(n, 5), "bp", 2000, q=1.5) * decay(n, 2.5)
        return pcm(in_sequence(silence(300), click, silence(35), click), 0.14)
    if event == "heartbeat":
        b = bitcrush(note(1000, 30, "square", 1, 8), 5)
        return pcm(in_sequence(b, silence(70), b), 0.16)
    if event == "end":
        rng = np.random.default_rng(23)
        steps = [note(float(rng.uniform(800, 3000)), 7, "square", 0.5, 1) for _ in range(20)]
        burst = bitcrush(in_sequence(*steps), 5)
        return pcm(burst * np.linspace(1, 0.2, len(burst)), 0.12)
    if event == "new":
        return pcm(bitcrush(in_sequence(*[note(f, 30, "square", 1, 6) for f in (1047, 1568, 2093)]), 6), 0.16)
    if event in ("open", "close"):
        n = samples(420)
        f0, f1 = (200, 1600) if event == "open" else (1600, 150)
        wave = oscillator(glide(f0, f1, n), n, "square") * envelope(n, 5, 80)
        bit = np.linspace(3, 12, 6) if event == "open" else np.linspace(12, 3, 6)
        chunks = np.array_split(wave, 6)
        sweep = in_sequence(*[bitcrush(p, int(b)) for p, b in zip(chunks, bit)])
        ending = fm(2093, 60, 2.0, 1.5) if event == "open" else silence(10)
        return pcm(band(in_sequence(sweep, ending), 200, 3600), 0.2)
    if event == "waiting":
        return pcm(in_sequence(fm(1320, 40), silence(40), fm(1760, 40), glitch(60, 4, 31) * 0.4), 0.18)
    return b""


def _radio(event):
    if event == "heard":
        return pcm(squelch(30), 0.18)
    if event == "noise":
        n = samples(220)
        crackle = (np.random.default_rng(9).random(n) > 0.995) * 1.0
        return pcm(band(white_noise(n, 9) * 0.5 + crackle, 600, 3200) * decay(n, 120), 0.14)
    if event == "error":
        t = lambda: note(400, 140, "tri", 5, 30)
        return pcm(in_sequence(t(), silence(60), t()), 0.25)
    if event == "working":
        return b""   # a real radio stays silent while you wait
    if event == "heartbeat":
        return pcm(note(1000, 50), 0.15)
    if event == "end":
        return pcm(in_sequence(note(1300, 90, attack=3, release=10), squelch(160)), 0.2)
    if event == "new":
        return pcm(in_sequence(note(1800, 60), note(2200, 60)), 0.16)
    if event in ("open", "close"):
        tones = (600, 900) if event == "open" else (900, 600)
        return pcm(in_sequence(squelch(60), silence(40), note(tones[0], 90), note(tones[1], 90)), 0.2)
    if event == "waiting":
        p = note(1200, 40)
        return pcm(in_sequence(p, silence(50), p, silence(50), p), 0.16)
    return b""


# --- realistic sounds (second round) -----------------------------------------------

def strike(freq, partials, ms, noise=0.0, seed=13):
    """A struck object: sum of damped sinusoids (ratio, amplitude, decay in ms) and a touch of
    noise at the attack, the beater."""
    n = samples(ms)
    x = sum(a * oscillator(freq * r, n) * decay(n, tau) for r, a, tau in partials)
    if noise:
        k = samples(4)
        x[:k] += noise * white_noise(k, seed) * np.linspace(1, 0, k)
    return x * envelope(n, 1, min(30, ms / 3))


WOOD = ((1, 1.0, 25), (2.76, 0.4, 12), (5.4, 0.15, 6))
GLASS = ((1, 1.0, 220), (2.32, 0.5, 140), (4.25, 0.25, 90), (6.63, 0.12, 60))
BELL = ((1, 1.0, 500), (2.0, 0.5, 400), (2.4, 0.35, 300), (3.0, 0.25, 250), (4.5, 0.12, 180))
MARIMBA = ((1, 1.0, 180), (3.9, 0.25, 50), (9.2, 0.08, 20))


def real_squelch(ms=140, seed=21):
    """Squelch tail: a puff of noise in the radio band that cuts off sharply."""
    n = samples(ms)
    r = band(white_noise(n, seed), 400, 3000)
    return r * np.concatenate([np.linspace(0.6, 1, n - samples(8)), np.linspace(1, 0, samples(8))])


def selcall(digits, ms=70):
    """Selective tone call (ZVEI): one note per digit, like pagers and service radios."""
    zvei = {"0": 2400, "1": 1060, "2": 1160, "3": 1270, "4": 1400, "5": 1530, "6": 1670, "7": 1830, "8": 2000, "9": 2200}
    return in_sequence(*[note(zvei[c], ms, attack=2, release=4) for c in digits])


def quindar(start=True, ms=250):
    return note(2525 if start else 2475, ms, attack=3, release=8)


def _walkie(event):
    if event == "heard":
        return pcm(real_squelch(110), 0.16)
    if event == "noise":
        n = samples(140)
        bonk = oscillator(440, n, "tri") * decay(n, 45)
        return pcm(band(bonk, 200, 3000), 0.2)
    if event == "error":
        t = lambda: note(600, 150, "sine", 4, 20)
        return pcm(in_sequence(t(), silence(70), t()), 0.22)
    if event == "working":
        n = samples(6)
        return pcm(in_sequence(silence(300), biquad(white_noise(n, 31), "bp", 1800, q=2) * decay(n, 1.5)), 0.08)
    if event == "heartbeat":
        return pcm(note(1000, 40), 0.12)
    if event == "end":
        return pcm(in_sequence(note(1500, 80, attack=2, release=6), real_squelch(150)), 0.16)
    if event == "new":
        return pcm(selcall("153"), 0.15)
    if event == "open":
        return pcm(in_sequence(selcall("135", 60), silence(40), real_squelch(90)), 0.16)
    if event == "close":
        return pcm(in_sequence(real_squelch(90), silence(40), selcall("531", 60)), 0.16)
    if event == "waiting":
        pair = in_sequence(note(1000, 90), note(1500, 90))
        return pcm(in_sequence(pair, silence(120), pair), 0.15)
    return b""


def _mission(event):
    if event == "heard":
        return pcm(quindar(False), 0.12)
    if event == "noise":
        n = samples(220)
        rustle = band(white_noise(n, 17), 300, 3000) * (0.5 + 0.5 * np.sin(2 * np.pi * 9 * np.arange(n) / SR))
        return pcm(rustle * envelope(n, 20, 100), 0.12)
    if event == "error":
        return pcm(in_sequence(note(750, 120), note(1000, 120), note(750, 120)), 0.2)
    if event == "working":
        return pcm(in_sequence(silence(300), note(1000, 15, attack=1, release=6)), 0.06)
    if event == "heartbeat":
        return pcm(note(2500, 80), 0.1)
    if event == "end":
        return pcm(quindar(False), 0.12)
    if event == "new":
        p = quindar(True, 60)
        return pcm(in_sequence(p, silence(60), p), 0.12)
    if event in ("open", "close"):
        n = samples(600)
        rustle = band(white_noise(n, 19), 300, 3000) * (np.linspace(0, 1, n) if event == "open" else np.linspace(1, 0, n))
        tone = quindar(event == "open")
        return pcm(in_sequence(tone, rustle * 0.25) if event == "open" else in_sequence(rustle * 0.25, tone), 0.12)
    if event == "waiting":
        p = quindar(True, 120)
        return pcm(in_sequence(p, silence(150), p), 0.12)
    return b""


# Bolder objects (third round, "too bland"): more body, sharper attack, a touch of room, higher volume
FULL_WOOD = ((1, 1.0, 70), (2.57, 0.55, 35), (4.1, 0.3, 18), (0.5, 0.35, 90))
STEEL = ((1, 1.0, 260), (2.76, 0.6, 180), (5.4, 0.35, 120), (8.93, 0.2, 80))
LONG_GLASS = ((1, 1.0, 450), (2.32, 0.6, 300), (4.25, 0.35, 200), (6.63, 0.2, 120))
SMALL_BELL = ((1, 1.0, 1100), (2.32, 0.5, 700), (4.1, 0.35, 500), (5.6, 0.2, 300))
FULL_MARIMBA = ((1, 1.0, 450), (3.9, 0.3, 90), (9.2, 0.1, 30))


def room(x, mix=0.18):
    """A touch of small room: the sound feels like a real object on the table, not a beep."""
    return reverb(x, mix, tail_ms=300, length=0.6)


def punchy(x, drive=1.8):
    """A bit of saturation: the attack comes out sharp even at low volume."""
    peak = np.max(np.abs(x)) or 1
    return np.tanh(drive * x / peak)


def _objects(event):
    if event == "heard":
        return pcm(room(punchy(strike(760, FULL_WOOD, 220, 0.6))), 0.32)
    if event == "noise":
        pair = in_sequence(strike(560, FULL_WOOD, 120, 0.4), silence(50), strike(420, FULL_WOOD, 160, 0.4))
        return pcm(room(biquad(punchy(pair), "lp", 1800)), 0.26)
    if event == "error":
        clonk = lambda: punchy(strike(330, STEEL, 420, 0.8), 2.2)
        return pcm(room(band(together(clonk(), in_sequence(silence(160), clonk())), 250, 3400)), 0.34)
    if event == "working":
        return pcm(in_sequence(silence(300), punchy(strike(2400, WOOD, 30, 0.6))), 0.10)
    if event == "heartbeat":
        return pcm(in_sequence(strike(1800, WOOD, 35, 0.5), silence(130), strike(1400, WOOD, 35, 0.5)), 0.14)
    if event == "end":   # two glasses clinking
        return pcm(room(together(strike(1760, LONG_GLASS, 700, 0.3), in_sequence(silence(25), strike(2217, LONG_GLASS, 650, 0.3)))), 0.24)
    if event == "new":  # the reception desk bell
        return pcm(room(punchy(strike(2100, SMALL_BELL, 1200, 0.8), 1.4), 0.12), 0.28)
    if event == "open":
        click = strike(1200, FULL_WOOD, 60, 0.7)
        rising = together(strike(523, FULL_MARIMBA, 500, 0.3), in_sequence(silence(120), strike(784, FULL_MARIMBA, 600, 0.3)))
        return pcm(room(punchy(in_sequence(click, silence(60), rising), 1.4)), 0.28)
    if event == "close":
        falling = together(strike(784, FULL_MARIMBA, 500, 0.3), in_sequence(silence(120), strike(523, FULL_MARIMBA, 600, 0.3)))
        thud = strike(380, FULL_WOOD, 160, 0.7)
        return pcm(room(punchy(in_sequence(falling, thud), 1.4)), 0.28)
    if event == "waiting":   # a question: two rising notes
        return pcm(room(punchy(together(strike(784, FULL_MARIMBA, 450, 0.3), in_sequence(silence(150), strike(1047, FULL_MARIMBA, 600, 0.3))), 1.4)), 0.28)
    return b""


def _neon(event):
    """Fourth round: Blade Runner and Cyberpunk, "a little bolder". Compared to the first round: saturation on
    the brass, short echoes instead of long tails, louder, a digital accent here and there."""
    if event == "heard":
        n = samples(90)
        brass = cs80(glide(740, 1109, n), 90, cutoff=3200, attack=4, release=40)
        spark = in_sequence(silence(30), bitcrush(fm(2217, 40, 2.0, 1.2), 7) * 0.35)
        return pcm(echo(punchy(together(brass, spark), 2.2), 90, 0.2, 0.25, tail_ms=250), 0.32)
    if event == "noise":
        n = samples(200)
        down = cs80(glide(622, 311, n), 200, cutoff=2400, attack=5, release=80)
        return pcm(in_sequence(punchy(down, 2.0), bitcrush(glitch(90, 6, 5), 5) * 0.4), 0.26)
    if event == "error":
        n = samples(340)
        detuned = chord((311, 330), 340, cutoff=2000, attack=10, release=120, detune=12)
        gate = ((np.arange(n) // samples(45)) % 2 == 0) * 0.6 + 0.4
        return pcm(room(punchy(detuned * gate, 2.6), 0.15), 0.34)
    if event == "working":
        n = samples(30)
        ping = oscillator(1568, n) * decay(n, 10)
        return pcm(in_sequence(silence(300), echo(punchy(ping, 1.5), 140, 0.3, 0.4, tail_ms=300)), 0.12)
    if event == "heartbeat":
        p = punchy(note(990, 45, "tri", 3, 30), 1.5)
        return pcm(in_sequence(p, silence(80), p), 0.16)
    if event == "end":
        stab = chord((440, 554, 659), 160, cutoff=2600, attack=6, release=120)
        return pcm(reverb(punchy(stab, 1.8), 0.3, 400), 0.22)
    if event == "new":
        beeps = [punchy(fm(f, 35, 2.0, 0.8), 1.5) for f in (1568, 2093, 1760, 2349)]
        return pcm(echo(in_sequence(*[in_sequence(b, silence(12)) for b in beeps]), 100, 0.2, 0.25, tail_ms=250), 0.24)
    if event in ("open", "close"):
        n = samples(450)
        f0, f1 = (262, 523) if event == "open" else (523, 262)
        engine = punchy(cs80(glide(f0, f1, n), 450, cutoff=2200, attack=120, release=150, detune=12), 2.0)
        data = bitcrush(fm(2093 if event == "open" else 1047, 50, 2.0, 1.5), 6) * 0.5
        return pcm(room(in_sequence(engine, data) if event == "open" else in_sequence(data, engine), 0.2), 0.28)
    if event == "waiting":
        question = in_sequence(cs80(523, 110, cutoff=2800), silence(25), cs80(622, 150, cutoff=2800))
        return pcm(echo(punchy(question, 2.0), 110, 0.2, 0.25, tail_ms=250), 0.26)
    return b""


def soft(freq, ms, attack=8, release=None, warmth=1.3):
    """A sine with a touch of warmth (light saturation): serious, not a beep."""
    n = samples(ms)
    x = np.tanh(warmth * oscillator(freq, n)) / np.tanh(warmth)
    return x * envelope(n, attack, release if release is not None else ms * 0.7)


def air(ms, low=900, high=3000, seed=61, rising=True):
    """A breath of filtered noise that rises (or falls): the "cinema" part of the sound."""
    n = samples(ms)
    shape = np.linspace(0, 1, n) ** 2 if rising else np.linspace(1, 0, n) ** 2
    return band(white_noise(n, seed), low, high) * shape


def big_hall(x, mix=0.25, tail_ms=700):
    return reverb(x, mix, tail_ms=tail_ms, length=0.8)


def _cinema(event):
    if event == "heard":   # a clean confirmation: a perfect fifth, short
        x = together(soft(880, 140, 4), soft(1320, 140, 4) * 0.5)
        return pcm(big_hall(x, 0.2, 400), 0.26)
    if event == "noise":     # nothing heard: two soft falling notes
        x = in_sequence(soft(587, 110, 6), silence(20), soft(494, 170, 6))
        return pcm(big_hall(x, 0.2, 400), 0.22)
    if event == "error":     # a low pulsing throb, like a real alert
        n = samples(520)
        pulse = 0.55 + 0.45 * np.sin(2 * np.pi * 6 * np.arange(n) / SR)
        body = together(soft(392, 520, 10, 200, 1.8), soft(784, 520, 10, 200) * 0.3) * pulse
        return pcm(big_hall(body + 0.15 * air(520, 300, 900, 63, False), 0.2, 500), 0.32)
    if event == "working":     # a soft falling beat, barely perceptible
        n = samples(90)
        return pcm(in_sequence(silence(300), soft(glide(523, 392, n), 90, 3, 70)), 0.10)
    if event == "heartbeat":
        b = soft(659, 70, 4, 50)
        return pcm(in_sequence(b, silence(110), b * 0.8), 0.14)
    if event == "end":       # end of transmission: a falling fifth that fades out in the hall
        x = together(soft(1320, 260, 6, 230), in_sequence(silence(90), soft(880, 360, 6, 330)))
        return pcm(big_hall(x, 0.3, 700), 0.2)
    if event == "new":      # new data: a breath that rises and ends on a clear note
        x = in_sequence(air(320, 1000, 3200, 65), soft(1760, 260, 3, 240))
        return pcm(big_hall(x, 0.25, 600), 0.24)
    if event == "open":   # system online: a chord that comes in softly
        n = samples(700)
        pad = sum(soft(f, 700, 350, 250) for f in (330, 495, 660)) / 3
        return pcm(big_hall(pad + 0.2 * air(700, 800, 2600, 67), 0.25, 700), 0.28)
    if event == "close":
        n = samples(700)
        bend = np.linspace(1, 0.94, n)
        pad = sum(soft(f * bend, 700, 30, 600) for f in (660, 495, 330)) / 3
        return pcm(big_hall(pad + 0.2 * air(700, 800, 2600, 69, False), 0.25, 700), 0.28)
    if event == "waiting":     # a question: a rising fourth
        x = in_sequence(soft(660, 180, 6), silence(40), soft(880, 260, 6))
        return pcm(big_hall(x, 0.25, 500), 0.24)
    return b""


BAR = ((1, 1.0, 900), (2.76, 0.35, 400), (5.4, 0.12, 200))   # metal bar: the on-board chime


def chime(freq, ms=900):
    return strike(freq, BAR, ms, 0.15)


def _booth(event):
    if event == "heard":
        return pcm(chime(1046, 500), 0.24)
    if event == "noise":
        return pcm(chime(523, 450), 0.2)
    if event == "error":     # like the cabin alert: three identical chimes
        return pcm(together(*[in_sequence(silence(i * 280), chime(659, 700)) for i in range(3)]), 0.3)
    if event == "working":
        return pcm(in_sequence(silence(300), chime(1568, 200) * 0.6), 0.08)
    if event == "heartbeat":
        return pcm(chime(880, 400), 0.12)
    if event == "end":       # the low "ding" at the end of an announcement
        return pcm(chime(784, 900), 0.2)
    if event == "new":      # high-low, the cabin call
        return pcm(together(chime(1175, 900), in_sequence(silence(350), chime(880, 1000))), 0.24)
    if event == "open":
        return pcm(together(chime(784, 900), in_sequence(silence(300), chime(1046, 1000))), 0.24)
    if event == "close":
        return pcm(together(chime(1046, 900), in_sequence(silence(300), chime(784, 1000))), 0.24)
    if event == "waiting":     # ding-dong, like a passenger call
        return pcm(together(chime(659, 900), in_sequence(silence(380), chime(880, 1000))), 0.24)
    return b""


def hall_beep(variant, volume=0.05):
    """Distant control room: a beep from equipment with room echo. The bot plays one now and then
    while sessions are working, in place of the ticks."""
    notes = (880, 1175, 1319, 1568)
    b = note(notes[variant % len(notes)], 40, attack=2, release=20)
    return pcm(reverb(in_sequence(silence(10), b), 0.6, 800), volume)


_THEMES = {"classic": _classic, "blade": _blade, "cyber": _cyber, "radio": _radio,
         "walkie": _walkie, "mission": _mission, "objects": _objects, "neon": _neon,
         "cinema": _cinema, "booth": _booth}


def sound(theme, event):
    """PCM of the event in the theme, b"" if the theme has no sound for that event."""
    return _THEMES.get(canonical_theme(theme), _classic)(event)


def theme(name):
    """All the sounds of a theme, computed once: {event: pcm}."""
    return {e: sound(name, e) for e in EVENTS}


# --- per-agent signatures: a different motif per session, before each message ---

_MOTIFS = {   # notes in Hz: up, down, three up, three down, one repeated
    "alpha": (659, 988), "bravo": (988, 659), "charlie": (587, 784, 988),
    "delta": (988, 784, 587), "echo": (784, 784, 784),
}


def signature(theme_name, name):
    theme_name = canonical_theme(theme_name)
    notes = _MOTIFS.get(name)
    if theme_name in ("radio", "walkie") or notes is None:
        return _morse(name[:1])
    if theme_name == "mission":   # in the mission theme the start tone is the same for everyone: the voice tells them apart
        return pcm(quindar(True), 0.12)
    if theme_name == "objects":
        hits = [in_sequence(silence(i * 110), strike(f, FULL_MARIMBA, 400, 0.3)) for i, f in enumerate(notes)]
        return pcm(room(punchy(together(*hits), 1.4)), 0.26)
    if theme_name == "cyber":
        seq = in_sequence(*[in_sequence(bitcrush(fm(f, 50, 2.0, 1.2), 6), silence(15)) for f in notes])
        return pcm(seq, 0.2)
    if theme_name == "blade":
        seq = in_sequence(*[in_sequence(cs80(f, 70, cutoff=2400, attack=6, release=40), silence(20)) for f in notes])
        return pcm(echo(seq, 130, 0.25, 0.3), 0.2)
    if theme_name == "cinema":
        seq = in_sequence(*[in_sequence(soft(f, 120, 6), silence(30)) for f in notes])
        return pcm(big_hall(seq, 0.25, 500), 0.22)
    if theme_name == "booth":
        return pcm(together(*[in_sequence(silence(i * 200), chime(f, 700)) for i, f in enumerate(notes)]), 0.22)
    if theme_name == "neon":
        seq = in_sequence(*[in_sequence(cs80(f, 70, cutoff=3000, attack=4, release=40), silence(20)) for f in notes])
        return pcm(echo(punchy(seq, 2.0), 100, 0.2, 0.25, tail_ms=200), 0.24)
    return pcm(in_sequence(*[in_sequence(note(f, 70), silence(20)) for f in notes]), 0.2)


def _morse(letter, hz=800, dot_ms=45):
    parts = []
    for symbol in MORSE.get(letter.lower(), "."):
        parts += [note(hz, dot_ms * (3 if symbol == "-" else 1), attack=3, release=8), silence(dot_ms)]
    return pcm(in_sequence(*parts), 0.2)


# --- voice and bed ---------------------------------------------------------------

REPLICANT = {   # band, bits, samples held, echo (ms, feedback, mix)
    "light": ((350, 3200), None, 1, (45, 0.25, 0.25)),
    "medium": ((380, 3100), 11, 1, (50, 0.25, 0.25)),   # 9 bits was too dirty: 11 bits and a slightly shorter echo
    "strong": ((350, 3200), 7, 2, (70, 0.25, 0.25)),
}


def replicant(voice_pcm, level="light", strong=False):
    """Piper's voice as if from an on-board radio: narrow band, short echo, a few bits fewer."""
    (low, high), bit, hold, (ms, fb, mix) = REPLICANT["strong" if strong else LEVEL_ALIASES.get(level, level)]
    x = band(from_pcm(voice_pcm), low, high)
    if bit:
        x = bitcrush(x / (np.max(np.abs(x)) or 1), bit, hold)
    return pcm(echo(x, ms, fb, mix, tail_ms=150), 0.8)


def bed(theme_name, seconds=12):
    """Background while sessions work: fades in and out softly."""
    theme_name = BED_ALIASES.get(theme_name, canonical_theme(theme_name))
    n = samples(seconds * 1000)
    t = np.arange(n) / SR
    if theme_name == "channel":     # open channel: a hiss just above silence, like a radio left on
        x = band(white_noise(n, 51), 400, 2800)
        return pcm(x * envelope(n, 1500, 2500), 0.02)
    if theme_name == "breath":    # almost nothing: every eight seconds a breath that rises and falls
        x = np.zeros(n)
        wave = band(white_noise(samples(1800), 53), 300, 1200)
        wave *= np.sin(np.linspace(0, np.pi, len(wave))) ** 2
        for start in range(samples(1000), n - len(wave), samples(8000)):
            x[start:start + len(wave)] += wave
        return pcm(x, 0.05)
    if theme_name == "hall":       # distant control room: now and then a beep from equipment, with room echo
        rng = np.random.default_rng(57)
        x = np.zeros(n)
        t0 = samples(800)
        while t0 < n - samples(400):
            b = note(float(rng.choice([880, 1175, 1319, 1568])), 40, attack=2, release=20)
            x[t0:t0 + len(b)] += b
            t0 += samples(float(rng.uniform(3500, 7000)))
        return pcm(reverb(x, 0.6, 800)[:n] * envelope(n, 500, 1000), 0.03)
    if theme_name == "cyber":
        hum = oscillator(110, n, "square") * (0.6 + 0.4 * np.sin(2 * np.pi * 2 * t))   # pulses twice a second
        x = band(hum, 200, 1200) * 0.6
        rng = np.random.default_rng(41)
        for start in rng.integers(0, n - samples(80), 6):
            b = bitcrush(fm(float(rng.uniform(1500, 2600)), 60), 6) * 0.25
            x[start:start + len(b)] += b
    else:   # blade: a long detuned chord that breathes
        x = sum(cs80(f, seconds * 1000, cutoff=900, attack=2000, release=2000, detune=14, vibrato=10)
                for f in (220, 330, 392)) / 3
        x *= 0.7 + 0.3 * np.sin(2 * np.pi * 0.15 * t)
        x = reverb(x, 0.4, 1500)[:n]
    return pcm(x * envelope(n, 1500, 2500), 0.08)
