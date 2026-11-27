"""What an acting rule writes: templates filled in from a line of output.

Output is untrusted. Captured values never become a path of their own (see
_FILE_VALUE) and are refused, not shortened, when they are too long: a
token cut in half would be worse than none.
"""

import re

import pluto_job_defs as d
from pluto_out_fields import QueryError

#: Rule keys of each acting ``then``: what it needs.
NEEDS = {"append": ("file", "text"), "input": ("text",)}

#: Placeholders every line and job has.
BUILT_IN = ("_job", "_name", "_line")

_HOLE = re.compile(r"\{([A-Za-z_]\w*)\}")
_FILE_VALUE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._\-]{0,99}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


class ActionError(Exception):
    """This line cannot be acted on."""


def check(rule):
    """Raises QueryError unless the keys fit the rule's ``then``."""
    then = rule["then"]
    need = NEEDS.get(then, ())
    for key in need:
        if not rule.get(key):
            raise QueryError("watch: then=%s needs %s" % (then, key))
        if len(rule[key]) > d.ACT_TEXT_MAX:
            raise QueryError("watch: %s longer than %d characters" % (key, d.ACT_TEXT_MAX))
    for key in ("file", "text"):
        if rule.get(key) and key not in need:
            raise QueryError("watch: %s goes with then=append%s" % (
                key, " or input" if key == "text" else ""))


def holes(rule):
    """Names of the {placeholders} a rule's action uses, in order."""
    if rule["then"] not in NEEDS:
        return []
    return list(dict.fromkeys(_HOLE.findall(rule.get("file", "") + rule.get("text", ""))))


def check_holes(rule, captured, params):
    """Every placeholder must have a source. *captured*: the names ``match``
    captures, or None when only the line itself can tell."""
    if captured is None:
        return
    known = [*captured, *params, *BUILT_IN]
    for name in holes(rule):
        if name not in known:
            raise QueryError("watch: {%s} is neither captured by match nor a parameter "
                             "of the job; there are: %s" % (name, ", ".join(known)))


def expand(rule, rec, job, text):
    """The action for one line: plain data for pluto_job_act."""
    def value(name):
        v = rec.get(name)
        if v is None:
            v = job.params.get(name)
        if v is None and name in ("_job", "_name"):
            v = job.name if name == "_name" else job.label
        if v is None and name == "_line":
            v = text
        if v is None:
            raise ActionError("the line has no field {%s}" % name)
        return v if type(v) is str else str(v)

    def in_file(m):
        v = value(m[1])
        if not _FILE_VALUE.match(v):
            raise ActionError("{%s}=%r cannot be part of a file name" % (m[1], v[:60]))
        return v

    if len(text) >= d.MATCH_LINE_MAX:
        raise ActionError("the line is too long")     # what it captured may be cut
    out = {"kind": rule["then"]}
    if "file" in NEEDS[rule["then"]]:
        out["file"] = _HOLE.sub(in_file, rule["file"])
    body = _HOLE.sub(lambda m: _CONTROL.sub(" ", value(m[1])), rule["text"])
    if len(body) > d.ACT_TEXT_MAX:
        raise ActionError("text longer than %d characters" % d.ACT_TEXT_MAX)
    out["text"] = body
    return out
