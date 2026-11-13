# heartbeat-mcp

A heartbeat for a coding agent that waits on background work. The agent says what is running and when it expects it
to be done; when that time has passed, the agent is told and cannot end its turn without looking.

- `heartbeat.py set --in 40m --label "..." [--check "..."]`, `done <id>`, `extend <id> --in 20m`, `list`
- `heartbeat.py wait`: started by the agent as a background command; it exits at the first deadline, and the harness
  re-invokes an idle agent when a background command exits. This is what wakes an idle session.
- `heartbeat.py hook <PostToolUse|UserPromptSubmit|Stop>`: the hook entry. Tool results and prompts carry overdue
  beats (repeated every `HEARTBEAT_NAG_SEC`, default 300 s) and a reminder when beats are pending and no waiter runs
  (every `HEARTBEAT_ARM_NAG_SEC`, default 600 s). Stop is refused once per continuation while a beat is overdue, or
  while beats are pending and no waiter runs.
- `heartbeat.py mcp`: the same operations as MCP tools (`heartbeat_set`, `heartbeat_done`, `heartbeat_extend`,
  `heartbeat_list`) over stdio.

State: one JSON file per session in `~/.local/state/heartbeat-mcp/` (`HEARTBEAT_STATE_DIR` overrides), changed under
a file lock. The session comes from `CLAUDE_CODE_SESSION_ID`, the hook's `session_id`, or the session file of the
nearest ancestor process. Standard library only.

Deployed copy: `~/.mcp-servers/heartbeat-mcp/heartbeat.py` (the hooks in `~/.claude/settings.json` and the MCP entry
in `~/.claude.json` name it). After a change: run `python3 -I selftest.py` (must print `bad=0`), then copy the file.

Limits: a hook that fails answers `{}` (the host's hooks never break because of it, so a broken heartbeat is silent);
the waiter must be started by the agent (the Stop hook insists on it); beats belong to one session.
