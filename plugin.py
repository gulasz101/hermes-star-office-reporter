"""
Hermes Star Office Reporter Plugin

Auto-reports Hermes tool activity to a Star Office UI dashboard.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)


class StarOfficeReporter:
    """Reports Hermes events to Star Office UI dashboard."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize reporter with config from ~/.hermes/config.yaml."""
        self.enabled = config.get("enabled", False)
        self.dashboard_url = config.get("dashboard_url", "")
        self.agent_name = config.get("agent_name", f"hermes-{os.getenv('USER', 'unknown')}")
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
        try:
            event = {
                "version": 1,
                "agent": self.agent_name,
                "event_type": event_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }

            url = f"{self.dashboard_url.rstrip('/')}/events"
            resp = requests.post(
                url, json=event, timeout=self.timeout, headers={"Content-Type": "application/json"}
            )
            resp.raise_for_status()
            logger.debug(f"Pushed {event_type} to {url}")

        except requests.RequestException as e:
            logger.warning(f"Failed to push {event_type} to dashboard: {e}")
            if self.retry:
                # Optional: implement retry logic here
                pass
        except Exception as e:
            logger.error(f"Unexpected error pushing {event_type}: {e}")


# Plugin entry point
def load(hermes_config: Dict[str, Any]) -> StarOfficeReporter:
    """
    Load plugin and return reporter instance.
    
    Hermes calls this on startup with the full config dict.
    Returns an object with hook methods (on_tool_complete, etc.)
    """
    star_office_config = hermes_config.get("star_office", {})
    reporter = StarOfficeReporter(star_office_config)
    
    if reporter.enabled:
        logger.info(f"Star Office Reporter enabled: {reporter.dashboard_url}")
    else:
        logger.info("Star Office Reporter disabled")
    
    return reporter
