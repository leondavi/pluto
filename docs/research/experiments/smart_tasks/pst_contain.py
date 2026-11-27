"""Keeps agents from signalling anything but their own experiment.

Agents run as the user of the machine. One of them typed
``pkill -f judge.py -P 1``; pkill stops reading options at the pattern, so it
matched every process with a "1" in its command line, a teammate's session,
the Pluto server and the owner's database among them. Two measures, the same
in every arm:

* the whole runner re-executes itself in a macOS sandbox whose processes can
  signal only each other;
* ``pkill`` and ``killall`` on the agents' PATH spare the sessions of the
  agents and the Pluto server, which on a real team would belong to others.
"""

import os
import shutil
import stat
import sys

#: ps and top are set-uid, which a sandbox may not execute; they only read,
#: so they run outside it.
PROFILE = ("(version 1)(allow default)(deny signal)(allow signal (target same-sandbox))"
           '(allow process-exec (with no-sandbox) (literal "/bin/ps") (literal "/usr/bin/top"))')
MARK = "PST_CONTAINED"
SANDBOX = "/usr/bin/sandbox-exec"

#: Candidates come from pgrep with the caller's own arguments, so that a
#: pattern means exactly what it means to pkill.
SHIM = r"""#!/bin/bash
# {name}: as usual, but never an agent's session or the Pluto server.
sig=-TERM; args=()
for a in "$@"; do
  case "$a" in
    -[0-9]|-[0-9][0-9]|-HUP|-INT|-QUIT|-KILL|-TERM|-USR1|-USR2|-STOP|-CONT|-SIG[A-Z]*) sig=$a;;
    *) args+=("$a");;
  esac
done
{find}
[ -z "$pids" ] && exit 1
# Never this command or its callers: BSD pgrep leaves them out, Linux pgrep only
# itself, and this script's own command line holds the pattern.
anc=" $$ "; q=$$
while [ "${q:-1}" -gt 1 ]; do
  q=$(ps -o ppid= -p "$q" 2>/dev/null | tr -d ' '); anc="$anc$q "
done
hit=1
for p in $pids; do
  case "$anc" in *" $p "*) continue;; esac
  c=$(ps -ww -o command= -p "$p" 2>/dev/null) || continue
  case "$c" in
    *"claude -p "*|*beam.smp*|*agent_smart_tasks/smart_tasks.py*) continue;;
  esac
  kill "$sig" "$p" 2>/dev/null && hit=0
done
exit $hit
"""
FIND = {"pkill": 'pids=$(/usr/bin/pgrep "${args[@]}")',
        "killall": 'pids=$(for n in "${args[@]}"; do /usr/bin/pgrep -x -- "$n"; done)'}


def reexec(allow_uncontained=False):
    """Continue inside the sandbox; returns only there."""
    if os.environ.get(MARK):
        return
    if sys.platform != "darwin" or not os.path.exists(SANDBOX):
        if allow_uncontained:
            return
        sys.exit("no containment for this platform: agents could signal any process of "
                 "yours. Use a machine or account of its own and --allow-uncontained.")
    os.environ[MARK] = "1"
    sys.stdout.flush()
    os.execv(SANDBOX, [SANDBOX, "-p", PROFILE, sys.executable, *sys.argv])


def install(directory):
    """Write the guarded commands; returns the directory for PATH."""
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory)
    for name, find in FIND.items():
        path = os.path.join(directory, name)
        with open(path, "w") as f:
            f.write(SHIM.replace("{name}", name).replace("{find}", find))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory
