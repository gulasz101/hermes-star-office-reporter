#!/usr/bin/env python3
"""Quick integration test: push test event to local dashboard."""

import json
import sys
from datetime import datetime, timezone

import requests

def test_push_event():
    """Push a test event and verify 200 response."""
    event = {
        "version": 1,
        "agent": "hermes-szarik",
        "event_type": "session_start",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {"test": "plugin_integration"}
    }
    
    url = "http://localhost:19000/events"
    resp = requests.post(url, json=event, timeout=5)
    resp.raise_for_status()
    
    print(f"✓ Event pushed: {resp.status_code}")
    print(f"✓ Response: {resp.json()}")
    return True

if __name__ == "__main__":
    try:
        test_push_event()
    except Exception as e:
        print(f"✗ Failed: {e}", file=sys.stderr)
        sys.exit(1)
