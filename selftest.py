#!/usr/bin/env python3
"""Self-test of heartbeat.py: runs the real program as subprocesses against a temporary state directory.
Usage: python3 -I selftest.py   (prints one line per check and `bad=<n>` at the end; exit 0 only when bad=0)."""
import fcntl
import importlib.util
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.realpath(__file__))
PROGRAM = os.path.join(HERE, "heartbeat.py")
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("heartbeat_under_test", PROGRAM)
program = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(program)           # only for building state files; every operation runs as a subprocess
bad = 0


def check(name, condition, detail=""):
    global bad
    print(("ok   " if condition else "FAIL ") + name + (f"  [{str(detail)[:300]}]" if not condition else ""))
    if not condition:
        bad += 1


def run(arguments, env, stdin=None):
    done = subprocess.run([sys.executable, "-I", PROGRAM, *arguments], input=stdin, env=env,
                          capture_output=True, text=True, timeout=30)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def hook(event, env, session, **extra):
    code, out, _ = run(["hook", event], env, stdin=json.dumps({"session_id": session, **extra}))
    return json.loads(out)


def context(answer):
    return answer.get("hookSpecificOutput", {}).get("additionalContext", "")


def start_waiter(env, *arguments):
    process = subprocess.Popen([sys.executable, "-I", PROGRAM, "wait", *arguments], env=env,
                               stdout=subprocess.PIPE, text=True)
    time.sleep(0.6)
    return process


def read_state(path):
    """The state file as written; read under the program's own lock, so a rewrite in progress is never seen half done."""
    with open(path) as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        return json.load(handle)


