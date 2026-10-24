"""Drive Ableton Live through the ClaudeLiveBridge Remote Script."""

from .client import BRIDGE_NAME, BridgeError, LiveClient, connect

__all__ = ["BRIDGE_NAME", "BridgeError", "LiveClient", "connect"]
