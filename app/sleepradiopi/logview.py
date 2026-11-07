"""The radio's log, for the desktop's Log window (GET /api/log).

On the appliance syslog's file is in memory (/var/log is a link to /tmp, a RAM disk), so reading it writes nothing
to the card, and a restart clears it. This only reads: the newest lines of the file, each split into when, how bad
(DEBUG / INFO / WARNING / ERROR / CRITICAL), who said it and what, optionally just the problems, or the lines with
some text in them.

The radio's own lines come through `logger` as "<time> <host> user.notice sleepradiopi: 2026-11-07 21:40:19,592
ERROR sleepradiopi.audio.speaker: aplay stopped ...", so their level is the one Python logged, not syslog's. The
lines of a traceback arrive one by one without a level of their own: "Traceback ..." is an error, and the lines after
it take the level of the line above.
"""
import os
import re
from pathlib import Path

# the older file (syslog rotates it when it's big) then the current one; SLEEPRADIOPI_LOG_FILE: one file, for trying it
# somewhere that isn't the radio
LOG_FILES = ((Path(os.environ["SLEEPRADIOPI_LOG_FILE"]),) if os.environ.get("SLEEPRADIOPI_LOG_FILE")
             else (Path("/var/log/messages.0"), Path("/var/log/messages")))
MAX_LINES = 1000
DEFAULT_LINES = 500
READ_MAX = 1_000_000            # bytes of the newest file read at most (it is rotated at a few hundred KB)
MAX_QUERY = 100

KERNEL_BOOT_S = 120             # the kernel's warnings in its first two minutes are the drivers starting, not trouble

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
PROBLEMS = {"WARNING", "ERROR", "CRITICAL"}

_LINE = re.compile(r"^(\w{3}\s+\d{1,2} \d\d:\d\d:\d\d) (\S+) (\S+) (.*)$", re.S)
_PY = re.compile(r"^([^\s:]+): \d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) ([^\s:]+): (.*)$", re.S)
_KERNEL_AT = re.compile(r"^\[\s*(\d+(?:\.\d+)?)\]")
_SYSLOG = {"emerg": "CRITICAL", "alert": "CRITICAL", "crit": "CRITICAL", "err": "ERROR", "warn": "WARNING", "warning": "WARNING",
           "notice": "INFO", "info": "INFO", "debug": "DEBUG"}


def parse(line: str, above: dict | None = None) -> dict:
    """One line of the file -> {"t": "Nov  7 21:40:19", "level", "who", "text"}. above: the line before, whose level a
    traceback's lines (said under the radio's own tag, with no level of their own) take."""
    m = _LINE.match(line)
    if not m:                                              # (not even syslog's own shape: just text)
        return {"t": "", "level": (above or {}).get("level", "INFO"), "who": "", "text": line}
    when, _host, facility, rest = m.groups()
    py = _PY.match(rest)
    if py:
        return {"t": when, "level": py.group(2), "who": py.group(3), "text": py.group(4), "tag": py.group(1)}
    tag, sep, text = rest.partition(": ")
    if not sep:
        tag, text = "", rest
    level = _SYSLOG.get(facility.rpartition(".")[2], "INFO")
    if tag == "kernel" and level == "WARNING":
        at = _KERNEL_AT.match(text)
        if at and float(at.group(1)) < KERNEL_BOOT_S:
            level = "INFO"                                 # (the drivers starting: they log at warning level)
    if tag == "sleepradiopi":
        if text.startswith("Traceback (most recent call last)"):
            level = "ERROR"                                # (an uncaught one, from a process: no level of its own)
        elif above and above.get("tag") == tag and above["level"] in PROBLEMS:
            level = above["level"]                         # (the rest of a traceback)
    return {"t": when, "level": level, "who": tag, "text": text, "tag": tag}


def _read(files) -> str:
    """The files' text, the older first; at most READ_MAX bytes of the newest."""
    parts = []
    for f in files:
        try:
            size = f.stat().st_size
            with open(f, "rb") as fh:
                if size > READ_MAX:
                    fh.seek(size - READ_MAX)
                parts.append(fh.read().decode("utf-8", "replace"))
        except OSError:
            continue
    return "".join(p if p.endswith("\n") or not p else p + "\n" for p in parts)


def read_log(level: str = "all", q: str = "", limit: int = DEFAULT_LINES, files=None) -> dict:
    """-> {"exists", "lines": [{"t", "level", "who", "text"}] (the newest last), "matched", "shown", "truncated"}.
    level "problems": warnings and worse only. q: lines with this text in them (any case)."""
    files = LOG_FILES if files is None else files
    limit = max(1, min(MAX_LINES, int(limit)))
    q = (q or "").strip()[:MAX_QUERY].casefold()
    exists = any(Path(f).exists() for f in files)
    lines, above = [], None
    for raw in _read(files).splitlines():
        if not raw.strip():
            continue
        p = parse(raw, above)
        above = p
        if level == "problems" and p["level"] not in PROBLEMS:
            continue
        if q and q not in f"{p['who']} {p['text']} {p['level']}".casefold():
            continue
        lines.append({k: p[k] for k in ("t", "level", "who", "text")})
    shown = lines[-limit:]
    return {"exists": exists, "lines": shown, "matched": len(lines), "shown": len(shown), "truncated": len(lines) > limit}
