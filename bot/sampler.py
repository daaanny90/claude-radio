"""
Sound sampler, to listen to on the board before choosing a theme:
    python sampler.py [--round 2] [--bot http://127.0.0.1:8790] [--files-only /tmp/sampler]

Runs inside the bot container (Piper is needed). Posts to the board, under the "radio" session, one item per
group: the four themes with a full scene and the individual sounds, the per-agent signatures, one voice per
agent, the "replicant" voice, the bed while sessions work. With --files-only it just writes the WAVs.
"""
import argparse
import base64
import gc
import io
import json
import os
import urllib.request
import wave

import sounds
from radio_bot import PIPER_DIR, PIPER_VOICE, Speaker

# One voice per agent among those already downloaded: male and female, American and British, to tell them apart by ear
AGENT_VOICES = {
    "alpha": "en_US-lessac-medium",
    "bravo": "en_US-hfc_female-medium",
    "charlie": "en_GB-alan-medium",
    "delta": "en_GB-jenny_dioco-medium",
    "echo": "en_GB-northern_english_male-medium",
}
DESCRIPTIONS = {
    "heard": "at the end of a phrase: the bot got it",
    "noise": "there was no voice in the transmission",
    "error": "Whisper or the synthetic voice failed",
    "working": "while a session works, in place of the ticks",
    "heartbeat": "every 20 minutes, link alive",
    "end": "after every agent reply",
    "new": "new item on the board",
    "open-close": "session opened, then closed",
    "waiting": "an agent is waiting for your OK",
}
THEME_NAMES = {"classic": "classic, the original beeps", "blade": "Blade Runner", "cyber": "Cyberpunk", "radio": "military radio",
               "walkie": "professional walkie-talkie", "mission": "Apollo mission", "objects": "objects",
               "neon": "neon, bolder Blade Runner and Cyberpunk", "cinema": "cinema, serious sci-fi",
               "booth": "booth, on-board airliner chimes"}


def wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sounds.SR)
        w.writeframes(pcm)
    return buf.getvalue()


def pause(ms):
    return sounds.pcm(sounds.silence(ms), 0) if ms else b""


def speak(voice, phrases):
    """One voice at a time, then free the memory: memory is tight."""
    sp = Speaker(voice, PIPER_DIR)
    out = [sp.say(f) for f in phrases]
    del sp
    gc.collect()
    return out


def scene(theme, ack, reply):
    """A whole conversation: heard, confirmation, two working cues, bravo's reply, end, new item on the board."""
    s = sounds.theme(theme)
    signature = sounds.signature(theme, "bravo") if theme != "classic" else b""
    return b"".join([
        s["heard"], pause(400), ack, pause(600), s["working"], pause(1200), s["working"], pause(900),
        signature, pause(150 if signature else 0), reply, s["end"], pause(500), s["new"],
    ])


def voice_over(background, voice, from_ms=4000):
    """The voice over a background, to hear whether the volume works."""
    x = sounds.from_pcm(background).copy()
    v = sounds.from_pcm(voice)
    i = sounds.samples(from_ms)
    x[i:i + len(v)] += v[:len(x) - i]
    return sounds.pcm(x, float(abs(x).max()))