def edit_state(path, change):
    with open(path, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        stored = json.load(handle)
        change(stored)
        handle.seek(0)
        handle.truncate()
        json.dump(stored, handle)


def proc_stat(pid):
    """(state letter, start time in clock ticks) of a process, read from the fields after the last ')'."""
    with open(f"/proc/{pid}/stat", "rb") as handle:
        raw = handle.read()
    rest = raw[raw.rindex(b")") + 1:].split()
    return rest[0].decode(), int(rest[19])


def spawn(env, *arguments, **options):
    return subprocess.Popen([options.pop("python", sys.executable), "-I", PROGRAM, *arguments], env=env,
                            stdout=subprocess.PIPE, **options)


def registered(process, path, timeout=10):
    """Waits until the process has recorded itself as a waiter (or has exited); True when it is recorded."""
    end = time.time() + timeout
    while time.time() < end and process.poll() is None:
        if any(waiter.get("pid") == process.pid for waiter in read_state(path)["waiters"]):
            return True
        time.sleep(0.02)
    return False


def end(*processes):
    for process in processes:
        if process.poll() is None:
            try:
                os.kill(process.pid, signal.SIGCONT)
            except OSError:
                pass
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()


def blocked(answer):
    return answer.get("decision") == "block"


with tempfile.TemporaryDirectory() as folder:
    session = str(uuid.uuid4())
    other = str(uuid.uuid4())
    env = {"PATH": os.environ.get("PATH", ""), "HOME": folder, "HEARTBEAT_STATE_DIR": os.path.join(folder, "state"),
           "CLAUDE_CODE_SESSION_ID": session, "HEARTBEAT_WAIT_POLL_SEC": "1", "HEARTBEAT_NAG_SEC": "2",
           "HEARTBEAT_ARM_NAG_SEC": "2"}
    state_file = os.path.join(folder, "state", f"{session}.json")

    # A session with no state: every hook answers {} and creates nothing.
    check("hook without state answers {}", hook("PostToolUse", env, session) == {})
    check("hook without state creates no file", not os.path.exists(state_file))

    # Caller errors.
    code, _, err = run(["open", "a", "--label", "x", "--in", "soon"], env)
    check("a bad duration is refused", code == 2 and "not a duration" in err, err)
    code, _, err = run(["--session", "not-a-uuid", "list"], env)
    check("a session id that is not a UUID is refused", code == 2 and "UUID" in err, err)
    code, _, err = run(["close", "nope"], env)
    check("an unknown channel is refused", code == 2 and "no open channel" in err, err)
    code, _, err = run(["open", "two words", "--label", "x", "--in", "1m"], env)
    check("a channel name with a space is refused", code == 2 and "channel name" in err, err)
    code, _, err = run(["open", "a", "--label", "x"], env)
    check("a channel without a deadline or an interval is refused", code == 2 and "deadline" in err, err)

    # Two channels: A is due soon and has a notation, B is far away and has none (the notation is optional).
    code, out, _ = run(["open", "A", "--label", "first job", "--in", "3s", "--notation", "read /tmp/x, then merge"], env)
    check("open prints the channel, its notation and how to start a waiter",
          code == 0 and "[A] first job" in out and "to do: read /tmp/x, then merge" in out and "wait" in out, out)
    code, out, _ = run(["open", "B", "--label", "second job", "--in", "1h"], env)
    check("a channel without a notation shows none", code == 0 and "to do:" not in out, out)
    code, _, err = run(["open", "A", "--label", "again", "--in", "1m"], env)
    check("an open channel's name cannot be opened again", code == 2 and "already open" in err, err)
    answer = hook("Stop", env, session)
    check("Stop is blocked while open channels are not watched",
          answer.get("decision") == "block" and "not watched" in answer.get("reason", "") and
          "A" in answer["reason"] and "B" in answer["reason"], answer)
    check("Stop is not blocked a second time in one continuation",
          hook("Stop", env, session, stop_hook_active=True) == {})
    answer = hook("PostToolUse", env, session)
    check("a tool result reminds that channels are not watched", "not watched" in context(answer), answer)
    check("the hook names its event", answer.get("hookSpecificOutput", {}).get("hookEventName") == "PostToolUse")
    check("the reminder is rate-limited", hook("PostToolUse", env, session) == {})
    check("another session sees nothing", hook("PostToolUse", env, other) == {})

    # A recorded waiter that is not this program waiting does not count: a live pid of another program, a dead pid.
    for label, pid in (("another program's pid", os.getpid()), ("a dead pid", 2 ** 22 + 12345)):
        with open(state_file) as handle:
            stored = json.load(handle)
        stored["waiters"] = [{"pid": pid, "started": time.time(), "channels": None}]
        with open(state_file, "w") as handle:
            json.dump(stored, handle)
        answer = hook("Stop", env, session)
        check(f"a recorded waiter that is {label} does not count",
              answer.get("decision") == "block" and "not watched" in answer.get("reason", ""), answer)
        code, out, _ = run(["list"], env)
        check(f"list shows the channels as not covered for {label}", "NOT covered" in out, out)

    # A waiter for named channels covers only those; a waiter without names covers all.
    partial = start_waiter(env, "--channel", "B")
    answer = hook("Stop", env, session)
    check("a waiter for B alone leaves A unwatched",
          answer.get("decision") == "block" and "(A)" in answer.get("reason", ""), answer)
    code, _, err = run(["wait", "--channel", "nope"], env)
    check("a waiter for an unknown channel is refused", code == 2 and "no open channel" in err, err)
    waiter = start_waiter(env)
    check("Stop passes while a waiter watches every channel and nothing fired", hook("Stop", env, session) == {})
    code, out, _ = run(["list"], env)
    check("list shows both channels as covered",
          "first job" in out and "second job" in out and "cover every open channel" in out, out)
    started = time.time()
    output, _ = waiter.communicate(timeout=20)
    check("the waiter exits when A's deadline passes", waiter.returncode == 0 and time.time() - started < 8)
    check("the waiter names the fired channel with its notation, and the quiet one",
          "[A] first job" in output and "OVERDUE" in output and "to do: read /tmp/x, then merge" in output and
          "quiet: 1 channel(s) still open: B" in output, output)
    check("the waiter for B alone is still waiting", partial.poll() is None)

    # Overdue.
    answer = hook("Stop", env, session)
    check("Stop is blocked while a channel fired, with the notation",
          answer.get("decision") == "block" and "[A]" in answer.get("reason", "") and
          "to do: read /tmp/x" in answer["reason"], answer)
    answer = hook("UserPromptSubmit", env, session)
    check("a prompt carries the fired channel", "OVERDUE" in context(answer) and "first job" in context(answer), answer)
    check("the fired reminder is rate-limited", hook("PostToolUse", env, session) == {})
    time.sleep(2.2)
    check("the fired reminder repeats after the interval", "OVERDUE" in context(hook("PostToolUse", env, session)))
    code, out, _ = run(["wait"], env)
    check("a waiter started while a channel fired returns at once", code == 0 and "[A]" in out, out)
    code, out, _ = run(["extend", "A", "--in", "1h"], env)
    check("extend gives a new deadline", code == 0 and "extended" in out and "OVERDUE" not in out, out)
    check("an extended channel does not fire", "OVERDUE" not in json.dumps(hook("UserPromptSubmit", env, session)))

    # The thing waited for speaks: a note fires the channel until it is acknowledged.
    code, out, _ = run(["note", "B", "--text", "needs a decision"], env)
    output, _ = partial.communicate(timeout=20)
    check("a note wakes the waiter of that channel",
          partial.returncode == 0 and "[B]" in output and "NOTE" in output and "needs a decision" in output, output)
    check("a noted channel blocks Stop", hook("Stop", env, session).get("decision") == "block")
    code, out, _ = run(["ack", "B"], env)
    check("ack clears the notes and keeps the channel open", code == 0 and "NOTE" not in out and "due" in out, out)

    # ... and finish fires it until it is closed.
    waiter = start_waiter(env)
    code, out, _ = run(["finish", "B", "--text", "VERIFY: PASS"], env)
    output, _ = waiter.communicate(timeout=20)
    check("finish wakes the waiter with the text", "FINISHED: VERIFY: PASS" in output and "[B]" in output, output)
    check("a finished channel is not also overdue", "OVERDUE" not in output.split("quiet:")[0], output)
    code, out, _ = run(["close", "B"], env)
    check("close ends the channel", code == 0 and "closed [B]" in out, out)
    code, out, _ = run(["list"], env)
    check("a closed channel is not listed", "second job" not in out and "first job" in out, out)
    code, out, _ = run(["open", "B", "--label", "second job, again", "--in", "1h"], env)
    check("a closed channel's name can be opened again", code == 0, out)

    # A liveness interval: every beat moves the deadline; silence longer than the interval fires.
    run(["open", "L", "--label", "alive job", "--every", "2s"], env)
    time.sleep(1.2)
    code, out, _ = run(["beat", "L", "--text", "step 3"], env)
    check("a beat is recorded with its text", code == 0 and "last sign of life" in out and "(1 so far): step 3" in out, out)
    code, out, _ = run(["beat", "L"], env)
    check("a beat without text clears the previous text", "(2 so far)" in out and "step 3" not in out, out)
    time.sleep(1.2)
    check("a beat moved the deadline", "[L]" not in json.dumps(hook("Stop", env, session)))
    time.sleep(2.2)
    answer = hook("Stop", env, session)
    check("silence longer than the interval fires", "[L]" in answer.get("reason", "") and "OVERDUE" in answer["reason"],
          answer)
    run(["close", "L"], env)

    # A channel bound to a subagent: that agent's tool calls are signs of life, and the agent gets no reminders.
    run(["open", "G", "--label", "subagent", "--every", "2s", "--agent", "agent-1"], env)
    time.sleep(1.3)
    check("a subagent's tool call gets no reminder", hook("PostToolUse", env, session, agent_id="agent-1") == {})
    time.sleep(1.3)
    code, out, _ = run(["list"], env)
    check("the bound agent's tool call moved the deadline",
          "[G] subagent" in out and "(1 so far)" in out and "OVERDUE" not in out.split("[G]")[1].split("\n")[0], out)
    check("another subagent's tool call is not a sign of life",
          hook("PostToolUse", env, session, agent_id="agent-2") == {} and
          "(1 so far)" in run(["list"], env)[1])
    time.sleep(2.2)
    check("a fired channel is not reported into a subagent's context",
          hook("PostToolUse", env, session, agent_id="agent-2") == {})
    check("a silent bound agent fires its channel", "[G]" in hook("Stop", env, session).get("reason", ""))
    run(["beat", "G", "--text", "from outside"], env)
    hook("PostToolUse", env, session, agent_id="agent-1")
    check("a bound agent's tool call clears the text of an earlier beat",
          "from outside" not in run(["list"], env)[1], run(["list"], env)[1])
    run(["close", "G"], env)

    # The notation can be set and cleared later.
    code, out, _ = run(["notate", "A", "--text", "rebase, gate, push"], env)
    check("notate sets the notation", "to do: rebase, gate, push" in out, out)
    code, out, _ = run(["notate", "A"], env)
    check("notate without text clears it", "to do:" not in out, out)

    # Many channels at once.
    for index in range(40):
        run(["open", f"c{index}", "--label", f"job {index}", "--in", "1h"], env)
    code, out, _ = run(["list"], env)
    check("forty more channels are open at once", all(f"[c{index}]" in out for index in range(40)), out[:200])
    waiter = start_waiter(env)
    check("one waiter covers all of them", hook("Stop", env, session) == {})
    run(["finish", "c17"], env)
    output, _ = waiter.communicate(timeout=20)
    check("the waiter names exactly the channel that fired",
          "1 channel(s) fired" in output and "[c17]" in output.split("quiet:")[0], output[:300])
    for index in range(40):
        run(["close", f"c{index}"], env)
    for name in ("A", "B"):
        run(["close", name], env)
    check("Stop passes with no open channel", hook("Stop", env, session) == {})
    code, out, _ = run(["wait"], env)
    check("a waiter has nothing to wait for", code == 0 and "no open channel" in out, out)

    # A deadline that cannot be shown is refused before anything is stored (it would break every later listing).
    for bad_time in ("nan", "inf", "1e300h"):
        code, _, err = run(["open", "t", "--label", "x", "--in", bad_time], env)
        check(f"a deadline of {bad_time} is refused", code == 2 and "not a time that can be represented" in err, err)
    code, out, _ = run(["list"], env)
    check("a refused deadline stored nothing", code == 0 and out == "no open channels", out)
    run(["open", "t", "--label", "x", "--in", "1h"], env)
    code, _, err = run(["extend", "t", "--in", "nan"], env)
    check("extend refuses a deadline that cannot be shown", code == 2 and "represented" in err, err)
    code, out, _ = run(["list"], env)
    check("the channel is intact after the refused extend", code == 0 and "[t] x" in out, out)
    run(["close", "t"], env)

    # A liveness interval below a minute is shown in seconds, not as "0 min".
    code, out, _ = run(["open", "s30", "--label", "short", "--every", "30s"], env)
    check("an interval of 30 seconds is shown as 30 s", "every 30 s" in out and "every 0 min" not in out, out)
    run(["close", "s30"], env)

    # The waiter is recognised however it was started: a relative path, a symbolic link, a path with a space.
    run(["open", "R", "--label", "relative", "--in", "1h"], env)
    started = []
    link = os.path.join(folder, "hb-link.py")
    os.symlink(PROGRAM, link)
    for label, command, directory in (("a relative path", [sys.executable, "-I", "heartbeat.py", "wait"], HERE),
                                      ("a symbolic link", [sys.executable, "-I", link, "wait"], folder)):
        process = subprocess.Popen(command, env=env, cwd=directory, stdout=subprocess.PIPE, text=True)
        started.append(process)
        time.sleep(0.6)
        check(f"a waiter started by {label} is recognised", hook("Stop", env, session) == {})
        process.terminate()
        process.communicate(timeout=10)
        check(f"a waiter that ended is not counted ({label})", hook("Stop", env, session).get("decision") == "block")
    run(["close", "R"], env)

    spaced = os.path.join(folder, "with space")
    os.makedirs(spaced)
    copy = os.path.join(spaced, "heartbeat.py")
    with open(PROGRAM) as source, open(copy, "w") as target:
        target.write(source.read())
    done = subprocess.run([sys.executable, "-I", copy, "open", "Q", "--label", "x", "--in", "1h"], env=env,
                          capture_output=True, text=True, timeout=30)
    check("the hint quotes a program path that contains a space",
          f"python3 -I '{os.path.realpath(copy)}' wait" in done.stdout, done.stdout)
    run(["close", "Q"], env)

    # State written by version 0.1 is carried over.
    with open(state_file, "w") as handle:
        json.dump({"beats": [{"id": "abc123", "label": "old beat", "check": "old check", "created": time.time(),
                              "due": time.time() + 600, "status": "pending", "last_nag": 0.0},
                             {"id": "def456", "label": "old done", "check": "", "created": 0, "due": 0,
                              "status": "done"}],
                   "waiter": None, "last_arm_nag": 0.0}, handle)
    code, out, _ = run(["list"], env)
    check("a pending beat of the old format becomes a channel with its check as the notation",
          "[abc123] old beat" in out and "to do: old check" in out and "old done" not in out, out)
    run(["close", "abc123"], env)

    # A hook never fails the host: bad input.
    done = subprocess.run([sys.executable, "-I", PROGRAM, "hook", "Stop"], input="not json", env=env,
                          capture_output=True, text=True)
    check("bad hook input answers {}", done.returncode == 0 and json.loads(done.stdout) == {})

    # The MCP server.
    def call(identifier, tool, **arguments):
        return {"jsonrpc": "2.0", "id": identifier, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        call(3, "heartbeat_open", name="m1", label="mcp job", minutes=30, notation="check agent x"),
        call(4, "heartbeat_open", label="unnamed, by interval", every_minutes=10, notation=None),
        call(5, "heartbeat_note", name="m1", text="look"),
        call(6, "heartbeat_list"),
        call(7, "heartbeat_ack", name="m1"),
        call(8, "heartbeat_finish", name="m1", text="done ok"),
        call(9, "heartbeat_extend", name="m1", minutes=5),
        call(10, "heartbeat_notate", name="m1", notation=None),
        call(11, "heartbeat_close", name="m1"),
        call(12, "heartbeat_close", name="zz"),
        call(13, "heartbeat_open", label="no time"),
        {"jsonrpc": "2.0", "id": 14, "method": "nope"},
        call(15, "heartbeat_note", name="zz"),
        call(16, "heartbeat_beat"),
        call(17, "heartbeat_open", label="nan", minutes=float("nan")),
        {"jsonrpc": "2.0", "id": 18, "method": "tools/call", "params": {"name": "heartbeat_nope", "arguments": {}}},
    ]
    code, out, _ = run(["mcp"], env, stdin="\n".join(json.dumps(r) for r in requests) + "\n")
    replies = {reply["id"]: reply for reply in map(json.loads, out.splitlines())}
    text = {key: reply["result"]["content"][0]["text"] for key, reply in replies.items()
            if "result" in reply and "content" in reply["result"]}
    check("the server answers every request and no notification", sorted(replies) == list(range(1, 19)), out[:200])
    check("initialize echoes the protocol version and declares tools",
          replies[1]["result"]["protocolVersion"] == "2025-06-18" and "tools" in replies[1]["result"]["capabilities"])
    check("nine tools are listed", [tool["name"] for tool in replies[2]["result"]["tools"]] ==
          ["heartbeat_open", "heartbeat_beat", "heartbeat_note", "heartbeat_finish", "heartbeat_extend",
           "heartbeat_ack", "heartbeat_notate", "heartbeat_close", "heartbeat_list"])
    check("heartbeat_open opens a channel with its notation and says how to start a waiter",
          "[m1] mcp job" in text[3] and "to do: check agent x" in text[3] and "wait" in text[3], text[3])
    check("heartbeat_open takes an interval, no name and a null notation",
          not replies[4]["result"]["isError"] and "to do:" not in text[4], text[4])
    check("heartbeat_list shows the note", "[m1]" in text[6] and "NOTE" in text[6] and "look" in text[6], text[6])
    check("heartbeat_ack clears it", "NOTE" not in text[7], text[7])
    check("heartbeat_finish reports the text", "FINISHED: done ok" in text[8], text[8])
    check("heartbeat_extend reopens the channel", "FINISHED" not in text[9] and "due" in text[9], text[9])
    check("heartbeat_notate with null clears the notation", "to do:" not in text[10], text[10])
    check("heartbeat_close closes it", "closed [m1]" in text[11], text[11])
    check("a tool error is a tool result, not a protocol error", replies[12]["result"]["isError"] is True)
    check("a channel without any time is a tool error", replies[13]["result"]["isError"] is True, text[13])
    check("an unknown method is a protocol error", replies[14]["error"]["code"] == -32601)
    check("a missing argument is named", replies[15]["result"]["isError"] and "missing argument 'text'" in text[15] and
          "missing argument 'name'" in text[16], (text[15], text[16]))
    check("a deadline that cannot be shown is a tool error", replies[17]["result"]["isError"] and
          "represented" in text[17], text[17])
    check("an unknown tool is named as such", replies[18]["result"]["isError"] and "unknown tool" in text[18], text[18])

    # A recorded waiter counts only if it is the process that recorded itself, running, and waiting.
    def new_session(**extra):
        sid = str(uuid.uuid4())
        return sid, dict(env, CLAUDE_CODE_SESSION_ID=sid, **extra), os.path.join(folder, "state", f"{sid}.json")

    sid, penv, pfile = new_session(HEARTBEAT_WAIT_POLL_SEC="30")
    run(["open", "B", "--label", "long job", "--in", "1h"], penv)
    first = spawn(penv, "wait")
    try:
        up = registered(first, pfile)
        check("a waiter records itself", up)
        record = next((w for w in read_state(pfile)["waiters"] if w.get("pid") == first.pid), {}) if up else {}
        check("a waiter records its process start time, as read from /proc",
              record.get("start_ticks") == proc_stat(first.pid)[1], record)
        check("Stop passes while that waiter runs", hook("Stop", penv, sid) == {})
        edit_state(pfile, lambda stored: stored["waiters"][0].update(start_ticks=record.get("start_ticks", 0) + 1))
        answer = hook("Stop", penv, sid)
        check("a recorded pid whose process started at another time does not count",
              blocked(answer) and "not watched" in answer.get("reason", ""), answer)
        run(["notate", "B", "--text", "x"], penv)
        check("a waiter that does not count is dropped when the state is next written",
              not read_state(pfile)["waiters"], read_state(pfile)["waiters"])
    finally:
        end(first)

    # A stopped process does not wake anything.
    second = spawn(penv, "wait")
    try:
        registered(second, pfile)
        check("a second waiter is counted", hook("Stop", penv, sid) == {})
        os.kill(second.pid, signal.SIGSTOP)
        stopped_at = time.time()
        while proc_stat(second.pid)[0] not in "Tt" and time.time() - stopped_at < 10:
            time.sleep(0.01)
        check("the waiter is stopped", proc_stat(second.pid)[0] in "Tt", proc_stat(second.pid))
        answer = hook("Stop", penv, sid)
        check("a stopped waiter does not count", blocked(answer) and "not watched" in answer.get("reason", ""), answer)
    finally:
        end(second)

    # "wait" must be the subcommand, not any argument: `hook wait` stays alive reading its input.
    impostor = spawn(penv, "hook", "wait", stdin=subprocess.PIPE)
    try:
        edit_state(pfile, lambda stored: stored["waiters"].append(
            {"pid": impostor.pid, "started": time.time(), "start_ticks": proc_stat(impostor.pid)[1],
             "channels": None}))
        answer = hook("Stop", penv, sid)
        check("a process with the word wait as another command's argument does not count",
              blocked(answer) and "not watched" in answer.get("reason", ""), answer)
    finally:
        end(impostor)

    # The subcommand is found after the global option, in every spelling argparse accepts.
    for label, front in (("--session ID", ["--session", sid]), ("--session=ID", [f"--session={sid}"]),
                         ("an abbreviated --sess ID", ["--sess", sid])):
        process = spawn(penv, *front, "wait")
        try:
            up = registered(process, pfile)
            check(f"a waiter started with {label} is recognised", up and hook("Stop", penv, sid) == {}, up)
        finally:
            end(process)

    # A command name with spaces and parentheses does not confuse the process fields.
    odd = os.path.join(folder, "a b) 3 (4 5")
    os.symlink(sys.executable, odd)
    process = spawn(penv, "wait", python=odd)
    try:
        up = registered(process, pfile)
        record = next((w for w in read_state(pfile)["waiters"] if w.get("pid") == process.pid), {}) if up else {}
        check("a waiter whose command name holds spaces and a parenthesis records its start time",
              record.get("start_ticks") == proc_stat(process.pid)[1], (record, open(f"/proc/{process.pid}/stat").read()))
        check("and is recognised", hook("Stop", penv, sid) == {})
    finally:
        end(process)
    run(["close", "B"], penv)

    # A waiter that decided to exit unregisters itself before it prints, and what it prints is bounded.
    def crowd(path, fired_count, quiet_count):
        """A state file with channels that have fired (finished) and channels that are quiet, long labels."""
        stored = {"channels": {}, "waiters": [], "last_arm_nag": 0.0}
        for index in range(fired_count + quiet_count):
            name = f"{'f' if index < fired_count else 'q'}{index}"
            channel = program.new_channel(name, "x" * 250, None, time.time() + 3600, None, None)
            if index < fired_count:
                channel["status"] = program.FINISHED
            stored["channels"][name] = channel
        with open(path, "w") as handle:
            json.dump(stored, handle)

    sid, penv, pfile = new_session()
    os.makedirs(os.path.dirname(pfile), exist_ok=True)
    crowd(pfile, 500, 0)                     # about 150 KB of output, far above a pipe's 64 KiB and the 8 KiB buffer
    process = spawn(penv, "wait")
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10)
        first_byte = os.read(process.stdout.fileno(), 1) if ready else b""
        check("a waiter with a lot to say starts to print", first_byte != b"")
        check("a waiter is no longer registered once it prints",
              not any(w.get("pid") == process.pid for w in read_state(pfile)["waiters"]),
              read_state(pfile)["waiters"])
    finally:
        end(process)

    sid, penv, pfile = new_session()
    crowd(pfile, 1, 600)                     # one fired channel and 600 quiet ones, nobody reads the pipe
    process = spawn(penv, "wait")
    try:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        check("a waiter with many quiet channels exits without anyone reading its output", process.poll() == 0,
              process.poll())
        process.kill()
        output = process.communicate()[0].decode()
        quiet_line = next((line for line in output.splitlines() if line.strip().startswith("quiet:")), "")
        check("the fired channel is printed in full", "[f0] " + "x" * 250 in output, output[:300])
        check("the quiet channels are a count and a few names",
              "600 channel(s)" in quiet_line and len(quiet_line) < 300 and len(output) < 2000,
              (len(quiet_line), len(output)))
    finally:
        end(process)

    # The session is found through an ancestor's session file when the environment does not name it.
    bare = dict(env)
    del bare["CLAUDE_CODE_SESSION_ID"]
    os.makedirs(os.path.join(folder, ".claude", "sessions"))
    with open(os.path.join(folder, ".claude", "sessions", f"{os.getpid()}.json"), "w") as handle:
        json.dump({"sessionId": session}, handle)
    code, out, _ = run(["list"], bare)
    check("the session is found from the ancestor's session file", code == 0 and "unnamed, by interval" in out, out)

print(f"bad={bad}")
sys.exit(0 if bad == 0 else 1)
