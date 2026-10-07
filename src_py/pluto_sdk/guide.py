"""Render the agent guide (``agent_guide_template.md``) for a server."""

import datetime
import os

from pluto_client_def import (
    DEFAULT_GUIDE_OUTPUT_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
    GUIDE_TEMPLATE_RELATIVE,
)

#: The template lives in ``src_py/``, one level above this package.
_SRC_PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def generate_agent_guide(
    output_path: str = DEFAULT_GUIDE_OUTPUT_PATH,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """
    Render the agent guide template and write it to output_path.

    Substitutes {{host}}, {{port}}, and {{generated_at}} in the template.
    Creates intermediate directories as needed.

    Returns the rendered guide content (also printed to stdout so the
    calling agent can read it directly).
    """
    template_path = os.path.normpath(os.path.join(_SRC_PY_DIR, GUIDE_TEMPLATE_RELATIVE))

    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    content = content.replace("{{generated_at}}", now)
    content = content.replace("{{host}}", host)
    content = content.replace("{{port}}", str(port))

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return content
