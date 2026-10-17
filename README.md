# hermes-star-office-reporter

A [Hermes](https://github.com/NousResearch/Hermes-Agent) plugin that reports agent
activity to a [Star Office UI](https://github.com/gulasz101/star-office-ui-fork)
dashboard, "the Lodge". Each Hermes profile gets its own room, and its pixel character
walks to the desk, the rack or the bookshelf as the agent works. It uses only the stdlib,
pushes from a background thread, and never blocks a turn.

Siblings: [`claude-code-star-office-reporter`](https://github.com/gulasz101/claude-code-star-office-reporter)
and [`openchamber-star-office-reporter`](https://github.com/gulasz101/openchamber-star-office-reporter).
They send the same event envelope to the same dashboard.

## Install

```bash
cd ~/.hermes/plugins
git clone https://github.com/gulasz101/hermes-star-office-reporter.git star-office

hermes plugins enable star-office
hermes config set plugins.entries.star-office.settings.dashboard_url http://127.0.0.1:19000
hermes config set plugins.entries.star-office.settings.enabled true
```

Requires Hermes Agent 0.1.0+ and Python 3.9+.

## Configure

Settings live under `plugins.entries.star-office.settings` in
`~/.hermes/profiles/<profile>/config.yaml` (full example: `config.example.yaml`):

```yaml
plugins:
  entries:
    star-office:
      settings:
        enabled: true
        dashboard_url: http://127.0.0.1:19000
        # agent_name: hermes-myprofile     # default: hermes-<profile>
        # token: "…"                       # per-agent ingest token, see below
        report_tool_calls: true
        report_sessions: true
        timeout_seconds: 5
```

> A top-level `star_office:` block is **never read**. The plugin then disables itself
> silently: it still lists as enabled, and the only trace is
> `Star Office Reporter disabled` in the log. Put the settings under
> `plugins.entries.star-office.settings`.

### Agent name

The name is `hermes-<profile>` for the profile the **session** runs as, resolved per
session. One multiplexed gateway serving several profiles therefore still gives each
profile its own room. A session in the root home reports as `hermes-default`. An explicit
`agent_name` overrides all of this.

### Ingest token

When the dashboard sets `SOU_INGEST_TOKENS`, `POST /events` needs
`Authorization: Bearer <token>`, and the token decides which room may be written. Mint
one per profile in the dashboard checkout. It prints the plaintext once:

```bash
uv run python backend/tokens.py new hermes-myprofile
```

Give it to the plugin with the `token` setting, or with `STAR_OFFICE_TOKEN` in the
environment (for example from SOPS in a container). The setting wins.
`STAR_OFFICE_API_KEY` is accepted as a legacy alias.

The token is never logged at any level and never put in the payload or URL. Every log line
passes through `redact()`.

## What it reports

| Hermes hook | `event_type` | payload |
|---|---|---|
| tool executed | `tool_call` | `tool`, `success`, plus the labels below |
| session starts / ends | `session_start` / `session_end` | none |

The payloads are labels for a pixel character, not content:

| Tool | Sent | Never sent |
|---|---|---|
| `terminal` | `command`: the program plus at most one allow-listed subcommand (`git push`, `npm run build`) | arguments, flags, `key=value`, paths, URLs, env prefixes |
| `read_file` | `path` basename | directories, absolute paths |
| `write_file` / `patch` | `path` basename + `bytes` | file contents, diffs |
| `web_search`, `browser_*`, others | `tool` + `success` | queries, URLs |
| vault tools (`browser_vault_*`) | nothing | everything |

```
git -c user.password=SECRET push   ->  git push
cd /repo && docker build -t x .    ->  docker build
sudo FOO=bar /usr/bin/env git push ->  git push
cat notes.txt | grep secret        ->  cat
```

Allow-listed programs: `git npm pnpm yarn docker kubectl cargo go helm flux make uv pip
poetry`. The subcommand must match `^[a-z][a-z0-9-]{0,20}$`. This is the same rule the
Claude Code reporter uses (`tests/test_command_label.py` pins it). The dashboard
sanitises everything again at its door.

## Troubleshooting

| Symptom | Check |
|---|---|
| No room on the dashboard | Settings under `plugins.entries.star-office.settings`, not top-level. Look for `Star Office Reporter disabled` in the log. `curl <dashboard_url>/health` from the Hermes host. |
| Settings changed, nothing happens | A running gateway keeps the plugin registration it read at boot. Run `hermes plugins enable star-office` (it reloads over the control socket) or restart the gateway. |
| `HTTP 401` in the log | The dashboard requires a token: set `token` or `STAR_OFFICE_TOKEN`. |
| `HTTP 403` | The token was minted for another name. It must equal the reported agent (`hermes-<profile>`). |
| `HTTP 429` | The per-token rate limit was hit; raise `SOU_INGEST_RATE_LIMIT` on the dashboard. |
| Work shows up in `hermes-default` instead of the profile's room | Use 1.1.0 or later (it resolves the profile per session), or set `agent_name`. |

Verify by effect: after a turn that used tools, the dashboard's room must move, and
`GET /events/recent?agent=hermes-<profile>` on a dev dashboard must list the events.

## Development

```bash
python3 -m unittest discover -s tests -v
# end to end against a real token-protected dashboard (scratch backend, throwaway tokens)
python3 tools/live_ingest_check.py --fork ~/Projects/star-office-ui-fork
```

## License

MIT
