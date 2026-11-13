#!/usr/bin/env python3
"""Self-test of heartbeat.py: runs the real program as subprocesses against a temporary state directory.
Usage: python3 -I selftest.py   (prints one line per check and `bad=<n>` at the end; exit 0 only when bad=0)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.realpath(__file__))
PROGRAM = os.path.join(HERE, "heartbeat.py")
bad = 0


def check(name, condition, detail=""):
    global bad
    print(("ok   " if condition else "FAIL ") + name + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        bad += 1


def run(arguments, env, stdin=None):
    done = subprocess.run([sys.executable, "-I", PROGRAM, *arguments], input=stdin, env=env,
                          capture_output=True, text=True, timeout=30)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def hook(event, env, session, **extra):
    code, out, _ = run(["hook", event], env, stdin=json.dumps({"session_id": session, **extra}))
    return code, json.loads(out)


with tempfile.TemporaryDirectory() as folder:
    session = str(uuid.uuid4())
    other = str(uuid.uuid4())
    env = {"PATH": os.environ.get("PATH", ""), "HOME": folder, "HEARTBEAT_STATE_DIR": os.path.join(folder, "state"),
           "CLAUDE_CODE_SESSION_ID": session, "HEARTBEAT_WAIT_POLL_SEC": "1", "HEARTBEAT_NAG_SEC": "2",
           "HEARTBEAT_ARM_NAG_SEC": "2"}

    # A session with no state: every hook answers {} and creates nothing.
    code, answer = hook("PostToolUse", env, session)
    check("hook without state answers {}", code == 0 and answer == {})
    check("hook without state creates no file", not os.path.exists(os.path.join(folder, "state", f"{session}.json")))

    # Caller errors.
    code, _, err = run(["set", "--in", "soon", "--label", "x"], env)
    check("a bad duration is refused", code == 2 and "not a duration" in err, err)
    code, _, err = run(["--session", "not-a-uuid", "list"], env)
    check("a session id that is not a UUID is refused", code == 2 and "UUID" in err, err)
    code, _, err = run(["done", "nope"], env)
    check("an unknown beat id is refused", code == 2 and "no beat" in err, err)

    # A pending beat, not overdue.
    code, out, _ = run(["set", "--in", "3s", "--label", "first job", "--check", "look at /tmp/x"], env)
    beat_id = out.split()[1]
    check("set prints the id and how to arm the waiter", code == 0 and "wait" in out, out)
    code, answer = hook("Stop", env, session)
    check("Stop is blocked while a beat is pending and no waiter runs",
          answer.get("decision") == "block" and "no waiter" in answer.get("reason", ""), str(answer))
    code, answer = hook("Stop", env, session, stop_hook_active=True)
    check("Stop is not blocked a second time in one continuation", answer == {}, str(answer))
    code, answer = hook("PostToolUse", env, session)
    context = answer.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("a tool result reminds that no waiter runs", "no waiter" in context, str(answer))
    check("the hook names its event", answer.get("hookSpecificOutput", {}).get("hookEventName") == "PostToolUse")
    code, answer = hook("PostToolUse", env, session)
    check("the waiter reminder is rate-limited", answer == {}, str(answer))
    code, answer = hook("PostToolUse", env, other)
    check("another session sees nothing", answer == {}, str(answer))

    # A recorded waiter that is not this program waiting does not count: a live pid of another program, a dead pid.
    state_file = os.path.join(folder, "state", f"{session}.json")
    for label, pid in (("another program's pid", os.getpid()), ("a dead pid", 2 ** 22 + 12345)):
        with open(state_file) as handle:
            stored = json.load(handle)
        stored["waiter"] = {"pid": pid, "started": time.time()}
        with open(state_file, "w") as handle:
            json.dump(stored, handle)
        code, answer = hook("Stop", env, session)
        check(f"a recorded waiter that is {label} does not count as running",
              answer.get("decision") == "block" and "no waiter" in answer.get("reason", ""), str(answer))
        code, out, _ = run(["list"], env)
        check(f"list shows the waiter as not running for {label}", "waiter: not running" in out, out)

    # The waiter: alive while pending, exits at the deadline and names the beat.
    waiter = subprocess.Popen([sys.executable, "-I", PROGRAM, "wait"], env=env, stdout=subprocess.PIPE, text=True)
    time.sleep(0.6)
    code, answer = hook("Stop", env, session)
    check("Stop passes while the waiter runs and nothing is overdue", answer == {}, str(answer))
    code, out, _ = run(["list"], env)
    check("list shows the beat and the running waiter", "first job" in out and "waiter: running" in out, out)
    started = time.time()
    output, _ = waiter.communicate(timeout=20)
    check("the waiter exits at the deadline", waiter.returncode == 0 and time.time() - started < 8)
    check("the waiter names the overdue beat and its check", "first job" in output and "look at /tmp/x" in output,
          output)

    # Overdue.
    code, answer = hook("Stop", env, session)
    check("Stop is blocked while a beat is overdue",
          answer.get("decision") == "block" and "overdue" in answer.get("reason", "") and beat_id in answer["reason"],
          str(answer))
    code, answer = hook("UserPromptSubmit", env, session)
    context = answer.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("a prompt carries the overdue beat", "overdue" in context and "first job" in context, str(answer))
    code, answer = hook("PostToolUse", env, session)
    check("the overdue reminder is rate-limited", answer == {}, str(answer))
    time.sleep(2.2)
    code, answer = hook("PostToolUse", env, session)
    check("the overdue reminder repeats after the interval",
          "overdue" in answer.get("hookSpecificOutput", {}).get("additionalContext", ""), str(answer))
    code, out, _ = run(["wait"], env)
    check("a waiter started while a beat is overdue returns at once", code == 0 and "overdue" in out, out)

    # Extend, then done.
    code, out, _ = run(["extend", beat_id, "--in", "1h"], env)
    check("extend moves the deadline", code == 0 and "extended" in out, out)
    code, answer = hook("UserPromptSubmit", env, session)
    check("an extended beat is not overdue", "overdue" not in json.dumps(answer), str(answer))
    code, out, _ = run(["done", beat_id], env)
    check("done finishes the beat", code == 0 and "done" in out, out)
    code, answer = hook("Stop", env, session)
    check("Stop passes with no pending beat", answer == {}, str(answer))
    code, out, _ = run(["wait"], env)
    check("the waiter has nothing to wait for", code == 0 and "no pending beats" in out, out)

    # A hook never fails the host: bad input.
    done = subprocess.run([sys.executable, "-I", PROGRAM, "hook", "Stop"], input="not json", env=env,
                          capture_output=True, text=True)
    check("bad hook input answers {}", done.returncode == 0 and json.loads(done.stdout) == {})

    # The MCP server.
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "heartbeat_set", "arguments": {"label": "mcp job", "minutes": 30, "check": "agent x"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "heartbeat_list", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "heartbeat_done", "arguments": {"id": "zz"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "nope"},
    ]
    code, out, _ = run(["mcp"], env, stdin="\n".join(json.dumps(r) for r in requests) + "\n")
    replies = {reply["id"]: reply for reply in map(json.loads, out.splitlines())}
    check("the server answers every request and no notification", sorted(replies) == [1, 2, 3, 4, 5, 6], out)
    check("initialize echoes the protocol version and declares tools",
          replies[1]["result"]["protocolVersion"] == "2025-06-18" and "tools" in replies[1]["result"]["capabilities"])
    check("four tools are listed", [t["name"] for t in replies[2]["result"]["tools"]] ==
          ["heartbeat_set", "heartbeat_done", "heartbeat_extend", "heartbeat_list"])
    check("heartbeat_set sets a beat and says how to arm the waiter",
          not replies[3]["result"]["isError"] and "wait" in replies[3]["result"]["content"][0]["text"])
    check("heartbeat_list shows it", "mcp job" in replies[4]["result"]["content"][0]["text"])
    check("a tool error is a tool result, not a protocol error", replies[5]["result"]["isError"] is True)
    check("an unknown method is a protocol error", replies[6]["error"]["code"] == -32601)

    # The session is found through an ancestor's session file when the environment does not name it.
    bare = dict(env)
    del bare["CLAUDE_CODE_SESSION_ID"]
    os.makedirs(os.path.join(folder, ".claude", "sessions"))
    with open(os.path.join(folder, ".claude", "sessions", f"{os.getpid()}.json"), "w") as handle:
        json.dump({"sessionId": session}, handle)
    code, out, _ = run(["list"], bare)
    check("the session is found from the ancestor's session file", code == 0 and "mcp job" in out, out)

print(f"bad={bad}")
sys.exit(0 if bad == 0 else 1)
