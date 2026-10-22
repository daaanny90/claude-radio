"""
Check from the server, without a phone: a fake client says a phrase
(synthesized with Piper) and checks that the bot transcribes it and replies
by voice. Prints the transcript and the timings.

Usage:  docker compose exec -T bot python smoke_test.py ["phrase to say"]
"""
import csv
import re
import sys
import time

import radio_bot  # applies the ssl patch and reads the configuration
import pymumble_py3 as pymumble
from pymumble_py3.constants import PYMUMBLE_CLBK_SOUNDRECEIVED, PYMUMBLE_CONN_STATE_CONNECTED

PHRASES = {
    "it": "Prova uno due tre. Apri il progetto e fammi sapere quando hai finito.",
    "en": "Testing one two three. Open the project and let me know when you are done.",
}
PHRASE = sys.argv[1] if len(sys.argv) > 1 else PHRASES.get(radio_bot.WHISPER_LANGUAGE, PHRASES["en"])
OUTCOMES = ("transcript", "noise", "hallucination", "whisper_error", "tts_error", "utterance")
received = []


def on_sound(user, chunk):
    if user["name"] == radio_bot.BOT_NAME:
        received.append(chunk.pcm)


def words(text):
    return set(re.findall(r"[a-zà-ú0-9]+", text.lower()))


def read_log():
    with open(radio_bot.EventLog(radio_bot.LOG_DIR).path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    print(f"Synthesizing with Piper ({radio_bot.PIPER_VOICE}): \"{PHRASE}\"")
    audio = radio_bot.Speaker(radio_bot.PIPER_VOICE, radio_bot.PIPER_DIR).say(PHRASE)
    print(f"Audio of {len(audio) / radio_bot.BYTES_PER_MS / 1000:.1f} s")
    rows_before = len(read_log())

    client = pymumble.Mumble(radio_bot.HOST, "probe", port=radio_bot.PORT,
                             password=radio_bot.PASSWORD, reconnect=False)
    client.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, on_sound)
    client.set_receive_sound(True)
    client.start()
    client.is_ready()
    time.sleep(1)
    if client.connected != PYMUMBLE_CONN_STATE_CONNECTED:
        print(f"FAILED: cannot connect to {radio_bot.HOST}:{radio_bot.PORT}")
        sys.exit(1)

    client.sound_output.add_sound(audio)
    outcome = None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and outcome is None:
        time.sleep(0.5)
        for row in read_log()[rows_before:]:
            if row["user"] == "probe" and row["event"] in OUTCOMES:
                outcome = row
    # The voice reply arrives in real time: wait until no more audio comes in for 1.5 s
    until = time.monotonic() + 30
    while outcome is not None and time.monotonic() < until:
        n = len(received)
        time.sleep(1.5)
        if len(received) == n:
            break
    client.stop()

    if outcome is None:
        print("FAILED: the bot logged no outcome within 120 s (check docker compose logs bot)")
        sys.exit(1)
    reply_ms = len(b"".join(received)) / radio_bot.BYTES_PER_MS
    print(f"Outcome: {outcome['event']}  {outcome['detail']}")
    print(f"Transcribed: \"{outcome['text']}\"")
    print(f"Audio received from the bot: {reply_ms:.0f} ms (beep + reply)")
    common = words(PHRASE) & words(outcome["text"])
    share = len(common) / max(1, len(words(PHRASE)))
    ok = outcome["event"] == "transcript" and reply_ms > 1000 and share >= 0.5
    print(f"{'OK' if ok else 'FAILED'}: {len(common)}/{len(words(PHRASE))} words recognized")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
