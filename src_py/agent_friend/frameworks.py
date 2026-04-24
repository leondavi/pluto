"""Agent framework detection, role discovery, and Pluto server status/config helpers."""

import json
import os
import shutil
import urllib.request

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Default library/roles directory: two levels up from src_py/agent_friend/
_DEFAULT_ROLES_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "library", "roles")
)


KNOWN_FRAMEWORKS = {
    "claude": {
        "cmd_names": ["claude"],
        "display": "Claude Code",
        "default_args": [],
        "ready_pattern": None,  # Claude uses silence timeout
        # ``claude --model <name>`` (e.g. ``claude-sonnet-4-5``).
        "model_flag": "--model",
    },
    "copilot": {
        "cmd_names": ["copilot"],
        "display": "GitHub Copilot CLI",
        "default_args": [],
        "ready_pattern": r"Describe a task to get started",
        # ``copilot --model <name>`` (e.g. ``gpt-5.2``, ``claude-sonnet-4.5``,
        # ``claude-haiku-4.5``).
        "model_flag": "--model",
    },
    "aider": {
        "cmd_names": ["aider"],
        "display": "Aider",
        "default_args": [],
        "ready_pattern": r"^[>›] $",
        # ``aider --model <name>`` (e.g. ``sonnet``, ``gpt-4o``).
        "model_flag": "--model",
    },
    "cursor": {
        "cmd_names": ["cursor"],
        "display": "Cursor",
        "default_args": [],
        "ready_pattern": None,
        # Cursor CLI does not expose a stable model flag in interactive mode.
        "model_flag": None,
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


def get_framework_cmd(framework: str, model: str | None = None) -> list[str]:
    """Return the command list for a known framework, or ``[framework]``.

    If *model* is supplied and the framework declares a ``model_flag``,
    ``[<flag>, <model>]`` is appended so the underlying CLI starts with the
    requested model.  Unknown frameworks fall back to ``[framework]`` (with
    ``--model <model>`` appended when *model* is given, since most modern
    agent CLIs accept that convention).
    """
    info = KNOWN_FRAMEWORKS.get(framework)
    if not info:
        cmd = [framework]
        if model:
            cmd += ["--model", model]
        return cmd
    cmd = info["cmd_names"][:1] + list(info["default_args"])
    if model:
        flag = info.get("model_flag")
        if flag:
            cmd += [flag, model]
    return cmd


def get_framework_model_flag(framework: str) -> str | None:
    """Return the CLI flag a framework uses to select its model, or ``None``."""
    info = KNOWN_FRAMEWORKS.get(framework)
    return info.get("model_flag") if info else None


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


# ── Role discovery ─────────────────────────────────────────────────────────────

def list_roles(roles_dir: str | None = None) -> list[dict]:
    """
    Scan *roles_dir* for ``*.md`` files and return a list of dicts:
    ``{"name": str, "path": str}`` sorted alphabetically by name.

    If *roles_dir* is omitted the default ``library/roles/`` directory
    relative to the project root is used.  Returns an empty list when the
    directory does not exist or contains no ``*.md`` files.
    """
    directory = roles_dir or _DEFAULT_ROLES_DIR
    if not os.path.isdir(directory):
        return []
    roles = []
    for entry in sorted(os.listdir(directory)):
        if entry.endswith(".md"):
            name = entry[:-3]  # strip .md
            roles.append({"name": name, "path": os.path.join(directory, entry)})
    return roles


def load_role(path: str) -> str:
    """
    Read and return the content of a role file.

    Raises ``FileNotFoundError`` if *path* does not exist.
    """
    with open(path, encoding="utf-8") as f:
        return f.read()
