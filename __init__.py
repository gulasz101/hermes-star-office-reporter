"""
Hermes Star Office Reporter Plugin

Auto-reports Hermes tool activity to a Star Office UI dashboard.

Hook registration follows the plugin API: load() receives ctx and calls
ctx.register_hook() to register hooks.

Profile identity (SOU-189): a multiplexed gateway process serves several profiles
from ONE process, so the plugin instance loaded here may see sessions that belong to
a different profile than the one the process launched with. The agent name is
therefore resolved PER SESSION at hook time from the home the routed turn binds
(contextvars), falling back to the profile this instance was loaded from. The
`agent_name` setting, when set, overrides everything.
"""

import importlib
import json
import logging
import os
import re
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- sanitising --
# SOU-192: payload values are *labels for a pixel character*, not content. The
# Claude Code reporter (``reporter/claude-code``, SOU-157/173) already sends a
# program plus at most one allow-listed subcommand and a file *basename*; these
# primitives are ported from it verbatim (same regexes, same tables) so the two
# reporters emit the SAME label for the same command and cannot drift apart.
#
# Why it matters: the v3 renderer sanitises before drawing a bubble, so nothing
# user-visible leaked — but the raw strings sat in the event DB and came back
# from ``GET /events/recent``, which has no auth.

MAX_VALUE_CHARS = 120

