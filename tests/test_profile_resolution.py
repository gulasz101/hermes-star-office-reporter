#!/usr/bin/env python3
"""Profile-resolution + push-path tests for the Star Office reporter (SOU-189).

Runnable two ways (no pytest needed for the plain run):

    python3 -m unittest discover -s tests -v          # inside the Hermes runtime
    pytest tests/test_profile_resolution.py           # if you have pytest

The resolution cases need ``hermes_constants`` (Hermes runtime) and are skipped when it
is unavailable — the pure-path and push-path cases run anywhere.
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "star_office_reporter", Path(__file__).resolve().parents[1] / "__init__.py"
)
reporter_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter_module)

StarOfficeReporter = reporter_module.StarOfficeReporter
_profile_from_home = reporter_module._profile_from_home
_bound_profile_home = reporter_module._bound_profile_home
_session_profile = reporter_module._session_profile

try:
    from hermes_constants import (  # type: ignore
        get_default_hermes_root,
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    HAVE_HERMES = True
except Exception:  # pragma: no cover - standalone checkout
    HAVE_HERMES = False


class ProfileFromHomeTest(unittest.TestCase):
    def test_root_home_is_default(self):
        if HAVE_HERMES:
            home = str(get_default_hermes_root())
        else:
            home = str(Path.home() / ".hermes")
        self.assertEqual(_profile_from_home(home), "default")

    def test_named_profile_home(self):
        if HAVE_HERMES:
            root = Path(get_default_hermes_root())
        else:
            root = Path.home() / ".hermes"
        self.assertEqual(_profile_from_home(root / "profiles" / "szarik"), "szarik")
        self.assertEqual(_profile_from_home(str(root / "profiles" / "szarik") + "/"), "szarik")

    def test_elsewhere_is_unknown(self):
        self.assertIsNone(_profile_from_home("/tmp/some/other/place"))
        self.assertIsNone(_profile_from_home(""))
        self.assertIsNone(_profile_from_home(None))


@unittest.skipUnless(HAVE_HERMES, "hermes_constants not importable")
class RoutedSessionTest(unittest.TestCase):
    """A multiplexed gateway binds the routed profile's home around the turn."""

    def setUp(self):
        self.root = Path(get_default_hermes_root())
        self.profile_home = self.root / "profiles" / "szarik"

    def test_override_wins_over_process_home(self):
        # The process serves 'default' (its own home), the turn is routed to szarik.
        token = set_hermes_home_override(str(self.profile_home))
        try:
            self.assertEqual(_profile_from_home(_bound_profile_home()), "szarik")
            self.assertEqual(_session_profile("default"), "szarik")
        finally:
            reset_hermes_home_override(token)

    def test_no_override_falls_back_to_process_profile(self):
        # With no routed scope, the process's own home wins (that IS the profile a
        # single-profile process serves; for a multiplexed one it is its launch profile).
        expected = _profile_from_home(str(get_hermes_home())) or "fallback"
        self.assertEqual(_session_profile("fallback"), expected)

    def test_env_pin_is_a_last_resort(self):
        # Only reached when the bound home says nothing at all (no override, no home).
        original = reporter_module._bound_profile_home
        reporter_module._bound_profile_home = lambda: None
        previous = os.environ.get("HERMES_PROFILE_NAME")
        os.environ["HERMES_PROFILE_NAME"] = "pinned"
        try:
            self.assertEqual(_session_profile("default"), "pinned")
        finally:
            if previous is None:
                os.environ.pop("HERMES_PROFILE_NAME", None)
            else:
                os.environ["HERMES_PROFILE_NAME"] = previous
            reporter_module._bound_profile_home = original


@unittest.skipUnless(HAVE_HERMES, "hermes_constants not importable")
class AgentNameTest(unittest.TestCase):
    def _reporter(self, **overrides):
        config = {
            "enabled": True,
            "dashboard_url": "http://127.0.0.1:1",
            "report_tool_calls": True,
            "report_sessions": True,
            "report_file_edits": True,
            "report_commands": True,
        }
        config.update(overrides)
        return StarOfficeReporter(config, "default")

    def test_routed_session_reports_its_own_profile(self):
        reporter = self._reporter()
        token = set_hermes_home_override(str(Path(get_default_hermes_root()) / "profiles" / "szarik"))
        try:
            self.assertEqual(reporter.agent_name_for_session("20261016_181258_196a84c8"), "hermes-szarik")
        finally:
            reset_hermes_home_override(token)

    def test_unrouted_session_reports_loaded_profile(self):
        # No routed scope: the instance's own profile is the answer.
        original = reporter_module._bound_profile_home
        reporter_module._bound_profile_home = lambda: None
        try:
            reporter = StarOfficeReporter(
                {"enabled": True, "dashboard_url": "http://127.0.0.1:1"}, "szarik"
            )
            self.assertEqual(reporter.agent_name_for_session("s1"), "hermes-szarik")
        finally:
            reporter_module._bound_profile_home = original

    def test_explicit_agent_name_overrides_everything(self):
        reporter = self._reporter(agent_name="hermes-pinned")
        token = set_hermes_home_override(str(Path(get_default_hermes_root()) / "profiles" / "szarik"))
        try:
            self.assertEqual(reporter.agent_name_for_session("s1"), "hermes-pinned")
        finally:
            reset_hermes_home_override(token)


class PushPathTest(unittest.TestCase):
    """End-to-end: hooks -> HTTP POST, with the per-session agent name on the wire."""

    events = []

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

    def setUp(self):
        PushPathTest.events = []

    def test_session_start_and_tool_call_reach_the_dashboard(self):
        events = PushPathTest.events
        original = reporter_module._bound_profile_home
        reporter_module._bound_profile_home = lambda: None  # unscoped: instance profile
        try:
            reporter = StarOfficeReporter(
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
            reporter.on_session_start("sess-1")
            reporter.on_tool_complete("terminal", {"command": "date -u"}, "ok", True, session_id="sess-1")
            reporter.on_tool_complete(
                "browser_vault_fill", {"handle": "vault:x"}, "ok", True, session_id="sess-1"
            )
        finally:
            reporter_module._bound_profile_home = original

        # The pushes happen on daemon threads and their ARRIVAL ORDER is not
        # guaranteed (this raced on the plugin's own Python 3.14 runtime: 1 run
        # in 3 saw ['tool_call', 'session_start']). What the test is about is
        # WHICH events reach the dashboard and how they are labelled, not their
        # interleaving — so compare as a multiset.
        for _ in range(100):
            if len(events) >= 2:
                break
            threading.Event().wait(0.05)

        kinds = sorted(event["event_type"] for _path, event in events)
        self.assertEqual(kinds, ["session_start", "tool_call"])
        self.assertTrue(all(path == "/events" for path, _ in events))
        for _path, event in events:
            self.assertEqual(event["agent"], "hermes-szarik")
            self.assertEqual(event["version"], 1)
        # SOU-192: the wire carries a label, not the command line — the allow-list
        # has no entry for `date`, so the argument is dropped like every other.
        by_kind = {event["event_type"]: event["payload"] for _path, event in events}
        self.assertEqual(by_kind["tool_call"]["command"], "date")
        # Vault tools must never be reported.
        self.assertNotIn("browser_vault_fill", json.dumps(events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
