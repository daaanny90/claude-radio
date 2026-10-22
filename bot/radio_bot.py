"""
Radio: the bot listens, transcribes, acts as a switchboard for Claude Code sessions and answers by voice.

It joins the main channel of the Mumble server. When an utterance ends it plays a beep and
sends the audio to whisper.cpp (VAD + transcription). Then the switchboard decides:

  "open <project> [ticket 1234] [request]"     opens a session: free NATO name, spoken
                                               confirmation, then the launcher on the
                                               host creates the worktree and the tmux session
  "<name>, <request>"                          to the session with that name
  utterance without a name                     to the last session that spoke
  "yes" / "no"                                 answer to the bot's last question
                                               (permission, open, close); with no
                                               pending question it goes to the session
  "status" / "status <name>"                   what the sessions are doing
  "repeat" / "repeat <name>"                   reads the last message again
  "quiet [N minutes]" / "speak"                keeps replies to itself, then reads them
  "<name>, close"                              closes the session (the worktree stays)

The commands are English. A few Italian (and German) synonyms are accepted too, for Italian
speakers: apri, stato, ripeti, silenzio, parla, chiudi, ferma/basta, sì/vai, nein...

With no session connected it repeats "I heard: ..." (step 2). With ECHO=1 it repeats the audio.

HTTP API (port API_PORT, published on localhost only):
  GET  /events?session=name   SSE for a session's adapter (message, permission)
  POST /say        {session, text, kind}   Claude speaks (kind: answer, progress, done, blocked)
  POST /permission {session, request_id, tool_name, description, input_preview}
  GET  /launcher/events       SSE for the launcher (open, close)
  POST /launcher/result       {name, ok, dir, detail, closed}
  POST /inject     {text, user}   for tests: as if it were a transcribed utterance
  POST /show       {session, title, text, format, files}   puts an item on the board
  POST /operator/open  {target, request}   the operator opens a session (registered project or folder)
  POST /operator/close {names}             the operator closes sessions, without asking: it already asked
  POST /record     {action: start|stop}   recording for demos, then onto the board
  POST /record/voice {text, voice}   for recorded demos: a line from the user in a synthetic voice, file only
  POST /move       {session, dir, brief}   the session moves to another folder (from the channel's move tool)
  POST /topic      {session, topic}   a session's topic (replies carry it in /say too)
  POST /board/code               board access code, without speaking it (for the desk and the tests)
  GET  /sessions   state of the sessions and of the pending questions
"""
import base64
import csv
import difflib
import io
import itertools
import json
import logging
import math
import os
import queue
import random
import re
import signal
import socket
import ssl
import struct
import sys
import threading
import time
import unicodedata
import wave
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import requests

import board
import sounds

# pymumble 1.6.1 calls ssl.wrap_socket, which Python 3.12 removed.
# Without this patch the bot crashes on the first connection.
if not hasattr(ssl, "wrap_socket"):
    def _wrap_socket(sock, keyfile=None, certfile=None, server_side=False, **_ignored):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # The Mumble server uses a self-signed certificate, and the traffic
        # stays on a private network (Tailscale) anyway: no verification, as pymumble did.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if certfile:
            ctx.load_cert_chain(certfile, keyfile)
        return ctx.wrap_socket(sock, server_side=server_side)

    ssl.wrap_socket = _wrap_socket

import pymumble_py3 as pymumble  # noqa: E402  (must come after the patch)
from pymumble_py3.constants import (  # noqa: E402
    PYMUMBLE_CLBK_CONNECTED,
    PYMUMBLE_CLBK_DISCONNECTED,
    PYMUMBLE_CLBK_SOUNDRECEIVED,
    PYMUMBLE_CLBK_USERCREATED,
    PYMUMBLE_CLBK_USERREMOVED,
)


def env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


HOST = os.getenv("MUMBLE_HOST", "mumble")
PORT = int(env_float("MUMBLE_PORT", 64738))
PASSWORD = os.getenv("MUMBLE_PASSWORD", "")
BOT_NAME = os.getenv("BOT_NAME", "radio")
SILENCE_MS = env_float("SILENCE_MS", 700)          # silence that ends an utterance
MIN_UTTERANCE_MS = env_float("MIN_UTTERANCE_MS", 300)  # below this duration it is a "blip"
MAX_UTTERANCE_S = env_float("MAX_UTTERANCE_S", 60)  # beyond this the utterance is closed anyway
HEARTBEAT_MIN = env_float("HEARTBEAT_MIN", 0)      # 0 = off
ECHO = os.getenv("ECHO", "0") != "0"               # 1 = repeat the audio instead of transcribing
ECHO_DELAY_S = env_float("ECHO_DELAY_S", 0)        # with ECHO, wait before repeating (transport test)
REPLY_PREROLL_MS = env_float("REPLY_PREROLL_MS", 0)  # silence before the reply: wakes up Bluetooth headphones
AUDIO_PER_PACKET = env_float("AUDIO_PER_PACKET", 0.02)  # seconds of audio per Mumble packet (0.02-0.06)
LOG_DIR = os.getenv("LOG_DIR", "/logs")

WHISPER_URL = os.getenv("WHISPER_URL", "http://whisper:8080/inference")   # accurate model
WHISPER_FAST_URL = os.getenv("WHISPER_FAST_URL", "")                       # fast model, optional
WHISPER_FAST_MAX_MS = env_float("WHISPER_FAST_MAX_MS", 2500)               # utterances up to this length go to the fast one
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "it")
WHISPER_PROMPT = os.getenv("WHISPER_PROMPT", "")   # vocabulary hint for Whisper
WHISPER_BEAM_SIZE = int(env_float("WHISPER_BEAM_SIZE", 1))
PIPER_DIR = os.getenv("PIPER_DIR", "/models/piper")
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_US-lessac-medium")
# One voice per session, to tell them apart by ear: "alpha=en_US-lessac-medium,bravo=..." (empty = all use PIPER_VOICE).
# They are loaded when the session connects and freed when it leaves: each voice takes memory
# Sounds and voice: theme (classic, objects, walkie, mission...: see sounds.py), "replicant" filter on the voices
# (light, medium, strong; empty = clean voice), background while the sessions work (hall; empty = the ticks),
# sound signature of each session before its messages
SOUND_THEME = sounds.canonical_theme(os.getenv("SOUND_THEME", "classic"))
VOICE_FX = os.getenv("VOICE_FX", "")
BED = sounds.BED_ALIASES.get(os.getenv("BED", ""), os.getenv("BED", ""))
BED_LEVEL = env_float("BED_LEVEL", 0.05)
AGENT_SIGNATURES = env_float("AGENT_SIGNATURES", 0) > 0
RECORD_MAX_S = env_float("RECORD_MAX_S", 480)      # a recording ends by itself after eight minutes
RECORD_RATE = 16000                                # in the file: 16 kHz is enough for voice and sounds, and eight minutes fit on the board
AGENT_VOICES = dict(kv.split("=", 1) for kv in os.getenv("AGENT_VOICES", "").replace(" ", "").split(",") if "=" in kv)
PIPER_LENGTH_SCALE = env_float("PIPER_LENGTH_SCALE", 1.0)   # 1.2 = speaks 20% slower
REPLY_PREFIX = os.getenv("REPLY_PREFIX", "I heard:")
API_PORT = int(env_float("API_PORT", 8790))
BOARD_PORT = int(env_float("BOARD_PORT", 8791))     # board for the phone (0 = off)
BOARD_DIR = os.getenv("BOARD_DIR", "/board")
BOARD_KEEP = int(env_float("BOARD_KEEP", 100))      # how many items it keeps, the oldest are deleted
BOARD_URL = os.getenv("BOARD_URL", "")              # address of the board, to give back to the agents
MAX_POST_BYTES = 40 * 1024 * 1024                   # cap for a POST to the API (base64 screenshots)
PROJECTS_FILE = os.getenv("PROJECTS_FILE", "/config/projects.json")
STATE_DIR = os.getenv("STATE_DIR", "/state")        # state files written by the Claude Code hooks
QUIET_DEFAULT_MIN = env_float("QUIET_DEFAULT_MIN", 30)
# A question from a session stays open until you answer it, for at most this many seconds
ASK_OPEN_S = env_float("ASK_OPEN_S", 600)
# "Who is that for?": for this many seconds it is enough to say the name, and the held utterance goes to it
WHO_FOR_S = env_float("WHO_FOR_S", 60)
# A session opened without a task waits for the first one: for this many seconds unnamed utterances go to it
FRESH_S = env_float("FRESH_S", 900)
# After a cut at MAX_UTTERANCE_S, a transmission restarting within this many seconds continues the same utterance
CONTINUATION_GAP_S = env_float("CONTINUATION_GAP_S", 3)
# How long to wait for the voice of an agent that is still loading, before speaking with the radio's own
VOICE_WAIT_S = env_float("VOICE_WAIT_S", 12)
# The host's home folder, to resolve the ~ paths coming from the sessions (the bot runs in Docker)
HOST_HOME = os.getenv("HOST_HOME", "")
ACK_MODE = os.getenv("ACK_MODE", "echo")              # confirmation of the understood utterance: echo | short | name | beep
ACK_MAX_WORDS = int(env_float("ACK_MAX_WORDS", 30))   # with echo, how many words it repeats at most
TICK_S = env_float("TICK_S", 15)                      # discreet tick while a session works (0 = off)
PROGRESS_S = env_float("PROGRESS_S", 45)              # progress sentence while it works (0 = off)
PROGRESS_CHANGE_S = env_float("PROGRESS_CHANGE_S", 0)  # announce an activity change (file, command, delegation) if at least this much time has passed (0 = off: Claude says the milestones)
MIC_OPEN_WARN_S = env_float("MIC_OPEN_WARN_S", 90)    # warning if the microphone stays open (0 = off)
MIC_OPEN_REPEAT_S = env_float("MIC_OPEN_REPEAT_S", 120)  # then it repeats it now and then, with the waiting sentences
STATE_STALE_S = 900                                   # state older than this: the session is not working
AWAIT_MAX_S = 180                                     # utterance sent and no sign of life: stop waiting
FALLBACK_WORDS = 60                                   # words read from the terminal if Claude did not use reply
FALLBACK_DELAY_S = 20                                 # wait before reading them: with a background subagent the turn restarts by itself

SAMPLE_RATE = 48000          # pymumble works at 48 kHz, 16 bit, mono
WHISPER_RATE = 16000         # Whisper wants 16 kHz
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000

# Phrases Whisper makes up on silence or noise: they come from the subtitles it was trained on.
# The VAD avoids them almost always, this is the second filter. The patterns are Italian subtitle
# boilerplate ("sottotitoli" = subtitles, "grazie per aver guardato" = thanks for watching,
# "iscriviti al canale" = subscribe to the channel), matching WHISPER_LANGUAGE=it.
HALLUCINATIONS = re.compile(
    r"sottotitoli|amara\.org|grazie per aver guardato|iscriviti al canale|www\.|\[musica\]|♪",
    re.IGNORECASE,
)

# Short answers to a bot question, as Whisper transcribes them
# (sì, si, vai, ok vai: Italian synonyms for Italian speakers; nein: German)
VERDICT_YES = {"yes", "yeah", "yep", "yup", "ok", "okay", "sure", "go", "go ahead", "approve",
               "approved", "allow", "allowed", "do it", "confirm", "confirmed", "sì", "si", "vai", "ok vai"}
VERDICT_NO = {"no", "nope", "deny", "denied", "stop", "don't", "dont", "negative", "cancel", "reject", "nein"}

# Session names: NATO alphabet, assigned in order. Whisper sometimes spells them differently.
NATO = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
        "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango",
        "uniform", "victor", "whiskey", "xray", "yankee", "zulu"]
NAME_ALIASES = {"alfa": "alpha", "charly": "charlie", "charley": "charlie", "charli": "charlie", "eco": "echo",
                "whisky": "whiskey", "x ray": "xray", "x-ray": "xray", "juliett": "juliet", "julia": "juliet"}
# The name at the start of the utterance, with or without a comma: Whisper does not always write the comma
# ("Delta Open UI Library." must go to delta, not to the last session that spoke)
LEAD_WORD = re.compile(r"[\W_]*([A-Za-z]+(?:[ -]ray)?)\b([\s,.:;!?-]*)")
# Words Whisper leaves before the name, or from someone correcting themselves: "Okay Charlie, push", "Echo, no, Charlie, ..."
LEAD_FILLERS = {"ok", "okay", "hey", "um", "uh", "no", "sorry", "wait"}
# Misspelled names that no alias covers ("Foxfot," meant foxtrot): they are fuzzy-matched against the open
# sessions. With a comma a resemblance is enough, without one it takes more. Real words that look too similar stay words
FUZZY_NAME_PUNCT = 0.75
FUZZY_NAME_BARE = 0.86
NOT_NAMES = {"delete", "delay", "detail", "charge", "brave", "echoes", "operation", "operate", "forgot", "hotels",
             "golfing", "deleted", "delayed", "charged"}
# After a name without a comma, these mean the utterance is about that session, not to it: "Delta is stuck"
ABOUT_WORDS = {"is", "was", "has", "had", "did", "does", "will", "would", "should", "could", "can", "seems",
               "looks", "said", "says", "just", "and", "or", "isn't", "wasn't", "hasn't", "didn't", "doesn't", "won't"}