# Wrappers that are not the program the agent actually ran.
COMMAND_WRAPPERS = {"sudo", "doas", "env", "time", "nohup", "command", "nice", "timeout"}
# `timeout 300 cmd` / `timeout 5m cmd`: the duration is the wrapper's argument, not the program.
DURATION = re.compile(r"^[0-9]+(\.[0-9]+)?[smhd]?$")
# Directory changes chained in front of the real work (`cd /repo && git push`) are
# skipped; a command that is only a `cd` still reports as `cd`. The directory is
# never sent.
DIR_CHANGERS = {"cd", "pushd"}
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|]")
PYTHONS = re.compile(r"^python[0-9.]*$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PROGRAM = re.compile(r"^[A-Za-z0-9._+-]{1,32}$")
TOKEN_SPLIT = re.compile(r"\s+")

# Programs allowed to carry one subcommand token in the label. Mirrors the
# Claude Code reporter, which mirrors frontend/v3/activity.js.
SUBCOMMAND_PROGRAMS = {
    "git", "npm", "pnpm", "yarn", "docker", "kubectl", "cargo", "go",
    "helm", "flux", "make", "uv", "pip", "poetry",
}
# npm/pnpm/yarn need `run <script>` as TWO tokens: activity.js computes the
# runner verb from the token after "run", so a lone "npm run" would classify
# `npm run build` as ops instead of the garage.
RUNNER_PROGRAMS = {"npm", "pnpm", "yarn"}
# A subcommand is a bare lowercase identifier, never a flag, path, URL,
# key=value pair or value. Anything that does not match is skipped and not
# echoed — this regex is the whole privacy boundary of the feature.
SUBCOMMAND = re.compile(r"^[a-z][a-z0-9-]{0,20}$")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def text_of(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    """A trimmed, control-character-free string, or "" for anything else."""
    if isinstance(value, str):
        return _CONTROL_CHARS.sub(" ", value).strip()[:limit]
    return ""


def basename(value: Any) -> str:
    """Last path segment only — the whole path is the thing we must not send."""
    raw = text_of(value, 1024)
    if not raw:
        return ""
    normalised = raw.replace("\\", "/")
    parts = [part for part in normalised.split("/") if part not in ("", ".")]
    if not parts:
        return ""
    last = parts[-1]
    if last in {"..", "/"} or "\x00" in last:
        return ""
    # A drive letter or a leftover separator means we did not get a basename.
    if ":" in last or "/" in last or "\\" in last:
        return ""
    return last[:MAX_VALUE_CHARS]


def command_parts(command: Any):
    """First real command -> ``(program, [arguments after it])``.

    Leading ``cd``/``pushd`` segments are skipped when something follows them.
    Env prefixes (``FOO=bar``), wrapper tokens (``sudo``, ``env``, ``timeout 300`` …)
    and flags are skipped while finding the program; ``python -m <module>`` reports
    the module. Nothing usable -> ``("shell", [])``.
    """
    raw = text_of(command, 4096)
    if not raw:
        return "", []
    segments = [seg for seg in SEGMENT_SPLIT.split(raw) if seg.strip()]
    parsed = None
    for seg in segments:
        parsed = _segment_parts(seg)
        if parsed[0] not in DIR_CHANGERS:
            return parsed
    return parsed or ("shell", [])


def _segment_parts(segment: str):
    tokens = [token for token in TOKEN_SPLIT.split(segment.strip()) if token]
    skip_duration = False
    for index, token in enumerate(tokens):
        if skip_duration and DURATION.match(token):
            skip_duration = False
            continue
        if ASSIGNMENT.match(token) or token.startswith("-"):
            continue  # env assignment or a flag of the wrapper
        name = basename(token.replace("\\", "/")) or token
        name = name.split("/")[-1]
        if name in COMMAND_WRAPPERS:
            skip_duration = name == "timeout"
            continue
        if not PROGRAM.match(name) or ":" in name:
            return "shell", []
        rest = tokens[index + 1:]
        if PYTHONS.match(name) and len(rest) >= 2 and rest[0] == "-m" and PROGRAM.match(rest[1]):
            return rest[1], rest[2:]
        return name, rest
    return "shell", []


def program(command: Any) -> str:
    """Program name of a shell command: first segment, first real token.

    Never arguments, never a path. `sudo -u x npm i` -> `npm`;
    `FOO=bar ./bin/run.sh -v` -> `run.sh`; nothing usable -> `shell`.
    """
    return command_parts(command)[0]


def command_label(command: Any) -> str:
    """Privacy-safe label: the program plus ONE allow-listed subcommand (SOU-192).

    ``git push origin main`` -> ``git push``, ``docker build -t x .`` ->
    ``docker build``, ``npm run build`` -> ``npm run build`` (runner programs keep
    the script verb, two tokens). The subcommand is the first argument that is a
    bare lowercase identifier; flags and value-ish tokens (``key=value``, paths,
    URLs, uppercase) are skipped and never echoed. Programs outside
    ``SUBCOMMAND_PROGRAMS`` keep program-name-only behaviour, so
    ``git -c user.password=SECRET push`` -> ``git push`` and nothing else about
    that command leaves the machine.
    """
    prog, args = command_parts(command)
    if not prog or prog.lower() not in SUBCOMMAND_PROGRAMS:
        return prog
    picked = []
    for token in args:
        if token.startswith("-") or not SUBCOMMAND.match(token):
            continue  # a flag or a value: never a subcommand, never echoed
        picked.append(token)
        want = 2 if prog.lower() in RUNNER_PROGRAMS and picked[0] == "run" else 1
        if len(picked) >= want:
            break
    return " ".join([prog] + picked)


def _profile_from_home(home: Any) -> Optional[str]:
    """Profile name a Hermes home path stands for: ``"default"``, ``"<name>"`` or ``None``.

    Mirrors ``hermes_cli.profiles.get_active_profile_name()``'s rule (the root home is
    ``default``; ``<root>/profiles/<name>`` is that profile) without importing it, so the
    plugin keeps working if the internals move.
    """
    if not home:
        return None
    try:
        path = Path(str(home)).expanduser().resolve()
    except Exception:
        return None
    root: Optional[Path] = None
    try:
        from hermes_constants import get_default_hermes_root  # type: ignore

        root = Path(get_default_hermes_root()).expanduser().resolve()
    except Exception:
        root = None
    if root is None:
        env_root = (os.environ.get("HERMES_HOME") or "").strip()
        root = Path(env_root).expanduser().resolve() if env_root else (Path.home() / ".hermes")
    if path == root:
        return "default"
    try:
        relative = path.relative_to(root / "profiles")
    except ValueError:
        return None
    return relative.parts[0] if relative.parts else None


def _bound_profile_home() -> Optional[str]:
    """Home the CURRENT session is scoped to, from the signals a routed turn binds.

    A multiplexed gateway scopes a routed turn by binding the profile's home (and its
    secret scope) as contextvars; an unscoped session falls through to the process's own
    home. First hit wins, ``get_hermes_home`` last.
    """
    for module_name, attribute in (
        ("hermes_constants", "get_hermes_home_override"),
        ("agent.secret_scope", "current_secret_scope_home"),
        ("hermes_constants", "get_hermes_home"),
    ):
        try:
            value = getattr(importlib.import_module(module_name), attribute)()
        except Exception:
            continue
        if value:
            return str(value)
    return None


def _session_profile(load_profile: Optional[str]) -> Optional[str]:
    """Profile the current session runs FOR, else the profile this instance was loaded from.

    The env pins (``HERMES_PROFILE_NAME``/``HERMES_PROFILE``) are consulted only when the
    bound home says nothing: ``os.environ`` belongs to the LAUNCH profile, so under a
    routed turn it would relabel another profile's work as the host's.
    """
    profile = _profile_from_home(_bound_profile_home())
    if profile:
        return profile
    for env_name in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value
    return load_profile


class StarOfficeReporter:
    """Reports Hermes events to Star Office UI dashboard."""

    def __init__(self, config: Dict[str, Any], profile_name: Optional[str] = None):
        """Initialize reporter with config from the plugin settings block."""
        self.enabled = config.get("enabled", False)
        self.dashboard_url = config.get("dashboard_url", "")

        # Profile this plugin instance was loaded from; the per-session resolution
        # below overrides it whenever the session's own profile can be told.
        self.profile_name = profile_name or "default"

        configured_agent_name = str(config.get("agent_name") or "").strip()
        self._agent_name_override = configured_agent_name
        self.agent_name = configured_agent_name or f"hermes-{self.profile_name}"

        self.report_tool_calls = config.get("report_tool_calls", True)
        self.report_file_edits = config.get("report_file_edits", True)
        self.report_commands = config.get("report_commands", True)
        self.report_sessions = config.get("report_sessions", True)
        self.timeout = config.get("timeout_seconds", 5)
        self.retry = config.get("retry_on_failure", False)

        # Sessions already logged, so the resolution is reported once per session
        # without flooding the log at tool-call rate.
        self._seen_sessions: Dict[str, str] = {}
        self._seen_lock = threading.Lock()

        # Privacy filters: never report these tool names
        self._secret_tools = {
            "browser_vault_fill",
            "browser_vault_save_login",
            "browser_vault_enter_code",
        }

        if self.enabled and not self.dashboard_url:
            logger.warning("star_office enabled but dashboard_url not set")
            self.enabled = False

    def agent_name_for_session(self, session_id: Optional[str] = None) -> str:
        """Agent name this session's events are reported under.

        Precedence: explicit ``agent_name`` setting > the profile this session runs for
        (resolved per session, so a multiplexed gateway's routed turns report as their
        own agent) > the profile this plugin instance was loaded from.
        """
        if self._agent_name_override:
            return self._agent_name_override
        profile = _session_profile(self.profile_name)
        name = f"hermes-{profile}" if profile else self.agent_name

        key = str(session_id or "")
        with self._seen_lock:
            first = key not in self._seen_sessions
            if first:
                self._seen_sessions[key] = name
                if len(self._seen_sessions) > 512:  # bound the bookkeeping
                    self._seen_sessions.pop(next(iter(self._seen_sessions)))
        if first:
            logger.info(
                "star_office: session %s reports as %s (plugin profile: %s)",
                key or "<none>", name, self.profile_name,
            )
        return name

    def on_tool_complete(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: Any,
        success: bool,
        session_id: Optional[str] = None,
    ):
        """Called after every tool execution."""
        if not self.enabled:
            return

        # Privacy: never report vault operations
        if tool_name in self._secret_tools:
            logger.debug(f"Skipping {tool_name} (vault operation)")
            return

        # Filter by config
        if tool_name == "terminal" and not self.report_commands:
            return
        if tool_name in ("write_file", "patch") and not self.report_file_edits:
            return
        if not self.report_tool_calls:
            return

        # Build payload. Values are labels, never content (SOU-192): a terminal
        # command is reduced to program + one allow-listed subcommand, a file to
        # its basename. Both are the same rules the Claude Code reporter sends,
        # and neither contains a path.
        payload = {
            "tool": tool_name,
            "success": success,
        }

        if tool_name == "read_file":
            name = basename(args.get("path"))
            if name:
                payload["path"] = name
        elif tool_name in ("write_file", "patch"):
            name = basename(args.get("path"))
            if name:
                payload["path"] = name
            content = args.get("content")
            payload["bytes"] = len(content) if isinstance(content, str) else 0
        elif tool_name == "terminal":
            payload["command"] = command_label(args.get("command")) or "shell"
        # Anything else (web_search, browser_navigate, MCP tools, …) reports the
        # tool name and status only: /events/recent has no auth, and the renderer
        # (frontend/v3/activity.js) reads no other field — a search query or a
        # navigation URL is user content with no consumer.

        self._push_event("tool_call", payload, session_id)

    def on_session_start(self, session_id: Optional[str] = None):
        """Called when Hermes session starts."""
        if self.enabled and self.report_sessions:
            self._push_event("session_start", {}, session_id)

    def on_session_end(self, session_id: Optional[str] = None):
        """Called when Hermes session ends."""
        if self.enabled and self.report_sessions:
            self._push_event("session_end", {}, session_id)

    def _push_event(
        self, event_type: str, payload: Dict[str, Any], session_id: Optional[str] = None
    ):
        """Push event to dashboard (non-blocking)."""
        agent_name = self.agent_name_for_session(session_id)

        def _do_push():
            try:
                event = {
                    "version": 1,
                    "agent": agent_name,
                    "event_type": event_type,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": payload,
                }

                url = f"{self.dashboard_url.rstrip('/')}/events"
                data = json.dumps(event).encode('utf-8')
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp.read()
                logger.debug(f"Pushed {event_type} to {url}")

            except urllib.error.HTTPError as e:
                logger.warning(f"Failed to push {event_type} to dashboard: HTTP {e.code}")
            except urllib.error.URLError as e:
                logger.warning(f"Failed to push {event_type} to dashboard: {e.reason}")
            except Exception as e:
                logger.error(f"Unexpected error pushing {event_type}: {e}")

        # Push in background thread to not block Hermes
        threading.Thread(target=_do_push, daemon=True).start()


# Singleton reporter instance (set by register())
_reporter: Optional[StarOfficeReporter] = None


def _on_tool_complete(tool_name: str = "", args: Any = None, result: Any = None,
                      success: bool = True, session_id: str = "", **kwargs):
    """Hook callback for tool completion."""
    if _reporter:
        _reporter.on_tool_complete(tool_name, args or {}, result, success, session_id=session_id)


def _on_session_start(session_id: str = "", **kwargs):
    """Hook callback for session start."""
    if _reporter:
        _reporter.on_session_start(session_id)


def _on_session_end(session_id: str = "", **kwargs):
    """Hook callback for session end."""
    if _reporter:
        _reporter.on_session_end(session_id)


def register(ctx: Any):
    """
    Register hooks with Hermes.

    Hermes calls this on startup with the plugin context (ctx).

    Config contract (verified against hermes_cli/plugins.py + plugins_loader.py,
    2026-10-15): PluginContext has NO `.config` attribute. Plugin settings live at
    ``plugins.entries.<plugin_id>.settings.<key>`` and are read with
    ``ctx.get_config(key, default)`` — that is also what the manifest's
    ``config_schema`` is validated against. A top-level ``star_office:`` block is
    NOT visible to the plugin at all, which silently disabled the reporter
    (readiness was fine, zero events were reported).
    """
    global _reporter

    def _cfg(key: str, default: Any = None) -> Any:
        getter = getattr(ctx, "get_config", None)
        if callable(getter):
            value = getter(key, None)
            if value is not None:
                return value
        # Legacy fallback: top-level `star_office:` block (pre-settings layout).
        legacy = getattr(ctx, "config", None)
        if isinstance(legacy, dict):
            section = legacy.get("star_office") or {}
            if isinstance(section, dict) and key in section:
                return section[key]
        return default

    profile_name = getattr(ctx, "profile_name", None)

    star_office_config: Dict[str, Any] = {
        "enabled": _cfg("enabled", False),
        "dashboard_url": _cfg("dashboard_url", ""),
        "report_tool_calls": _cfg("report_tool_calls", True),
        "report_file_edits": _cfg("report_file_edits", True),
        "report_commands": _cfg("report_commands", True),
        "report_sessions": _cfg("report_sessions", True),
        "timeout_seconds": _cfg("timeout_seconds", 5),
        "retry_on_failure": _cfg("retry_on_failure", False),
    }
    # Only forward agent_name when it is actually configured — passing None would
    # defeat the reporter's own default (`hermes-<profile>`).
    agent_name = _cfg("agent_name", None)
    if agent_name:
        star_office_config["agent_name"] = agent_name

    _reporter = StarOfficeReporter(star_office_config, profile_name)

    if not _reporter.enabled:
        logger.info("Star Office Reporter disabled")
        return

    # Register hooks. Hook names must be Hermes hook names (hermes_cli/plugins.py
    # VALID_HOOKS) — `on_tool_complete` is the internal callback, not a hook, and
    # declaring an unknown hook in provides_hooks fails `hermes plugins doctor`.
    ctx.register_hook("post_tool_call", _on_tool_complete)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)

    logger.info(f"Star Office Reporter enabled: {_reporter.dashboard_url} (agent: {_reporter.agent_name})")
