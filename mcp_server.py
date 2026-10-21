"""Compatibility executable for the MCP subprocess."""
import sys
from backend.app.mcp import server as _server
sys.modules[__name__] = _server