def round2():
    """Second round, after "Blade Runner is too much like a video game": realistic sounds, light backgrounds, per-agent voices."""
    ack, = speak(PIPER_VOICE, ["To bravo, media viewer zoom: run the unit tests."])
    reply, = speak(AGENT_VOICES["bravo"], ["Bravo, media viewer zoom: all tests pass. The merge request is on the board."])
    items = []
    for theme in ("walkie", "mission", "objects"):
        s = sounds.theme(theme)
        files = [(f"scene-{theme}.wav", scene(theme, ack, reply))]
        lines = [f"scene-{theme}: a whole conversation; the reply is already in bravo's voice"]
        for event in DESCRIPTIONS:
            data = s["open"] + pause(700) + s["close"] if event == "open-close" else s[event]
            if data:
                files.append((f"{theme}-{event}.wav", data))
                lines.append(f"{theme}-{event}: {DESCRIPTIONS[event]}")
        if theme == "mission":
            lines.append("(the tones are the real Apollo ones: 2525 Hz when a transmission starts, 2475 Hz when it ends)")
        items.append((f"Sounds 2, {THEME_NAMES[theme]}", "\n".join(lines), files))
    items.append(("Sounds 2, per-agent signatures",
                  "Before every message. Walkie-talkie: the initial in Morse, A B C D E. Objects: marimba motifs, "
                  "alpha two notes up, bravo two down, charlie three up, delta three down, echo one repeated. Mission: the "
                  "start tone is the same for everyone, the voice tells them apart.", [
        ("signatures-walkie.wav", b"".join(sounds.signature("walkie", n) + pause(700) for n in sounds.NATO)),
        ("signatures-objects.wav", b"".join(sounds.signature("objects", n) + pause(700) for n in sounds.NATO)),
        ("signature-mission.wav", sounds.signature("mission", "bravo")),
    ]))
    files = []
    for name in ("channel", "breath", "hall"):
        t = sounds.bed(name)
        files += [(f"bed-{name}.wav", t), (f"bed-{name}-with-voice.wav", voice_over(t, reply))]
    items.append(("Sounds 2, light background",
                  "Twelve seconds each, much quieter than the first round. Channel: the hiss of a radio left on. "
                  "Breath: almost silence, one breath every eight seconds. Hall: a distant beep of equipment now and then. "
                  "Each also with a reply on top. Once installed, the background goes quiet while you speak and under the voices.", files))
    items.append(("Sounds 2: how to listen",
                  "With headphones, starting from the scenes. Then tell me:\n"
                  "1. the theme: walkie-talkie, Apollo mission, objects (or military radio and classic from the first round)\n"
                  "2. the per-agent signatures: yes or no\n"
                  "3. the background: channel, breath, hall or none\n"
                  "4. the replicant voice from the first round: none or light\n\n"
                  "The per-agent voices are decided: they start with the next bot restart.", []))
    return items


