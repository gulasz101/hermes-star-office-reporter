#!/usr/bin/env python3
"""SOU-192 — privacy parity with ``reporter/claude-code``: labels, basenames, no blobs.

The Hermes reporter used to send **whole command lines** and **whole paths** in
``tool_call`` payloads:

    {"tool": "terminal", "command": "cd ~/.hermes/hermes-agent && grep -n \\"…\\" -A 12 hermes_cli…"}
    {"tool": "read_file", "path": "/Users/<user>/.hermes/profiles/szarik/cache/…"}

The v3 renderer sanitises both before drawing a bubble (``frontend/v3/activity.js``),
so nothing user-visible leaked — but the raw strings sat in the event DB and came
back from ``GET /events/recent``, which has **no auth**. That matters for any
later public view (the Twitch idea, SOU-217/218).

The Claude Code reporter (``reporter/claude-code``, SOU-157/173) already sends
only a program plus one allow-listed subcommand, and a file *basename*. These
tests pin the Hermes reporter to the same contract, case for case — the tables
below are lifted from ``reporter/claude-code/tests/test_subcommands.py`` and
``test_privacy.py`` so the two reporters cannot drift apart.

Runnable two ways (no pytest needed for the plain run):

    python3 -m unittest discover -s tests -v
    pytest tests/test_command_label.py

Every case drives the REAL reporter: ``on_tool_complete`` -> ``_push_event`` ->
HTTP POST against a local stub dashboard. The assertions read the bytes that
actually left the process, not an import of its internals.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("star_office_reporter", ROOT / "__init__.py")
reporter_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter_module)

StarOfficeReporter = reporter_module.StarOfficeReporter

# Values the reporter must never transmit carry this token.
CANARY = "CANARY"
CANARY_PATH = f"/Users/{CANARY}-user/.hermes/profiles/szarik/cache/scratch/probe.py"
PATHISH = re.compile(r"[/\\]|[A-Za-z]:[/\\]")

# --------------------------------------------------------------------------
# case tables — mirrored from reporter/claude-code/tests/test_subcommands.py
# --------------------------------------------------------------------------

# Allow-listed CLI families: program + one bare subcommand.
ALLOWED = [
    ("git push origin main", "git push"),
    ("git status", "git status"),
    ("git commit -m 'note to self'", "git commit"),
    ("git switch --detach", "git switch"),
    ("docker build -t x .", "docker build"),
    ("docker compose up -d", "docker compose"),
    ("kubectl apply -f manifest.yaml", "kubectl apply"),
    ("cargo build --release", "cargo build"),
    ("go test ./...", "go test"),
    ("helm upgrade my-release ./chart", "helm upgrade"),
    ("flux reconcile kustomization web", "flux reconcile"),
    ("make clean", "make clean"),
    ("uv run pytest", "uv run"),
    ("pip install requests", "pip install"),
    ("poetry add rich", "poetry add"),
    ("npm install lodash", "npm install"),
    ("npm i -g pnpm", "npm i"),
    ("yarn add left-pad", "yarn add"),
    ("git rev-parse HEAD", "git rev-parse"),
]

# Runner programs keep the script verb as a SECOND token (activity.js reads it).
RUNNERS = [
    ("npm run build", "npm run build"),
    ("npm run test --watch", "npm run test"),
    ("npm run", "npm run"),
    ("pnpm run lint", "pnpm run lint"),
    ("yarn run build", "yarn run build"),
    ("npm --silent run build", "npm run build"),
]

# Flags, key=value pairs, paths and URLs are skipped and never echoed.
# The ticket's own case is the first row.
PRIVACY = [
    ("git -c user.password=SECRET push", "git push"),
    ("git -c credential.helper=store push", "git push"),
    ("git push https://user:TOKEN@host/repo.git", "git push"),
    ("docker build --build-arg TOKEN=abc -t x .", "docker build"),
    ("git checkout feature/login", "git checkout"),
    ("git -c color.ui=never log", "git log"),
    ("git -c a=b -c c=d", "git"),  # no bare identifier at all -> program only
]

# Env prefixes and wrapper tokens are stripped as today, with the subcommand kept.
WRAPPERS = [
    ("sudo git push", "git push"),
    ("sudo -n git push", "git push"),
    ("FOO=bar git push", "git push"),
    ("  /usr/bin/env FOO=bar npm run build ", "npm run build"),
]

# Outside the allow-list: program name only, exactly as before.
NOT_ALLOWED = [
    ("rm -rf /tmp/x", "rm"),
    ("pytest tests -q", "pytest"),
    ("ls -la", "ls"),
    ("cat notes.txt | grep CANARY", "cat"),
    ("ssh deploy@host", "ssh"),
    ("bash deploy.sh", "bash"),
    ("./deploy.sh", "deploy.sh"),
    ("timeout 30 git status", "git status"),  # timeout + duration are a wrapper
    ("git", "git"),
]

# Only the first real segment gets the label; leading cd/pushd segments are skipped.
SEGMENTS = [
    ("cd build && make test", "make test"),
    ("echo hi && git push", "echo"),
]

# The exact shape this ticket was filed about (SOU-160 AC2 observation).
CD_AND_WRAPPERS = [
    ("cd /home/me/repo && git push origin main", "git push"),
    ("cd /tmp; docker build -t x .", "docker build"),
    ("cd a && cd b && npm run build", "npm run build"),
    ("pushd /x && make test", "make test"),
    ("cd /only/here", "cd"),
    ("timeout 300 .venv/bin/python -m pytest -q", "pytest"),
    ("timeout 5m git pull", "git pull"),
    ("python3 -m pip install x", "pip install"),
    ("uv run pytest backend/tests", "uv run"),
    ("cd /x && PGPASSWORD=s3cr3t psql -h db", "psql"),
    # Hermes's own work: this is the string from the ticket body.
    ('cd ~/.hermes/hermes-agent && grep -n "sync" -A 12 hermes_cli.py', "grep"),
]

ALL_CASES = ALLOWED + RUNNERS + PRIVACY + WRAPPERS + NOT_ALLOWED + SEGMENTS + CD_AND_WRAPPERS

# Secrets that appear in the cases above and must never reach the wire.
SECRETS_IN_CASES = ["/home/me/repo", "/tmp", "s3cr3t", "/only/here", ".venv", "TOKEN", "SECRET",
                    "user.password", "credential.helper"]


def walk(node, path="payload"):
    """Yield (path, key, value) for every scalar in a payload tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


