"""MCP tools for PlutoSmartStreams.

Thin wrappers over :class:`PlutoJobsClient`. Descriptions are kept short:
they are paid for on every model turn.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Optional, Union

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from agent_smart_streams import compact, fleet, submit
from agent_smart_streams.constants import (
    ACTION_ALIASES, CORE_TOOLS, GROUP_MAX, MANIFEST_PAGE, SUBMIT_MAX,
)
from agent_smart_streams.compact import short
from agent_smart_streams.notices import NoticeBoard
from agent_smart_streams.schema import fold, slim_tools
from pluto_jobs_def import (
    LIST_DEFAULT, READ_DEFAULT, WAIT_DEFAULT_S,
)
from pluto_sdk import PlutoError, PlutoJobsClient

Strs = Union[str, list[str]]
READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
RUNS = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def register_tools(mcp: FastMCP, jobs: PlutoJobsClient, piggyback=None,
                   toolset: str = "all") -> NoticeBoard:
    board = NoticeBoard(jobs)
    waiter = fleet.Waiter(jobs)
    board.watch_seen = waiter.watch_seen

    async def _run(fn, *args, shape=None, **kwargs) -> str:
        def work():
            try:
                result = fn(*args, **kwargs)
                if shape:
                    result = shape(result)
            except PlutoError as exc:
                result = {"error": str(exc)}
            except OSError as exc:
                result = {"error": f"Pluto server unreachable: {exc}"}
            return board.attach(result)
        board.busy += 1
        try:
            result = await asyncio.to_thread(work)
        finally:
            board.busy -= 1
        if piggyback:
            result = await piggyback(result)
        return compact.dumps(result)

    @mcp.tool(annotations=RUNS, structured_output=False)
    async def job_start(
        command: Optional[str] = None, argv: Optional[list[str]] = None,
        each: Optional[dict[str, Union[str, list[str]]]] = None,
        commands: Optional[list[str]] = None, names: Optional[list[str]] = None,
        cwd: Optional[str] = None, env: Optional[dict[str, str]] = None,
        name: Optional[str] = None, tags: Optional[list[str]] = None,
        timeout_s: Optional[float] = None, stdin: bool = False,
        parent: Optional[str] = None, rerun_of: Optional[str] = None,
        task_id: Optional[str] = None, max_output_bytes: Optional[int] = None,
        wait_s: int = 0,
    ) -> str:
        """Launch a shell command (or argv) as a durable background job and
        return at once. All output is kept on the server; it outlives this
        session. Start many at once with a template: command="prog --n {n}",
        each={"n": "1-12"} ("1-4,8" or a list; several keys give every
        combination), or with a commands list. Many get a group handle that
        works as a job id in other calls. wait_s also waits for all of them,
        like job_wait. parent nests under a job; rerun_of repeats one."""
        here = cwd or (None if rerun_of else os.getcwd())
        if here:
            here = os.path.abspath(here)
        spec = dict(cwd=here, env=env, tags=tags, timeout_s=timeout_s,
                    stdin=stdin or None, parent=parent, rerun_of=rerun_of,
                    task_id=task_id, max_output_bytes=max_output_bytes)

        def batch():
            if each:
                if not command or commands or argv or names:
                    raise PlutoError("each goes with command only")
                return submit.expand(command, each)
            if command or argv:
                raise PlutoError("give commands, or command/argv, not both")
            if names and len(names) != len(commands):
                raise PlutoError("names and commands differ in length")
            if len(commands) > SUBMIT_MAX:
                raise PlutoError(f"more than {SUBMIT_MAX} jobs in one call")
            return list(zip(names or [name] * len(commands), commands))

        def work():
            if each or commands:
                out = submit.start_all(jobs, batch(), spec)
                handle = out["group"]
            else:
                out = compact.started(jobs.start(command=command, argv=argv,
                                                 name=name, **spec))
                handle = out["job_id"]
            out.update(submit.output_path("{id}" if "group" in out else handle))
            if wait_s:
                w = compact.waited(waiter.wait(handle, None, wait_all=True,
                                               timeout_s=wait_s))
                out["waited"] = w
            return out
        return await _run(work)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_status(job_id: Optional[str] = None,
                         job_ids: Optional[list[str]] = None) -> str:
        """State, exit code, active/idle, elapsed, output size, your cursor
        and unread bytes, of one job or several."""
        return await _run(fleet.one_or_many, jobs, lambda j: dict(
            compact.status(jobs.call("status", job_id=j)),
            **submit.output_path(short(j))), job_id, job_ids)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_window(
        job_id: str, offset: Optional[int] = None, line: Optional[int] = None,
        tail: Optional[int] = None, cursor: bool = False, before: int = 0,
        lines: Optional[int] = None, max_bytes: int = READ_DEFAULT,
        stream: Optional[str] = None, strip_ansi: bool = False,
        hashes: bool = False,
    ) -> str:
        """Read a bounded window of output at a byte offset, a 1-based line,
        the last `tail` lines, or your cursor (default: the end). `before`
        starts earlier (bytes for offset, lines for line). Never moves the
        cursor. Max 32768 bytes."""
        return await _run(
            jobs.call, "window", job_id=job_id, offset=offset, line=line,
            tail=tail, cursor=cursor or None, before=before or None,
            lines=lines, max_bytes=max_bytes, stream=stream,
            strip_ansi=strip_ansi or None,
            shape=lambda r: compact.read(r, hashes))

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_grep(
        pattern: str, job_id: Optional[str] = None,
        job_ids: Optional[list[str]] = None, before: int = 0, after: int = 0,
        max_matches: Optional[int] = None, ignore_case: bool = False, fixed: bool = False,
        invert: bool = False, from_offset: Optional[int] = None,
        to_offset: Optional[int] = None, from_line: Optional[int] = None,
        to_line: Optional[int] = None, stream: Optional[str] = None,
    ) -> str:
        """Regex search over the output of a job (or several, with job_ids)
        on the server, with context lines. max_matches is per job (50, or 10
        when several). Returns line@offset:text for
        matches, line-text for context. If resume_at is present, continue
        with from_offset=resume_at."""
        return await _run(
            fleet.grep, jobs, job_id, job_ids, pattern=pattern,
            before=before or None, after=after or None, max_matches=max_matches,
            ignore_case=ignore_case or None, fixed=fixed or None,
            invert=invert or None, from_offset=from_offset, to_offset=to_offset,
            from_line=from_line, to_line=to_line, stream=stream)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_analyze(
        job_id: Optional[str] = None, job_ids: Optional[list[str]] = None,
        extract: Optional[str] = None, where: Optional[Strs] = None,
        by: Optional[Strs] = None, agg: Optional[Strs] = None,
        compare: Optional[Strs] = None, rows: Union[bool, list[str]] = False,
        sort: Optional[str] = None, limit: Optional[int] = None,
        stream: Optional[str] = None, from_line: Optional[int] = None,
        to_line: Optional[int] = None, from_offset: Optional[int] = None,
        to_offset: Optional[int] = None, resume: Optional[dict[str, int]] = None,
    ) -> str:
        """Understand output without scripts. With only job ids: an outline
        (line templates with counts, number ranges, likely errors, rare lines,
        which jobs deviate). Otherwise a query; L123 is a line for job_window.
        extract: "auto" (default: key=value, JSON, CSV/TSV, access log), a
        template "epoch {e:int} loss={loss:float} {rest}", csv:a,b,c or
        re:(?P<x>..); alone it summarises the fields.
        where (all must hold): "status >= 500", "msg contains timed out",
        "x ~ regex", "has err", "dur > 2s", "loss > prev*1.5", "seq != prev+1"
        (prev: previous matching record of the job and by group).
        by: fields; also _job _name _level _time _line _n.
        agg: count, min:f max: sum: mean: p95: first: last: distinct: argmin:.
        rows: true or fields, to list matching lines. sort: "-p95:dur".
        compare: fields; aligns records of the jobs by the by keys and reports
        where jobs disagree and who is odd. After INCOMPLETE pass resume."""
        return await _run(
            fleet.analyze, jobs, job_id, job_ids, extract=extract, where=where,
            by=by, agg=agg, compare=compare, rows=rows or None, sort=sort,
            limit=limit, stream=stream, from_line=from_line, to_line=to_line,
            from_offset=from_offset, to_offset=to_offset, resume=resume)

    @mcp.tool(annotations=RUNS, structured_output=False)
    async def job_watch(
        action: str = "add", job_id: Optional[str] = None,
        job_ids: Optional[list[str]] = None, name: Optional[str] = None,
        match: Optional[str] = None, where: Optional[Strs] = None,
        by: Optional[Strs] = None, extract: Optional[str] = None,
        count: Optional[int] = None, window_s: Optional[float] = None,
        idle_s: Optional[float] = None, absent_s: Optional[float] = None,
        stalled: Optional[str] = None, stalled_n: Optional[int] = None,
        then: str = "notify", once: bool = False,
        cooldown_s: Optional[float] = None, max_hits: Optional[int] = None,
        from_now: bool = False, rule: Optional[str] = None,
    ) -> str:
        """Standing rule on live jobs' output; hits come as notices, wake you,
        and end job_wait(until="watch"). Fires on lines with match (text, a
        {field} template or re:..) and/or where (as in job_analyze, with prev,
        by, extract), or each count matches [within window_s]; or after idle_s
        without output, absent_s without a matching line, or when field
        stalled keeps its value for stalled_n matches. then: notify, cancel
        or pause the job. Repeats after cooldown_s up to max_hits (defaults
        30, 20) unless once. Reads existing output too unless from_now.
        action: add, list, remove (rule = id or name; default all)."""
        return await _run(
            fleet.watch, jobs, action, job_id, job_ids, name=name, match=match,
            where=where, by=by, extract=extract, count=count, window_s=window_s,
            idle_s=idle_s, absent_s=absent_s, stalled=stalled, stalled_n=stalled_n,
            then=then, once=once or None, cooldown_s=cooldown_s,
            max_hits=max_hits, from_now=from_now or None, rule=rule)

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def job_next_chunk(
        job_id: str, max_bytes: int = READ_DEFAULT, stream: Optional[str] = None,
        from_offset: Optional[int] = None, strip_ansi: bool = False,
        encoding: Optional[str] = None, hashes: bool = False,
    ) -> str:
        """Read the next unread output and advance your durable cursor.
        from_offset rewinds. encoding="base64" returns raw bytes."""
        return await _run(
            jobs.call, "next", job_id=job_id, max_bytes=max_bytes,
            stream=stream, from_offset=from_offset,
            strip_ansi=strip_ansi or None, encoding=encoding,
            request_id=uuid.uuid4().hex,
            shape=lambda r: compact.read(r, hashes))

    @mcp.tool(annotations=RUNS, structured_output=False)
    async def job_control(
        action: str, job_id: Optional[str] = None,
        job_ids: Optional[list[str]] = None, signal: Optional[str] = None,
        tree: bool = False,
    ) -> str:
        """action: cancel (or halt), pause, resume, signal (with signal=
        INT|TERM|HUP|USR1|USR2|QUIT|KILL|STOP|CONT), on one job or several.
        tree=true also applies it to child jobs."""
        def work():
            r = fleet.control(jobs, job_id, job_ids, action=action,
                              signal=signal, tree=tree or None)
            if ACTION_ALIASES.get(action, action) in ("cancel", "halt"):
                board.expect_finish(r["ok"])
            return r
        return await _run(work)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_wait(
        job_id: Optional[str] = None, job_ids: Optional[list[str]] = None,
        until: str = "exit", pattern: Optional[str] = None,
        wait_all: bool = False, timeout_s: int = WAIT_DEFAULT_S,
        from_offset: Optional[int] = None, idle_ms: Optional[int] = None,
    ) -> str:
        """Block (max 300 s) until a job exits, or until="output", "idle"
        (idle_ms), "pattern" (regex appears in output) or "watch" (a job_watch
        rule fired). Give job_ids to
        watch many; returns when one has news, or with wait_all when all do.
        An exited job comes with its last line. Each event is reported
        once, so just call again with the same ids. Use this, not polling."""
        return await _run(
            waiter.wait, job_id, job_ids, until=until, pattern=pattern,
            wait_all=wait_all, from_offset=from_offset, idle_ms=idle_ms,
            timeout_s=timeout_s,
            shape=compact.waited)

    @mcp.tool(annotations=RUNS, structured_output=False)
    async def job_input(job_id: str, text: str = "", close: bool = False) -> str:
        """Write text to the stdin of a job started with stdin=true."""
        return await _run(jobs.input, job_id, text, close)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_list(
        scope: str = "mine", states: Optional[list[str]] = None,
        tags: Optional[list[str]] = None, parent: Optional[str] = None,
        owner: Optional[str] = None, name: Optional[str] = None,
        job_ids: Optional[list[str]] = None, limit: int = LIST_DEFAULT,
    ) -> str:
        """List jobs (id name state elapsed size owner), including those from
        earlier sessions. scope: mine, user or all. Filter by owner, text in
        the name, states or tags; parent lists children; job_ids shows those."""
        def work():
            if job_ids:
                rows = [jobs.call("status", job_id=j)
                        for j in fleet.ids_of(jobs, None, job_ids)]
                return {"total": len(rows), "jobs": rows}
            r = jobs.call("list", scope="all" if owner else scope, states=states,
                          tags=tags, parent=parent,
                          limit=GROUP_MAX if owner or name else limit)
            if owner or name:
                rows = fleet.filtered(r["jobs"], owner, name)
                r = {"total": len(rows), "jobs": rows[:limit]}
            return r
        return await _run(work, shape=compact.listed)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_open(job_id: Optional[str] = None,
                       path: Optional[str] = None) -> str:
        """Open a job package: command, provenance, state, short head and
        tail, nested streams and reviews. With path, first import a .pjob
        file (never runs anything)."""
        def work():
            jid = job_id
            if path:
                jid = jobs.import_(path)["job_id"]
            return jobs.call("open", job_id=jid)
        return await _run(work)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_manifest(job_id: str, start: int = 0, limit: int = MANIFEST_PAGE,
                           verify: bool = False) -> str:
        """Chunk manifest (64 KiB chunks, chained SHA-256). verify=true
        re-hashes the listed chunks on the server."""
        return await _run(jobs.call, "manifest", job_id=job_id, limit=limit,
                          verify=verify or None, **{"from": start})

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def job_transfer(to: str, job_id: Optional[str] = None,
                           job_ids: Optional[list[str]] = None) -> str:
        """Hand jobs to another agent id, who inherits your cursors."""
        def one(j):
            jobs.call("transfer", job_id=j, to=to)
            return "ok"
        return await _run(lambda: {"to": to, "jobs": fleet.each(
            one, fleet.ids_of(jobs, job_id, job_ids))})

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def job_review(
        job_id: str, verdict: Optional[str] = None, note: Optional[str] = None,
        from_offset: Optional[int] = None, to_offset: Optional[int] = None,
    ) -> str:
        """Attach a verdict and note, pinned by hash to a byte range of the
        output. Without verdict and note, list reviews."""
        return await _run(
            jobs.call, "review", job_id=job_id, verdict=verdict, note=note,
            from_offset=from_offset, to_offset=to_offset,
            action="add" if verdict or note else "list")

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def job_export(job_id: str, path: Optional[str] = None) -> str:
        """Write a portable .pjob archive. It contains the command and all
        output, so it may contain secrets."""
        return await _run(jobs.export, job_id,
                          os.path.abspath(path) if path else None)

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def job_stream(
        job_id: str, action: str, stream: Optional[str] = None,
        name: Optional[str] = None, pattern: Optional[str] = None,
        from_offset: Optional[int] = None, to_offset: Optional[int] = None,
        text: Optional[str] = None, followup_id: Optional[str] = None,
        status: Optional[str] = None, assignee: Optional[str] = None,
    ) -> str:
        """Streams nested in a job's stream. action: split (view of a byte
        range, or one view per line matching pattern), push (append text to
        a new or existing pushed stream), pause, resume, halt, list,
        followup (track an item; status open|in_progress|done|dropped).
        Read a nested stream by passing its id as stream= to job_window,
        job_grep or job_next_chunk."""
        return await _run(
            jobs.call, "stream", job_id=job_id, action=action, stream=stream,
            name=name, pattern=pattern, from_offset=from_offset,
            to_offset=to_offset, text=text, followup_id=followup_id,
            status=status, assignee=assignee)

    if toolset != "all":
        fold(mcp, CORE_TOOLS if toolset == "core" else ())
    slim_tools(mcp)
    return board
