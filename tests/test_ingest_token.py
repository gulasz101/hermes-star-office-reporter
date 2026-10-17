#!/usr/bin/env python3
"""SOU-222 (Phase 13 P13-E) — the Hermes reporter sends its ingest token.

Once the dashboard runs with ``SOU_INGEST_TOKENS`` (P13-B / SOU-219,
``backend/ingest_auth.py`` in ``gulasz101/star-office-ui-fork``) its ``POST
/events`` door answers **401** without a known token, **403** when the token is
bound to another agent than the envelope claims, and **429** when that token's
bucket is empty. The reporter therefore has to carry a per-profile token, and it
has to carry it as a *secret*:

* read from ``plugins.entries.star-office.settings.token`` (where SOPS-managed
  deployment puts it) or from ``STAR_OFFICE_TOKEN``;
* sent as ``Authorization: Bearer <token>``;
* **never** logged — not at INFO, not at DEBUG, not in a warning about a
  rejection, not in the payload, not in the URL.

Runnable two ways (no pytest needed for the plain run):

    python3 -m unittest discover -s tests -v
    pytest tests/test_ingest_token.py

The wire-level cases drive the REAL reporter (``on_tool_complete`` ->
``_push_event`` -> HTTP POST) at a stub dashboard that records the headers it
received, so the assertions read what actually left the process — a token that
is configured but not sent looks identical to a working one from the inside.

The end-to-end check against a token-protected dashboard (a real Flask backend
with ``SOU_INGEST_TOKENS`` set, 201/401/403 observed on the wire) cannot live
here: the backend is a different repository. It is ``tools/live_ingest_check.py``
in this repo, and it is what the ticket's live AC was signed off with.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("star_office_reporter", ROOT / "__init__.py")
reporter_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter_module)

StarOfficeReporter = reporter_module.StarOfficeReporter
resolve_token = reporter_module.resolve_token
token_from_environ = reporter_module.token_from_environ
redact = reporter_module.redact
http_hint = reporter_module.http_hint

LOGGER_NAME = "star_office_reporter"

# A token shaped like the ones ``backend/tokens.py new`` mints (token_urlsafe(32))
TOKEN = "sou222-livecheck-TOKEN-do-not-log-0123456789"
OTHER_TOKEN = "sou222-other-agent-TOKEN-do-not-log-9876543210"
CANARY = "CANARY-SOU222"


class _NoTokenEnvMixin:
    """Base for cases whose meaning depends on no token being in the env."""

    def setUp(self):
        super().setUp()
        self._saved = {name: os.environ.pop(name, None)
                       for name in reporter_module.TOKEN_ENV_ALIASES}

    def tearDown(self):
        for name, value in self._saved.items():
            if value is not None:
                os.environ[name] = value
        super().tearDown()


class TokenResolutionTest(_NoTokenEnvMixin, unittest.TestCase):
    """Where the token comes from, and which source wins."""

    def test_setting_wins_over_environment(self):
        with mock.patch.dict(os.environ, {"STAR_OFFICE_TOKEN": OTHER_TOKEN}):
            self.assertEqual(resolve_token(TOKEN), (TOKEN, "setting"))

    def test_environment_used_when_the_setting_is_absent(self):
        with mock.patch.dict(os.environ, {"STAR_OFFICE_TOKEN": TOKEN}):
            self.assertEqual(resolve_token(None), (TOKEN, "STAR_OFFICE_TOKEN"))

    def test_blank_setting_falls_through_to_the_environment(self):
        # An empty YAML value (`token:`) or a whitespace-only one must not shadow
        # a provisioned environment variable.
        with mock.patch.dict(os.environ, {"STAR_OFFICE_TOKEN": TOKEN}):
            for blank in ("", "   ", None):
                with self.subTest(blank=repr(blank)):
                    self.assertEqual(resolve_token(blank), (TOKEN, "STAR_OFFICE_TOKEN"))

    def test_the_documented_legacy_alias_is_accepted(self):
        # STAR_OFFICE_API_KEY is the name the reporter skill documented; a profile
        # that followed it must not end up silently unauthenticated.
        with mock.patch.dict(os.environ, {"STAR_OFFICE_API_KEY": TOKEN}):
            self.assertEqual(token_from_environ(), (TOKEN, "STAR_OFFICE_API_KEY"))

    def test_nothing_configured_is_not_an_error(self):
        self.assertEqual(resolve_token(None), ("", None))
        self.assertEqual(token_from_environ(), ("", None))

    def test_whitespace_around_a_token_is_trimmed(self):
        with mock.patch.dict(os.environ, {"STAR_OFFICE_TOKEN": f"  {TOKEN}\n"}):
            self.assertEqual(resolve_token(None), (TOKEN, "STAR_OFFICE_TOKEN"))


class RedactionTest(unittest.TestCase):
    def test_token_is_masked_everywhere_it_appears(self):
        text = f"pushing to dashboard with {TOKEN} and again {TOKEN}"
        masked = redact(text, TOKEN)
        self.assertNotIn(TOKEN, masked)
        self.assertEqual(masked.count(reporter_module.REDACTED), 2)

    def test_bearer_runs_are_masked_even_when_the_token_is_not_known(self):
        masked = redact("Authorization: Bearer abc.def-123 was rejected")
        self.assertNotIn("abc.def-123", masked)
        self.assertIn("Bearer ***", masked)

    def test_no_token_configured_is_not_a_crash(self):
        self.assertEqual(redact("nothing to hide", *()), "nothing to hide")
        self.assertEqual(redact("nothing to hide", "", None), "nothing to hide")

    def test_hints_name_the_fix_without_a_secret(self):
        for code in (401, 403, 429, 500, 201):
            with self.subTest(code=code):
                hint = http_hint(code)
                self.assertNotIn(TOKEN, hint)
                self.assertNotIn("Bearer", hint)
        self.assertIn("token", http_hint(401))
        self.assertIn("another agent", http_hint(403))


class _StubDashboard:
    """A dashboard stub that answers a chosen status and records the headers."""

    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.requests.append({
                    "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()},
                    "body": body,
                })
                self.send_response(outer.status)
                if outer.status == 429:
                    self.headers["Retry-After"] = "1"
                self.end_headers()
                self.wfile.write(b'{"ok": false}')

            def log_message(self, *args):  # silence the stub's own stderr noise
                pass

        self.requests = []
        self.status = 201
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _ReporterHarness(unittest.TestCase):
    """Records every log record the reporter emits, token-safety included."""

    def setUp(self):
        self.stub = _StubDashboard()
        self.records = []
        self._handler = logging.Handler()
        self._handler.emit = lambda record: self.records.append(record)
        self._logger = logging.getLogger(LOGGER_NAME)
        self._previous_level = self._logger.level
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        # Deterministic identity: the per-session profile probe is not under test.
        self._original_bound_home = reporter_module._bound_profile_home
        reporter_module._bound_profile_home = lambda: None

    def tearDown(self):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous_level)
        reporter_module._bound_profile_home = self._original_bound_home
        self.stub.close()

    def reporter(self, **overrides):
        config = {
            "enabled": True,
            "dashboard_url": self.stub.url,
            "report_tool_calls": True,
            "report_sessions": True,
            "report_file_edits": True,
            "report_commands": True,
        }
        config.update(overrides)
        return StarOfficeReporter(config, "szarik")

    def push(self, reporter, tool="terminal", args=None, wait_for_request=True):
        """One tool call -> the request the stub received (and its headers)."""
        before = len(self.stub.requests)
        reporter.on_tool_complete(tool, args or {"command": "git status"}, "ok", True,
                                  session_id="sou-222")
        if not wait_for_request:
            return None
        for _ in range(200):
            if len(self.stub.requests) > before:
                break
            threading.Event().wait(0.02)
        self.assertGreater(len(self.stub.requests), before,
                           "the reporter did not reach the dashboard stub")
        return self.stub.requests[-1]

    def log_text(self):
        return "\n".join(record.getMessage() for record in self.records)

    def wait_for_log(self, needle):
        for _ in range(200):
            if needle in self.log_text():
                return True
            threading.Event().wait(0.02)
        return False


class TokenOnTheWireTest(_NoTokenEnvMixin, _ReporterHarness):
    """AC: the header is sent when configured, and absent when it is not."""

    def test_bearer_header_is_sent_when_the_token_is_configured(self):
        request = self.push(self.reporter(token=TOKEN))
        self.assertEqual(request["path"], "/events")
        self.assertEqual(request["headers"].get("Authorization"), f"Bearer {TOKEN}")
        # The token is a header, not content: it must not be in the envelope.
        self.assertNotIn(TOKEN, request["body"].decode("utf-8"))
        self.assertNotIn(TOKEN, json.dumps(json.loads(request["body"])))

    def test_no_authorization_header_when_nothing_is_configured(self):
        request = self.push(self.reporter())
        self.assertNotIn("Authorization", request["headers"])
        self.assertNotIn("authorization", {k.lower() for k in request["headers"]})

    def test_every_session_event_carries_the_header(self):
        reporter = self.reporter(token=TOKEN)
        for hook in (lambda: reporter.on_session_start("s1"),
                     lambda: reporter.on_session_end("s1"),
                     lambda: reporter.on_tool_complete("read_file", {"path": "/x/y.py"},
                                                       "ok", True, session_id="s1")):
            before = len(self.stub.requests)
            hook()
            for _ in range(200):
                if len(self.stub.requests) > before:
                    break
                threading.Event().wait(0.02)
        self.assertEqual(len(self.stub.requests), 3)
        for request in self.stub.requests:
            with self.subTest(path=request["path"]):
                self.assertEqual(request["headers"].get("Authorization"), f"Bearer {TOKEN}")

    def test_register_wires_the_setting_and_the_environment(self):
        class FakeCtx:
            def __init__(self, settings, profile_name="szarik"):
                self.settings = settings
                self.profile_name = profile_name
                self.hooks = []

            def get_config(self, key, default=None):
                return self.settings.get(key, default)

            def register_hook(self, name, fn):
                self.hooks.append(name)

        base = {"enabled": True, "dashboard_url": self.stub.url}

        ctx = FakeCtx(dict(base, token=TOKEN))
        reporter_module.register(ctx)
        try:
            self.assertEqual(reporter_module._reporter.token, TOKEN)
            self.assertEqual(reporter_module._reporter.token_source, "setting")
            self.assertEqual(sorted(ctx.hooks),
                             ["on_session_end", "on_session_start", "post_tool_call"])
        finally:
            reporter_module._reporter = None

        with mock.patch.dict(os.environ, {"STAR_OFFICE_TOKEN": OTHER_TOKEN}):
            ctx = FakeCtx(dict(base))
            reporter_module.register(ctx)
            try:
                self.assertEqual(reporter_module._reporter.token, OTHER_TOKEN)
                self.assertEqual(reporter_module._reporter.token_source, "STAR_OFFICE_TOKEN")
            finally:
                reporter_module._reporter = None

        ctx = FakeCtx(dict(base))
        reporter_module.register(ctx)
        try:
            self.assertEqual(reporter_module._reporter.token, "")
            self.assertIsNone(reporter_module._reporter.token_source)
        finally:
            reporter_module._reporter = None

    def test_disabled_reporter_reports_nothing_at_all(self):
        reporter = self.reporter(enabled=False, token=TOKEN)
        reporter.on_tool_complete("terminal", {"command": "git status"}, "ok", True)
        reporter.on_session_start("s1")
        threading.Event().wait(0.2)
        self.assertEqual(self.stub.requests, [])


class TokenNeverInLogsTest(_NoTokenEnvMixin, _ReporterHarness):
    """AC: the token never appears in logs or debug output — at any level."""

    def test_debug_output_of_a_successful_push_is_token_free(self):
        self.push(self.reporter(token=TOKEN))
        for _ in range(200):
            if "Pushed tool_call" in self.log_text():
                break
            threading.Event().wait(0.02)
        text = self.log_text()
        self.assertIn("Pushed tool_call", text, "no debug line captured at all")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(f"Bearer {TOKEN}", text)

    def test_a_rejection_is_explained_without_the_token(self):
        cases = {
            401: "requires an ingest token",
            403: "another agent",
            429: "rate limited",
            500: "dashboard error",
        }
        for status, hint in cases.items():
            with self.subTest(status=status):
                self.records.clear()
                self.stub.status = status
                self.push(self.reporter(token=TOKEN))
                self.assertTrue(self.wait_for_log(f"HTTP {status}"),
                                f"no warning for HTTP {status}: {self.log_text()!r}")
                text = self.log_text()
                self.assertIn(hint, text)
                self.assertNotIn(TOKEN, text)
                self.assertNotIn(f"Bearer {TOKEN}", text)

    def test_a_rejected_push_fails_open(self):
        # The reporter is a hook's side effect: a 401 must not raise into the turn.
        self.stub.status = 401
        reporter = self.reporter(token=TOKEN)
        reporter.on_tool_complete("terminal", {"command": "git status"}, "ok", True)
        reporter.on_session_end("s1")

    def test_transport_failure_logs_no_token(self):
        reporter = self.reporter(token=TOKEN)
        reporter.dashboard_url = "http://127.0.0.1:1"  # nothing listens there
        reporter.on_tool_complete("terminal", {"command": "git status"}, "ok", True)
        for _ in range(200):
            if "Failed to push" in self.log_text():
                break
            threading.Event().wait(0.02)
        text = self.log_text()
        self.assertIn("Failed to push", text)
        self.assertNotIn(TOKEN, text)

    def test_enabled_without_a_token_warns_loudly_and_once(self):
        self.records.clear()
        self.reporter()
        text = self.log_text()
        self.assertIn("no ingest token configured", text)
        self.assertIn("unauthenticated", text)

    def test_boot_line_names_the_source_not_the_value(self):
        class FakeCtx:
            def __init__(self, settings):
                self.settings = settings
                self.profile_name = "szarik"

            def get_config(self, key, default=None):
                return self.settings.get(key, default)

            def register_hook(self, name, fn):
                pass

        self.records.clear()
        reporter_module.register(FakeCtx({
            "enabled": True,
            "dashboard_url": self.stub.url,
            "token": TOKEN,
        }))
        text = self.log_text()
        try:
            self.assertIn("ingest token: from setting", text)
            self.assertNotIn(TOKEN, text)
        finally:
            reporter_module._reporter = None

    def test_a_canary_payload_never_picks_the_token_up(self):
        # Guards the obvious future regression: someone "helpfully" adding the
        # token to the payload for the dashboard to echo back.
        request = self.push(self.reporter(token=TOKEN),
                            tool="read_file", args={"path": f"/tmp/{CANARY}.py"})
        body = request["body"].decode("utf-8")
        self.assertNotIn(TOKEN, body)
        self.assertNotIn("Authorization", body)
        # The header is the ONE place the token is allowed to appear, exactly once.
        carrying = {name for name, value in request["headers"].items() if TOKEN in value}
        self.assertEqual(carrying, {"Authorization"})
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {TOKEN}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
