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
        # token: "…"                                # Per-agent ingest token (see below)
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

## Ingest tokens (SOU-222, Phase 13 P13-E)

Once the dashboard runs with `SOU_INGEST_TOKENS` set, its `POST /events` door is locked:
the request must carry `Authorization: Bearer <token>`, and the token (not the envelope)
decides which agent's room may be written. The reporter sends the token for its own
profile:

```yaml
plugins:
  entries:
    star-office:
      settings:
        token: "<the value for this profile's agent>"
```

or via the environment, which is how a container gets it from SOPS:

```bash
export STAR_OFFICE_TOKEN="<the value for this profile's agent>"
```

**Precedence:** the per-profile `token` setting wins; `STAR_OFFICE_TOKEN` is the fallback.
`STAR_OFFICE_API_KEY` is accepted as a legacy alias for the same variable, because the
reporter skill documented that name before this field existed — a profile that followed it
must not end up silently sending unauthenticated writes to a locked dashboard.

Mint the value with the fork's CLI, which prints it **once** and stores only its sha256:

```bash
cd <the star-office-ui-fork checkout>
uv run python backend/tokens.py new hermes-szarik     # <- the `agent` is the reported name
```

Reporter behaviour around the token:

| Situation | What happens |
|---|---|
| token configured | `Authorization: Bearer <token>` on every `POST /events` |
| no token configured | the header is omitted and one **warning** is logged at boot — the door stays open on an un-tokened LAN box, so nothing breaks before the tokens are provisioned |
| `401` | the reporter logs `HTTP 401 - the dashboard requires an ingest token …` and fails open (the turn continues) |
| `403` | `HTTP 403 - this token writes another agent's room …`: the token's agent must equal the reported `agent` (`hermes-<profile>`) |
| `429` | `HTTP 429 - rate limited` (per-token bucket); drop reporting or raise `SOU_INGEST_RATE_LIMIT` server-side |

**The token is a secret and the reporter treats it as one:** it is never logged — not at
INFO, not at DEBUG, not in a rejection warning — and never put in the payload or the URL.
Every log line that carries dynamic data goes through `redact()`, which masks the token and
any `Bearer <value>` run, so a debug line stays safe even if it is handed a header dump.
The value itself is never written to disk or to the repo; deployment sources it from SOPS.

Verify it end to end (real Flask backend with a real `SOU_INGEST_TOKENS` store, real
`POST /events`):

```bash
python3 tools/live_ingest_check.py --fork ~/Projects/star-office-ui-fork
```

The script mints throwaway tokens, starts a scratch backend on a free port with its own
SQLite file, drives the REAL reporter at it for four cases (token → 201 and stored; unknown
token → 401; another agent's token → 403; no token against an open backend → 201), reads the
events back through `GET /events/recent` as the independent oracle, and then removes the
scratch directory. It prints no token.

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

- Never sends credentials or secrets in event payloads — the ingest token travels in the
  `Authorization` header only, and never reaches a log line (see "Ingest tokens")
- Respects Hermes vault privacy (vault tools are dropped entirely)
- Commands are reduced to program + allow-listed subcommand; files to basenames;
  search queries and URLs are not sent at all (see "What leaves the machine")
- The dashboard URL should stay internal network or VPN-only until the public face
  (Phase 13) is deployed; the ingest token protects `POST /events` once
  `SOU_INGEST_TOKENS` is set, so configure it before the dashboard leaves the LAN

## Development

```bash
# Run tests (stdlib unittest — no pytest needed)
python3 -m unittest discover -s tests -v

# End-to-end against a real token-protected dashboard (spawns a scratch backend)
python3 tools/live_ingest_check.py --fork ~/Projects/star-office-ui-fork

# Lint
black __init__.py
mypy __init__.py
```

## License

MIT
