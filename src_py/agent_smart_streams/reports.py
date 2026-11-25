"""Plain-text and JSON reports for --history, --stats and --manage."""

from __future__ import annotations

import datetime as dt
import re

COLUMNS = (("job_id", 27), ("name", 14), ("user", 9), ("opener", 12),
           ("owner", 12), ("host", 12), ("opened", 16), ("elapsed", 9),
           ("state", 11), ("activity", 8), ("size", 8))


def when(ms) -> str:
    if not ms:
        return "-"
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def duration(ms) -> str:
    s = int((ms or 0) // 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s" if m else f"{s}s"


def size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return ""


def clean(text) -> str:
    """No control characters: names come from agents and go to a tty."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", str(text))


def row(job: dict) -> dict:
    return {"job_id": job["job_id"], "name": job.get("name") or "-",
            "user": job.get("user", "-"), "opener": job.get("opener", "-"),
            "owner": job.get("owner", "-"), "host": job.get("host", "-"),
            "opened": when(job.get("opened_at")),
            "elapsed": duration(job.get("elapsed_ms")),
            "state": job.get("state", "?"),
            "activity": job.get("activity", "-"), "size": size(job.get("size"))}


def line(values: dict) -> str:
    return " ".join(clean(values[k])[:w].ljust(w) for k, w in COLUMNS)


def table(jobs: list[dict]) -> str:
    head = line({k: k.upper() for k, _ in COLUMNS})
    return "\n".join([head] + [line(row(j)) for j in jobs])


_UNITS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def parse_age(text: str) -> int:
    m = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if not m:
        raise ValueError(f"expected a duration like 30m, 12h or 7d, got {text!r}")
    return int(m.group(1)) * _UNITS[m.group(2)]


def history_lines(records: list[dict]) -> str:
    out = [f"{'AT':16} {'EVENT':11} {'JOB':27} {'OWNER':14} {'STATE':11} "
           f"{'EXIT':4} {'SIZE':8} COMMAND"]
    for r in records:
        cmd = r.get("command") or " ".join(r.get("argv") or [])
        code = r.get("exit_code")
        out.append(
            f"{when(r.get('at')):16} {clean(r['kind']):11.11} {r['id']:27} "
            f"{clean(r.get('owner')):14.14} {clean(r.get('state')):11.11} "
            f"{'-' if code is None else code!s:4} {size(r.get('size')):8} "
            f"{clean(cmd)[:60]}")
    return "\n".join(out)


def stats_lines(s: dict) -> str:
    out = [f"Jobs: {s['jobs']}"]
    for title, key in (("State", "by_state"), ("Activity", "by_activity"),
                       ("Owner", "by_owner"), ("User", "by_user")):
        for k, v in sorted(s[key].items(), key=lambda kv: -kv[1]):
            out.append(f"  {title:9} {clean(k):20} {v}")
    live = s["since_server_start"]
    out += [f"Total run time:       {duration(s['total_elapsed_ms'])}",
            f"Total queue time:     {duration(s['total_queued_ms'])}",
            f"Output captured:      {size(s['bytes_captured'])}",
            f"Read by cursor:       {size(s['bytes_read_by_cursor'])}",
            "Since server start:   "
            f"{live['jobs_started']} started, {live['greps']} greps, "
            f"{size(live['bytes_delivered'])} delivered to agents"]
    return "\n".join(out)
