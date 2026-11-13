# heartbeat-mcp

Heartbeat channels for a coding agent that waits on background work. The agent opens one channel per thing it waits
for; there is no limit on the number of channels. When a channel fires, the agent is told and cannot end its turn
without looking.

## A channel

- a **name** and a **label**;
- a **deadline** (`--in 40m`: when the work should be finished) and, optionally, a **liveness interval**
  (`--every 20m`: the longest silence allowed; every sign of life moves the deadline);
- a **notation** (optional, may be absent): what to do when the channel fires. It is shown every time the channel
  is reported;
- optionally an **agent** id: tool calls made inside that subagent count as signs of life (the hook sees them).

A channel fires when its deadline passes without a sign of life (`OVERDUE`), when the thing waited for reports that it
is done (`FINISHED`), or when it holds a note (`NOTE`).

## Commands

The agent:

    heartbeat.py open [NAME] --label "..." [--in 40m] [--every 20m] [--notation "..."] [--agent ID]
    heartbeat.py extend NAME --in 20m      # looked, the work goes on (also acknowledges notes)
    heartbeat.py ack NAME                  # read the notes, the deadline stays
    heartbeat.py notate NAME [--text ...]  # set or clear the notation
    heartbeat.py close NAME                # looked at the result, done with it
    heartbeat.py list
    heartbeat.py wait [--channel NAME]...  # run in the background: exits when a watched channel fires

The thing waited for (a script, a subagent; it needs the session's `CLAUDE_CODE_SESSION_ID`, which commands and
subagents started by the session inherit, or `--session`):

    heartbeat.py beat NAME [--text ...]    # still alive
    heartbeat.py note NAME --text "..."    # needs attention
    heartbeat.py finish NAME [--text ...]  # done

## How the agent is told

- `heartbeat.py hook PostToolUse|UserPromptSubmit`: fired channels are added to the agent's context (repeated every
  `HEARTBEAT_NAG_SEC`, default 300 s), and a reminder when open channels are not watched by a waiter (every
  `HEARTBEAT_ARM_NAG_SEC`, default 600 s). A tool call inside a subagent gets no reminder.
- `heartbeat.py hook Stop`: the turn cannot end while a channel has fired, or while an open channel is not watched by
  a waiter (once per continuation, so it cannot loop).
- `heartbeat.py wait`: a select over the watched channels (all of them when none is named). The harness re-invokes an
  idle agent when a background command exits, so this is what wakes an idle session. Several waiters may run.
- `heartbeat.py mcp`: the same operations as MCP tools over stdio (`heartbeat_open`, `_beat`, `_note`, `_finish`,
  `_extend`, `_ack`, `_notate`, `_close`, `_list`).

## State and deployment

One JSON file per session in `~/.local/state/heartbeat-mcp/` (`HEARTBEAT_STATE_DIR` overrides), changed under a file
lock. The session comes from `CLAUDE_CODE_SESSION_ID`, the hook's `session_id`, or the session file of the nearest
ancestor process. Standard library only.

Deployed copy: `~/.mcp-servers/heartbeat-mcp/heartbeat.py` (the hooks in `~/.claude/settings.json` and the MCP entry
in `~/.claude.json` name it). After a change: `python3 -I selftest.py` must print `bad=0`, then copy the file.

## Limits

- A hook that fails answers `{}`: the host's hooks never break because of it, so a broken heartbeat is silent.
- A waiter must be started by the agent (the Stop hook insists on it).
- Channels belong to one session.
- A beat or finish signal from another machine needs a way back to this one (run the signalling command locally
  after the remote command returns).
