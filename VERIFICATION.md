# Verification — September 28, 2026

## Automated checks

23 tests passed on macOS with Python 3.12.3. Both source modules also passed Python bytecode compilation.

Covered:

- Claude structured rejection/reset parsing and Codex account-limit window selection.
- No retry on usage warnings, assistant/tool prose mentioning limits, or authentication failure.
- Multiple exhausted windows, unrelated buckets, explicit timestamps, relative durations, bounded fallback.
- Persistent session IDs; resume targets the original session rather than the latest conversation.
- Real simulator subprocess runs for both provider adapters.
- Worker restart while waiting, followed by dispatch no earlier than its persisted reset time and within two seconds of its due time in the test.
- Exclusive worker lock and duplicate active-session protection.
- Running cancellation and cancellation winning over late completion updates.
- Crash recovery requiring review of an interrupted running job.

## Real installed clients

- Codex CLI `0.158.0-alpha.2.1`: version/help checked; read-only account usage query succeeded through app-server; a tiny no-tools task completed; resuming that exact saved session also completed.
- Claude Code `2.1.200`: version/help checked; a live no-tools attempt reached the client but was rejected because the current organization has disabled subscription access for Claude Code. The job correctly entered `attention`. Successful authenticated Claude execution is therefore not verified on this machine; its limit/resume adapter is covered by the simulator.
- Neither account was deliberately exhausted. Actual hours-long reset behavior is not experimentally verified; the timing/restart test uses provider-shaped simulated reset events.
- Windows runtime, Windows Task Scheduler, and macOS LaunchAgent registration were not exercised. Startup scripts are supplied but not installed automatically. The detached worker's start/status checks are exercised on macOS.

The initial restricted execution environment prevented Codex from opening its own state database. Repeating the live checks with normal client filesystem access succeeded. No credentials were read or copied by Reset Runner.