class ReporterHarness(unittest.TestCase):
    """One stub dashboard, one reporter, many tool calls."""

    events: list = []

    @classmethod
    def setUpClass(cls):
        outer = cls

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                outer.events.append((self.path, json.loads(self.rfile.read(length))))
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):  # silence
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        type(self).events = []
        # Deterministic agent identity: the bound-home probe is not what is under test.
        self._original_bound_home = reporter_module._bound_profile_home
        reporter_module._bound_profile_home = lambda: None
        self.reporter = StarOfficeReporter(
            {
                "enabled": True,
                "dashboard_url": f"http://127.0.0.1:{self.server.server_address[1]}",
                "report_tool_calls": True,
                "report_sessions": True,
                "report_file_edits": True,
                "report_commands": True,
            },
            "szarik",
        )

    def tearDown(self):
        reporter_module._bound_profile_home = self._original_bound_home

    # -- driving the real push path ---------------------------------------
    def push(self, tool_name, args, success=True):
        """One tool call -> the single envelope that left the process."""
        before = len(type(self).events)
        self.reporter.on_tool_complete(tool_name, args, "ok", success, session_id="sou-192")
        for _ in range(100):
            if len(type(self).events) > before:
                break
            threading.Event().wait(0.02)
        self.assertEqual(
            len(type(self).events), before + 1,
            f"{tool_name}: expected exactly one POST",
        )
        path, event = type(self).events[-1]
        self.assertEqual(path, "/events")
        return event

    def label(self, command):
        """The ``command`` value that actually reached the dashboard."""
        event = self.push("terminal", {"command": command})
        self.assertEqual(event["event_type"], "tool_call")
        return event["payload"]["command"]


