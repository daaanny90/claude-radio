"""
Summary of the radio log.

Usage:
    python summary.py              # the whole log
    python summary.py today        # today only
    python summary.py 2026-10-15   # that day only
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import date

LOG_PATH = os.path.join(os.getenv("LOG_DIR", "/logs"), "radio-log.csv")
MAX_PHRASES = 40   # how many transcribed phrases to print at the bottom


def parse_detail(detail):
    values = {}
    for part in (detail or "").split():
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return values


def mean(values):
    return sum(values) / len(values) if values else 0.0


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else None
    if day == "today":
        day = date.today().isoformat()

    if not os.path.exists(LOG_PATH):
        print(f"No log found in {LOG_PATH}")
        return

    users = defaultdict(lambda: {
        "phrases": 0, "speech_s": 0.0, "blips": 0, "noise": 0, "discarded": 0, "errors": 0,
        "latencies": [], "whisper": [], "tts": [],
        "disconnects": 0, "offline_s": 0.0, "offline_max_s": 0.0, "offline_now": False,
    })
    phrases = []
    bot_drops = 0
    heartbeats = 0
    first_ts = last_ts = None

    with open(LOG_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if day and not row["timestamp"].startswith(day):
                continue
            first_ts = first_ts or row["timestamp"]
            last_ts = row["timestamp"]
            event, user, detail = row["event"], row["user"], parse_detail(row["detail"])
            text = row.get("text") or ""
            clock = row["timestamp"][11:19]
            u = users[user]
            if event in ("utterance", "transcript"):
                u["phrases"] += 1
                u["speech_s"] += int(detail.get("dur_ms", 0)) / 1000
                if event == "transcript":
                    u["latencies"].append(int(detail.get("latency_ms", 0)) / 1000)
                    u["whisper"].append(int(detail.get("whisper_ms", 0)) / 1000)
                    u["tts"].append(int(detail.get("tts_ms", 0)) / 1000)
                    phrases.append((clock, user, text))
            elif event == "noise":
                u["noise"] += 1
            elif event == "hallucination":
                u["discarded"] += 1
                phrases.append((clock, user, f"[discarded] {text}"))
            elif event in ("whisper_error", "tts_error"):
                u["errors"] += 1
                phrases.append((clock, user, f"[{event}] {text}"))
            elif event == "blip":
                u["blips"] += 1
            elif event == "user_left":
                u["disconnects"] += 1
                u["offline_now"] = True
            elif event == "user_joined":
                u["offline_now"] = False
                if "offline_s" in detail:
                    secs = float(detail["offline_s"])
                    u["offline_s"] += secs
                    u["offline_max_s"] = max(u["offline_max_s"], secs)
            elif event in ("bot_disconnected", "bot_crashed"):
                bot_drops += 1
            elif event == "heartbeat":
                heartbeats += 1

    if first_ts is None:
        print("No events in the requested period.")
        return

    print(f"Period: {first_ts}  ->  {last_ts}")
    print(f"Bot drops: {bot_drops}   Heartbeats sent: {heartbeats}\n")
    for name, u in sorted(users.items()):
        if name in ("", os.getenv("BOT_NAME", "radio")):
            continue
        print(f"{name}")
        print(f"  phrases transcribed: {u['phrases']}  ({u['speech_s']:.0f} s of speech)")
        print(f"  noise (the VAD found no speech): {u['noise']}   "
              f"hallucinations discarded: {u['discarded']}   whisper/piper errors: {u['errors']}")
        if u["latencies"]:
            print(f"  latency end of phrase -> reply: mean {mean(u['latencies']):.1f} s, max {max(u['latencies']):.1f} s"
                  f"  (whisper {mean(u['whisper']):.1f} s, piper {mean(u['tts']):.1f} s)")
        print(f"  blips (transmissions too short, often pocket noise): {u['blips']}")
        print(f"  disconnections: {u['disconnects']}  total offline: {u['offline_s']:.0f} s"
              f"  longest: {u['offline_max_s']:.0f} s")
        if u["offline_now"]:
            print("  currently disconnected")
        print()

    if phrases:
        print(f"Last phrases ({min(len(phrases), MAX_PHRASES)} of {len(phrases)}):")
        for clock, user, text in phrases[-MAX_PHRASES:]:
            print(f"  {clock}  {user:<8} {text}")


if __name__ == "__main__":
    main()
