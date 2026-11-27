"""The three SmartTasks definition files must agree."""

import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src_py"))
sys.path.insert(0, os.path.join(ROOT, "src_erl", "priv"))

import pluto_job_defs as priv  # noqa: E402
import pluto_jobs_def as client  # noqa: E402


def hrl(name):
    with open(os.path.join(ROOT, "src_erl", "include", name)) as f:
        return dict(re.findall(r"-define\((\w+),\s*(.*?)\)\.", f.read(), re.S))


ERL = {**hrl("pluto_jobs.hrl"), **hrl("pluto_protocol.hrl")}


def text(name):
    return re.fullmatch(r'(?:<<)?"(.*)"(?:>>)?', ERL[name].strip()).group(1)


def strings(name):
    out = []
    for item in re.findall(r'\?(\w+)|<<"([^"]*)">>', ERL[name]):
        out.append(text(item[0]) if item[0] else item[1])
    return tuple(out)


def test_formats_and_limits():
    for erl, value in (("JOB_CHUNK_BYTES", priv.CHUNK),
                       ("JOB_RECORD_BYTES", priv.RECORD),
                       ("JOB_SCHEMA", priv.SCHEMA),
                       ("DEFAULT_JOB_MIN_FREE_BYTES", priv.MIN_FREE),
                       ("STREAM_LOG_MAX", priv.STREAM_LOG_MAX),
                       ("STREAM_MAX", priv.STREAM_MAX),
                       ("JOB_AGENT_ID_MAX", priv.AGENT_ID_MAX),
                       ("JOB_SHORT_ID_LEN", client.SHORT_ID_LEN),
                       ("JOB_READ_DEFAULT", client.READ_DEFAULT),
                       ("JOB_READ_MAX", client.READ_MAX),
                       ("GREP_DEFAULT_HITS", client.GREP_MATCHES),
                       ("JOB_WAIT_MAX_MS", client.WAIT_MAX_S * 1000),
                       ("JOB_WAIT_DEFAULT_MS", client.WAIT_DEFAULT_S * 1000)):
        assert int(ERL[erl]) == value, erl
    # The server waits a little longer than the helper, the client longer still.
    assert int(ERL["ANALYZE_DEADLINE_MS"]) == priv.ANALYZE_DEADLINE_S * 1000
    assert client.ANALYZE_TIMEOUT_S * 1000 > sum(int(ERL[f"ANALYZE_{k}_MS"]) for k in
                                                 ("DEADLINE", "SLACK", "QUEUE"))
    assert int(ERL["WATCH_RULES_MAX"]) == priv.WATCH_RULES_MAX
    assert int(ERL["WATCH_EXIT_MEMORY"]) == priv.EXIT_MEMORY
    assert int(ERL["WATCH_SILENT_MS"]) > 5000 * priv.WATCH_HEARTBEAT_S
    unescape = lambda s: s.replace("\\\\", "\\").lower()  # noqa: E731
    assert unescape(text("JOB_ID_RE")) == priv.JOB_ID.lower()
    assert unescape(text("STREAM_ID_RE")) == priv.STREAM_ID.lower()


def test_file_names():
    for name in ("PACKAGE", "STATE", "OUTPUT", "MANIFEST", "CURSORS", "REVIEWS",
                 "STREAMS", "WATCH", "HB", "ABORT"):
        assert text(f"JOB_FILE_{name}") == getattr(priv, f"FILE_{name}")
    assert text("JOB_DIR_CONTROL") == priv.DIR_CONTROL
    assert text("JOB_DIR_STREAMS") == priv.DIR_STREAMS
    assert text("HOME_FILE_KEY") == client.KEY_FILE


def test_enumerations():
    states = {text(k) for k in ERL if re.fullmatch(
        r"JOB_(QUEUED|STARTING|RUNNING|PAUSED|SUCCEEDED|FAILED|CANCELLED|"
        r"SIGNALED|TIMED_OUT|OUTPUT_LIMIT|ORPHANED|LOST|SNAPSHOT)", k)}
    assert len(states) == 13
    assert states == set(client.FINAL_STATES) | set(client.LIVE_STATES) | {"snapshot"}
    assert set(client.FINAL_STATES) == set(priv.FINAL) | {"lost"}
    assert set(client.LIVE_STATES) == set(priv.LIVE) | {"queued", "orphaned"}
    assert strings("JOB_SIGNALS") == priv.SIGNALS == client.SIGNALS
    assert strings("FOLLOWUP_STATUSES") == priv.FOLLOWUP_STATUSES
    assert {text("STREAM_SPLIT"), text("STREAM_PUSH")} == set(priv.STREAM_KINDS)
    assert {text("STREAM_OPEN"), text("STREAM_PAUSED"), text("STREAM_HALTED")} \
        == set(priv.STREAM_STATES)
    assert text("EVT_JOB_FINISHED") == client.EVENT_FINISHED
    assert text("EVT_JOB_TRANSFERRED") == client.EVENT_TRANSFERRED
    assert text("EVT_JOB_WATCH") == client.EVENT_WATCH
    assert text("ERR_BAD_KEY") == client.ERR_BAD_KEY
    assert text("CAP_SMARTTASKS") == client.CAPABILITY


def test_watch_fields():
    import pluto_job_package as package
    import pluto_out_rules as rules
    assert strings("WATCH_KEYS") == rules._FIELDS
    assert set(rules._FIELDS) == set(package.WATCH_SPEC)


def test_mcp_config_shapes():
    from agent_smart_tasks import mcp_config
    kw = dict(python="py", entry="e", agent_id="a", host="h", port="1")
    one = mcp_config.build(framework="claude", **kw)["mcpServers"]
    assert set(one) == {"smarttasks"} and "--attach" not in one["smarttasks"]["args"]
    two = mcp_config.build(framework="copilot", pluto_entry="p", home="/x", **kw)["mcpServers"]
    assert set(two) == {"smarttasks", "pluto"}
    assert two["smarttasks"]["type"] == "local" and "--attach" in two["smarttasks"]["args"]
    assert two["smarttasks"]["env"] == {"PLUTO_SMARTTASKS_HOME": "/x"}