# Switchboard commands (English, with a few Italian synonyms for Italian speakers: apri, stato, ripeti,
# silenzio, parla, chiudi)
CMD_OPEN = re.compile(r"^(?:open|apri|start)\b[\s,:]*(.*)$", re.IGNORECASE | re.DOTALL)
CMD_STATUS = re.compile(r"^(?:status|stato)\b[\s,:]*([a-z\- ]+)?", re.IGNORECASE)
CMD_REPEAT = re.compile(r"^(?:repeat|ripeti|say again)\b[\s,:]*([a-z\- ]+)?", re.IGNORECASE)
CMD_QUIET = re.compile(r"^(?:quiet|silence|silenzio|mute)\b(?:\D*(\d+)\s*min)?", re.IGNORECASE)
CMD_RESUME = re.compile(r"^(?:speak|resume|unmute|go on|parla)\b", re.IGNORECASE)
CMD_CLOSE = re.compile(r"^(?:close|chiudi|shut down)\b", re.IGNORECASE)
BOARD_CODE_SPOKEN = re.compile(r"Board code: [\d, ]+\.")
# "board code", "Please give me the board code", "code for the board": any short utterance containing it will do,
# not only one that starts with it (it used to be matched at the start, so that sentence went to a session).
# bacheca/codice/per/della/del/la are the Italian words for board/code/for/of the
CMD_BOARD = re.compile(
    r"\b(?:board|bord|bored|bacheca)[\s,.:!-]*(?:code|codice|pin|login)\b"
    r"|\b(?:code|codice|pin)\s+(?:for|of|per|della|del)\s+(?:the\s+|la\s+)?(?:board|bord|bacheca)\b",
    re.IGNORECASE)
BOARD_CMD_MAX_WORDS = 10
# The operator: a dedicated Claude Code session (operator/) for the radio's own housekeeping. "Operator, close everyone"
# (operatore: Italian synonym)
OPERATOR = "operator"
CMD_OPERATOR = re.compile(r"^\W*(?:the\s+)?(?:operator|operatore)\b[\s,.:;!?]*(.*)$", re.IGNORECASE | re.DOTALL)
# Recording for demos: "record" starts it, "stop recording" ends it and puts it on the board. They come before the
# emergency word: "stop recording" must not stop the sessions (registra, fine registrazione: Italian synonyms)
CMD_RECORD_START = re.compile(r"^\W*(?:start\s+recording|record|recording\s+on|registra)[\s.!]*$", re.IGNORECASE)
CMD_RECORD_STOP = re.compile(r"^\W*(?:stop\s+recording|end\s+recording|recording\s+off|fine\s+registrazione|stop\s+registra\w*)[\s.!]*$",
                             re.IGNORECASE)
# Emergency word: "Stop." alone stops all the sessions and silences the bot (also "all stop", "break break",
# as on a radio); "Delta, stop." or "Stop delta." stops delta only. It only counts as a short utterance: "Delta, stop
# the server" stays a normal message. Stopping = an Esc in the session's terminal, via the launcher.
# ferma/fermati/fermo/basta (stop), tutto/tutti (everything/everyone), subito/adesso (now): Italian synonyms
STOP_WORDS = r"(?:stop|abort|halt|freeze|ferma|fermati|fermo|basta)"
CMD_STOP_ALL = re.compile(rf"^\W*(?:{STOP_WORDS}(?:\s+(?:all|everything|everyone|everybody|tutto|tutti|now|it|please|subito|adesso))*"
                          rf"|all\s+stop|break\s+break)[\s.!]*$", re.IGNORECASE)
CMD_STOP_ONE = re.compile(rf"^\W*{STOP_WORDS}(?:\s+(?:now|it|please|subito|adesso))?[\s.!]*$", re.IGNORECASE)
CMD_STOP_NAMED = re.compile(rf"^\W*{STOP_WORDS}[\s,]+([a-z][a-z\- ]{{1,12}}?)[\s.!]*$", re.IGNORECASE)
# "who", "who is who", "roll call": who is on and what they are working on (chi c'è, chi lavora: Italian synonyms)
CMD_WHO = re.compile(r"^(?:who(?:'s| is)?(?:\s+who)?|roll\s*call|sessions|chi c'è|chi lavora)\b", re.IGNORECASE)
# Words that don't count as a call by topic ("Zoom, push it" goes to the zoom session)
TOPIC_STOPWORDS = {"status", "repeat", "quiet", "silence", "speak", "resume", "close", "open", "start", "board",
                   "okay", "yes", "sure", "thanks", "thank", "please", "radio", "stop", "wait", "hello", "what",
                   "where", "when", "which", "tell", "give", "show", "send", "make", "this", "that", "then"}
# Ticket keys come from the projects file: each project's "ticket_prefix" (a Jira-style key such as SHOP),
# the spoken forms of it in "ticket_spoken" ("my shop" for MYSHOP) and the file's "extra_ticket_prefixes".
# A ticket said without a prefix gets the project's prefix, or DEFAULT_TICKET_PREFIX.
DEFAULT_TICKET_PREFIX = os.getenv("DEFAULT_TICKET_PREFIX", "TICKET")
# What a session opened for a ticket is asked to do, unless the project sets "ticket_request"
DEFAULT_TICKET_REQUEST = "Read the ticket and send me the plan in three sentences."


# --- audio -----------------------------------------------------------------