class CommandLabelTest(ReporterHarness):
    """SOU-192 AC1: program + at most one allow-listed subcommand, nothing else."""

    def test_allowed_programs_carry_one_subcommand(self):
        for command, expected in ALLOWED:
            with self.subTest(command=command):
                self.assertEqual(self.label(command), expected)

    def test_runner_programs_carry_the_script_verb(self):
        for command, expected in RUNNERS:
            with self.subTest(command=command):
                self.assertEqual(self.label(command), expected)

    def test_privacy_cases_never_echo_a_value(self):
        for command, expected in PRIVACY:
            with self.subTest(command=command):
                self.assertEqual(self.label(command), expected)

    def test_wrappers_and_env_prefixes_are_stripped(self):
        for command, expected in WRAPPERS:
            with self.subTest(command=command):
                self.assertEqual(self.label(command), expected)

    def test_programs_outside_the_allow_list_keep_program_only(self):
        for command, expected in NOT_ALLOWED:
            with self.subTest(command=command):
                self.assertEqual(self.label(command), expected)

    def test_cd_prefix_and_duration_wrappers(self):
        for command, expected in CD_AND_WRAPPERS:
            with self.subTest(command=command):
                self.assertEqual(self.label(command), expected)

    def test_program_plus_subcommand_plus_segments_table_is_exhaustive(self):
        for command, expected in ALL_CASES:
            with self.subTest(command=command):
                self.assertEqual(self.label(command), expected)

    def test_secret_values_are_nowhere_in_the_envelope(self):
        for command, _expected in ALL_CASES:
            with self.subTest(command=command):
                body = json.dumps(self.push("terminal", {"command": command}))
                for secret in SECRETS_IN_CASES:
                    self.assertNotIn(secret, body, f"{command}: {secret!r} leaked")

    def test_unusable_command_still_reports_as_shell(self):
        for command in ("", "   ", None, 123, "&&&", ";"):
            with self.subTest(command=repr(command)):
                self.assertEqual(self.label(command), "shell")

    def test_label_is_single_line_and_small(self):
        for command, _expected in ALL_CASES:
            value = self.label(command)
            self.assertNotIn("\n", value, command)
            self.assertLessEqual(len(value), 120, (command, value))

    def test_home_paths_are_never_sent(self):
        """The concrete leak from the ticket: a Hermes home path in a command."""
        event = self.push(
            "terminal",
            {"command": "cd ~/.hermes/hermes-agent && grep -rn 'sync' -A 3 hermes_cli/ | head"},
        )
        body = json.dumps(event)
        for leak in ("/.hermes/", "hermes-agent", "hermes_cli", "~", "-rn", "sync"):
            self.assertNotIn(leak, body, f"{leak!r} leaked -> {body}")


