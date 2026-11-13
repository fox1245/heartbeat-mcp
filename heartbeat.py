#!/usr/bin/env python3
"""Heartbeat for a coding agent.

The agent names a piece of background work and when it expects it to be done. When that time has passed and the
agent has not said the work is done, the agent is told, in three ways:

  * a hook on tool results and user prompts adds a line to the agent's context (while the agent is working);
  * a Stop hook refuses to end the turn while a beat is overdue, or while beats are pending and no waiter runs;
  * a waiter (`heartbeat.py wait`), which the agent starts as a background command, exits at the first deadline.
    The harness re-invokes an idle agent when a background command exits, so this is what wakes an idle session.

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

VERSION = "0.1.0"


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
NAG_EVERY_SEC = _setting("HEARTBEAT_NAG_SEC", 300.0)          # an overdue beat is repeated in context this often
ARM_NAG_EVERY_SEC = _setting("HEARTBEAT_ARM_NAG_SEC", 600.0)  # "no waiter is running" is repeated this often
WAIT_POLL_SEC = _setting("HEARTBEAT_WAIT_POLL_SEC", 15.0)     # the waiter re-reads the state this often
UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0}
SELF = os.path.realpath(__file__)


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


@contextlib.contextmanager
def state(sid: str, write: bool = True):
    """The session's state as a dict, read (and written back) under an exclusive lock."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    descriptor = os.open(state_path(sid), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        raw = handle.read()
        data = json.loads(raw) if raw.strip() else {}
        data.setdefault("beats", [])
        data.setdefault("waiter", None)
        data.setdefault("last_arm_nag", 0.0)
        yield data
        if write:
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


def clock(moment: float) -> str:
    return datetime.datetime.fromtimestamp(moment).strftime("%H:%M:%S")


def describe(beat: dict, now: float) -> str:
    late = now - beat["due"]
    when = f"due {clock(beat['due'])}, {int(late // 60)} min ago" if late >= 0 else \
        f"due {clock(beat['due'])}, in {int(-late // 60) + 1} min"
    check = f"; check: {beat['check']}" if beat.get("check") else ""
    return f"[{beat['id']}] {beat['label']} ({when}){check}"


def pending(data: dict) -> list[dict]:
    return [beat for beat in data["beats"] if beat["status"] == "pending"]


def overdue(data: dict, now: float) -> list[dict]:
    return [beat for beat in pending(data) if beat["due"] <= now]


def waiter_alive(data: dict) -> bool:
    """Whether the recorded waiter is still this program waiting (a reused pid of another program does not count)."""
    waiter = data.get("waiter")
    if not waiter:
        return False
    try:
        with open(f"/proc/{waiter['pid']}/cmdline", "rb") as handle:
            arguments = handle.read().split(b"\0")
    except OSError:
        return False
    return SELF.encode() in arguments and b"wait" in arguments


# ---------------------------------------------------------------------------------------------------------------------
# Operations (shared by the command line and the MCP server)
# ---------------------------------------------------------------------------------------------------------------------

ARM_HINT = (f"Start the waiter as a background command so an idle session is woken at the deadline: "
            f"python3 -I {SELF} wait")


def op_set(sid: str, label: str, seconds: float, check: str | None) -> dict:
    if not label.strip():
        raise HeartbeatError("a beat needs a label")
    now = time.time()
    beat = {"id": secrets.token_hex(3), "label": label.strip(), "check": (check or "").strip(),
            "created": now, "due": now + seconds, "status": "pending", "last_nag": 0.0}
    with state(sid) as data:
        data["beats"].append(beat)
        alive = waiter_alive(data)
    return {"beat": beat, "waiter_running": alive}


def _find(data: dict, beat_id: str) -> dict:
    for beat in data["beats"]:
        if beat["id"] == beat_id:
            return beat
    raise HeartbeatError(f"no beat with id {beat_id!r}")


