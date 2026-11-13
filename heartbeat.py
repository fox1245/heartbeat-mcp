#!/usr/bin/env python3
"""Heartbeat channels for a coding agent.

The agent opens one channel per thing it waits for (a subagent, a gate, a long command). There is no limit on the
number of channels. A channel has a deadline, optionally a liveness interval, and an optional notation: what to do
when the channel fires. The thing waited for can speak on its channel: `beat` (still alive), `note` (needs attention)
and `finish` (done). A channel fires when its deadline passes without a sign of life, when it is finished, or when it
holds a note. The agent is then told, in three ways:

  * a hook on tool results and user prompts adds a line to the agent's context (while the agent is working);
  * a Stop hook refuses to end the turn while a channel has fired, or while open channels are not covered by a waiter;
  * a waiter (`heartbeat.py wait`), started by the agent as a background command, watches every channel (or the named
    ones) and exits when one fires. The harness re-invokes an idle agent when a background command exits, so this is
    what wakes an idle session.

One file, standard library only: the command line, the hook entry and the MCP server (stdio) share the state code.
State is one JSON file per session under the state directory, changed under an exclusive file lock.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import secrets
import sys
import time
import uuid

VERSION = "0.2.0"


def _setting(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


STATE_DIR = os.environ.get("HEARTBEAT_STATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".local", "state", "heartbeat-mcp")
NAG_EVERY_SEC = _setting("HEARTBEAT_NAG_SEC", 300.0)          # a fired channel is repeated in context this often
ARM_NAG_EVERY_SEC = _setting("HEARTBEAT_ARM_NAG_SEC", 600.0)  # "channels are not covered" is repeated this often
WAIT_POLL_SEC = _setting("HEARTBEAT_WAIT_POLL_SEC", 15.0)     # a waiter re-reads the state this often
NAME_MAX = 64
UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0}
SELF = os.path.realpath(__file__)

OPEN, FINISHED, CLOSED = "open", "finished", "closed"


class HeartbeatError(Exception):
    """A caller error; the message is shown as it is."""


# ---------------------------------------------------------------------------------------------------------------------
# Session and state
# ---------------------------------------------------------------------------------------------------------------------

def resolve_session(explicit: str | None = None) -> str:
    """The session this process belongs to: the given id, the harness's environment variable, or the session file of
    the nearest ancestor process that has one (an MCP server and a hook are descendants of the agent's process)."""
    sid = explicit or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        pid = os.getppid()
        for _ in range(16):
            try:
                with open(os.path.join(os.path.expanduser("~"), ".claude", "sessions", f"{pid}.json")) as handle:
                    sid = json.load(handle)["sessionId"]
                break
            except (OSError, KeyError, ValueError):
                pass
            try:
                with open(f"/proc/{pid}/stat") as handle:
                    pid = int(handle.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                break
            if pid <= 1:
                break
    if not sid:
        raise HeartbeatError("no session id: set CLAUDE_CODE_SESSION_ID or pass --session")
    try:
        return str(uuid.UUID(sid))          # also keeps the id safe as a file name
    except ValueError as error:
        raise HeartbeatError(f"the session id is not a UUID: {sid!r}") from error


def state_path(sid: str) -> str:
    return os.path.join(STATE_DIR, f"{sid}.json")


def _migrate(data: dict) -> None:
    """State written by version 0.1 (a list of beats, one waiter) becomes channels."""
    for beat in data.pop("beats", []):
        if beat.get("status") == "pending":
            data["channels"][beat["id"]] = new_channel(beat["id"], beat.get("label", ""), beat.get("check") or None,
                                                       beat["due"], None, None, created=beat.get("created"))
    data.pop("waiter", None)


@contextlib.contextmanager
def state(sid: str, write: bool = True):
    """The session's state as a dict, read (and written back) under an exclusive lock."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    descriptor = os.open(state_path(sid), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        raw = handle.read()
        data = json.loads(raw) if raw.strip() else {}
        data.setdefault("channels", {})
        data.setdefault("waiters", [])
        data.setdefault("last_arm_nag", 0.0)
        _migrate(data)
        yield data
        if write:
            data["waiters"] = [waiter for waiter in data["waiters"] if _is_waiter(waiter)]
            handle.seek(0)
            handle.truncate()
            json.dump(data, handle, indent=1)


def parse_duration(text: str) -> float:
    """Seconds from "90s", "40m", "2h" or a bare number of minutes."""
    text = text.strip()
    if not text:
        raise HeartbeatError("an empty duration")
    unit = UNITS.get(text[-1].lower())
    number = text[:-1] if unit else text
    try:
        seconds = float(number) * (unit or UNITS["m"])
    except ValueError as error:
        raise HeartbeatError(f"not a duration: {text!r} (use 90s, 40m, 2h, or minutes)") from error
    if seconds <= 0:
        raise HeartbeatError("a duration must be positive")
    return seconds


def check_name(name: str) -> str:
    if not name or len(name) > NAME_MAX or not name.isprintable() or any(ch.isspace() for ch in name):
        raise HeartbeatError(f"a channel name is 1 to {NAME_MAX} printable characters without spaces: {name!r}")
    return name


def clock(moment: float) -> str:
    return datetime.datetime.fromtimestamp(moment).strftime("%H:%M:%S")


# ---------------------------------------------------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------------------------------------------------

def new_channel(name: str, label: str, notation: str | None, due: float, interval: float | None, agent: str | None,
                created: float | None = None) -> dict:
    return {"name": name, "label": label, "notation": notation, "created": created or time.time(), "due": due,
            "interval": interval, "agent": agent, "status": OPEN, "last_beat": None, "beats": 0, "notes": [],
            "finish_text": None, "last_nag": 0.0}


def live(data: dict) -> list[dict]:
    """Channels that are not closed, oldest deadline first."""
    return sorted((ch for ch in data["channels"].values() if ch["status"] != CLOSED), key=lambda ch: ch["due"])


def events_of(channel: dict, now: float) -> list[str]:
    """Why a channel fires now: "finished", "note", "overdue" (none when it is quiet)."""
    reasons = []
    if channel["status"] == FINISHED:
        reasons.append("finished")
    if channel["notes"]:
        reasons.append("note")
    if channel["status"] == OPEN and channel["due"] <= now:
        reasons.append("overdue")
    return reasons


def fired(data: dict, now: float) -> list[dict]:
    return [channel for channel in live(data) if events_of(channel, now)]


def describe(channel: dict, now: float) -> str:
    parts = []
    reasons = events_of(channel, now)
    if "finished" in reasons:
        parts.append("FINISHED" + (f": {channel['finish_text']}" if channel.get("finish_text") else ""))
    if "overdue" in reasons:
        parts.append(f"OVERDUE: due {clock(channel['due'])}, {int((now - channel['due']) // 60)} min ago")
    elif channel["status"] == OPEN:
        parts.append(f"due {clock(channel['due'])}, in {int((channel['due'] - now) // 60) + 1} min")
    for note in channel["notes"]:
        parts.append(f"NOTE {clock(note['t'])}: {note['text']}")
    if channel.get("interval"):
        parts.append(f"expects a sign of life every {int(channel['interval'] // 60)} min")
    if channel.get("last_beat"):
        parts.append(f"last sign of life {clock(channel['last_beat'])} ({channel['beats']} so far)")
    elif channel.get("interval") or channel.get("agent"):
        parts.append("no sign of life yet")
    text = f"[{channel['name']}] {channel['label']} ({'; '.join(parts)})"
    if channel.get("notation"):
        text += f"\n      to do: {channel['notation']}"
    return text


def _is_waiter(waiter: dict) -> bool:
    """Whether a recorded waiter is still this program waiting (a reused pid of another program does not count)."""
    try:
        with open(f"/proc/{waiter['pid']}/cmdline", "rb") as handle:
            arguments = handle.read().split(b"\0")
    except (OSError, KeyError):
        return False
    return SELF.encode() in arguments and b"wait" in arguments


def uncovered(data: dict) -> list[dict]:
    """Open channels that no running waiter watches (a waiter with no channel list watches all)."""
    waiters = [waiter for waiter in data["waiters"] if _is_waiter(waiter)]
    if any(waiter.get("channels") is None for waiter in waiters):
        return []
    watched = {name for waiter in waiters for name in waiter.get("channels") or []}
    return [channel for channel in live(data) if channel["status"] == OPEN and channel["name"] not in watched]


# ---------------------------------------------------------------------------------------------------------------------
# Operations (shared by the command line and the MCP server)
# ---------------------------------------------------------------------------------------------------------------------

ARM_HINT = (f"Start a waiter as a background command so an idle session is woken when a channel fires: "
            f"python3 -I {SELF} wait")


def _get(data: dict, name: str) -> dict:
    channel = data["channels"].get(name)
    if channel is None or channel["status"] == CLOSED:
        raise HeartbeatError(f"no open channel named {name!r}")
    return channel


def op_open(sid: str, name: str | None, label: str, seconds: float | None, every: float | None,
            notation: str | None, agent: str | None) -> dict:
    if not label.strip():
        raise HeartbeatError("a channel needs a label")
    if seconds is None and every is None:
        raise HeartbeatError("a channel needs a deadline (--in) or a liveness interval (--every)")
    name = check_name(name) if name else secrets.token_hex(3)
    now = time.time()
    with state(sid) as data:
        existing = data["channels"].get(name)
        if existing is not None and existing["status"] != CLOSED:
            raise HeartbeatError(f"channel {name!r} is already open")
        data["channels"][name] = new_channel(name, label.strip(), (notation or "").strip() or None,
                                             now + (seconds if seconds is not None else every), every,
                                             (agent or "").strip() or None)
        return {"channel": dict(data["channels"][name]), "covered": not uncovered(data)}


def op_beat(sid: str, name: str, text: str | None = None) -> dict:
    """A sign of life from the thing waited for. With a liveness interval it moves the deadline."""
    now = time.time()
    with state(sid) as data:
        channel = _get(data, name)
        channel["last_beat"] = now
        channel["beats"] += 1
        if channel["status"] == OPEN and channel.get("interval"):
            channel["due"] = now + channel["interval"]
            channel["last_nag"] = 0.0
        if text:
            channel["last_text"] = text.strip()
        return dict(channel)


def op_note(sid: str, name: str, text: str) -> dict:
    """The thing waited for asks for attention; the channel fires until the agent acknowledges it."""
    if not text.strip():
        raise HeartbeatError("a note needs text")
    with state(sid) as data:
        channel = _get(data, name)
        channel["notes"].append({"t": time.time(), "text": text.strip()})
        channel["last_nag"] = 0.0
        return dict(channel)


def op_finish(sid: str, name: str, text: str | None) -> dict:
    """The thing waited for reports that it is done; the channel fires until the agent closes it."""
    with state(sid) as data:
        channel = _get(data, name)
        channel["status"] = FINISHED
        channel["finish_text"] = (text or "").strip() or None
        channel["finished"] = time.time()
        channel["last_nag"] = 0.0
        return dict(channel)


def op_extend(sid: str, name: str, seconds: float) -> dict:
    """The agent looked and the work goes on: a new deadline; notes are acknowledged."""
    with state(sid) as data:
        channel = _get(data, name)
        channel["status"] = OPEN
        channel["due"] = time.time() + seconds
        channel["notes"] = []
        channel["finish_text"] = None
        channel["last_nag"] = 0.0
        return dict(channel)


def op_ack(sid: str, name: str) -> dict:
    """The agent read the channel's notes; the deadline stays."""
    with state(sid) as data:
        channel = _get(data, name)
        channel["notes"] = []
        channel["last_nag"] = 0.0
        return dict(channel)


def op_notate(sid: str, name: str, notation: str | None) -> dict:
    """Set or clear (None) what to do when the channel fires."""
    with state(sid) as data:
        channel = _get(data, name)
        channel["notation"] = (notation or "").strip() or None
        return dict(channel)


def op_close(sid: str, name: str) -> dict:
    """The agent looked at the result; the channel is done with."""
    now = time.time()
    with state(sid) as data:
        channel = _get(data, name)
        channel["status"] = CLOSED
        channel["closed"] = now
        data["channels"] = {key: ch for key, ch in data["channels"].items()
                            if ch["status"] != CLOSED or now - ch.get("closed", 0) < 86400}
        return dict(channel)


def op_list(sid: str) -> dict:
    with state(sid, write=False) as data:
        return {"channels": live(data), "uncovered": [channel["name"] for channel in uncovered(data)]}


def render_list(listing: dict) -> str:
    if not listing["channels"]:
        return "no open channels"
    now = time.time()
    lines = [describe(channel, now) for channel in listing["channels"]]
    lines.append("waiters cover every open channel" if not listing["uncovered"] else
                 "NOT covered by a waiter: " + ", ".join(listing["uncovered"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------
# The waiter: a select over channels
# ---------------------------------------------------------------------------------------------------------------------

def run_wait(sid: str, names: list[str] | None) -> int:
    """Blocks until one of the watched channels fires (every channel when none is named), then says which and why."""
    with state(sid) as data:
        for name in names or []:
            _get(data, name)
        data["waiters"].append({"pid": os.getpid(), "started": time.time(), "channels": names or None})
    try:
        while True:
            now = time.time()
            with state(sid, write=False) as data:
                watched = [channel for channel in live(data) if not names or channel["name"] in names]
                others = [channel for channel in live(data) if names and channel["name"] not in names]
            if not watched:
                print("heartbeat: no open channel to wait for")
                return 0
            hits = [channel for channel in watched if events_of(channel, now)]
            if hits:
                print(f"HEARTBEAT: {len(hits)} channel(s) fired. Look at each now, then close it, extend it or "
                      f"acknowledge its notes:")
                for channel in hits:
                    print("  " + describe(channel, now))
                quiet = [channel for channel in watched + others if not events_of(channel, now)]
                for channel in quiet:
                    print("  quiet: " + describe(channel, now))
                if quiet:
                    print("Start a waiter again afterwards: channels are still open.")
                return 0
            next_due = min(channel["due"] for channel in watched if channel["status"] == OPEN)
            time.sleep(max(1.0, min(WAIT_POLL_SEC, next_due - now)))
    finally:
        with state(sid) as data:
            data["waiters"] = [waiter for waiter in data["waiters"] if waiter.get("pid") != os.getpid()]


# ---------------------------------------------------------------------------------------------------------------------
# The hook (reads the harness's JSON on stdin, answers with JSON; never fails the host)
# ---------------------------------------------------------------------------------------------------------------------

def hook_answer(event: str, payload: dict) -> dict:
    try:
        sid = resolve_session(payload.get("session_id"))
    except HeartbeatError:
        return {}
    if not os.path.exists(state_path(sid)):
        return {}                                     # a session that never opened a channel costs one stat call
    now = time.time()
    agent = payload.get("agent_id")
    with state(sid) as data:
        if agent:
            # A tool call made inside a subagent: a sign of life on the channels bound to that agent. The reminders
            # below are for the session's own agent, so a subagent's call gets none.
            for channel in live(data):
                if channel.get("agent") == agent and channel["status"] == OPEN:
                    channel["last_beat"] = now
                    channel["beats"] += 1
                    if channel.get("interval"):
                        channel["due"] = now + channel["interval"]
                        channel["last_nag"] = 0.0
            return {}
        hits = fired(data, now)
        bare = uncovered(data)
        if event == "Stop":
            if payload.get("stop_hook_active"):
                return {}                             # the turn already continued because of a Stop hook: no loop
            if hits:
                listing = "\n".join("  " + describe(channel, now) for channel in hits)
                return {"decision": "block",
                        "reason": "Heartbeat: channels fired. Look at each one now (read the work's own output, "
                                  "resume or stop its agent), then close it, extend it or acknowledge its notes:\n"
                                  f"{listing}\n(python3 -I {SELF} close <name> | extend <name> --in <time> | "
                                  "ack <name>)"}
            if bare:
                return {"decision": "block",
                        "reason": f"Heartbeat: {len(bare)} open channel(s) are not watched by a waiter "
                                  f"({', '.join(channel['name'] for channel in bare)}), so nothing would wake this "
                                  f"session when one fires. {ARM_HINT}"}
            return {}
        lines = []
        if any(now - channel.get("last_nag", 0.0) >= NAG_EVERY_SEC for channel in hits):
            for channel in hits:
                channel["last_nag"] = now
            lines.append("HEARTBEAT fired (look now, then close, extend or ack):\n" +
                         "\n".join("  " + describe(channel, now) for channel in hits))
        if bare and not hits and now - data.get("last_arm_nag", 0.0) >= ARM_NAG_EVERY_SEC:
            data["last_arm_nag"] = now
            lines.append(f"HEARTBEAT: {len(bare)} open channel(s) are not watched by a waiter. {ARM_HINT}")
        if not lines:
            return {}
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": "\n".join(lines)}}


def run_hook(event: str) -> int:
    try:
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            payload = {}
        answer = hook_answer(event, payload if isinstance(payload, dict) else {})
    except Exception:                                  # a reminder tool must never break the host's hooks
        answer = {}
    print(json.dumps(answer))
    return 0


# ---------------------------------------------------------------------------------------------------------------------
# The MCP server (JSON-RPC 2.0 over stdio, one JSON object per line)
# ---------------------------------------------------------------------------------------------------------------------

_NAME = {"type": "string", "description": "The channel's name."}
TOOLS = [
    {"name": "heartbeat_open",
     "description": "Open a channel for one thing you wait for (a subagent, a gate, a long command). Any number of "
                    "channels can be open. Give the time you expect it to take (minutes), or a liveness interval "
                    "(every_minutes: the deadline moves on every sign of life), or both. notation says what to do when "
                    "the channel fires. agent binds the channel to a subagent id: that subagent's tool calls then "
                    "count as signs of life. Also start a waiter in the background (the result says how).",
     "inputSchema": {"type": "object", "required": ["label"], "properties": {
         "name": {"type": "string", "description": "A short name without spaces; generated when omitted."},
         "label": {"type": "string", "description": "What is running, in a few words."},
         "minutes": {"type": "number", "description": "Expected time until it is finished."},
         "every_minutes": {"type": "number", "description": "Longest silence allowed between signs of life."},
         "notation": {"type": ["string", "null"], "description": "What to do when the channel fires."},
         "agent": {"type": ["string", "null"], "description": "A subagent id to bind the channel to."}}}},
    {"name": "heartbeat_beat",
     "description": "A sign of life on a channel, from the thing waited for (moves the deadline of a channel that "
                    "has a liveness interval).",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {
         "name": _NAME, "text": {"type": "string"}}}},
    {"name": "heartbeat_note",
     "description": "Ask for attention on a channel: it fires until its notes are acknowledged.",
     "inputSchema": {"type": "object", "required": ["name", "text"], "properties": {
         "name": _NAME, "text": {"type": "string"}}}},
    {"name": "heartbeat_finish",
     "description": "Report on a channel that the work is done: it fires until it is closed.",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {
         "name": _NAME, "text": {"type": "string"}}}},
    {"name": "heartbeat_extend",
     "description": "You looked and the work goes on: a new deadline (now + minutes); notes are acknowledged.",
     "inputSchema": {"type": "object", "required": ["name", "minutes"], "properties": {
         "name": _NAME, "minutes": {"type": "number"}}}},
    {"name": "heartbeat_ack",
     "description": "Acknowledge a channel's notes; the deadline stays.",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {"name": _NAME}}},
    {"name": "heartbeat_notate",
     "description": "Set or clear (null) a channel's notation: what to do when it fires.",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {
         "name": _NAME, "notation": {"type": ["string", "null"]}}}},
    {"name": "heartbeat_close",
     "description": "Close a channel after you looked at the work's result.",
     "inputSchema": {"type": "object", "required": ["name"], "properties": {"name": _NAME}}},
    {"name": "heartbeat_list",
     "description": "List open channels with their state, and which are not watched by a waiter.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _minutes(arguments: dict, key: str) -> float | None:
    value = arguments.get(key)
    if value is None:
        return None
    seconds = float(value) * 60.0
    if seconds <= 0:
        raise HeartbeatError(f"{key} must be positive")
    return seconds


def call_tool(name: str, arguments: dict) -> str:
    sid = resolve_session()
    now = time.time()
    if name == "heartbeat_open":
        result = op_open(sid, arguments.get("name"), str(arguments["label"]), _minutes(arguments, "minutes"),
                         _minutes(arguments, "every_minutes"), arguments.get("notation"), arguments.get("agent"))
        text = "opened " + describe(result["channel"], now)
        return text if result["covered"] else f"{text}\n{ARM_HINT}"
    channel_name = str(arguments.get("name", ""))
    if name == "heartbeat_beat":
        return "beat on " + describe(op_beat(sid, channel_name, arguments.get("text")), now)
    if name == "heartbeat_note":
        return "noted on " + describe(op_note(sid, channel_name, str(arguments["text"])), now)
    if name == "heartbeat_finish":
        return "finished " + describe(op_finish(sid, channel_name, arguments.get("text")), now)
    if name == "heartbeat_extend":
        seconds = _minutes(arguments, "minutes")
        if seconds is None:
            raise HeartbeatError("minutes is required")
        return "extended " + describe(op_extend(sid, channel_name, seconds), now)
    if name == "heartbeat_ack":
        return "acknowledged " + describe(op_ack(sid, channel_name), now)
    if name == "heartbeat_notate":
        return "notation set on " + describe(op_notate(sid, channel_name, arguments.get("notation")), now)
    if name == "heartbeat_close":
        channel = op_close(sid, channel_name)
        return f"closed [{channel['name']}] {channel['label']}"
    if name == "heartbeat_list":
        return render_list(op_list(sid))
    raise HeartbeatError(f"unknown tool {name!r}")


def mcp_reply(request: dict) -> dict | None:
    method = request.get("method")
    if "id" not in request:
        return None                                   # a notification gets no answer
    base = {"jsonrpc": "2.0", "id": request["id"]}
    params = request.get("params") or {}
    if method == "initialize":
        return {**base, "result": {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "heartbeat", "version": VERSION},
            "instructions": "Open one channel per thing you wait for (heartbeat_open) with the time you expect it to "
                            "take and a notation of what to do when it fires, and keep a waiter running in the "
                            "background (the result names the command). When a channel fires, look at the work "
                            "itself (its output file, its process), then heartbeat_close, heartbeat_extend or "
                            "heartbeat_ack. Things you wait for can speak on their channel: beat, note, finish."}}
    if method == "ping":
        return {**base, "result": {}}
    if method == "tools/list":
        return {**base, "result": {"tools": TOOLS}}
    if method == "tools/call":
        try:
            text = call_tool(str(params.get("name")), params.get("arguments") or {})
            return {**base, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except (HeartbeatError, KeyError, TypeError, ValueError) as error:
            return {**base, "result": {"content": [{"type": "text", "text": f"heartbeat: {error}"}], "isError": True}}
    return {**base, "error": {"code": -32601, "message": f"method not found: {method}"}}


def run_mcp() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        reply = mcp_reply(request) if isinstance(request, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="heartbeat", description=__doc__.split("\n\n")[0])
    parser.add_argument("--session", help="session id (default: the harness's environment)")
    commands = parser.add_subparsers(dest="command", required=True)

    opener = commands.add_parser("open", help="open a channel for one thing you wait for")
    opener.add_argument("name", nargs="?", help="a short name without spaces (generated when omitted)")
    opener.add_argument("--label", required=True)
    opener.add_argument("--in", dest="duration", help="expected time until it is finished: 90s, 40m, 2h, or minutes")
    opener.add_argument("--every", help="longest silence allowed between signs of life")
    opener.add_argument("--notation", "--check", dest="notation", default=None,
                        help="what to do when the channel fires")
    opener.add_argument("--agent", default=None, help="a subagent id whose tool calls count as signs of life")
    for verb, text in (("beat", "a sign of life, from the thing waited for"),
                       ("finish", "the thing waited for is done"),
                       ("note", "the thing waited for asks for attention")):
        sub = commands.add_parser(verb, help=text)
        sub.add_argument("name")
        sub.add_argument("--text", default=None, required=(verb == "note"))
    extend = commands.add_parser("extend", help="you looked and the work goes on: a new deadline")
    extend.add_argument("name")
    extend.add_argument("--in", dest="duration", required=True)
    commands.add_parser("ack", help="acknowledge a channel's notes").add_argument("name")
    notate = commands.add_parser("notate", help="set or clear what to do when the channel fires")
    notate.add_argument("name")
    notate.add_argument("--text", default=None, help="omit to clear the notation")
    commands.add_parser("close", aliases=["done"], help="close a channel after you looked").add_argument("name")
    commands.add_parser("list", help="open channels and whether waiters cover them")
    waiter = commands.add_parser("wait", help="block until a channel fires (run it in the background)")
    waiter.add_argument("--channel", action="append", default=None, help="watch only this channel (repeatable)")
    hook = commands.add_parser("hook", help="hook entry: JSON on stdin, JSON on stdout")
    hook.add_argument("event")
    commands.add_parser("mcp", help="MCP server on stdio")
    arguments = parser.parse_args(argv)

    if arguments.command == "hook":
        return run_hook(arguments.event)
    if arguments.command == "mcp":
        return run_mcp()
    try:
        sid = resolve_session(arguments.session)
        now = time.time()
        if arguments.command == "open":
            result = op_open(sid, arguments.name, arguments.label,
                             parse_duration(arguments.duration) if arguments.duration else None,
                             parse_duration(arguments.every) if arguments.every else None,
                             arguments.notation, arguments.agent)
            print("opened " + describe(result["channel"], now))
            if not result["covered"]:
                print(ARM_HINT)
        elif arguments.command == "beat":
            print("beat on " + describe(op_beat(sid, arguments.name, arguments.text), now))
        elif arguments.command == "finish":
            print("finished " + describe(op_finish(sid, arguments.name, arguments.text), now))
        elif arguments.command == "note":
            print("noted on " + describe(op_note(sid, arguments.name, arguments.text), now))
        elif arguments.command == "extend":
            print("extended " + describe(op_extend(sid, arguments.name, parse_duration(arguments.duration)), now))
        elif arguments.command == "ack":
            print("acknowledged " + describe(op_ack(sid, arguments.name), now))
        elif arguments.command == "notate":
            print("notation set on " + describe(op_notate(sid, arguments.name, arguments.text), now))
        elif arguments.command in ("close", "done"):
            channel = op_close(sid, arguments.name)
            print(f"closed [{channel['name']}] {channel['label']}")
        elif arguments.command == "list":
            print(render_list(op_list(sid)))
        elif arguments.command == "wait":
            return run_wait(sid, arguments.channel)
    except HeartbeatError as error:
        print(f"heartbeat: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
