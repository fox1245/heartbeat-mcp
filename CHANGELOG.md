# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- A deadline of `nan`, `inf` or one beyond the calendar was stored and then made every later listing raise.
  `open` and `extend` now refuse it before storing anything.
- A waiter started through a relative path or a symbolic link was not counted as covering the open channels, so the
  Stop hook kept refusing. Waiters are now matched by the resolved path of their arguments.
- The program path in printed hints is shell-quoted, so a path that contains a space can be pasted.
- A liveness interval under a minute is shown in seconds (it was shown as "every 0 min").
- The text of a `beat` was stored but never shown. It is now shown with the last sign of life, and a `beat` without
  text clears it.
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