class PathBasenameTest(ReporterHarness):
    """SOU-192 AC1: file tools send the basename only, never a path."""

    def test_read_file_sends_basename_only(self):
        event = self.push("read_file", {"path": CANARY_PATH})
        self.assertEqual(event["payload"]["path"], "probe.py")
        self.assertNotIn(CANARY, json.dumps(event))
        self.assertNotIn("/Users/", json.dumps(event))

    def test_write_file_sends_basename_plus_byte_count(self):
        event = self.push("write_file", {"path": CANARY_PATH, "content": "hello\nworld"})
        self.assertEqual(event["payload"]["path"], "probe.py")
        self.assertEqual(event["payload"]["bytes"], 11)
        self.assertNotIn(CANARY, json.dumps(event))

    def test_patch_sends_basename_only(self):
        event = self.push(
            "patch",
            {"path": CANARY_PATH, "old_string": CANARY, "new_string": CANARY},
        )
        self.assertEqual(event["payload"]["path"], "probe.py")
        self.assertNotIn(CANARY, json.dumps(event), "patch arguments are not content")

    def test_windows_and_relative_and_odd_paths(self):
        cases = [
            (r"C:\Users\CANARY-user\repo\app.py", "app.py"),
            ("repo/sub/dir/module.py", "module.py"),
            ("tracking/2026-10-16-sou192-note.md", "2026-10-16-sou192-note.md"),
            ("/", ""),          # no basename -> key omitted
            ("", ""),
            (None, ""),
        ]
        for raw, expected in cases:
            with self.subTest(path=repr(raw)):
                payload = self.push("read_file", {"path": raw})["payload"]
                self.assertEqual(payload.get("path", ""), expected)

    def test_no_path_separator_survives_any_payload_value(self):
        for tool, args in (
            ("read_file", {"path": CANARY_PATH}),
            ("write_file", {"path": CANARY_PATH, "content": "x"}),
            ("patch", {"path": CANARY_PATH, "old_string": "a", "new_string": "b"}),
            ("terminal", {"command": "cd /only/here && docker build -t x ."}),
        ):
            with self.subTest(tool=tool):
                payload = self.push(tool, args)["payload"]
                for path, key, value in walk(payload):
                    if isinstance(value, str):
                        self.assertIsNone(
                            PATHISH.search(value), f"{path}={value!r} looks like a path"
                        )


class BlobFreeTest(ReporterHarness):
    """SOU-192: no free-text the renderer never reads may sit in the feed."""

    def test_web_search_sends_no_query(self):
        """The renderer maps web_search to 'searching web' and never reads the query."""
        event = self.push("web_search", {"query": f"{CANARY} my private question"})
        self.assertNotIn("query", event["payload"])
        self.assertNotIn(CANARY, json.dumps(event))

    def test_browser_navigate_sends_no_url(self):
        """A URL can carry tokens and signed parameters; nothing reads it."""
        event = self.push("browser_navigate", {"url": f"https://x/?token={CANARY}"})
        self.assertNotIn("url", event["payload"])
        self.assertNotIn(CANARY, json.dumps(event))

    def test_unknown_tool_sends_tool_name_and_status_only(self):
        event = self.push("some_new_tool", {"secret_arg": CANARY})
        self.assertEqual(event["payload"], {"tool": "some_new_tool", "success": True})

    def test_forbidden_payload_keys_never_appear(self):
        forbidden = {
            "prompt", "content", "old_string", "new_string", "edits", "tool_response",
            "message", "title", "error", "stdout", "stderr", "query", "url",
            "command_args", "result",
        }
        for tool, args in (
            ("read_file", {"path": CANARY_PATH}),
            ("write_file", {"path": CANARY_PATH, "content": CANARY}),
            ("patch", {"path": CANARY_PATH, "old_string": CANARY, "new_string": CANARY}),
            ("terminal", {"command": f"git -c user.password={CANARY} push"}),
            ("web_search", {"query": CANARY}),
            ("browser_navigate", {"url": CANARY}),
        ):
            with self.subTest(tool=tool):
                payload = self.push(tool, args)["payload"]
                for path, key, _value in walk(payload):
                    self.assertNotIn(key.lower(), forbidden, f"{tool}: {path}.{key}")


class FailOpenTest(ReporterHarness):
    """The reporter is a hook's side effect: junk args must not raise."""

    def test_odd_arg_types_do_not_raise(self):
        for tool, args in (
            ("terminal", {"command": 123}),
            ("terminal", {}),
            ("read_file", {"path": ["a", "b"]}),
            ("write_file", {"path": None, "content": None}),
            ("write_file", {"path": "a.py", "content": {"not": "a string"}}),
            ("patch", {"path": {"weird": True}}),
        ):
            with self.subTest(tool=tool, args=repr(args)):
                self.push(tool, args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
