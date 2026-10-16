# Hermes Star Office Reporter

Hermes plugin that automatically reports agent activity to a Star Office UI dashboard.

## What it does

Hooks into Hermes tool execution and pushes events to a remote Star Office UI dashboard via HTTP POST. The dashboard visualizes agent activity in a Phaser.js pixel-art office.

## Installation

```bash
# Clone this repo into your Hermes plugins directory
cd ~/.hermes/plugins
git clone https://github.com/gulasz101/hermes-star-office-reporter.git star-office

# Enable the plugin and point it at the dashboard (settings live under
# plugins.entries.<id>.settings — a top-level section is invisible to the plugin)
hermes plugins enable star-office
hermes config set plugins.entries.star-office.settings.dashboard_url http://192.168.1.119:19000
hermes config set plugins.entries.star-office.settings.enabled true
```

## Configuration

Plugin settings belong under `plugins.entries.star-office.settings` in
`~/.hermes/profiles/<profile>/config.yaml` (see `config.example.yaml`):

```yaml
plugins:
  entries:
    star-office:
      settings:
        enabled: true
        dashboard_url: http://192.168.1.119:19000  # Your dashboard URL
        # agent_name: hermes-szarik                 # Defaults to hermes-{profile}
        report_tool_calls: true
        report_file_edits: true
        report_commands: true
        report_sessions: true
        timeout_seconds: 5
        retry_on_failure: false
```

**Why not a top-level `star_office:` block:** `PluginContext` (the object passed to
`register(ctx)`) exposes `get_config(key)`, which resolves
`plugins.entries.<plugin_id>.settings.<key>` — it has **no `.config` attribute**. A
top-level section is never read, and the plugin disables itself **silently**: it still
lists as enabled and the only trace is `Star Office Reporter disabled` in the log.
Verify the wiring by effect, not by status: after a session that used tools,
`GET /events/recent` on the dashboard must show your agent's events.

Settings changes do **not** reach an already-running process — plugins are discovered once
per process, and a long-lived gateway keeps the registration it read at boot. In a
multiplexed gateway (one process serving several profiles) that is a silent trap: the
reporter can stay `disabled` in memory hours after the settings say `enabled`. Make an
enable/settings change land in a running gateway with:

```bash
hermes plugins enable star-office     # nudges the running gateway over its control socket
```

which forces plugin re-discovery in the live process and re-wires its adapters. Verify by
effect — the plugin's own log line plus event rows on the dashboard — never by config alone.
A gateway restart also does it, and is the only option for a gateway that predates the
control-socket reload verb.

## Which agent name is reported

`hermes-{profile}` of the profile the **session** runs for, resolved per session at event
time — a multiplexed gateway process serves several profiles, and each session tells the
plugin which one it is (the routed turn binds that profile's home):

- routed/CLI session for profile `szarik` → `hermes-szarik`
- a session in the root home → `hermes-default`
- an explicit `agent_name` setting overrides all of it

So one process, one plugin, many profiles — and the dashboard does not end up with a second
office for work that belongs to a named profile.

## Event schema

Events follow the Star Office UI v1 schema:

```json
{
  "version": 1,
  "agent": "hermes-szarik",
  "event_type": "tool_call",
  "timestamp": "2026-10-13T20:30:00Z",
  "payload": {
    "tool": "read_file",
    "path": "app.py",
    "success": true
  }
}
```

## What leaves the machine (SOU-192)

Payload values are **labels for a pixel character, not content**. `GET /events/recent`
has no auth today and the same feed is meant to feed a public view later, so the
reporter sends the minimum that the renderer can draw from:

| Tool | Sent | Never sent |
|---|---|---|
| `terminal` | `command`: the program plus **at most one allow-listed subcommand** — `git push`, `npm run build`, `docker build` | arguments, flags, `key=value` pairs, paths, URLs, `cd` targets, env prefixes |
| `read_file` | `path`: the **basename** — `app.py` | any directory component or absolute path |
| `write_file` / `patch` | `path` basename + `bytes` (byte count of `content`) | file contents, `old_string`/`new_string` |
| `web_search`, `browser_navigate`, others | `tool` + `success` only | the query text, the URL |
| vault tools (`browser_vault_*`) | nothing at all | — |

The command rule is the same one the Claude Code reporter applies
(`reporter/claude-code` in `gulasz101/star-office-ui-fork`, SOU-157/173) and the same
allow-list, so both reporters label a command identically:

```
git -c user.password=SECRET push   ->  git push
cd /repo && docker build -t x .    ->  docker build
npm --silent run build             ->  npm run build
sudo FOO=bar /usr/bin/env git push ->  git push
cat notes.txt | grep secret        ->  cat        (not on the allow-list)
```

Allow-listed programs: `git`, `npm`, `pnpm`, `yarn`, `docker`, `kubectl`, `cargo`,
`go`, `helm`, `flux`, `make`, `uv`, `pip`, `poetry`. A subcommand must be a bare
lowercase identifier (`^[a-z][a-z0-9-]{0,20}$`) — anything else is skipped and never
echoed. `tests/test_command_label.py` pins the table, case for case, against the
Claude Code suite's.

## Event types reported

- `tool_call` — after any tool execution (a command arrives as `tool: "terminal"`)
- `session_start` — when a Hermes session starts
- `session_end` — when a Hermes session ends

`file_edit` and `command_run` belong to the **Claude Code** reporter's vocabulary;
this one carries a manual file or command as a `tool_call`, which is what the v3
renderer (`frontend/v3/activity.js`) already maps.

## Requirements

- Hermes Agent 0.1.0+
- Python 3.9+ — **stdlib only**, no external dependencies

## Security

- Never sends credentials or secrets in event payloads
- Respects Hermes vault privacy (vault tools are dropped entirely)
- Commands are reduced to program + allow-listed subcommand; files to basenames;
  search queries and URLs are not sent at all (see "What leaves the machine")
- Dashboard URL should be internal network or VPN-only until auth is implemented

## Development

```bash
# Run tests (stdlib unittest — no pytest needed)
python3 -m unittest discover -s tests -v

# Lint
black __init__.py
mypy __init__.py
```

## License

MIT
