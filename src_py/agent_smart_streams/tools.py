"""MCP tools for PlutoSmartStreams.

Thin wrappers over :class:`PlutoJobsClient`. Descriptions are kept short:
they are paid for on every model turn.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from agent_smart_streams import compact
from pluto_sdk import PlutoError, PlutoJobsClient

READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
RUNS = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


class NoticeBoard:
    """Attaches durable job notices to tool results.

    A notice is acknowledged by the call *after* the one that carried it,
    so a result lost on the way to the model shows it again.
    """

    def __init__(self, jobs: PlutoJobsClient):
        self.jobs = jobs
        self._ack = 0

    def attach(self, result: dict) -> dict:
        try:
            notices = self.jobs.call("notices", ack=self._ack)["notices"]
        except (PlutoError, OSError):
            return result
        if not notices:
            return result
        self._ack = max(n["seq"] for n in notices)
        told = compact.final_jobs(result)
        fresh = [{k: n[k] for k in ("event", "job_id", "name", "state")
                  if n.get(k) is not None} for n in notices
                 if not (n["event"] == "job_finished" and n["job_id"] in told)]
        return dict(result, _job_notices=fresh) if fresh else result


def register_tools(mcp: FastMCP, jobs: PlutoJobsClient, piggyback=None) -> None:
    board = NoticeBoard(jobs)

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
        result = await asyncio.to_thread(work)
        if piggyback:
            result = await piggyback(result)
        return compact.dumps(result)

    @mcp.tool(annotations=RUNS, structured_output=False)
    async def job_start(
        command: Optional[str] = None, argv: Optional[list[str]] = None,
        cwd: Optional[str] = None, env: Optional[dict[str, str]] = None,
        name: Optional[str] = None, tags: Optional[list[str]] = None,
        timeout_s: Optional[float] = None, stdin: bool = False,
        parent: Optional[str] = None, rerun_of: Optional[str] = None,
        task_id: Optional[str] = None, max_output_bytes: Optional[int] = None,
    ) -> str:
        """Launch a shell command (or argv) as a durable background job and
        return at once. All output is kept on the server; it outlives this
        session. Start several to run them concurrently. parent nests it
        under another job; rerun_of repeats an earlier job."""
        here = cwd or (None if rerun_of else os.getcwd())
        if here:
            here = os.path.abspath(here)
        return await _run(
            jobs.start, command=command, argv=argv, cwd=here, env=env,
            name=name, tags=tags, timeout_s=timeout_s, stdin=stdin or None,
            parent=parent, rerun_of=rerun_of, task_id=task_id,
            max_output_bytes=max_output_bytes, shape=compact.started)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_status(job_id: str) -> str:
        """State, exit code, active/idle, elapsed, output size, your cursor
        and unread bytes."""
        return await _run(jobs.call, "status", job_id=job_id, shape=compact.status)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_window(
        job_id: str, offset: Optional[int] = None, line: Optional[int] = None,
        tail: Optional[int] = None, cursor: bool = False, before: int = 0,
        lines: Optional[int] = None, max_bytes: int = 8192,
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
        max_matches: int = 50, ignore_case: bool = False, fixed: bool = False,
        invert: bool = False, from_offset: Optional[int] = None,
        to_offset: Optional[int] = None, from_line: Optional[int] = None,
        to_line: Optional[int] = None, stream: Optional[str] = None,
    ) -> str:
        """Regex search over the output of a job (or several, with job_ids)
        on the server, with context lines. Returns line@offset:text for
        matches, line-text for context. If resume_at is present, continue
        with from_offset=resume_at."""
        def work(**kw):
            if not job_ids:
                return compact.grep(jobs.call("grep", job_id=job_id, **kw))
            out = {}
            for jid in job_ids:
                try:
                    out[jid] = compact.grep(jobs.call("grep", job_id=jid, **kw))
                except PlutoError as exc:
                    out[jid] = {"error": str(exc)}
            return out
        return await _run(
            work, pattern=pattern,
            before=before or None, after=after or None, max_matches=max_matches,
            ignore_case=ignore_case or None, fixed=fixed or None,
            invert=invert or None, from_offset=from_offset, to_offset=to_offset,
            from_line=from_line, to_line=to_line, stream=stream)

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def job_next_chunk(
        job_id: str, max_bytes: int = 8192, stream: Optional[str] = None,
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
        job_id: str, action: str, signal: Optional[str] = None,
        tree: bool = False,
    ) -> str:
        """action: cancel (or halt), pause, resume, signal (with signal=
        INT|TERM|HUP|USR1|USR2|QUIT|KILL). tree=true also applies it to
        child jobs."""
        return await _run(jobs.call, "control", job_id=job_id, action=action,
                          signal=signal, tree=tree or None)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_wait(
        job_id: Optional[str] = None, job_ids: Optional[list[str]] = None,
        until: str = "exit", pattern: Optional[str] = None,
        wait_all: bool = False, timeout_s: int = 50,
        from_offset: Optional[int] = None, idle_ms: Optional[int] = None,
    ) -> str:
        """Block (max 55 s) until a job exits, or until="output", "idle", or
        "pattern" (regex appears in output). Give job_ids for several;
        wait_all waits for every one. Use this instead of polling."""
        return await _run(
            jobs.wait, timeout_ms=max(1, min(int(timeout_s), 55)) * 1000,
            job_id=job_id, job_ids=job_ids, until=until, pattern=pattern,
            all=wait_all or None, from_offset=from_offset, idle_ms=idle_ms,
            shape=compact.waited)

    @mcp.tool(annotations=RUNS, structured_output=False)
    async def job_input(job_id: str, text: str = "", close: bool = False) -> str:
        """Write text to the stdin of a job started with stdin=true."""
        return await _run(jobs.call, "input", job_id=job_id, text=text,
                          close=close or None)

    @mcp.tool(annotations=READ, structured_output=False)
    async def job_list(
        scope: str = "mine", states: Optional[list[str]] = None,
        tags: Optional[list[str]] = None, parent: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """List jobs, including those from earlier sessions. scope: mine,
        user or all. parent lists the children of a job."""
        return await _run(jobs.call, "list", scope=scope, states=states,
                          tags=tags, parent=parent, limit=limit,
                          shape=compact.listed)

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
    async def job_manifest(job_id: str, start: int = 0, limit: int = 20,
                           verify: bool = False) -> str:
        """Chunk manifest (64 KiB chunks, chained SHA-256). verify=true
        re-hashes the listed chunks on the server."""
        return await _run(jobs.call, "manifest", job_id=job_id, limit=limit,
                          verify=verify or None, **{"from": start})

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def job_transfer(job_id: str, to: str) -> str:
        """Hand a job to another agent id, who inherits your cursor."""
        return await _run(jobs.call, "transfer", job_id=job_id, to=to)

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