def round3(theme="objects", fx="medium"):
    """The sounds installed in the radio: the chosen theme, voices with the filter, the hall in place of the ticks."""
    ack, = speak(PIPER_VOICE, ["To bravo, media viewer zoom: run the unit tests."])
    reply, = speak(AGENT_VOICES["bravo"], ["Bravo, media viewer zoom: all tests pass. The merge request is on the board."])
    if fx:
        ack, reply = sounds.replicant(ack, fx), sounds.replicant(reply, fx)
    s = sounds.theme(theme)
    beeps = [sounds.hall_beep(i, 0.05) for i in range(4)]
    scene3 = b"".join([s["heard"], pause(400), ack, pause(1500), beeps[1], pause(2500), beeps[3], pause(1800),
                       reply, s["end"], pause(600), s["new"]])
    files = [(f"scene-{theme}.wav", scene3)]
    for event in ("heard", "noise", "error", "end", "new", "open-close", "waiting"):
        data = s["open"] + pause(700) + s["close"] if event == "open-close" else s[event]
        files.append((f"{theme}-{event}.wav", data))
    hall = b"".join(beeps[i % 4] + pause(int(3500 + 900 * (i % 3))) for i in range(4))
    files += [("hall-while-working.wav", hall), (f"replicant-voice-{fx or 'clean'}.wav", reply)]
    return [(f"Installed sounds: {THEME_NAMES.get(theme, theme)}", f"What you hear on the radio now: theme {THEME_NAMES.get(theme, theme)}, "
             f"voices with the replicant filter {fx or 'off'}, and while the sessions work a distant beep from the hall every 3-7 "
             "seconds in place of the ticks. Everything is changed from .env: SOUND_THEME, VOICE_FX, BED, BED_LEVEL, AGENT_SIGNATURES.", files)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, default=1, help="sampler round: 1 (default), 2 or 3")
    ap.add_argument("--theme", default=os.getenv("SOUND_THEME", "objects"), help="with --round 3")
    ap.add_argument("--fx", default=os.getenv("VOICE_FX", "medium"), help="with --round 3")
    ap.add_argument("--bot", default="http://127.0.0.1:8790")
    ap.add_argument("--files-only", help="folder where to write the WAVs, without posting them to the board")
    args = ap.parse_args()
    if args.round == 2:
        publish(round2(), args)
        return
    if args.round == 3:
        publish(round3(args.theme, args.fx), args)
        return

    ack, reply, demo = speak(PIPER_VOICE, [
        "To bravo, media viewer zoom: run the unit tests.",
        "All tests pass. The merge request is on the board.",
        "Charlie: renamed. The branch is pushed and the merge request is on the board.",
    ])
    items = []   # (title, text, [(file name, pcm)])

    for theme in ("classic", "blade", "cyber", "radio"):
        s = sounds.theme(theme)
        files = [(f"scene-{theme}.wav", scene(theme, ack, reply))]
        lines = [f"scene-{theme}: a whole conversation, from your phrase to bravo's reply"]
        for event in DESCRIPTIONS:
            if event == "open-close":
                data = s["open"] + pause(700) + s["close"] if s["open"] else b""
            else:
                data = s[event]
            if data:
                files.append((f"{theme}-{event}.wav", data))
                lines.append(f"{theme}-{event}: {DESCRIPTIONS[event]}")
        if theme == "radio":
            lines.append("(working: nothing, a real radio stays quiet while you wait)")
        items.append((f"Sounds, theme {THEME_NAMES[theme]}", "\n".join(lines), files))

    signatures = []
    for theme in ("blade", "cyber", "radio"):
        signatures.append((f"signatures-{theme}.wav", b"".join(sounds.signature(theme, n) + pause(700) for n in sounds.NATO)))
    items.append(("Per-agent signatures", "A different motif per session, before each of its messages. In each file, "
                  "in order: alpha (two notes up), bravo (two down), charlie (three up), delta (three down), echo (one repeated). "
                  "In the radio theme it is the letter in Morse: A, B, C, D, E.", signatures))

    voices = []
    for name, voice in AGENT_VOICES.items():
        phrase = speak(voice, [f"{name.capitalize()} here. The zoom tests pass and the merge request is on the board."])[0]
        voices.append((f"{name}-{voice}.wav", sounds.signature("blade", name) + pause(150) + phrase))
    items.append(("One voice per agent", "The Blade Runner signature and then the proposed voice for each session, all already downloaded. "
                  f"The bot itself (confirmations, alerts) keeps the current voice, {PIPER_VOICE}.", voices))

    items.append(("Replicant voice", "The same normal phrase, with the on-board radio filter, and with the stronger filter.", [
        ("voice-normal.wav", demo),
        ("voice-replicant.wav", sounds.replicant(demo)),
        ("voice-replicant-strong.wav", sounds.replicant(demo, strong=True)),
    ]))

    blade = sounds.bed("blade")
    with_voice = bytearray(blade)
    start = len(with_voice) // 2 - len(reply) // 2
    start -= start % 2
    mixed = (sounds.from_pcm(bytes(with_voice[start:start + len(reply)])) + sounds.from_pcm(reply))
    with_voice[start:start + len(reply)] = sounds.pcm(mixed, float(abs(mixed).max()))
    items.append(("Background while they work", "Twelve seconds of background: fades in and out softly. "
                  "The third file has a reply on top, to hear the right volume.", [
        ("bed-blade.wav", blade), ("bed-cyber.wav", sounds.bed("cyber")),
        ("bed-blade-with-voice.wav", bytes(with_voice)),
    ]))

    items.append(("Sound sampler: how to listen",
                  "With headphones, starting from the scenes: each one is a whole conversation with its theme.\n\n"
                  "Then tell me, aloud or in chat:\n"
                  "1. the theme: classic, Blade Runner, Cyberpunk or military radio\n"
                  "2. the per-agent signatures: yes or no\n"
                  "3. one voice per agent: yes or no, and whether any should change\n"
                  "4. the replicant voice: none, light or strong\n"
                  "5. the background while they work: none, Blade Runner or Cyberpunk\n\n"
                  "Until then the radio stays as it is.", []))

    publish(items, args)


def publish(items, args):
    if args.files_only:
        os.makedirs(args.files_only, exist_ok=True)
        for _, _, files in items:
            for name, data in files:
                with open(os.path.join(args.files_only, name), "wb") as f:
                    f.write(wav(data))
        print(f"written to {args.files_only}")
        return
    for title, text, files in items:
        body = {"session": "radio", "title": title, "text": text, "format": "text",
                "files": [{"name": n, "mime": "audio/wav", "data": base64.b64encode(wav(d)).decode()} for n, d in files]}
        req = urllib.request.Request(args.bot + "/show", json.dumps(body).encode(), {"Content-Type": "application/json"})
        print(title, json.loads(urllib.request.urlopen(req, timeout=60).read()))


if __name__ == "__main__":
    main()
