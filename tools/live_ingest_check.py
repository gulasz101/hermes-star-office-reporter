#!/usr/bin/env python3
"""SOU-222 live check — the reporter against a REAL token-protected dashboard.

The unit suite pins the reporter's own behaviour (the header goes out, the token
never reaches a log line). It cannot prove the other half of the AC, which is
that the fork's ``POST /events`` door *accepts* that header: 201 for the token's
own agent, 401 for an unknown token, 403 for another agent's token. That needs a
real Flask backend with a real ``SOU_INGEST_TOKENS`` store, which lives in a
different repository — hence this script.

What it does, using nothing but stdlib and the two checkouts already on disk:

1. mints two throwaway tokens with the FORK's own CLI
   (``backend/tokens.py new <agent>``), so the hashes and the wire format are the
   real ones — no token format is re-implemented here;
2. starts the fork's ``backend/app.py`` in a scratch directory, on a free port,
   with its own SQLite file and ``SOU_INGEST_TOKENS`` set to those two hashes;
3. drives the REAL ``StarOfficeReporter`` at it through ``on_tool_complete``
   (the same push path a live session uses) for each case below, reading the
   backend's own ``GET /events/recent`` before and after as the independent
   oracle — the verdict is what the dashboard STORED, not what the reporter says
   it sent;
4. tears the backend down and removes the scratch directory.

    token from STAR_OFFICE_TOKEN      -> 201, event stored
    unknown token                     -> 401, nothing stored
    another agent's token             -> 403, nothing stored
    no token, backend WITHOUT tokens  -> 201, event stored (no regression)

The plaintext tokens exist only in this process's memory and in the child CLI's
stdout; the backend is given sha256 digests, and nothing is written to disk. The
script never prints a token — only its digest prefix, which is safe by the
backend's own design.

    python3 tools/live_ingest_check.py --fork ~/Projects/star-office-ui-fork
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAIR_LINE = re.compile(r"^\s*([0-9a-f]{64}):(\S+)\s*$")
HTTP_STATUS = re.compile(r"HTTP (\d{3})")
LOG_PREFIX = "sou222-live"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def digest_prefix(digest: str) -> str:
    return digest[:8]


def load_reporter(fork_plugin: Path):
    spec = importlib.util.spec_from_file_location("star_office_reporter", fork_plugin / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def http_get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", "replace")


def mint_token(fork: Path, python: str, agent: str):
    """``(token, digest)`` from the fork's own tokens.py — no format guessing."""
    result = subprocess.run(
        [python, "backend/tokens.py", "new", agent],
        cwd=str(fork), capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise SystemExit(f"tokens.py failed for {agent!r}: {result.stderr.strip()}")
    lines = result.stdout.splitlines()
    token, digest = None, None
    for index, line in enumerate(lines):
        match = PAIR_LINE.match(line)
        if match:
            digest = match.group(1)
            if match.group(2) != agent:
                raise SystemExit(f"tokens.py returned a pair for {match.group(2)!r}, not {agent!r}")
            continue
        if "one-time token" in line:
            for follow in lines[index + 1:]:
                if follow.strip():
                    token = follow.strip()
                    break
    if not token or not digest:
        raise SystemExit(f"could not parse tokens.py output for {agent!r}")
    return token, digest


class Backend:
    """The fork's Flask app in a scratch directory, on its own port and DB."""

    def __init__(self, fork: Path, python: str, scratch: Path, name: str,
                 port: int, token_pairs=None):
        self.fork = fork
        self.python = python
        self.name = name
        self.port = port
        self.db_path = scratch / f"{name}.db"
        self.log_path = scratch / f"{name}.log"
        self.url = f"http://127.0.0.1:{port}"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("SOU_") and key not in ("DATABASE_URL", "STAR_BACKEND_PORT")}
        env["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        env["STAR_BACKEND_PORT"] = str(port)
        if token_pairs:
            env["SOU_INGEST_TOKENS"] = ",".join(f"{digest}:{agent}" for digest, agent in token_pairs)
        self.env_has_tokens = bool(token_pairs)
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.process = subprocess.Popen(
            [python, "backend/app.py"], cwd=str(fork), env=env,
            stdout=self._log, stderr=subprocess.STDOUT,
        )

    def wait_ready(self, timeout: float = 30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise SystemExit(f"{self.name}: backend exited early\n{self.tail()}")
            try:
                status, _body = http_get(f"{self.url}/status")
                if status == 200:
                    return
            except Exception:
                time.sleep(0.25)
        raise SystemExit(f"{self.name}: backend never became ready\n{self.tail()}")

    def stored(self, agent: str) -> int:
        """How many events the dashboard holds for ``agent`` (independent oracle)."""
        _status, body = http_get(f"{self.url}/events/recent?agent={agent}&limit=100")
        payload = json.loads(body)
        if isinstance(payload, dict):  # {"items": [...]} shape, or a bare list
            items = payload.get("items") or payload.get("events") or []
        else:
            items = payload
        return len(items)

    def tail(self, lines: int = 15) -> str:
        try:
            return "".join(self.log_path.read_text(encoding="utf-8").splitlines(True)[-lines:])
        except Exception:
            return "<no log>"

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self._log.close()


class LogCapture(logging.Handler):
    """Every log line the reporter emits, so a rejection is observable."""

    def __init__(self, module):
        super().__init__(level=logging.DEBUG)
        self.records = []
        self.logger = logging.getLogger(module.__name__)
        self._previous = self.logger.level
        self.logger.addHandler(self)
        self.logger.setLevel(logging.DEBUG)

    def emit(self, record):
        self.records.append(record.getMessage())

    def text(self) -> str:
        return "\n".join(self.records)

    def close_capture(self):
        self.logger.removeHandler(self)
        self.logger.setLevel(self._previous)


def reporter_for(module, url: str, agent: str, token: str = "", via="setting"):
    """A real reporter instance, wired the way a profile wires it."""
    if via == "env":
        # The whole register() -> PluginContext path, with the token only in the
        # environment (the shape a container gets from SOPS).
        class FakeCtx:
            profile_name = "szarik"

            def get_config(self, key, default=None):
                return {"enabled": True, "dashboard_url": url, "report_tool_calls": True,
                        "report_sessions": True, "report_file_edits": True,
                        "report_commands": True}.get(key, default)

            def register_hook(self, name, fn):
                pass

        previous = os.environ.get("STAR_OFFICE_TOKEN")
        os.environ["STAR_OFFICE_TOKEN"] = token
        try:
            module.register(FakeCtx())
        finally:
            if previous is None:
                os.environ.pop("STAR_OFFICE_TOKEN", None)
            else:
                os.environ["STAR_OFFICE_TOKEN"] = previous
        return module._reporter

    return module.StarOfficeReporter(
        {"enabled": True, "dashboard_url": url, "agent_name": agent, "token": token,
         "report_tool_calls": True, "report_sessions": True, "report_file_edits": True,
         "report_commands": True},
        "szarik",
    )


def run_case(module, backend: Backend, agent: str, label: str, *,
             token: str, via: str, expect: int, expect_stored: bool, timeout: float):
    """Push one event and report what the dashboard did with it."""
    capture = LogCapture(module)
    try:
        before = backend.stored(agent)
        reporter = reporter_for(module, backend.url, agent, token=token, via=via)
        reporter.on_tool_complete("terminal", {"command": "git status"}, "ok", True,
                                  session_id=LOG_PREFIX)
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = capture.text()
            if HTTP_STATUS.search(text) or "Pushed tool_call" in text:
                break
            time.sleep(0.1)
        text = capture.text()
        match = HTTP_STATUS.search(text)
        observed = int(match.group(1)) if match else (201 if "Pushed tool_call" in text else None)
        after = backend.stored(agent)
        stored_delta = after - before
        failure = capture.text() if observed != expect else ""
        return {
            "label": label,
            "expected": expect,
            "observed": observed,
            "stored_delta": stored_delta,
            "ok": observed == expect and (stored_delta > 0) == expect_stored,
            "hint": failure.strip().splitlines()[-1] if failure else "",
            "log": text,
        }
    finally:
        capture.close_capture()
        module._reporter = None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SOU-222 live ingest-token check.")
    parser.add_argument("--fork", default=str(Path.home() / "Projects" / "star-office-ui-fork"),
                        help="path to the star-office-ui-fork checkout (needs backend/app.py + tokens.py)")
    parser.add_argument("--plugin", default=str(ROOT), help="path to this reporter plugin checkout")
    parser.add_argument("--python", default=None,
                        help="interpreter for the backend (default: <fork>/venv/bin/python)")
    parser.add_argument("--agent", default="hermes-szarik",
                        help="the agent the token belongs to, i.e. the name the reporter reports as")
    parser.add_argument("--other-agent", default="live-check-other")
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds to wait per case")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args(argv)

    fork = Path(args.fork).expanduser().resolve()
    plugin = Path(args.plugin).expanduser().resolve()
    for required in (fork / "backend" / "app.py", fork / "backend" / "tokens.py",
                     plugin / "__init__.py"):
        if not required.exists():
            raise SystemExit(f"missing {required}")
    python = args.python or (str(fork / "venv" / "bin" / "python")
                             if (fork / "venv" / "bin" / "python").exists() else sys.executable)
    probe = subprocess.run([python, "-c", "import flask, sqlalchemy; print('deps ok')"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        raise SystemExit(f"{python} cannot run the backend (flask/sqlalchemy): {probe.stderr.strip()}")

    module = load_reporter(plugin)
    scratch = Path(tempfile.mkdtemp(prefix="sou222-live-"))
    backends = []
    failures = []
    print(f"plugin   : {plugin}  (version {_plugin_version(plugin)})")
    print(f"fork     : {fork}")
    print(f"python   : {python}")
    print(f"scratch  : {scratch}{'  (kept: --keep)' if args.keep else ''}")
    print()
    try:
        token, digest = mint_token(fork, python, args.agent)
        other_token, other_digest = mint_token(fork, python, args.other_agent)
        print(f"minted   : token for {args.agent!r} (sha256 {digest_prefix(digest)}…), "
              f"and for {args.other_agent!r} (sha256 {digest_prefix(other_digest)}…)")
        print("           the backend receives only those digests; the plaintext lives in "
              "this process's memory")
        print()

        protected = Backend(fork, python, scratch, "protected", free_port(),
                            token_pairs=[(digest, args.agent), (other_digest, args.other_agent)])
        backends.append(protected)
        protected.wait_ready()

        cases = [
            dict(label=f"token from STAR_OFFICE_TOKEN ({args.agent})", backend=protected,
                 token=token, via="env", expect=201, expect_stored=True),
            dict(label=f"token from the `token` setting ({args.agent})", backend=protected,
                 token=token, via="setting", expect=201, expect_stored=True),
            dict(label="unknown token", backend=protected,
                 token="definitely-not-a-configured-token", via="setting",
                 expect=401, expect_stored=False),
            dict(label=f"another agent's token ({args.other_agent})", backend=protected,
                 token=other_token, via="setting", expect=403, expect_stored=False),
        ]

        # The open box: no SOU_INGEST_TOKENS, so the same reporter must keep working
        # exactly as it does today (P13-G has not deployed tokens yet).
        open_backend = Backend(fork, python, scratch, "open", free_port())
        backends.append(open_backend)
        open_backend.wait_ready()
        cases += [
            dict(label="no token against an un-tokened backend", backend=open_backend,
                 token="", via="setting", expect=201, expect_stored=True),
            dict(label="a token against an un-tokened backend (header ignored)",
                 backend=open_backend, token=token, via="setting",
                 expect=201, expect_stored=True),
        ]

        results = []
        for case in cases:
            backend = case.pop("backend")
            result = run_case(module, backend, args.agent, timeout=args.timeout, **case)
            results.append(result)
            secret_leak = (token in result["log"]) or (other_token in result["log"])
            if secret_leak:
                result["ok"] = False
                result["hint"] = "TOKEN APPEARED IN THE REPORTER'S LOG"

        print(f"{'case':<52} {'want':>5} {'got':>5} {'stored':>7}  verdict")
        print("-" * 90)
        for result in results:
            stored = f"{result['stored_delta']:+d}" if result["stored_delta"] else "0"
            print(f"{result['label']:<52} {result['expected']:>5} "
                  f"{str(result['observed']):>5} {stored:>7}  "
                  f"{'PASS' if result['ok'] else 'FAIL'}")
            if not result["ok"]:
                failures.append(result)
        print()
        for result in failures:
            print(f"FAILED: {result['label']}")
            print(f"        expected HTTP {result['expected']}, observed {result['observed']}, "
                  f"stored delta {result['stored_delta']:+d}")
            if result["hint"]:
                print(f"        reporter said: {result['hint']}")
            print()
        print(f"reporter version {_plugin_version(plugin)}; "
              f"token values printed: 0; tokens on disk: 0")
        return 1 if failures else 0
    finally:
        for backend in backends:
            backend.stop()
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            print(f"scratch kept at {scratch}")


def _plugin_version(plugin: Path) -> str:
    manifest = plugin / "plugin.yaml"
    if not manifest.exists():
        return "unknown"
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