def op_done(sid: str, beat_id: str) -> dict:
    with state(sid) as data:
        beat = _find(data, beat_id)
        beat["status"] = "done"
        beat["finished"] = time.time()
        data["beats"] = [b for b in data["beats"] if b["status"] == "pending" or
                         time.time() - b.get("finished", 0) < 86400]
        return dict(beat)


def op_extend(sid: str, beat_id: str, seconds: float) -> dict:
    with state(sid) as data:
        beat = _find(data, beat_id)
        if beat["status"] != "pending":
            raise HeartbeatError(f"beat {beat_id} is not pending")
        beat["due"] = time.time() + seconds
        beat["last_nag"] = 0.0
        return dict(beat)


def op_list(sid: str) -> dict:
    with state(sid, write=False) as data:
        return {"pending": pending(data), "waiter_running": waiter_alive(data)}


def render_list(listing: dict) -> str:
    now = time.time()
    lines = [describe(beat, now) for beat in sorted(listing["pending"], key=lambda b: b["due"])]
    lines.append("waiter: running" if listing["waiter_running"] else "waiter: not running")
    return "\n".join(lines) if listing["pending"] else "no pending beats"


# ---------------------------------------------------------------------------------------------------------------------
# The waiter
# ---------------------------------------------------------------------------------------------------------------------

def run_wait(sid: str) -> int:
    with state(sid) as data:
        data["waiter"] = {"pid": os.getpid(), "started": time.time()}
    try:
        while True:
            with state(sid, write=False) as data:
                waiting = pending(data)
            now = time.time()
            if not waiting:
                print("heartbeat: no pending beats; nothing to wait for")
                return 0
            late = [beat for beat in waiting if beat["due"] <= now]
            if late:
                print("HEARTBEAT: overdue, look at each now (then `done <id>` or `extend <id> --in <time>`):")
                for beat in late:
                    print("  " + describe(beat, now))
                others = [beat for beat in waiting if beat["due"] > now]
                for beat in others:
                    print("  still pending: " + describe(beat, now))
                print("Start the waiter again afterwards if beats remain.")
                return 0
            time.sleep(max(1.0, min(WAIT_POLL_SEC, min(beat["due"] for beat in waiting) - now)))
    finally:
        with state(sid) as data:
            if data.get("waiter") and data["waiter"].get("pid") == os.getpid():
                data["waiter"] = None


# ---------------------------------------------------------------------------------------------------------------------
# The hook (reads the harness's JSON on stdin, answers with JSON; never fails the host)
# ---------------------------------------------------------------------------------------------------------------------

def hook_answer(event: str, payload: dict) -> dict:
    try:
        sid = resolve_session(payload.get("session_id"))
    except HeartbeatError:
        return {}
    if not os.path.exists(state_path(sid)):
        return {}                                     # a session that never set a beat costs one stat call
    now = time.time()
    with state(sid) as data:
        late = overdue(data, now)
        waiting = pending(data)
        alive = waiter_alive(data)
        if event == "Stop":
            if payload.get("stop_hook_active"):
                return {}                             # the turn already continued because of a Stop hook: no loop
            if late:
                listing = "\n".join("  " + describe(beat, now) for beat in late)
                return {"decision": "block",
                        "reason": "Heartbeat: work you expected to be finished is overdue. Look at each item now "
                                  "(read its output, resume or stop its agent), then mark it done or extend it:\n"
                                  f"{listing}\n(python3 -I {SELF} done <id> | extend <id> --in <time>)"}
            if waiting and not alive:
                return {"decision": "block",
                        "reason": f"Heartbeat: {len(waiting)} beat(s) are pending and no waiter is running, so "
                                  f"nothing would wake this session at the deadline. {ARM_HINT}"}
            return {}
        lines = []
        due_for_nag = [beat for beat in late if now - beat.get("last_nag", 0.0) >= NAG_EVERY_SEC]
        if due_for_nag:
            for beat in due_for_nag:
                beat["last_nag"] = now
            lines.append("HEARTBEAT overdue (check now, then `done` or `extend`): " +
                         " | ".join(describe(beat, now) for beat in late))
        if waiting and not late and not alive and now - data.get("last_arm_nag", 0.0) >= ARM_NAG_EVERY_SEC:
            data["last_arm_nag"] = now
            lines.append(f"HEARTBEAT: {len(waiting)} beat(s) pending and no waiter is running. {ARM_HINT}")
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

