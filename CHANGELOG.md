# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- The `Stop` hook also refuses, in a continuation (`stop_hook_active`), while open channels have no waiter. It counts
  the refusals made in a row for that reason; after `HEARTBEAT_STOP_UNWATCHED_MAX` of them (default 3) it lets the turn
  end and says so in a `systemMessage`. Starting a waiter, or a Stop that does not block, resets the count.
- `wait` prints the fired channels in full and, for the quiet ones, their number and the names of the first five.
- A recorded waiter now carries its process start time; records written by 0.2.0 do not, so a waiter that is still
  running from before the upgrade is not counted until it is started again.

### Fixed

- After a channel fired, the `Stop` block named only the fired channels. When the agent then closed, extended or
  acknowledged the channel, the session could go idle with open channels that no waiter watched. The block now names
  the unwatched open channels and says to start a waiter once the fired channels are handled, and `close`, `extend`
  and `ack` (command line and MCP tools) print the same hint as `open` when open channels are left unwatched.
- A waiter that has seen a fired channel and will exit on its next poll no longer counts as watching the other
  channels.
- A waiter now removes its own record in the locked section in which it decides to exit, before it prints. A waiter
  with many channels could block on a full output pipe and stay recorded as running forever, and a `Stop` hook running
  in between counted a waiter that was about to be gone.
- A recorded waiter counts only if it is the process that recorded itself: the process start time must match (a reused
  process id no longer counts), a stopped process does not count, and `wait` must be the subcommand of the invocation,
  not any argument. Records that do not count are dropped when the state is next written; a stopped waiter is kept.

- A deadline of `nan`, `inf` or one beyond the calendar was stored and then made every later listing raise.
  `open` and `extend` now refuse it before storing anything.
- A waiter started through a relative path or a symbolic link was not counted as covering the open channels, so the
  Stop hook kept refusing. Waiters are now matched by the resolved path of their arguments.
- The program path in printed hints is shell-quoted, so a path that contains a space can be pasted.
- A liveness interval under a minute is shown in seconds (it was shown as "every 0 min").
- The text of a `beat` was stored but never shown. It is now shown with the last sign of life, and a `beat` without
  text, or a tool call of the bound subagent, clears it.
- An MCP call with a missing required argument now names the argument.
- Every command-line argument has help text, and the "no session id" error no longer suggests a flag the MCP server
  does not take.

## [0.2.0] - 2026-11-14

### Added

- Channels: any number of things to wait for, each with its own deadline.
- A liveness interval (`--every`): every sign of life moves the deadline.
- Notations: optional text saying what to do when a channel fires, shown every time it is reported.
- Signals from the thing waited for: `beat`, `note` and `finish`.
- Bound subagents (`--agent`): the tool calls of a subagent count as signs of life on its channels.
- A waiter that selects over every channel, or over the ones named with `--channel`.
- State written by 0.1.0 is carried over to channels.

## [0.1.0] - 2026-11-14

### Added

- Beats with deadlines.
- A waiter that wakes an idle session when a deadline passes.
- Claude Code hooks (`PostToolUse`, `UserPromptSubmit`, `Stop`) and an MCP server over stdio.
