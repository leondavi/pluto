"""Agent framework detection + Pluto server status/config helpers."""

import json
import os
import shutil
import urllib.request

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))


KNOWN_FRAMEWORKS = {
    "claude": {
        "cmd_names": ["claude"],
        "display": "Claude Code",
        "default_args": [],
        "ready_pattern": None,  # Claude uses silence timeout
    },
    "copilot": {
        "cmd_names": ["copilot"],
        "display": "GitHub Copilot CLI",
        "default_args": [],
        "ready_pattern": r"Describe a task to get started",
    },
    "aider": {
        "cmd_names": ["aider"],
        "display": "Aider",
        "default_args": [],
        "ready_pattern": r"^[>›] $",
    },
    "cursor": {
        "cmd_names": ["cursor"],
        "display": "Cursor",
        "default_args": [],
        "ready_pattern": None,
    },
}


def detect_available_frameworks() -> list[dict]:
    """Scan ``$PATH`` for known agent framework executables."""
    available = []
    for key, info in KNOWN_FRAMEWORKS.items():
        for cmd_name in info["cmd_names"]:
            path = shutil.which(cmd_name)
            if path:
                available.append({
                    "key": key,
                    "display": info["display"],
                    "cmd": cmd_name,
                    "path": path,
                })
                break
    return available


def get_framework_cmd(framework: str) -> list[str]:
    """Return the command list for a known framework, or ``[framework]``."""
    info = KNOWN_FRAMEWORKS.get(framework)
    if not info:
        return [framework]
    return info["cmd_names"][:1] + info["default_args"]


def get_framework_ready_pattern(framework: str) -> str | None:
    """Return the prompt-ready regex for a framework, or ``None``."""
    info = KNOWN_FRAMEWORKS.get(framework)
    return info["ready_pattern"] if info else None


def check_pluto_status(host: str, http_port: int) -> dict | None:
    """Hit Pluto's ``/health`` endpoint; return dict or ``None`` if unreachable."""
    try:
        url = f"http://{host}:{http_port}/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def load_pluto_config() -> dict:
    """Load ``config/pluto_config.json`` from the project root."""
    candidates = [
        os.path.normpath(
            os.path.join(_THIS_DIR, "..", "..", "config", "pluto_config.json")
        ),
        os.path.normpath(
            os.path.join(os.getcwd(), "config", "pluto_config.json")
        ),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
    return {}