TOOLS = [
    {"name": "heartbeat_set",
     "description": "Register background work and when you expect it to be finished. After that time, hooks tell "
                    "you it is overdue and refuse to end the turn until you looked. Returns the beat id. Also start "
                    "the waiter in the background (the result says how) so an idle session is woken at the deadline.",
     "inputSchema": {"type": "object", "required": ["label", "minutes"], "properties": {
         "label": {"type": "string", "description": "What is running, in a few words."},
         "minutes": {"type": "number", "description": "Expected time until it is finished, in minutes."},
         "check": {"type": "string", "description": "How to check it when the time has passed (a path, an id)."}}}},
    {"name": "heartbeat_done",
     "description": "Mark a beat as finished (after you looked at the work's result).",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}},
    {"name": "heartbeat_extend",
     "description": "Move a pending beat's deadline to now + minutes (after you looked and it is still running).",
     "inputSchema": {"type": "object", "required": ["id", "minutes"], "properties": {
         "id": {"type": "string"}, "minutes": {"type": "number"}}}},
    {"name": "heartbeat_list",
     "description": "List pending beats with their deadlines, and whether the waiter is running.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def call_tool(name: str, arguments: dict) -> str:
    sid = resolve_session()
    if name == "heartbeat_set":
        result = op_set(sid, str(arguments["label"]), float(arguments["minutes"]) * 60.0, arguments.get("check"))
        beat = result["beat"]
        text = f"beat {beat['id']} set: {beat['label']}, due {clock(beat['due'])}."
        return text if result["waiter_running"] else f"{text} {ARM_HINT}"
    if name == "heartbeat_done":
        beat = op_done(sid, str(arguments["id"]))
        return f"beat {beat['id']} done: {beat['label']}"
    if name == "heartbeat_extend":
        beat = op_extend(sid, str(arguments["id"]), float(arguments["minutes"]) * 60.0)
        return f"beat {beat['id']} extended: {beat['label']}, due {clock(beat['due'])}"
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
            "instructions": "When you start background work and will wait for it (a subagent, a gate, a long "
                            "command), call heartbeat_set with the time you expect it to take, and start the waiter "
                            "it names as a background command. When the beat is reported overdue, look at the work "
                            "itself (its output file, its process), then heartbeat_done or heartbeat_extend."}}
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
    add = commands.add_parser("set", help="register work and when it should be finished")
    add.add_argument("--in", dest="duration", required=True, help="90s, 40m, 2h, or minutes")
    add.add_argument("--label", required=True)
    add.add_argument("--check", default="", help="how to check it when the time has passed")
    done = commands.add_parser("done", help="mark a beat as finished")
    done.add_argument("id")
    extend = commands.add_parser("extend", help="move a pending beat's deadline")
    extend.add_argument("id")
    extend.add_argument("--in", dest="duration", required=True)
    commands.add_parser("list", help="pending beats and the waiter")
    commands.add_parser("wait", help="block until the first deadline (run it in the background)")
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
        if arguments.command == "set":
            result = op_set(sid, arguments.label, parse_duration(arguments.duration), arguments.check)
            beat = result["beat"]
            print(f"beat {beat['id']} set: {beat['label']}, due {clock(beat['due'])}")
            if not result["waiter_running"]:
                print(ARM_HINT)
        elif arguments.command == "done":
            beat = op_done(sid, arguments.id)
            print(f"beat {beat['id']} done: {beat['label']}")
        elif arguments.command == "extend":
            beat = op_extend(sid, arguments.id, parse_duration(arguments.duration))
            print(f"beat {beat['id']} extended: {beat['label']}, due {clock(beat['due'])}")
        elif arguments.command == "list":
            print(render_list(op_list(sid)))
        elif arguments.command == "wait":
            return run_wait(sid)
    except HeartbeatError as error:
        print(f"heartbeat: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
