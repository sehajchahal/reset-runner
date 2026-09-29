# Design and research

Built September 28, 2026.

## Decision

Use a Python standard-library daemon with a durable SQLite queue and CLI adapters. A plain MCP tool is callable by a host, but does not itself guarantee that a quota-exhausted host will schedule a future model turn. Owning the provider subprocess and external timer solves that lifecycle problem without a UI automation dependency.

The user registers a task or an exact existing session. The worker runs that session, classifies its terminal outcome, records a reset, and resumes at the due time. Successful turns stop. Only recognized limit failures retry automatically. No general “keep going forever” loop is added.

## Provider evidence

- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive): machine-readable event output and explicit saved-session resumption.
- [Codex app-server](https://developers.openai.com/codex/app-server): initialize/initialized handshake, account/rateLimits/read, usedPercent and Unix-seconds resetsAt. App-server is an evolving/experimental surface; failure falls back to known error timestamps or bounded retries.
- [Claude Code programmatic mode](https://code.claude.com/docs/en/headless): print/stream-json operation and resumption by session ID.
- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference): output-format, verbose, resume, and normal permission options.
- [Claude Agent SDK type reference](https://code.claude.com/docs/en/agent-sdk/python#ratelimitinfo): rejected rate-limit state and optional reset timestamp. Adapter accepts both CLI camelCase and SDK snake_case reset field spellings.

Local read-only CLI help was checked against Codex 0.158.0-alpha.2.1 and Claude Code 2.1.200. Implementation does not require importing either vendor SDK or a separate API key.

## Timing contract

The intended dispatch is reset + 3 seconds, with a 0.5-second idle scheduler interval. This is a target, not a real-time guarantee: sleep, connectivity, provider timestamp accuracy, or another running job may delay dispatch. Missing timestamps invoke visible 1–15 minute backoff. No attempt is made to circumvent limits.

## Persistence and lifecycle

SQLite owns durable job states. A process-scoped OS lock owns the worker. Session IDs are recorded immediately. Waiting jobs recover automatically; interrupted running jobs require explicit review because arbitrary agent side effects cannot be guaranteed exactly-once after a process crash. Background operation survives terminal closure; optional login startup handles reboots after login.

## Delivery scope

Portable source, CLI, detached background worker, per-user startup helper, macOS/Windows launchers, documentation, and automated regression tests. No arbitrary GUI chat takeover or iOS execution is claimed.
