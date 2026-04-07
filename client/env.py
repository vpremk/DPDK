"""
env.py — shared environment config loader for client scripts.

Reads from (in priority order):
  1. Process environment variables (already exported via `source .env`)
  2. ../.env file (auto-loaded via python-dotenv if installed, else manual parse)

Usage:
    from env import EMS_HOST, EMS_PORT, FIX_PORT, MKTDATA_PORT
"""

from __future__ import annotations
import os
from pathlib import Path

def _load_dotenv(path: Path) -> None:
    """Minimal .env parser — no dependency on python-dotenv."""
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().split("#")[0].strip()  # strip inline comments
            # Expand ${VAR} references already set in the env dict
            for k, v in os.environ.items():
                val = val.replace(f"${{{k}}}", v)
            if key and key not in os.environ:          # don't override real env vars
                os.environ[key] = val

# Load .env from project root (one level up from client/)
_load_dotenv(Path(__file__).parent.parent / ".env")

MAC_A_IP     = os.environ.get("MAC_A_IP",     "192.168.1.100")
MAC_B_IP     = os.environ.get("MAC_B_IP",     "192.168.1.165")
EMS_HOST     = os.environ.get("EMS_HOST",     MAC_B_IP)
FIX_PORT     = int(os.environ.get("FIX_PORT",     "4567"))
MKTDATA_PORT = int(os.environ.get("MKTDATA_PORT", "5678"))
EMS_PORT     = int(os.environ.get("EMS_PORT",     str(FIX_PORT)))