def tone(freq, ms, volume=0.25):
    """A beep with soft attack and release, as 16-bit PCM."""
    n = SAMPLE_RATE * ms // 1000
    fade = max(1, min(n // 4, SAMPLE_RATE // 100))
    out = bytearray()
    for i in range(n):
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = (n - i) / fade
        else:
            env = 1.0
        value = int(32767 * volume * env * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        out += struct.pack("<h", value)
    return bytes(out)


def silence(ms):
    return bytes(BYTES_PER_MS * int(ms))


BEEP_HEARD = tone(880, 120) + silence(100)
BEEP_HEARTBEAT = tone(660, 80) + silence(80) + tone(660, 80)
BEEP_ERROR = tone(330, 150) + silence(80) + tone(330, 150)   # low: something went wrong
BEEP_EMPTY = tone(440, 90, 0.2) + silence(60) + tone(370, 140, 0.2)   # heard, but there was no speech in it
# Working tick: short and quiet. The leading silence keeps Bluetooth headphones awake.
BEEP_TICK = silence(300) + tone(520, 40, 0.12) + silence(60) + tone(520, 40, 0.12)
# With a theme, the theme's sounds replace the beeps; plus end of message, waiting, board, open and close
SOUND_END = SOUND_WAIT = SOUND_NEW = SOUND_OPEN = SOUND_CLOSE = b""
if SOUND_THEME != "classic":
    _theme = sounds.theme(SOUND_THEME)
    BEEP_HEARD = _theme["heard"] or BEEP_HEARD
    BEEP_EMPTY = _theme["noise"] or BEEP_EMPTY
    BEEP_ERROR = _theme["error"] or BEEP_ERROR
    BEEP_TICK = _theme["working"] or BEEP_TICK
    BEEP_HEARTBEAT = _theme["heartbeat"] or BEEP_HEARTBEAT
    SOUND_END, SOUND_WAIT, SOUND_NEW = _theme["end"], _theme["waiting"], _theme["new"]
    SOUND_OPEN, SOUND_CLOSE = _theme["open"], _theme["close"]
BED_BEEPS = [sounds.hall_beep(i, BED_LEVEL) for i in range(4)] if BED == "hall" else []
SOUNDS = {"end": lambda: SOUND_END, "waiting": lambda: SOUND_WAIT}


def resample(pcm, src_rate, dst_rate):
    """Resamples 16-bit mono PCM. When downsampling it low-pass filters first, then interpolates."""
    if src_rate == dst_rate or not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if dst_rate < src_rate:
        # Low-pass FIR (windowed sinc) below half the target rate,
        # otherwise the high frequencies fold back into the voice
        taps = 63
        n = np.arange(taps) - (taps - 1) / 2
        h = np.sinc(2 * 0.45 * dst_rate / src_rate * n) * np.hamming(taps)
        x = np.convolve(x, h / h.sum(), mode="same")
    n_out = int(len(x) * dst_rate / src_rate)
    y = np.interp(np.arange(n_out) / dst_rate, np.arange(len(x)) / src_rate, x)
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16).tobytes()


def wav_bytes(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


# --- ears and mouth ----------------------------------------------------------

class Transcriber:
    """Sends the audio to whisper-server (whisper.cpp), which does VAD and transcription.

    Whisper always runs 30 seconds through the encoder, even for a "yes": on a CPU-only
    machine the small model costs 15 s per utterance, base 4 s. Short utterances, which are
    usually commands or one-word answers, go to the fast model; the others to the accurate one.
    """

    def __init__(self, url, fast_url="", fast_max_ms=0):
        self.url = url
        self.fast_url = fast_url
        self.fast_max_ms = fast_max_ms

    def pick(self, duration_ms):
        if self.fast_url and duration_ms <= self.fast_max_ms:
            return self.fast_url, "fast"
        return self.url, "accurate"

    def transcribe(self, pcm48k, duration_ms):
        url, tier = self.pick(duration_ms)
        audio = wav_bytes(resample(pcm48k, SAMPLE_RATE, WHISPER_RATE), WHISPER_RATE)
        fields = {
            "response_format": "json",
            "temperature": "0.0",
            "language": WHISPER_LANGUAGE,
            "beam_size": str(WHISPER_BEAM_SIZE),
        }
        if WHISPER_PROMPT:
            fields["prompt"] = WHISPER_PROMPT
        r = requests.post(url, files={"file": ("utterance.wav", audio, "audio/wav")},
                          data=fields, timeout=180)
        r.raise_for_status()
        # Empty text = the VAD found no speech (or Whisper understood nothing)
        return " ".join(r.json().get("text", "").split()), tier


class Speaker:
    """Synthesizes with Piper and brings the audio to 48 kHz for Mumble."""

    def __init__(self, voice_name, directory):
        from piper import PiperVoice, SynthesisConfig
        path = os.path.join(directory, voice_name + ".onnx")
        if not os.path.exists(path):
            raise SystemExit(f"Piper voice not found: {path}. Run download-models.sh.")
        self.voice = PiperVoice.load(path)
        self.rate = self.voice.config.sample_rate
        self.config = SynthesisConfig(length_scale=PIPER_LENGTH_SCALE)

    def chunks(self, text):
        """One sentence at a time, already at 48 kHz: it starts speaking before everything is synthesized."""
        for chunk in self.voice.synthesize(text, self.config):
            x = chunk.audio_float_array
            pcm = np.clip(np.rint(x * 32767), -32768, 32767).astype(np.int16).tobytes()
            yield resample(pcm, self.rate, SAMPLE_RATE)

    def say(self, text):
        return b"".join(self.chunks(text))


# --- log -------------------------------------------------------------------

class EventLog:
    """Append-only CSV: timestamp, event, user, detail (key=value), text."""

    FIELDS = ["timestamp", "event", "user", "detail", "text"]

    def __init__(self, directory):
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, "radio-log.csv")
        self._rotate_if_old_format()
        is_new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._file = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._lock = threading.Lock()
        if is_new:
            self._writer.writerow(self.FIELDS)
            self._file.flush()

    def _rotate_if_old_format(self):
        # The step 1 log had four columns: set it aside instead of mixing formats
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return
        with open(self.path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != self.FIELDS:
            old = self.path[:-4] + f".{datetime.now():%Y%m%d-%H%M%S}.csv"
            os.replace(self.path, old)
            logging.info("Log in the old format set aside as %s", old)

    def write(self, event, user="", detail="", text=""):
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            self._writer.writerow([timestamp, event, user, detail, text])
            self._file.flush()
        logging.info("%-14s %-8s %s%s", event, user, detail, f'  "{text}"' if text else "")


# --- projects and commands ------------------------------------------------------------

def parse_verdict(text):
    """'yes' / 'no' and variants ("No, stop."), otherwise None. Long sentences are not verdicts."""
    words = re.sub(r"[^\w\s']", " ", text.lower()).split()
    phrase = " ".join(words)
    if phrase in VERDICT_YES:
        return "allow"
    if phrase in VERDICT_NO:
        return "deny"
    if words and len(words) <= 4:
        if words[0] in ("yes", "yeah", "yep", "yup", "okay", "ok", "sure", "sì", "si"):
            return "allow"
        if words[0] in ("no", "nope", "negative"):
            return "deny"
    return None


def load_registry():
    """The projects file as a whole: {"projects": [...], "extra_ticket_prefixes": [...]}."""
    try:
        with open(PROJECTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logging.warning("Projects file not readable (%s): %s", PROJECTS_FILE, e)
        return {}


def load_projects():
    return load_registry().get("projects", [])


def _fold(text):
    """Lower case, no accents, no spaces or hyphens: 'My Shöp' -> 'myshop'."""
    text = unicodedata.normalize("NFKD", text.lower())
    return re.sub(r"[\s\-]+", "", "".join(c for c in text if not unicodedata.combining(c)))


_VOWELS = {"a": "[aàáâä]", "e": "[eèéêë]", "i": "[iìíîï]", "o": "[oòóôö]", "u": "[uùúûü]"}


def ticket_regex(registry):
    """The ticket pattern and the spoken prefix -> key map, built from the projects file."""
    spoken, forms = {}, set()
    for project in registry.get("projects", []):
        prefix = project.get("ticket_prefix")
        if prefix:
            for form in [prefix] + list(project.get("ticket_spoken", [])):
                spoken[_fold(form)] = prefix.upper()
                forms.add(form)
    for prefix in registry.get("extra_ticket_prefixes", []):
        spoken[_fold(prefix)] = prefix.upper()
        forms.add(prefix)
    # Each spoken form tolerates accents and spaces or hyphens between its words, longest first
    alternatives = [r"[\s-]*".join("".join(_VOWELS.get(c, re.escape(c)) for c in word) for word in form.lower().split())
                    for form in sorted((f for f in forms if f.strip()), key=len, reverse=True)]
    prefix_group = r"(?:(" + "|".join(alternatives) + r")[\s-]*)?" if alternatives else r"()?"
    pattern = re.compile(r"\b(?:(ticket|task|issue)[\s,]*)?" + prefix_group + r"(\d{2,5})\b", re.IGNORECASE)
    return pattern, spoken


def match_project(text, projects):
    """Looks for a project in the first words of the text. Returns (project, rest) or (None, text).

    Each alias is compared with the same number of words at the start of the text, approximately
    (Whisper writes "You I library" for "ui library"), without eating the words that follow.
    """
    words = re.sub(r"[^\w\s\-]", " ", text).split()
    # leading articles; il, la, progetto: the Italian equivalents of the, a, project
    while words and words[0].lower() in ("the", "a", "an", "il", "la", "project", "progetto"):
        words.pop(0)
    best, best_score, best_n = None, 0.0, 0
    for project in projects:
        for alias in [project["name"], project.get("say", "")] + project.get("aliases", []):
            alias = alias.lower().replace("-", " ")
            n = len(alias.split())
            if not alias or len(words) < n:
                continue
            candidate = " ".join(words[:n]).lower().replace("-", " ")
            score = difflib.SequenceMatcher(None, candidate, alias).ratio()
            if score > best_score or (score == best_score and n > best_n):
                best, best_score, best_n = project, score, n
    if best is not None and best_score >= 0.8:
        return best, " ".join(words[best_n:])
    return None, text


def find_project_anywhere(text, projects):
    """A project alias anywhere in the utterance ("the ticket, My Shop 1794"). Only for utterances
    with a ticket: without one, "open gardenlog, it calls the customer portal API" would open the portal."""
    words = [w.lower().replace("-", " ") for w in re.sub(r"[^\w\s\-]", " ", text).split()]
    best, best_score = None, 0.0
    for project in projects:
        for alias in [project.get("say", "")] + project.get("aliases", []):
            alias = alias.lower().replace("-", " ")
            n = len(alias.split())
            if not alias:
                continue
            for i in range(len(words) - n + 1):
                score = difflib.SequenceMatcher(None, " ".join(words[i:i + n]), alias).ratio()
                if score > best_score:
                    best, best_score = project, score
    return best if best_score >= 0.85 else None


def project_of_ticket(key, projects):
    """The project of a ticket, when its prefix belongs to one project only (two projects can share one)."""
    if not key:
        return None
    owners = [p for p in projects if (p.get("ticket_prefix") or DEFAULT_TICKET_PREFIX) == key.split("-")[0]]
    return owners[0] if len(owners) == 1 else None


def parse_ticket(text, prefix, registry=None):
    """'for ticket 1234 and ...' -> ('SHOP-1234', 'and ...') with prefix SHOP; no ticket -> (None, text).
    A prefix said aloud wins over the given one: "my shop 1794" is MYSHOP-1794 if a project says so."""
    pattern, spoken_map = ticket_regex(load_registry() if registry is None else registry)
    m = pattern.search(text)
    # A short number alone ("in 10 minutes") is not a ticket: it needs the word or the prefix
    if not m or (not m.group(1) and not m.group(2) and len(m.group(3)) < 3):
        return None, text
    spoken = spoken_map.get(_fold(m.group(2))) if m.group(2) else None
    key = f"{spoken or prefix}-{m.group(3)}"
    rest = (text[:m.start()] + " " + text[m.end():]).strip()
    rest = re.sub(r"^the\s*(?=[.,;:])", "", rest, flags=re.IGNORECASE)   # "the ticket, 1794. Fix..." -> "Fix..."
    # leading connectors to strip; per, e, poi: the Italian equivalents of for, and, then
    rest = re.sub(r"^(?:for|per|on|,|\.|;|:|and|e|then|poi|\s)+", "", rest, flags=re.IGNORECASE).strip()
    return key, rest


def read_state(name):
    """State written by the Claude Code hooks: {"state": working|idle|waiting, "ts": ...}."""
    try:
        with open(os.path.join(STATE_DIR, f"{name}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def since(ts):
    minutes = max(0, int((time.time() - ts) / 60))
    if minutes == 0:
        return "less than a minute"
    return f"{minutes} minute" + ("s" if minutes > 1 else "")


def trim_words(text, n):
    words = text.split()
    if len(words) <= n:
        return " ".join(words)
    return " ".join(words[:n]) + ", and so on"


def plain_text(text):
    """Text written for the terminal made speakable: no markdown, code blocks or links."""
    text = re.sub(r"```.*?```", " code ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*#_>|~]+", " ", text)
    return " ".join(text.split())


# How to say aloud what Claude is doing, from the tool name written by the hook
TOOL_VERBS = {"Read": "reading", "Edit": "editing", "MultiEdit": "editing", "Write": "writing",
              "NotebookEdit": "editing", "Glob": "looking for files", "Grep": "searching the code",
              "Bash": "shell", "Agent": "delegating", "Task": "delegating", "WebFetch": "browsing",
              "WebSearch": "searching the web", "Skill": "using a skill", "TodoWrite": "planning",
              "AskUserQuestion": "asking a question", "mcp__radio__reply": "replying",
              "mcp__radio__show": "updating the board", "ToolSearch": "loading tools"}
FILE_TOOLS = {"Read", "Edit", "MultiEdit", "Write", "NotebookEdit", "Glob"}
# For Bash the hook writes the description, if there is one, otherwise the command: this recognizes the command
COMMAND_LIKE = re.compile(r"[;|&$<>=`\\/]|--|(?:^|\s)-\w")
# Activities worth announcing as soon as they change: writing, running commands, delegating
SIGNIFICANT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "Bash", "Agent", "Task", "Skill"}


def spoken_target(tool, target):
    """'src/components/MediaViewer.vue' -> 'media viewer'; a command -> the first words."""
    target = (target or "").strip()
    if not target:
        return ""
    if tool in FILE_TOOLS or ("/" in target and " " not in target):
        base = re.sub(r"\.[A-Za-z0-9]+$", "", target.rstrip("/").split("/")[-1])
        base = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", base)
        return " ".join(w.lower() for w in re.split(r"[-_.\s]+", base) if w)[:60]
    if tool in ("Grep", "ToolSearch", "mcp__radio__show"):
        return ""
    if tool == "Bash" and COMMAND_LIKE.search(target):
        return ""   # a raw command and not the description: aloud it is just noise
    if tool == "WebFetch":
        return re.sub(r"^https?://([^/]+).*$", r"\1", target)
    return trim_words(target, 8)


def activity_text(state):
    """Short phrase about what the session is doing: 'editing media viewer', 'shell, run the tests'."""
    tool = state.get("tool") or ""
    if not tool:
        return "thinking"
    verb = TOOL_VERBS.get(tool) or ("using " + re.sub(r"^mcp__", "", tool).replace("_", " "))
    target = spoken_target(tool, state.get("target"))
    if not target:
        return verb
    if tool in ("Bash", "Agent", "Task", "Skill"):
        return f"{verb}, {target}"
    return f"{verb} {target}"


# --- Claude Code sessions --------------------------------------------------------

class Recording:
    """The radio as you hear it, in a file: the voice of whoever speaks (the Mumble packets) and everything the bot
    puts on the air, each at its own moment. For the bot the moment is when the piece actually goes on the air, that
    is now plus the audio already queued."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.pieces = []         # (seconds from the start, 48 kHz pcm)
        self.lock = threading.Lock()

    def add(self, when, pcm):
        with self.lock:
            self.pieces.append((when - self.t0, pcm))

    def trim(self, now):
        """The bot flushed the queue (a cut, a stop): whatever did not go on the air is removed from the file too."""
        limit = now - self.t0
        with self.lock:
            kept = []
            for t, pcm in self.pieces:
                if t >= limit:
                    continue
                end = t + len(pcm) / BYTES_PER_MS / 1000
                if end > limit:
                    pcm = pcm[:int((limit - t) * 1000) * BYTES_PER_MS]
                kept.append((t, pcm))
            self.pieces = kept

    def duration(self):
        return time.monotonic() - self.t0

    def wav(self):
        """Mixes everything, lowers the level if needed to avoid clipping, downsamples to RECORD_RATE and returns a WAV."""
        with self.lock:
            pieces = list(self.pieces)
        if not pieces:
            return None
        end = max(t + len(p) / BYTES_PER_MS / 1000 for t, p in pieces)
        x = np.zeros(int(end * SAMPLE_RATE) + 1, dtype=np.float32)
        for t, p in pieces:
            a = np.frombuffer(p, dtype="<i2").astype(np.float32) / 32768
            i = max(0, int(t * SAMPLE_RATE))
            a = a[:len(x) - i]
            x[i:i + len(a)] += a
        peak = float(np.abs(x).max()) or 1.0
        if peak > 0.98:
            x *= 0.98 / peak
        pcm = resample((x * 32767).astype("<i2").tobytes(), SAMPLE_RATE, RECORD_RATE)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RECORD_RATE)
            w.writeframes(pcm)
        return buf.getvalue()


class Sessions:
    """The connected Claude Code sessions: each has an event queue towards its adapter."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queues = {}        # name -> event queue (dicts) towards the adapter
        self.project = {}       # name -> project name (if opened by the switchboard)
        self.awaiting = set()   # sessions I just sent an utterance to: the reply is read out
        self.updates = {}       # name -> last message held back (secondary session)
        self.last_reply = {}    # name -> last reply
        self.asking = {}        # name -> when it asked you a question you have not answered yet
        self.fresh = {}         # name -> when it was opened without a task: it waits for the first one
        self.last_spoke = None  # last session that spoke: unnamed utterances go to it
        self.focus = None       # last session I sent an utterance to: all its replies are read out
        self.topic = {}         # name -> topic, two or three words on what it is doing ("media viewer zoom")
        self.ticket = {}        # name -> ticket from the branch (SHOP-1389), only for the board

    def connect(self, name):
        q = queue.Queue()
        with self.lock:
            self.queues[name] = q
            if self.last_spoke is None:
                self.last_spoke = name
        return q

    def disconnect(self, name, q):
        with self.lock:
            if self.queues.get(name) is q:
                del self.queues[name]
                self.awaiting.discard(name)
            if name not in self.queues:
                self.asking.pop(name, None)
                self.fresh.pop(name, None)
            if self.last_spoke == name and name not in self.queues:
                self.last_spoke = next(iter(self.queues), None)
            if self.focus == name and name not in self.queues:
                self.focus = None

    def names(self):
        with self.lock:
            return sorted(self.queues)

    def has(self, name):
        with self.lock:
            return name in self.queues

    def addressed(self, text):
        """The session name at the start of the utterance, even without a comma, and where the rest starts.
        With several names in a row the last one counts, the speaker correcting themselves ("Echo. Charlie, push" -> charlie).
        It can be a NATO name of a closed session: the caller decides. No name: (None, 0)."""
        found, rest_at, pos = None, 0, 0
        with self.lock:
            live = set(self.queues)
        known = set(NATO) | live | {OPERATOR}
        for _ in range(6):
            m = LEAD_WORD.match(text, pos)
            if not m:
                break
            word = m.group(1).lower()
            name = NAME_ALIASES.get(word, word)
            if name not in known and len(word) >= 4 and word not in NOT_NAMES and live:
                guess = max(live, key=lambda n: difflib.SequenceMatcher(None, word, n).ratio())
                score = difflib.SequenceMatcher(None, word, guess).ratio()
                punct = bool(m.group(2).strip())
                if score >= (FUZZY_NAME_PUNCT if punct else FUZZY_NAME_BARE) and (
                        punct or word not in (guess + "s", guess + "es")):   # "Deltas look fine": a plural, not a name
                    name = guess
            if name in known and (name in self.queues or m.group(2).strip() or name in NATO[:8]):
                after = text[m.end():].split(None, 1)
                # "Delta is stuck", "Charlie's branch": about the session, not to it
                if not m.group(2).strip() and (
                        text[m.end(1):m.end(1) + 1] == "'" or (after and after[0].lower() in ABOUT_WORDS)):
                    if not found:
                        return None, 0
                    break
                found, rest_at = name, m.end()
            elif word not in LEAD_FILLERS:
                break
            pos = m.end()
        return found, rest_at

    def route(self, text):
        """'Bravo, run the tests' -> ('bravo', 'run the tests'), also 'Bravo run the tests'.
        The name can belong to a session that does not exist: on_text does not redirect the utterance to another one.
        No name at the start: (target, text)."""
        name, rest_at = self.addressed(text)
        if name:
            return name, text[rest_at:].strip() or text
        m = re.match(r"^\W*([A-Za-z][A-Za-z\- ]{1,12}?)\s*[,.:;!?]+\s*(.*)$", text, re.DOTALL)
        if m:
            name = NAME_ALIASES.get(m.group(1).strip().lower(), m.group(1).strip().lower())
            with self.lock:
                if name in self.queues:
                    return name, m.group(2).strip() or text
                # Or a word from the topic, as long as it belongs to one session only: "Zoom, push it"
                word = m.group(1).strip().lower()
                if " " not in word and len(word) >= 4 and word not in TOPIC_STOPWORDS:
                    owners = [n for n in self.queues if word in re.findall(r"[a-z0-9]+", self.topic.get(n, "").lower())]
                    if len(owners) == 1:
                        return owners[0], m.group(2).strip() or text
        return self.target(parse_verdict(text) is not None), text

    def askers(self):
        """The sessions with an open question for you, oldest first."""
        now = time.monotonic()
        with self.lock:
            return [n for n, t in sorted(self.asking.items(), key=lambda x: x[1])
                    if n in self.queues and now - t < ASK_OPEN_S]

    def fresh_one(self):
        """The last session opened without a task, if it is still there and not too much time has passed."""
        now = time.monotonic()
        with self.lock:
            live = [(t, n) for n, t in self.fresh.items() if n in self.queues and now - t < FRESH_S]
        return max(live)[1] if live else None

    def candidates(self):
        """Who an unnamed utterance can be for: whoever has an open question, and the last one that spoke."""
        askers = self.askers()
        fresh = self.fresh_one()
        with self.lock:
            last = self.last_spoke if self.last_spoke in self.queues else next(iter(self.queues), None)
        out = askers + ([last] if last and last not in askers else [])
        return out + ([fresh] if fresh and fresh not in out else [])

    def target(self, verdict=False):
        """Without a name: the last session that spoke, or the only one connected. If another one has an open
        question the utterance is ambiguous: a yes or a no goes to whoever asked (if only one did), anything else
        gets None, and on_text asks who it is for. (Otherwise a "Yes." meant for the session that asked could land
        on one that arrived later with a "done" and that you were interrupting.)"""
        askers = self.askers()
        # Just opened without a task ("open customer portal", "bravo is ready"): the next utterance is the task,
        # as long as no other session has spoken after its "ready" (otherwise it is among the candidates of
        # "Who is that for"). A yes or a no stays with whoever asked
        fresh = self.fresh_one()
        if fresh and fresh == self.last_spoke and not (verdict and askers):
            return fresh
        cands = self.candidates()
        if len(cands) <= 1:
            return cands[0] if cands else None
        if verdict and len(askers) == 1:
            return askers[0]
        return None

    def send(self, name, event):
        with self.lock:
            q = self.queues.get(name)
        if q is None:
            return False
        q.put(event)
        return True


class ApiHandler(BaseHTTPRequestHandler):
    """The bot's HTTP API, used by the adapters (channel/radio.ts), the launcher and the tests."""

    bot = None   # set in main()

    def log_message(self, *_args):
        pass   # no access log

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, q, on_close):
        """Writes a queue's events as SSE for as long as the connection holds."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=5)
                except queue.Empty:
                    # Keeps the connection alive and detects if it dropped: a dead adapter only shows
                    # when writing, and until it is noticed the utterances for it are lost
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            on_close()

    def do_GET(self):
        url = urlparse(self.path)
        bot = self.bot
        if url.path == "/sessions":
            return self._json(200, {
                "sessions": bot.sessions.names(), "projects": dict(bot.sessions.project),
                "questions": [q["kind"] + ":" + q.get("name", q.get("session", "")) for q in bot.questions],
                "launcher": bot.launcher_q is not None, "quiet_until": bot.quiet_until,
            })
        if url.path == "/launcher/events":
            q = queue.Queue()
            bot.launcher_q = q
            bot.log.write("launcher_connected", BOT_NAME)
            return self._sse(q, lambda: bot.launcher_gone(q))
        if url.path != "/events":
            return self._json(404, {"error": "not found"})
        query = parse_qs(url.query)
        name = query.get("session", [""])[0].strip().lower()
        if not name:
            return self._json(400, {"error": "missing session"})
        q = bot.sessions.connect(name)
        bot.note_project(name, query.get("dir", [""])[0])
        bot.log.write("session_connected", name, f"sessions={','.join(bot.sessions.names())}")
        bot.on_session_connected(name)
        return self._sse(q, lambda: bot.on_session_gone(name, q))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_POST_BYTES:
            return self._json(413, {"error": f"request larger than {MAX_POST_BYTES // 1024 // 1024} MB"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid JSON"})
        path = urlparse(self.path).path
        bot = self.bot
        try:
            if path == "/say":
                bot.on_claude_reply(body["session"], body["text"], body.get("kind", "answer"), body.get("topic"))
            elif path == "/permission":
                bot.on_permission_request(body)
            elif path == "/launcher/result":
                bot.on_launcher_result(body)
            elif path == "/inject":
                if body.get("beep"):   # like a real utterance: the "received" beep comes at the end of the utterance
                    bot.play(BEEP_HEARD)
                outcome = bot.on_text(body.get("user", "test"), body["text"])
                return self._json(200, {"ok": True, "outcome": outcome})
            elif path == "/show":
                try:
                    item = bot.board_show(body)
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, {"ok": True, "id": item["id"], "url": BOARD_URL})
            elif path == "/operator/open":
                try:
                    name = bot.operator_open(body.get("target"), body.get("request", ""))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, {"ok": True, "name": name})
            elif path == "/operator/close":
                names = [n for n in body.get("names", []) if bot.sessions.has(n)]
                for n in names:
                    bot.launcher_send({"type": "close", "name": n})
                return self._json(200, {"ok": True, "closing": names})
            elif path == "/record":
                outcome = bot.record_start() if body.get("action") == "start" else bot.record_stop()
                return self._json(200, {"ok": True, "outcome": outcome})
            elif path == "/record/voice":
                try:
                    seconds = bot.record_voice(body["text"], body.get("voice") or PIPER_VOICE)
                except ValueError as e:
                    return self._json(409, {"error": str(e)})
                return self._json(200, {"ok": True, "seconds": seconds})
            elif path == "/move":
                try:
                    bot.on_move(body.get("session"), body.get("dir"), body.get("brief"))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
            elif path == "/topic":
                bot.set_topic(str(body["session"]).strip().lower(), body.get("topic"), body.get("from", "api"))
            elif path == "/board/code":
                if bot.board is None:
                    return self._json(409, {"error": "board switched off"})
                code = bot.board.new_code()
                bot.log.write("board_code", BOT_NAME, "via=api")
                return self._json(200, {"ok": True, "code": code})
            else:
                return self._json(404, {"error": "not found"})
        except KeyError as e:
            return self._json(400, {"error": f"missing {e}"})
        return self._json(200, {"ok": True})


# --- the bot ------------------------------------------------------------------

class RadioBot:
    def __init__(self, log, transcriber=None, speaker=None):
        self.log = log
        self.transcriber = transcriber
        self.speaker = speaker
        self.sessions = Sessions()
        self.lock = threading.Lock()
        self.buffers = {}          # mumble session -> utterance in progress
        self.offline_since = {}    # user name -> moment they left
        self.synced = False        # True after the server's first user list
        self.stopping = threading.Event()
        # Jobs for the worker, by priority: 0 permissions, 1 utterances to transcribe, 2 the rest
        self.work = queue.PriorityQueue()
        self.seq = itertools.count()
        self.questions = deque()   # questions asked aloud awaiting yes/no, the latest at the end
        self.opening = {}          # name -> data of a session being opened
        self.first_message = {}    # name -> first utterance to send as soon as the adapter connects
        self.who_for = None        # unnamed utterance held back after "Who is that for?"
        self.cut_at = {}           # mumble session -> when I cut an utterance at MAX_UTTERANCE_S
        self.continuation = None   # where the first part of a cut utterance went: the continuation goes there
        self.launcher_q = None     # queue towards the launcher, if connected
        self.quiet_until = 0.0     # quiet mode until this moment (time.time())
        self.held = []             # replies held back during the quiet
        self.last_spoken = ""
        self.feedback = {}         # session name -> moments of the last feedback (tick, sentence, send)
        self.mic_open = {}         # mumble session -> since when it has been transmitting without pauses
        self.last_feedback_check = 0.0
        self.board = None          # board (board.Board), set in main()
        self.deferred = deque()    # sentences that wait: the user was transmitting. (priority, text)
        self.speaking = None       # sentence on the air: cut if the user opens the microphone
        self.speech_lock = threading.Lock()
        self.last_voice = None     # last session that went on the air: on a change of voice the topic is said
        self.voices = {}           # session -> Speaker with its voice (AGENT_VOICES), loaded at connection
        self.voices_lock = threading.Lock()
        self.voices_loading = set()  # sessions whose voice is loading: whoever has to speak with it waits
        self.next_ambient = 0.0        # next beep of the hall (background while the sessions work)
        self.last_stop = None          # (who, when) of the last emergency word: two Esc in a row must not be sent
        self.recording = None          # Recording in progress, for demos
        self.demo_voice = None         # voice with which recorded demos read the user's lines

        self.mumble = pymumble.Mumble(HOST, BOT_NAME, port=PORT, password=PASSWORD, reconnect=True)
        self.mumble.set_application_string("claude-radio")
        callbacks = self.mumble.callbacks
        callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED, self.on_connected)
        callbacks.set_callback(PYMUMBLE_CLBK_DISCONNECTED, self.on_disconnected)
        callbacks.set_callback(PYMUMBLE_CLBK_USERCREATED, self.on_user_created)
        callbacks.set_callback(PYMUMBLE_CLBK_USERREMOVED, self.on_user_removed)
        callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self.on_sound)
        self.mumble.set_receive_sound(True)

    # --- connection events -------------------------------------------

    def humans(self):
        try:
            return [u["name"] for u in list(self.mumble.users.values()) if u["name"] != BOT_NAME]
        except Exception:
            return []

    def on_connected(self):
        self.synced = True
        try:
            # pymumble sends audio over the TCP channel: without TCP_NODELAY Nagle's algorithm
            # can merge the 20 ms packets into bursts, and the phone receives them in jerks
            self.mumble.control_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            logging.exception("TCP_NODELAY not set")
        try:
            # sound_output exists only once connected
            self.mumble.sound_output.set_audio_per_packet(AUDIO_PER_PACKET)
        except Exception:
            logging.exception("audio_per_packet not set")
        present = ",".join(self.humans())
        self.log.write("bot_connected", BOT_NAME, f"present={present}")

    def on_disconnected(self):
        if self.synced and not self.stopping.is_set():
            self.log.write("bot_disconnected", BOT_NAME)
        self.synced = False
        with self.lock:
            self.buffers.clear()

    def on_user_created(self, user):
        # During the initial synchronization the server lists who is already there:
        # those are not real joins.
        if not self.synced or user["name"] == BOT_NAME:
            return
        left_at = self.offline_since.pop(user["name"], None)
        detail = f"offline_s={time.monotonic() - left_at:.0f}" if left_at else ""
        self.log.write("user_joined", user["name"], detail)

    def on_user_removed(self, user, _message):
        if user["name"] == BOT_NAME:
            return
        self.offline_since[user["name"]] = time.monotonic()
        with self.lock:
            self.buffers.pop(user["session"], None)
        self.log.write("user_left", user["name"])

    # --- audio -----------------------------------------------------------

    def on_sound(self, user, chunk):
        now = time.monotonic()
        with self.lock:
            buf = self.buffers.get(user["session"])
            if buf is None:
                buf = {"name": user["name"], "session": user["session"], "pcm": bytearray(), "first": now, "last": now}
                # Right after a cut at MAX_UTTERANCE_S it is the continuation of the same utterance, not a new one
                cut = self.cut_at.pop(user["session"], None)
                if cut is not None and now - cut < CONTINUATION_GAP_S:
                    buf["continues"] = True
                self.buffers[user["session"]] = buf
            buf["pcm"] += chunk.pcm
            buf["last"] = now
            if self.recording is not None:
                self.recording.add(now, chunk.pcm)
            # Continuous transmission: starts again after a pause, needed for the "microphone open" warning
            mo = self.mic_open.get(user["session"])
            if mo is None or (now - mo["last"]) * 1000 >= SILENCE_MS:
                mo = {"name": user["name"], "since": now}
                self.mic_open[user["session"]] = mo
            mo["last"] = now

    def finished_utterances(self):
        now = time.monotonic()
        done = []
        with self.lock:
            for session, buf in list(self.buffers.items()):
                quiet = (now - buf["last"]) * 1000 >= SILENCE_MS
                too_long = now - buf["first"] >= MAX_UTTERANCE_S
                if quiet or too_long:
                    done.append(self.buffers.pop(session))
                    if too_long and not quiet:
                        buf["cut"] = True
                        self.cut_at[session] = now
        return done

    def user_talking(self):
        """True if someone is transmitting: the last packet is less than SILENCE_MS ago. It counts the packet, not
        the utterance: when a long utterance is cut at MAX_UTTERANCE_S the user is still speaking."""
        now = time.monotonic()
        with self.lock:
            return any((now - mo["last"]) * 1000 < SILENCE_MS for mo in self.mic_open.values())

    def audio_left(self):
        """Seconds of bot audio still queued towards the server."""
        try:
            return self.mumble.sound_output.get_buffer_size()
        except Exception:
            return 0.0

    def cut_speech(self, cur):
        """The user opened the microphone while the bot was speaking: it goes quiet at once and the sentence
        restarts from the beginning when they are done. Called by the main loop and by the worker: only the
        first call counts."""
        with self.speech_lock:
            if cur["cut"]:
                return
            cur["cut"] = True
        self.clear_output()
        self.deferred.appendleft((cur["priority"], cur["text"], cur["voice"], cur["tail"]))
        self.log.write("speech_cut", BOT_NAME, f"waiting={len(self.deferred)}", cur["text"])

    def ambient_tick(self):
        """"Hall" background: while at least one session works, every three to seven seconds a distant beep of
        equipment. Silent while you speak, while the bot speaks and in quiet mode."""
        if not BED_BEEPS:
            return
        now = time.monotonic()
        if now < self.next_ambient:
            return
        self.next_ambient = now + random.uniform(3.5, 7.0)
        if self.user_talking() or self.audio_playing() or self.quiet_until > time.time():
            return
        if any((read_state(n) or {}).get("state") == "working" for n in self.sessions.names()):
            self.play(random.choice(BED_BEEPS))

    def audio_tick(self):
        """At every turn of the main loop: quiet if the user opens the microphone; when they are done, the
        sentences that waited go back in the queue in the order they arrived."""
        talking = self.user_talking()
        cur = self.speaking
        if cur is not None and not cur["cut"]:
            on_air = not cur["done"] or time.monotonic() < cur["ends_at"]
            if talking and on_air and not cur["urgent"]:
                self.cut_speech(cur)
            elif not on_air:
                self.speaking = None
        if not talking and self.deferred:
            while self.deferred:
                priority, text, voice, tail = self.deferred.popleft()
                self.enqueue(priority, "speak", (text, voice, tail))

    def play(self, pcm):
        if self.recording is not None and pcm:
            self.recording.add(time.monotonic() + self.audio_left(), pcm)
        self.mumble.sound_output.add_sound(pcm)

    def clear_output(self):
        """Flushes the audio queued towards the server (cut or stop), and removes it from the recording too."""
        try:
            self.mumble.sound_output.clear_buffer()
        except Exception:
            pass
        if self.recording is not None:
            self.recording.trim(time.monotonic())

    # --- recording for demos -------------------------------------------------

    def record_start(self):
        if self.recording is not None:
            self.speak("Already recording.")
            return "record-already"
        self.recording = Recording()
        self.log.write("record_start", BOT_NAME)
        self.speak("Recording.", priority=1)
        return "record"

    def record_stop(self, why=""):
        rec, self.recording = self.recording, None
        if rec is None:
            self.speak("I was not recording.")
            return "record-none"
        minutes = rec.duration() / 60
        self.log.write("record_stop", BOT_NAME, f"s={int(rec.duration())} {why}".strip())
        self.speak("Recording stopped. The file goes on the board." if self.board else "Recording stopped.")
        def save():
            data = rec.wav()
            if not data or self.board is None:
                return
            clock = time.strftime("%H:%M")
            try:
                item = self.board.add("radio", f"Recording at {clock}, {minutes:.1f} minutes",
                                      "The radio as you heard it: your voice and everything that went on the air.", "text",
                                      [{"name": f"radio-{time.strftime('%Y%m%d-%H%M')}.wav", "mime": "audio/wav",
                                        "data": base64.b64encode(data).decode()}])
                self.log.write("board_show", "radio", f"id={item['id']} format=text files=1", item["title"])
            except ValueError as e:
                self.log.write("record_error", BOT_NAME, "", str(e))
        threading.Thread(target=save, daemon=True, name="recording").start()
        return "record-stop"

    def record_voice(self, text, voice):
        """For recorded demos: a line from the user read by a synthetic voice, only in the recording (it does not
        go on the air). Returns how long it lasts, so the demo waits before sending the text to the bot."""
        if self.recording is None:
            raise ValueError("not recording")
        if self.demo_voice is None or self.demo_voice[0] != voice:
            self.demo_voice = (voice, Speaker(voice, PIPER_DIR))
        pcm = self.demo_voice[1].say(text)   # no filter, like the real voice in live recordings
        self.recording.add(time.monotonic(), pcm)
        return len(pcm) / BYTES_PER_MS / 1000

    def audio_playing(self):
        """True if there is still bot audio queued towards the server (speech or beeps not finished)."""
        try:
            return self.mumble.sound_output.get_buffer_size() > 0
        except Exception:
            return False

    def check_mic_open(self):
        """Warns if someone transmits without pauses for too long (it happens with the PTT toggle) and repeats it
        every MIC_OPEN_REPEAT_S: while the microphone is open the other sentences wait, this warning does not."""
        if MIC_OPEN_WARN_S <= 0:
            return
        now = time.monotonic()
        with self.lock:
            due = [mo for mo in self.mic_open.values()
                   if now - mo["since"] >= MIC_OPEN_WARN_S and (now - mo["last"]) * 1000 < SILENCE_MS
                   and now - mo.get("warned_at", -1e9) >= MIC_OPEN_REPEAT_S]
            for mo in due:
                mo["warned_at"] = now
        for mo in due:
            seconds = int(now - mo["since"])
            waiting = len(self.deferred)
            self.log.write("mic_open", mo["name"], f"s={seconds} waiting={waiting}")
            length = f"{seconds // 60} minutes" if seconds >= 120 else f"{seconds} seconds"
            extra = f" {waiting} message{'s' if waiting != 1 else ''} waiting." if waiting else ""
            self.speak(f"Your mic has been open for {length}.{extra}", kind="urgent")

    def enqueue(self, priority, kind, payload):
        self.work.put((priority, next(self.seq), kind, payload))

    # --- the transcribed utterance -----------------------------------------------

    def handle(self, buf):
        duration_ms = len(buf["pcm"]) // BYTES_PER_MS
        if duration_ms < MIN_UTTERANCE_MS:
            # Useful to know how often the phone in the pocket transmits by itself.
            self.log.write("blip", buf["name"], f"dur_ms={duration_ms}")
            return
        # The "received" beep has already been played by the main loop, at the end of the utterance
        if self.transcriber is None:
            if ECHO_DELAY_S > 0:
                time.sleep(ECHO_DELAY_S)
            self.play(silence(REPLY_PREROLL_MS) + bytes(buf["pcm"]))
            self.log.write("utterance", buf["name"], f"dur_ms={duration_ms} delay_s={ECHO_DELAY_S:g}")
            return

        started = time.monotonic()
        try:
            text, tier = self.transcriber.transcribe(bytes(buf["pcm"]), duration_ms)
        except Exception as e:
            self.log.write("whisper_error", buf["name"], f"dur_ms={duration_ms}", str(e)[:200])
            self.play(BEEP_ERROR)
            return
        timing = f"dur_ms={duration_ms} whisper={tier} whisper_ms={int((time.monotonic() - started) * 1000)}"
        if not text or not re.search(r"[A-Za-z0-9]", text):   # "-" or "..." are not utterances
            self.log.write("noise", buf["name"], timing, text or "")
            self.play(BEEP_EMPTY)
            return
        if HALLUCINATIONS.search(text):
            self.log.write("hallucination", buf["name"], timing, text)
            self.play(BEEP_EMPTY)
            return

        timing += " cut=1" * bool(buf.get("cut")) + " continues=1" * bool(buf.get("continues"))
        if self.sessions.names() or self.questions or self.opening:
            self.log.write("transcript", buf["name"], timing, text)
            if buf.get("continues") and self.continue_text(buf, text):
                return
            result = self.on_text(buf["name"], text)
            if buf.get("cut"):
                # The first part of a cut utterance: the continuation will go where it went
                self.continuation = {"session": buf.get("session"), "result": result, "at": time.monotonic(),
                                     "name": self.sessions.focus if result == "message" else None,
                                     "question": self.questions[-1] if result == "open?" and self.questions else None}
            return

        # No session: step 2, "I heard: ..."
        started = time.monotonic()
        latency_ms = None
        try:
            for pcm in self.speaker.chunks(f"{REPLY_PREFIX} {text}"):
                if latency_ms is None:
                    # Latency as the speaker hears it: from when they stopped to when the reply starts
                    latency_ms = int((time.monotonic() - buf["last"]) * 1000)
                    pcm = silence(REPLY_PREROLL_MS) + pcm
                self.play(pcm)
        except Exception as e:
            self.log.write("tts_error", buf["name"], timing, str(e)[:200])
            self.play(BEEP_ERROR)
            return
        tts_ms = int((time.monotonic() - started) * 1000)
        if latency_ms is None:
            latency_ms = int((time.monotonic() - buf["last"]) * 1000)
        self.log.write("transcript", buf["name"], f"{timing} tts_ms={tts_ms} latency_ms={latency_ms}", text)

    # --- the switchboard -----------------------------------------------------

    def continue_text(self, buf, text):
        """The continuation of an utterance cut at MAX_UTTERANCE_S goes where the first part went: to the same
        session, into the same open request, or into the utterance held back for "Who is that for". (A long
        "Alpha, yes, ..." utterance cut at 60 s used to go to alpha while its continuation went to another session.)
        A name said at the start of the continuation wins. False: the continuation is a normal utterance."""
        cont, self.continuation = self.continuation, None
        if not cont or cont["session"] != buf.get("session") or time.monotonic() - cont["at"] > 120:
            return False
        if self.sessions.addressed(text)[0]:
            return False
        user = buf["name"]
        if cont["name"] and self.sessions.has(cont["name"]):
            self.log.write("continuation", user, f"session={cont['name']}", text)
            self.sessions.awaiting.add(cont["name"])
            self.sessions.send(cont["name"], {"type": "message", "text": text, "user": user})
            self.log.write("to_claude", user, f"session={cont['name']} continues=1", text)
            self.continuation = dict(cont, at=time.monotonic()) if buf.get("cut") else None
            return True
        q = cont["question"]
        if q is not None and q in self.questions:
            q["request"] = f"{q.get('request') or ''} {text}".strip()
            self.log.write("continuation", user, f"question={q.get('kind')}", text)
            self.continuation = dict(cont, at=time.monotonic()) if buf.get("cut") else None
            return True
        if cont["result"] == "who-for" and self.who_for:
            self.who_for["text"] = f"{self.who_for['text']} {text}"
            self.who_for["at"] = time.monotonic()
            self.log.write("continuation", user, "who_for=1", text)
            self.continuation = dict(cont, at=time.monotonic()) if buf.get("cut") else None
            return True
        return False

    def on_text(self, user, text):
        """Routes a transcribed utterance: answer to a question, command, or message to a session."""
        if CMD_RECORD_STOP.match(text):
            return self.record_stop()
        if CMD_RECORD_START.match(text):
            return self.record_start()
        # The emergency word comes before everything, even a pending question
        if CMD_STOP_ALL.match(text):
            return self.emergency_stop(self.sessions.names(), user, everyone=True)
        if (m := CMD_STOP_NAMED.match(text)):
            target = NAME_ALIASES.get(m.group(1).strip().lower(), m.group(1).strip().lower())
            if self.sessions.has(target):
                return self.emergency_stop([target], user)
        held, self.who_for = self.who_for, None
        if held and time.monotonic() - held["at"] < WHO_FOR_S:
            name, rest_at = self.sessions.addressed(text)
            if name and self.sessions.has(name) and not text[rest_at:].strip(" .,!?"):
                return self.deliver(name, held["text"], user)
        verdict = parse_verdict(text)
        if verdict and self.questions:
            return self.answer_question(user, text, verdict)

        if (m := CMD_OPEN.match(text)):
            return self.cmd_open(user, m.group(1))
        if (m := CMD_STATUS.match(text)):
            self.speak(self.status_text((m.group(1) or "").strip().lower() or None))
            return "status"
        if (m := CMD_QUIET.match(text)):
            minutes = float(m.group(1)) if m.group(1) else QUIET_DEFAULT_MIN
            self.quiet_until = time.time() + minutes * 60
            self.speak(f"Quiet for {minutes:g} minutes. Permissions still come through. Say speak to resume.")
            return "quiet"
        if CMD_RESUME.match(text):
            self.quiet_until = 0.0
            self.speak("Back." if self.held else "I was not quiet.")
            self.flush_held()
            return "resume"
        if CMD_BOARD.search(text) and len(text.split()) <= BOARD_CMD_MAX_WORDS:
            return self.cmd_board_code()
        if CMD_WHO.match(text):
            self.speak(self.roll_call())
            return "who"

        if (m := CMD_OPERATOR.match(text)):
            rest = m.group(1).strip() or "The human called you without a request: ask what they need, in one short sentence."
            if not self.sessions.has(OPERATOR):
                return self.call_operator(user, rest)
            return self.deliver(OPERATOR, rest, user)

        name, rest = self.sessions.route(text)
        if (m := CMD_REPEAT.match(rest)):
            return self.cmd_repeat((m.group(1) or "").strip().lower() or (name if rest != text else None))
        if name is not None and not self.sessions.has(name):
            # "Foxtrot, ..." with foxtrot closed: I do not redirect the utterance to another session
            if name == OPERATOR:
                return self.call_operator(user, rest)
            self.log.write("off_air", user, f"name={name}", text)
            if name in self.opening:
                self.speak(f"{name} is still opening, say it again in a moment.")
            else:
                on_air = self.sessions.names()
                self.speak(f"{name} is not on the air." + (f" On the air: {', '.join(on_air)}." if on_air else ""))
            return "off-air"
        if name is None and len(cands := self.sessions.candidates()) > 1:
            self.who_for = {"text": text, "at": time.monotonic()}
            self.log.write("who_for", user, "candidates=" + ",".join(cands), text)
            self.speak(f"Who is that for, {' or '.join(cands)}?")
            return "who-for"
        if name is None:
            if self.opening:
                self.speak("Still opening, give me a moment.")
            else:
                self.speak(f"{REPLY_PREFIX} {text}")
            return "echo"
        if rest != text and CMD_STOP_ONE.match(rest):
            return self.emergency_stop([name], user)
        if CMD_CLOSE.match(rest) and rest != text:
            self.ask({"kind": "close", "name": name}, f"Closing {name}. Say yes to confirm.")
            return "close?"
        if CMD_STATUS.match(rest) and rest != text:
            self.speak(self.status_text(name))
            return "status"
        return self.deliver(name, rest, user)

    def deliver(self, name, rest, user):
        """An utterance to a session: wait for its reply, confirm aloud."""
        if self.sessions.fresh.pop(name, None) is not None:
            project = next((p for p in load_projects() if p["name"] == self.sessions.project.get(name)), {})
            key, _ = parse_ticket(rest, (project.get("ticket_prefix") or DEFAULT_TICKET_PREFIX))
            if key:
                rest = f"Ticket {key}. {rest}"
                self.sessions.ticket[name] = key
        self.sessions.awaiting.add(name)
        self.sessions.focus = name
        self.sessions.asking.pop(name, None)
        self.sessions.send(name, {"type": "message", "text": rest, "user": user})
        self.log.write("to_claude", user, f"session={name}", rest)
        fb = self.feedback_of(name)
        fb["sent_wall"] = time.time()
        fb["spoken"] = fb["tick"] = time.monotonic()
        fb["last_kind"] = None
        fb["filled"] = None
        self.ack(name, rest)
        return "message"

    def call_operator(self, user, request):
        """The operator is not open: the launcher starts it, and the utterance reaches it as soon as it connects."""
        if OPERATOR in self.opening:
            self.speak("The operator is on the way.")
            return "operator-opening"
        if not self.launcher_send({"type": "open", "name": OPERATOR, "operator": True}):
            self.speak("The launcher is not running, I cannot call the operator.")
            return "operator-failed"
        self.opening[OPERATOR] = {"name": OPERATOR, "operator": True}
        self.first_message[OPERATOR] = request
        self.sessions.project[OPERATOR] = "radio"
        self.set_topic(OPERATOR, "radio housekeeping", "open")
        self.log.write("open", user, f"project=operator name={OPERATOR}", request)
        self.speak("Calling the operator.")
        return "operator-call"

    def operator_open(self, target, request):
        """From the operator (tools/operator_tool.py): opens without asking for confirmation, it already asked aloud.
        Registered project: the usual worktree; folder: the session opens there."""
        if self.launcher_q is None:
            raise ValueError("the launcher is not running")
        taken = set(self.sessions.names()) | set(self.opening) | {q.get("name") for q in self.questions}
        name = next((n for n in NATO if n not in taken), None)
        if name is None:
            raise ValueError("all names are taken")
        target = str(target or "").strip()
        project, rest = match_project(target, load_projects())
        if project is not None:
            ticket, extra = parse_ticket(f"{rest} {request}", (project.get("ticket_prefix") or DEFAULT_TICKET_PREFIX))
            branch = f"{ticket}-{name}" if ticket else f"radio-{name}"
            task = re.sub(r"^(?:and|then|,|\s)+", "", extra, flags=re.IGNORECASE).strip()
            if ticket:
                task = f"Ticket {ticket}. {task or project.get('ticket_request') or DEFAULT_TICKET_REQUEST}"
            self.launch({"name": name, "project": project, "ticket": ticket, "branch": branch,
                         "request": task, "user": "operator"})
            return name
        if not target.startswith(("/", "~")):
            raise ValueError("give a registered project name or an absolute folder path")
        self.opening[name] = {"name": name, "dir": target}
        if request:
            self.first_message[name] = request
        self.sessions.project[name] = os.path.basename(target.rstrip("/"))
        self.launcher_send({"type": "open", "name": name, "dir_direct": target})
        self.log.write("open", "operator", f"project={self.sessions.project[name]} name={name}", request)
        self.speak(f"Opening {name} in {re.sub(r'[-_.]+', ' ', self.sessions.project[name])}.")
        return name

    def ack(self, name, text):
        """Confirms aloud what I understood and who I sent it to, before Claude replies."""
        if ACK_MODE == "beep":
            return
        many = len(self.sessions.names()) > 1
        if ACK_MODE == "name":
            # Only who it went to, without repeating the sentence: with a single session the beep is enough
            if many:
                self.speak(f"To {name}.", priority=1)
            return
        if ACK_MODE == "short":
            self.speak(f"Sending to {self.label(name)}." if many else "Got it.", priority=1)
            return
        body = trim_words(text, ACK_MAX_WORDS)
        self.speak(f"To {self.label(name)}: {body}" if many else f"Got it: {body}", priority=1)

    def label(self, name):
        """'bravo, media viewer zoom': the name with the topic, so the sessions are not confused."""
        topic = self.sessions.topic.get(name)
        return f"{name}, {topic}" if topic else name

    def set_topic(self, name, topic, source):
        """A session's topic: a few words on what it is doing, said at the confirmation and on a change of voice."""
        clean = " ".join(re.findall(r"[\w'-]+", str(topic or ""))[:4])[:40].strip().lower()
        if not name or not clean or self.sessions.topic.get(name) == clean:
            return
        self.sessions.topic[name] = clean
        self.log.write("topic", name, f"from={source}", clean)

    def restore_routing(self):
        """After a bot restart: who spoke last and who the last utterance went to, taken from the log.
        Without it, an unnamed utterance would go to the first session that reconnects."""
        try:
            with open(self.log.path, newline="", encoding="utf-8") as f:
                rows = deque(csv.reader(f), maxlen=2000)
        except OSError:
            return
        last_spoke = focus = None
        last_kind = {}   # session -> kind of the last reply, if I have not sent it anything since
        for row in rows:
            if len(row) < 4:
                continue
            event, user, detail = row[1], row[2], row[3]
            if event == "claude_reply":
                if "spoken=0" not in detail:
                    last_spoke = user
                k = re.search(r"kind=(\w+)", detail)
                last_kind[user] = k.group(1) if k else None
            elif event == "to_claude" and (m := re.search(r"session=([\w-]+)", detail)):
                focus = m.group(1)
                last_kind[focus] = None
            elif event == "topic" and len(row) >= 5:
                self.sessions.topic[user] = row[4]
            elif event == "open" and (m := re.search(r"name=(\w+) branch=([A-Z]+-\d+)", detail)):
                self.sessions.ticket[m.group(1)] = m.group(2)
        self.sessions.last_spoke = last_spoke
        self.sessions.focus = focus
        for name, kind in last_kind.items():   # whoever was waiting for your ok keeps waiting for it
            if kind == "blocked":
                self.feedback_of(name)["last_kind"] = "blocked"
                self.sessions.asking[name] = time.monotonic()
        if last_spoke or focus:
            logging.info("From the log: last to speak %s, last utterance to %s", last_spoke, focus)

    def cmd_board_code(self):
        """Speaks the code to open the board on a new phone. In the log the sentence appears without the digits."""
        if self.board is None:
            self.speak("The board is switched off.")
            return "board-off"
        code = self.board.new_code()
        self.log.write("board_code", BOT_NAME, "via=voice")
        self.speak(f"Board code: {', '.join(code)}. It works once, for five minutes.", priority=1)
        return "board-code"

    def board_show(self, body):
        """An item from a session: it only saves it. No voice: the agent announces it."""
        if self.board is None:
            raise ValueError("the board is switched off")
        name = str(body.get("session") or "").strip().lower() or "?"
        item = self.board.add(name, body.get("title"), body.get("text"), body.get("format"), body.get("files"))
        self.sound(SOUND_NEW)
        self.log.write("board_show", name, f"id={item['id']} format={item['format']} files={len(item['files'])}",
                       item["title"])
        return item

    def emergency_stop(self, names, user, everyone=False):
        """Emergency word: an Esc in the sessions' terminals (Claude Code stops whatever it is doing, and closes
        any window it has open) and out of the queue whatever they had to say."""
        now = time.monotonic()
        key = ("*",) if everyone else tuple(sorted(names))
        if self.last_stop and self.last_stop[0] == key and now - self.last_stop[1] < 3:
            return "stop-repeat"   # two Esc in a row open the menu to rewind the conversation: no
        self.last_stop = (key, now)
        targets = set(names)
        for n in targets:
            fb = self.feedback_of(n)
            fb["stopped_wall"] = time.time()
            fb["fallback"] = None
            fb["last_kind"] = None
            self.sessions.awaiting.discard(n)
            self.sessions.updates.pop(n, None)
        # the pending questions of those sessions (permissions) are closed by the Esc; with the stop of everyone, all of them
        self.questions = deque(q for q in self.questions
                               if not (everyone or q.get("session") in targets or q.get("name") in targets))
        self.hush(None if everyone else targets)
        if targets:
            self.launcher_send({"type": "interrupt", "names": sorted(targets)})
        self.log.write("stop", user, f"sessions={','.join(sorted(targets)) or '-'} all={int(everyone)}")
        if not everyone:
            self.speak(f"{', '.join(sorted(targets))} stopped.", priority=0)
        else:
            self.speak("All stopped." if targets else "Quiet.", priority=0)
        return "stop"

    def hush(self, targets=None):
        """Removes from the queue and from the waiting list the sentences of the stopped sessions (with targets
        None: all speech and sounds) and cuts the one on the air."""
        def targeted(voice):
            return targets is None or voice in targets
        kept = []
        while True:
            try:
                item = self.work.get_nowait()
            except queue.Empty:
                break
            _p, _s, kind, payload = item
            voice = payload[1] if isinstance(payload, tuple) and len(payload) > 1 else None
            if kind in ("speak", "speak_filler", "sound") and (targets is None or (voice is not None and targeted(voice))):
                continue
            kept.append(item)
        for item in kept:
            self.work.put(item)
        self.deferred = deque(d for d in self.deferred if not (targets is None or (d[2] is not None and targeted(d[2]))))
        self.held = [h for h in self.held if not (targets is None or (len(h) > 1 and h[1] is not None and targeted(h[1])))]
        cur = self.speaking
        if cur is not None and not cur["cut"] and (targets is None or cur.get("voice") in targets):
            with self.speech_lock:
                cur["cut"] = True
            self.clear_output()
            self.log.write("speech_cut", BOT_NAME, "stop", cur["text"])

    def ask(self, question, text):
        self.questions.append(question)
        self.speak(text, priority=1)

    def answer_question(self, user, text, verdict):
        q = self.questions.pop()
        kind = q["kind"]
        if kind == "permission":
            self.sessions.send(q["session"], {"type": "permission", "request_id": q["request_id"], "behavior": verdict})
            self.log.write("verdict", user, f"session={q['session']} request_id={q['request_id']} behavior={verdict}", text)
            return "verdict"
        if kind == "open":
            if verdict == "allow":
                self.launch(q)
            else:
                self.speak("Cancelled.")
            return "open" if verdict == "allow" else "cancelled"
        if kind == "close":
            if verdict == "allow" and self.launcher_send({"type": "close", "name": q["name"]}):
                self.speak(f"Closing {q['name']}.")
            elif verdict == "allow":
                self.speak("The launcher is not running, I cannot close it.")
            else:
                self.speak("Cancelled.")
            return "close"
        return "ignored"

    def cmd_open(self, user, rest):
        if self.launcher_q is None:
            self.speak("The launcher is not running, I cannot open sessions.")
            return "open-failed"
        projects = load_projects()
        said_all = rest
        project, rest = match_project(rest, projects)
        key = parse_ticket(said_all, "?")[0]   # "?-1794": ticket said without a prefix
        if project is None and key:
            # "Open the ticket, My Shop 1794": the project from the prefix or said later in the sentence
            project = project_of_ticket(key, projects) or find_project_anywhere(said_all, projects)
        fresh = self.sessions.fresh_one()
        fresh_project = next((p for p in projects if p["name"] == self.sessions.project.get(fresh)), None)
        if fresh and (project is not None and project is fresh_project or project is None and key and (
                key.startswith("?-") or fresh_project and key.split("-")[0] == (fresh_project.get("ticket_prefix") or DEFAULT_TICKET_PREFIX))):
            # "open customer portal", "bravo is ready", then "Open the ticket, My Shop 1794..." used to
            # open another session in the lobby instead of giving the task to bravo
            self.log.write("open_to_fresh", user, f"session={fresh}", said_all)
            return self.deliver(fresh, f"Open {said_all}".strip(), user)
        taken = set(self.sessions.names()) | set(self.opening) | {q.get("name") for q in self.questions}
        name = next((n for n in NATO if n not in taken), None)
        if name is None:
            self.speak("All names are taken.")
            return "open-failed"
        if project is None:
            # Unregistered (or unnamed) project: the session starts in the lobby, finds the folder and moves there
            request = re.sub(r"^\W*(?:a |an |the )?(?:new |generic )?(?:session|agent|lobby)\b[\s,.]*", "", rest.strip(),
                             flags=re.IGNORECASE).strip()
            request = request or "The human opened a session without naming the project: ask which one, in one short sentence."
            question = {"kind": "open", "name": name, "project": None, "lobby": True, "ticket": None, "branch": "",
                        "request": request, "user": user}
            self.ask(question, f"Opening a new session as {name}. It finds the project and moves there. Say yes to confirm.")
            self.log.write("open_asked", user, f"project=lobby name={name}", request)
            return "open?"
        ticket, request = parse_ticket(rest, (project.get("ticket_prefix") or DEFAULT_TICKET_PREFIX))
        # leading connectors to strip; e, poi: the Italian equivalents of and, then
        request = re.sub(r"^(?:and|then|e|poi|,|\s)+", "", request, flags=re.IGNORECASE).strip()
        branch = f"{ticket}-{name}" if ticket else f"radio-{name}"
        if ticket and not request:
            request = f"Ticket {ticket}. {project.get('ticket_request') or DEFAULT_TICKET_REQUEST}"
        elif ticket:
            request = f"Ticket {ticket}. {request}"
        question = {"kind": "open", "name": name, "project": project, "ticket": ticket,
                    "branch": branch, "request": request, "user": user}
        said = project.get("say", project["name"])
        ticket_said = f", ticket {ticket.replace('-', ' ')}" if ticket else ""
        self.ask(question, f"Opening {said} as {name}{ticket_said}. Say yes to confirm.")
        self.log.write("open_asked", user, f"project={project['name']} name={name} branch={branch}", request)
        return "open?"

    def launch(self, q):
        name, project = q["name"], q["project"]
        self.opening[name] = q
        if q["request"]:
            self.first_message[name] = q["request"]
        if q.get("lobby"):
            sent = self.launcher_send({"type": "open", "name": name, "lobby": True})
        else:
            sent = self.launcher_send({
                "type": "open", "name": name, "dir": project["dir"], "base_branch": project.get("base_branch", "master"),
                "branch": q["branch"], "copy": project.get("copy", []), "install": project.get("install", True),
            })
        if not sent:
            self.opening.pop(name, None)
            self.first_message.pop(name, None)
            self.speak("The launcher is not running, I cannot open sessions.")
            return
        if q.get("lobby"):
            self.sessions.project[name] = "lobby"
            self.set_topic(name, "finding the project", "open")
            self.log.write("open", q["user"], f"project=lobby name={name}")
            self.speak(f"Opening {name}. This takes about half a minute.")
            return
        self.sessions.project[name] = project["name"]
        if (t := re.match(r"^([A-Z]+-\d+)", q["branch"])):
            self.sessions.ticket[name] = t.group(1)
        self.log.write("open", q["user"], f"project={project['name']} name={name} branch={q['branch']}")
        self.speak(f"Opening {name}. This takes about a minute.")

    def on_move(self, name, directory, brief):
        """A session asks to move to another folder (the channel's move tool, usually from the lobby).
        The launcher closes it and reopens it there: the conversation does not travel, the brief becomes the first utterance."""
        name = str(name or "").strip().lower()
        directory = str(directory or "").strip()
        if not self.sessions.has(name):
            raise ValueError(f"no session called {name}")
        if self.launcher_q is None:
            raise ValueError("the launcher is not running, the session cannot move")
        if not directory.startswith(("/", "~")):
            raise ValueError("give the absolute path of the project folder")
        # The bot runs in Docker: "~" means the host's home (HOST_HOME from compose), left as is if unknown
        host = lambda d: os.path.normpath(re.sub(r"^~(?=/|$)", HOST_HOME, d) if HOST_HOME else d)
        project = next((p for p in load_projects() if host(directory) == host(p["dir"])
                        or host(directory).startswith(host(p["dir"]) + "--")), None)
        if project is not None:
            return self.move_to_project(name, project, brief)
        where = os.path.basename(directory.rstrip("/")) or directory
        self.opening[name] = {"name": name, "move": True, "dir": directory}
        self.first_message[name] = f"You are now in {directory}, moved here from the radio lobby. {str(brief or '').strip()}".strip()
        self.sessions.project[name] = where
        self.sessions.ticket.pop(name, None)
        if self.sessions.topic.pop(name, None):
            self.log.write("topic", name, "from=move", "")
        self.log.write("move", name, f"dir={directory}", str(brief or ""))
        self.speak(f"{name} is moving to {re.sub(r'[-_.]+', ' ', where)}.")
        # the session asking for the move is the one about to close: first let it finish its reply
        threading.Timer(2.0, lambda: self.launcher_send({"type": "move", "name": name, "dir": directory})).start()

    def move_to_project(self, name, project, brief):
        """From the lobby to a registered project: the session reopens in its own worktree, as with "open".
        (The lobby used to answer that registered projects don't open from the lobby, and you had to open another session.)"""
        brief = str(brief or "").strip()
        ticket, _ = parse_ticket(brief, (project.get("ticket_prefix") or DEFAULT_TICKET_PREFIX))
        branch = f"{ticket}-{name}" if ticket else f"radio-{name}"
        said = project.get("say", project["name"])
        self.opening[name] = {"name": name, "move": True, "dir": project["dir"]}
        self.first_message[name] = (f"You are now in your own worktree of {said}, moved here from the radio lobby."
                                    f"{f' Ticket {ticket}.' if ticket else ''} {brief}").strip()
        self.sessions.project[name] = project["name"]
        if ticket:
            self.sessions.ticket[name] = ticket
        else:
            self.sessions.ticket.pop(name, None)
        if self.sessions.topic.pop(name, None):
            self.log.write("topic", name, "from=move", "")
        self.log.write("move", name, f"project={project['name']} branch={branch}", brief)
        self.speak(f"{name} is moving to {said}{f', ticket {ticket}'.replace('-', ' ') if ticket else ''}.")
        threading.Timer(2.0, lambda: self.launcher_send({
            "type": "move", "name": name, "project": True, "dir": project["dir"],
            "base_branch": project.get("base_branch", "master"), "branch": branch,
            "copy": project.get("copy", []), "install": project.get("install", True),
        })).start()

    def launcher_send(self, event):
        q = self.launcher_q
        if q is None:
            return False
        q.put(event)
        return True

    def launcher_gone(self, q):
        if self.launcher_q is q:
            self.launcher_q = None
        self.log.write("launcher_disconnected", BOT_NAME)

    def on_launcher_result(self, body):
        name = body.get("name", "?")
        q = self.opening.pop(name, None)
        if body.get("closed"):
            self.sessions.project.pop(name, None)
            self.log.write("closed", name, "", body.get("detail", ""))
            self.sound(SOUND_CLOSE)
            self.speak(f"{name} closed. The worktree stays until the merge.")
            return
        if not body.get("ok"):
            self.first_message.pop(name, None)
            detail = body.get("detail", "unknown error")
            if q and q.get("move"):
                self.log.write("move_failed", name, "", detail)
                if self.sessions.has(name):   # it did not move: I tell it too, since it asked
                    self.sessions.send(name, {"type": "message", "user": "radio",
                                              "text": f"[radio] The move did not happen: {detail}"})
                self.speak(f"{name} could not move: {detail}.")
                return
            self.sessions.project.pop(name, None)
            self.log.write("open_failed", name, "", detail)
            self.speak(f"Could not open {name}: {detail}.")
            return
        self.log.write("moved" if body.get("moved") else "opened", name, f"dir={body.get('dir', '')}", body.get("detail", ""))
        note = f" Note: {body['detail']}." if body.get("detail") else ""
        if q and not q.get("move") and name not in self.first_message:
            self.sessions.fresh[name] = time.monotonic()   # opened without a task: the next utterance is for it
        if self.sessions.has(name):
            self.deliver_first_message(name)
            self.sessions.last_spoke = name
            self.sound(SOUND_OPEN)
            self.speak(f"{name} is ready.{note}")
        else:
            self.speak(f"{name} is starting.{note}")

    def note_project(self, name, directory):
        """Session opened by hand: the project is inferred from the folder (or from its worktree)."""
        if name in self.sessions.project or not directory:
            return
        for project in load_projects():
            if directory.rstrip("/") == project["dir"].rstrip("/") or directory.startswith(project["dir"].rstrip("/") + "--"):
                self.sessions.project[name] = project["name"]
                return

    def load_voice(self, name):
        """Loads the session's voice in a separate thread: the first reply does not wait for Piper."""
        voice = AGENT_VOICES.get(name)
        if not voice or self.speaker is None or not isinstance(self.speaker, Speaker):
            return
        def load():
            try:
                with self.voices_lock:
                    if name in self.voices:
                        return
                    try:
                        self.voices[name] = Speaker(voice, PIPER_DIR)
                        logging.info("voice of %s: %s", name, voice)
                    except (Exception, SystemExit) as e:   # missing voice: the bot's own voice is used
                        logging.warning("voice of %s not loaded (%s): %s", name, voice, e)
            finally:
                self.voices_loading.discard(name)
        self.voices_loading.add(name)
        threading.Thread(target=load, daemon=True, name=f"voice-{name}").start()

    def drop_voice(self, name):
        if not self.sessions.has(name):
            with self.voices_lock:
                if self.voices.pop(name, None) is not None:
                    logging.info("voice of %s freed", name)

    def on_session_connected(self, name):
        self.load_voice(name)
        if name in self.first_message and name not in self.opening:
            self.deliver_first_message(name)
            self.sessions.last_spoke = name
            self.speak(f"{name} is ready.")

    def on_session_gone(self, name, q):
        self.sessions.disconnect(name, q)
        self.drop_voice(name)
        self.log.write("session_disconnected", name, f"sessions={','.join(self.sessions.names())}")

    def deliver_first_message(self, name):
        text = self.first_message.pop(name, None)
        if text:
            self.sessions.awaiting.add(name)
            self.sessions.focus = name
            self.sessions.send(name, {"type": "message", "text": text, "user": "radio"})
            self.log.write("to_claude", "radio", f"session={name} first=1", text)

    def cmd_repeat(self, name):
        if name and name in self.sessions.updates:
            text = self.sessions.updates.pop(name)
            self.speak(f"{name}: {text}", voice=name)
        elif name and name in self.sessions.last_reply:
            self.speak(f"{name}: {self.sessions.last_reply[name]}", voice=name)
        elif self.last_spoken:
            self.speak(self.last_spoken)
        else:
            self.speak("Nothing to repeat.")
        return "repeat"

    def status_text(self, name=None):
        names = [name] if name else self.sessions.names()
        if name and not self.sessions.has(name):
            return f"No session called {name}."
        parts = []
        for n in names:
            project = self.sessions.project.get(n, "an unknown project")
            state = read_state(n)
            if any(q["kind"] == "permission" and q.get("session") == n for q in self.questions):
                phrase = "waiting for your yes or no"
            elif state and state.get("state") == "working":
                phrase = f"working for {since(state['ts'])}"
                if state.get("steps"):
                    phrase += f", step {state['steps']}, {activity_text(state)}"
            elif state and state.get("state") == "waiting":
                phrase = f"waiting for you for {since(state['ts'])}"
            elif state:
                phrase = f"idle for {since(state['ts'])}"
            else:
                phrase = "no status yet"
            if n in self.sessions.updates:
                phrase += ", has an update"
            parts.append(f"{self.label(n)}, {phrase}" if self.sessions.topic.get(n) else f"{n} on {project}, {phrase}")
        for n in self.opening:
            parts.append(f"{n} is opening")
        if not parts:
            parts.append("No sessions open")
        if self.quiet_until > time.time():
            parts.append(f"quiet for {int((self.quiet_until - time.time()) / 60)} more minutes")
        return ". ".join(parts) + "."

    def roll_call(self):
        """Who is on and what they work on, briefly: to get your bearings when there are many sessions."""
        names = self.sessions.names()
        if not names:
            return "No sessions open."
        parts = []
        for n in names:
            what = self.sessions.topic.get(n) or self.sessions.project.get(n) or "no topic yet"
            parts.append(f"{n}: {what}, {self.short_state(n)}")
        count = "One session" if len(names) == 1 else f"{len(names)} sessions"
        return f"{count}. " + ". ".join(parts) + "."

    def short_state(self, name):
        """working, waiting for you, idle: for the roll call and for the board."""
        state = read_state(name) or {}
        # copy: also called by the board thread and the worker, while other threads touch the queue
        if any(q["kind"] == "permission" and q.get("session") == name for q in list(self.questions)):
            return "waiting for you"
        if state.get("state") == "working":
            return "working"
        if state.get("state") == "waiting" or self.feedback_of(name).get("last_kind") == "blocked":
            return "waiting for you"
        return "idle"

    def board_sessions(self):
        """The sessions for the board: name, topic, ticket, project, state."""
        out = []
        for n in self.sessions.names():
            state = self.short_state(n)
            out.append({
                "name": n, "topic": self.sessions.topic.get(n, ""), "ticket": self.sessions.ticket.get(n, ""),
                "project": self.sessions.project.get(n, ""),
                "state": {"working": "working", "waiting for you": "waiting"}.get(state, "idle"),
            })
        return out

    # --- Claude Code ---------------------------------------------------------

    def on_claude_reply(self, name, text, kind="answer", topic=None):
        if topic:
            self.set_topic(name, topic, "claude")
        self.sessions.last_reply[name] = text
        # A question you heard stays open until you answer it: unnamed utterances take it into account
        self.sessions.asking.pop(name, None)
        asks = kind == "blocked" or text.rstrip(" \"'\u201d)").endswith("?")
        # Everything is read from the session I last spoke to (focus) and from those with an open turn;
        # "awaiting" is closed by done, blocked, or the Stop hook (see feedback_tick)
        awaited = name == self.sessions.focus or name in self.sessions.awaiting
        if kind in ("done", "blocked"):
            self.sessions.awaiting.discard(name)
        fb = self.feedback_of(name)
        fb["spoken"] = fb["tick"] = time.monotonic()
        fb["reply_wall"] = time.time()
        fb["last_kind"] = kind
        many = len(self.sessions.names()) > 1
        self.log.write("claude_reply", name, f"kind={kind} spoken={int(not many or awaited or kind in ('done', 'blocked'))}", text)
        if many and not awaited and kind not in ("done", "blocked"):
            # Secondary session speaking on its own: I do not interrupt, I just flag it
            self.sessions.updates[name] = text
            self.speak(f"{self.label(name)} has an update.", kind="reply")
            return
        # "Last to speak" is the one you heard: a "has an update" does not count
        self.sessions.last_spoke = name
        if asks:
            self.sessions.asking[name] = time.monotonic()
        if many:
            # on a change of voice the name with the topic, then just the name
            prefix = self.label(name) if self.last_voice != name else name
            self.last_voice = name
            self.speak(f"{prefix}: {text}", kind="reply", voice=name, tail="waiting" if kind == "blocked" else "end")
        else:
            self.speak(text, kind="reply", voice=name, tail="waiting" if kind == "blocked" else "end")

    def on_permission_request(self, body):
        name = body["session"]
        self.sessions.last_spoke = name
        description = body.get("description", "")
        preview = body.get("input_preview", "")
        detail = description if description and description != "Run shell command" else preview[:160]
        self.log.write("claude_permission", name, f"request_id={body['request_id']} tool={body.get('tool_name', '')}", detail)
        self.questions.append({"kind": "permission", "session": name, "request_id": body["request_id"]})
        fb = self.feedback_of(name)
        fb["spoken"] = fb["tick"] = time.monotonic()
        self.speak(f"{name} asks permission to run {body.get('tool_name', 'a tool')}: {detail}. Say yes or no.", priority=0)

    # --- feedback while Claude works ------------------------------------------

    def feedback_of(self, name):
        fb = self.feedback.get(name)
        if fb is None:
            now = time.monotonic()
            fb = {"sent_wall": 0.0, "reply_wall": 0.0, "spoken": now, "tick": now, "ts": None, "last_kind": None}
            self.feedback[name] = fb
        return fb

    def feedback_tick(self):
        """From the main loop, once a second: tick, progress sentences, end of turn without a reply."""
        now = time.monotonic()
        if now - self.last_feedback_check < 1:
            return
        self.last_feedback_check = now
        quiet = self.quiet_until > time.time()
        many = len(self.sessions.names()) > 1
        for name in self.sessions.names():
            st = read_state(name) or {}
            ts = st.get("ts") or 0
            fresh = bool(st) and time.time() - ts < STATE_STALE_S
            state = st.get("state") if fresh else None
            fb = self.feedback_of(name)
            new_event = fb["ts"] is not None and ts != fb["ts"]
            fb["ts"] = ts
            awaiting = name in self.sessions.awaiting
            working = state == "working"
            unanswered = awaiting and fb["reply_wall"] < fb["sent_wall"]
            if ts and ts <= fb.get("stopped_wall", 0):   # stopped with the emergency word: the state is from before
                working = unanswered = False
                state = None

            # Stuck on a window in the terminal (usage limit, a Claude Code question...) while you wait for its
            # reply: I say it once, and no "still working". Permissions already have their spoken question
            if state == "waiting" and unanswered:
                if fb.get("waiting_ts") != ts:
                    fb["waiting_ts"] = ts
                    notice = (st.get("notice") or "").strip()
                    permission_pending = any(q["kind"] == "permission" and q.get("session") == name for q in list(self.questions))
                    if not permission_pending and "permission" not in notice.lower():
                        self.log.write("waiting_screen", name, "", notice)
                        line = f"{self.label(name) if many else name} is stuck on a window in the terminal and needs you."
                        if notice and "waiting for your input" not in notice.lower():
                            line += f" It says: {trim_words(notice, 20)}"
                        self.speak(line, kind="reply")
                continue

            # Delayed fallback: if in the meantime it restarts (background subagent, automatic turn)
            # or replies, it is no longer needed
            pending = fb.get("fallback")
            if pending:
                if working or fb["reply_wall"] > pending["wall"]:
                    fb["fallback"] = None
                elif now - pending["at"] >= FALLBACK_DELAY_S:
                    fb["fallback"] = None
                    fb["spoken"] = fb["tick"] = now
                    self.sessions.awaiting.discard(name)
                    self.log.write("no_reply", name, f"steps={pending['steps']}", pending["text"][:200])
                    # The notice in the radio's voice, the agent's words in its own
                    self.speak(f"{name} did not reply over the radio." + (" It wrote:" if pending["text"] else ""), kind="reply")
                    if pending["text"]:
                        self.speak(trim_words(pending["text"], FALLBACK_WORDS), kind="reply", voice=name)
                    continue

            # Stop hook: the turn is over. If Claude never went through the radio, or used other
            # tools after the last reply (an ack and then the work, never reported), I read what it wrote
            if new_event and state == "idle" and ts >= fb["sent_wall"] and awaiting:
                worked_after_reply = (st.get("tool_ts") or 0) > fb["reply_wall"] + 1
                fallback = unanswered or worked_after_reply
                self.log.write("turn_end", name, f"steps={st.get('steps', 0)} fallback={int(fallback)}")
                if fallback:
                    fb["fallback"] = {"at": now, "wall": time.time(), "steps": st.get("steps", 0),
                                      "text": plain_text(st.get("last_text") or "")}
                else:
                    self.sessions.awaiting.discard(name)
                    fb["last_kind"] = None
                continue

            # Utterance sent and no sign of life for minutes: it is not working
            if unanswered and not working and time.time() - fb["sent_wall"] > AWAIT_MAX_S:
                self.sessions.awaiting.discard(name)
                self.log.write("no_answer", name, f"after_s={AWAIT_MAX_S}")
                self.speak(f"{name} has not reacted in {AWAIT_MAX_S // 60} minutes. Check the terminal.")
                continue

            # Tick and progress only if it is really working (hook) or has not said anything yet.
            # A session without a state file (opened by hand without RADIO_STATE_DIR) has neither.
            if not (working or unanswered) or quiet:
                continue
            # Significant change of activity (new file written, command, delegation): I say it at once,
            # but not more often than PROGRESS_CHANGE_S; otherwise a sentence every PROGRESS_S of silence
            activity = (st.get("tool") or "", st.get("target") or "")
            changed = working and activity != fb.get("activity") and activity[0] in SIGNIFICANT_TOOLS
            fb["activity"] = activity
            due_change = PROGRESS_CHANGE_S > 0 and changed and now - fb["spoken"] >= PROGRESS_CHANGE_S
            # After PROGRESS_S of silence a sentence, but only if steps or activity changed since the last one:
            # the ticks are enough otherwise ("step 9, reading mobile zoomed" would be repeated over and over)
            news = (st.get("steps") or 0, activity) != fb.get("filled")
            due_silence = PROGRESS_S > 0 and now - fb["spoken"] >= PROGRESS_S and news
            if due_change or due_silence:
                fb["spoken"] = fb["tick"] = now
                fb["filled"] = (st.get("steps") or 0, activity)
                steps = st.get("steps") or 0
                what = activity_text(st) if working else "thinking"
                prefix = f"{name}, " if many else ""
                line = f"{prefix}step {steps}, {what}." if due_change and steps else \
                    (f"{prefix}still working, step {steps}, {what}." if steps else f"{prefix}still working, {what}.")
                self.log.write("progress", name, f"steps={steps} change={int(due_change)}", what)
                self.speak(line, kind="filler")
            elif TICK_S > 0 and not BED_BEEPS and now - fb["tick"] >= TICK_S and not self.audio_playing() and not self.user_talking():
                fb["tick"] = now
                logging.info("tick %s (step %s)", name, st.get("steps", 0))
                self.play(BEEP_TICK)

    # --- speaking ------------------------------------------------------------

    def speak(self, text, priority=2, kind="info", voice=None, tail=None):
        """kind: info, reply (held back in quiet mode), filler (dropped if the user transmits),
        urgent (goes over the user: only the open-microphone warning).
        voice: the session to speak as (its voice, if it has one), otherwise the bot's voice.
        tail: the sound after the sentence ("end", "waiting"), if the theme has one."""
        if kind == "reply" and self.quiet_until > time.time():
            self.held.append((text, voice, tail))
            return
        work = {"filler": "speak_filler", "urgent": "speak_urgent"}.get(kind, "speak")
        self.enqueue(0 if kind == "urgent" else priority, work, (text, voice, tail))

    def sound(self, pcm):
        """A theme sound in the same queue as the voice: it does not land in the middle of a sentence."""
        if pcm:
            self.enqueue(2, "sound", pcm)

    def flush_held(self):
        held, self.held = self.held, []
        for item in held:
            self.enqueue(2, "speak", item)

    def speak_now(self, item, kind="speak", priority=2):
        """In the worker: synthesizes and puts on the air, sentence by sentence. While the user transmits nothing
        is spoken: the sentence waits (filler ones are dropped, afterwards they would be stale) and if they open the
        microphone mid-sentence it goes quiet. Only the open-microphone warning goes over."""
        text, voice, tail = (tuple(item) + (None, None))[:3] if isinstance(item, tuple) else (item, None, None)
        urgent = kind == "speak_urgent"
        if not urgent and self.user_talking():
            if kind == "speak_filler":
                logging.info("filler sentence skipped, the user is transmitting: %s", text)
            else:
                self.deferred.append((priority, text, voice, tail))
                logging.info("sentence waiting (%d), the user is transmitting: %s", len(self.deferred), text[:80])
            return
        cur = {"text": text, "voice": voice, "tail": tail, "priority": priority, "cut": False, "done": False,
               "urgent": urgent, "ends_at": 0.0}
        self.speaking = cur
        started = time.monotonic()
        # Voice still loading (after a bot restart the voices arrive one at a time, 2-7 s each): wait for it.
        # Before, the reply came out in the radio's voice
        deadline = time.monotonic() + VOICE_WAIT_S
        while voice in self.voices_loading and voice not in self.voices and time.monotonic() < deadline:
            time.sleep(0.1)
        speaker = self.voices.get(voice) or self.speaker
        signature = sounds.signature(SOUND_THEME, voice) if AGENT_SIGNATURES and voice and SOUND_THEME != "classic" else b""
        try:
            for i, pcm in enumerate(speaker.chunks(text)):
                if cur["cut"]:
                    return
                if not urgent and self.user_talking():
                    self.cut_speech(cur)
                    return
                if VOICE_FX:
                    pcm = sounds.replicant(pcm, VOICE_FX)
                self.play(silence(REPLY_PREROLL_MS) + signature + pcm if i == 0 else pcm)
            if tail and not cur["cut"]:
                self.play(SOUNDS.get(tail, lambda: b"")())
        except Exception as e:
            self.log.write("tts_error", BOT_NAME, "", str(e)[:200])
            self.play(BEEP_ERROR)
            return
        finally:
            cur["ends_at"] = time.monotonic() + self.audio_left()
            cur["done"] = True
        if cur["cut"]:
            return
        self.last_spoken = text
        # the board code is spoken but not written: in the log (and in the Docker logs) it stays hidden
        logged = BOARD_CODE_SPOKEN.sub("Board code: (hidden).", text)
        self.log.write("spoken", BOT_NAME, f"tts_ms={int((time.monotonic() - started) * 1000)}", logged)

    # --- main loop -------------------------------------------------------

    def worker_loop(self):
        while True:
            _priority, _seq, kind, payload = self.work.get()
            if kind == "stop":
                return
            try:
                if kind == "utterance":
                    self.handle(payload)
                elif kind == "sound":
                    if not self.user_talking():
                        self.play(payload)
                else:
                    if self.held and self.quiet_until <= time.time():
                        self.flush_held()
                    self.speak_now(payload, kind, _priority)
            except Exception:
                logging.exception("Error in the worker (%s)", kind)

    def heartbeat_loop(self):
        interval = HEARTBEAT_MIN * 60
        while not self.stopping.wait(interval):
            listeners = self.humans()
            if self.synced and listeners and not self.user_talking():
                self.play(BEEP_HEARTBEAT)
                self.log.write("heartbeat", BOT_NAME, f"to={','.join(listeners)}")

    def run(self):
        logging.info("Connecting to %s:%s as '%s'", HOST, PORT, BOT_NAME)
        self.mumble.start()
        threading.Thread(target=self.worker_loop, daemon=True).start()
        if HEARTBEAT_MIN > 0:
            threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        while not self.stopping.is_set():
            if not self.mumble.is_alive():
                # pymumble does not survive a refusal from the server (wrong password,
                # name already in use): better to exit and be restarted
                # by Docker than to stay alive and mute.
                self.log.write("bot_crashed", BOT_NAME, "see=docker_compose_logs_bot")
                sys.exit(1)
            for buf in self.finished_utterances():
                # "received": at once, even if the worker is busy. Not at the cut at MAX_UTTERANCE_S:
                # the user is still speaking, the beep comes at the end of the transmission
                if len(buf["pcm"]) // BYTES_PER_MS >= MIN_UTTERANCE_MS and not buf.get("cut"):
                    self.play(BEEP_HEARD)
                self.enqueue(1, "utterance", buf)
            self.audio_tick()
            self.ambient_tick()
            if self.recording is not None and self.recording.duration() > RECORD_MAX_S:
                self.record_stop(f"max={int(RECORD_MAX_S)}")
            self.check_mic_open()
            self.feedback_tick()
            time.sleep(0.05)
        self.enqueue(9, "stop", None)
        self.log.write("bot_stopped", BOT_NAME)
        try:
            self.mumble.stop()
        except Exception:
            pass

    def stop(self, *_args):
        self.stopping.set()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("PyMumble").setLevel(logging.WARNING)
    transcriber = speaker = None
    logging.info("Packets of %.0f ms, preroll %.0f ms", AUDIO_PER_PACKET * 1000, REPLY_PREROLL_MS)
    if ECHO:
        logging.info("ECHO=1: repeating the audio, no transcription (delay %gs)", ECHO_DELAY_S)
    else:
        transcriber = Transcriber(WHISPER_URL, WHISPER_FAST_URL, WHISPER_FAST_MAX_MS)
        speaker = Speaker(PIPER_VOICE, PIPER_DIR)   # exits at once if the voice is missing
        logging.info("Whisper accurate: %s, fast: %s (utterances up to %.0f ms), language %s, beam %d.  Piper: %s at %d Hz, length_scale %.2f",
                     WHISPER_URL, WHISPER_FAST_URL or "no", WHISPER_FAST_MAX_MS, WHISPER_LANGUAGE,
                     WHISPER_BEAM_SIZE, PIPER_VOICE, speaker.rate, PIPER_LENGTH_SCALE)
        logging.info("Registered projects: %s", ", ".join(p["name"] for p in load_projects()) or "none")
    bot = RadioBot(EventLog(LOG_DIR), transcriber, speaker)
    bot.restore_routing()
    ApiHandler.bot = bot
    api = ThreadingHTTPServer(("0.0.0.0", API_PORT), ApiHandler)
    api.daemon_threads = True
    threading.Thread(target=api.serve_forever, daemon=True).start()
    logging.info("API for adapters and launcher on port %d", API_PORT)
    if BOARD_PORT > 0:
        bot.board = board.Board(BOARD_DIR, BOARD_KEEP)
        board.start(bot.board, bot.log, BOARD_PORT, bot.board_sessions)
        logging.info("Board on port %d (%s), items in %s", BOARD_PORT, BOARD_URL or "address not set", BOARD_DIR)
    signal.signal(signal.SIGTERM, bot.stop)
    signal.signal(signal.SIGINT, bot.stop)
    bot.run()


if __name__ == "__main__":
    main()
