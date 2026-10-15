"""
Hermes Star Office Reporter Plugin

Auto-reports Hermes tool activity to a Star Office UI dashboard.

Hook registration follows Ponytail's pattern: load() receives ctx and calls
ctx.register_hook() to dynamically register hooks.
"""

import json
import logging
import os
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class StarOfficeReporter:
    """Reports Hermes events to Star Office UI dashboard."""

    def __init__(self, config: Dict[str, Any], profile_name: Optional[str] = None):
        """Initialize reporter with config from ~/.hermes/config.yaml."""
        self.enabled = config.get("enabled", False)
        self.dashboard_url = config.get("dashboard_url", "")
        
        # Auto-detect agent name: hermes-{profile_name} or env fallback
        default_agent = f"hermes-{profile_name}" if profile_name else f"hermes-{os.getenv('USER', 'unknown')}"
        self.agent_name = config.get("agent_name", default_agent)
        self.report_tool_calls = config.get("report_tool_calls", True)
        self.report_file_edits = config.get("report_file_edits", True)
        self.report_commands = config.get("report_commands", True)
        self.report_sessions = config.get("report_sessions", True)
        self.timeout = config.get("timeout_seconds", 5)
        self.retry = config.get("retry_on_failure", False)

        # Privacy filters: never report these tool names
        self._secret_tools = {
            "browser_vault_fill",
            "browser_vault_save_login",
            "browser_vault_enter_code",
        }

        if self.enabled and not self.dashboard_url:
            logger.warning("star_office enabled but dashboard_url not set")
            self.enabled = False

    def on_tool_complete(
        self, tool_name: str, args: Dict[str, Any], result: Any, success: bool
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

        # Build payload (sanitized)
        payload = {
            "tool": tool_name,
            "success": success,
        }

        # Add safe args (never credentials)
        if tool_name == "read_file":
            payload["path"] = args.get("path", "")
        elif tool_name in ("write_file", "patch"):
            payload["path"] = args.get("path", "")
            payload["bytes"] = len(args.get("content", ""))
        elif tool_name == "terminal":
            cmd = args.get("command", "")
            # Truncate long commands
            payload["command"] = cmd[:100] + "..." if len(cmd) > 100 else cmd
        elif tool_name == "web_search":
            payload["query"] = args.get("query", "")
        elif tool_name == "browser_navigate":
            payload["url"] = args.get("url", "")

        self._push_event("tool_call", payload)

    def on_session_start(self):
        """Called when Hermes session starts."""
        if self.enabled and self.report_sessions:
            self._push_event("session_start", {})

    def on_session_end(self):
        """Called when Hermes session ends."""
        if self.enabled and self.report_sessions:
            self._push_event("session_end", {})

    def _push_event(self, event_type: str, payload: Dict[str, Any]):
        """Push event to dashboard (non-blocking)."""
        def _do_push():
            try:
                event = {
                    "version": 1,
                    "agent": self.agent_name,
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


# Singleton reporter instance (set by load())
_reporter: Optional[StarOfficeReporter] = None


def _on_tool_complete(tool_name: str = "", args: Any = None, result: Any = None, success: bool = True, **kwargs):
    """Hook callback for tool completion."""
    if _reporter:
        _reporter.on_tool_complete(tool_name, args or {}, result, success)


def _on_session_start(**kwargs):
    """Hook callback for session start."""
    if _reporter:
        _reporter.on_session_start()


def _on_session_end(**kwargs):
    """Hook callback for session end."""
    if _reporter:
        _reporter.on_session_end()


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
