"""--manage: full-screen job manager (stdlib curses)."""

from __future__ import annotations

import curses
import os
import sys

from agent_smart_tasks import compact, reports
from agent_smart_tasks.constants import (
    COLUMNS, REFRESH_MS, VIEW_CONTEXT, VIEW_LINES, VIEW_MATCHES,
)
from pluto_jobs_def import LIST_ALL, LIVE_STATES, READ_MAX
from pluto_sdk import PlutoError, PlutoJobsClient

HELP = ("up/down select  v view  g grep  m move  e export  p pause  r resume  "
        "c cancel  a all/active  q quit")


def run(jobs: PlutoJobsClient) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(reports.table(jobs.call("list", scope="all", limit=LIST_ALL)["jobs"]))
        return 0
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: Manager(scr, jobs).loop())
    return 0


class Manager:
    """One screen: the job table, a message line, and single-key actions."""

    def __init__(self, scr, jobs):
        self.scr, self.jobs = scr, jobs
        self.sel = 0
        self.all = True
        self.rows: list[dict] = []
        self.msg = ""

    def put(self, y, text, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h:
            try:
                self.scr.addnstr(y, 0, reports.clean(text), w - 1, attr)
            except curses.error:
                pass

    def refresh(self):
        try:
            jobs = self.jobs.call("list", scope="all", limit=LIST_ALL)["jobs"]
        except (PlutoError, OSError) as exc:
            self.msg = f"error: {exc}"
            return
        self.rows = [j for j in jobs if self.all or j["state"] in LIVE_STATES]
        self.sel = max(0, min(self.sel, len(self.rows) - 1))

    def draw(self):
        self.scr.erase()
        h, _ = self.scr.getmaxyx()
        self.put(0, f"PlutoSmartTasks - {len(self.rows)} jobs", curses.A_BOLD)
        self.put(1, reports.line({k: k.upper() for k, _ in COLUMNS}),
                 curses.A_UNDERLINE)
        page = max(1, h - 5)
        top = (self.sel // page) * page
        for i, j in enumerate(self.rows[top:top + page]):
            self.put(2 + i, reports.line(reports.row(j)),
                     curses.A_REVERSE if top + i == self.sel else 0)
        self.put(h - 2, self.msg)
        self.put(h - 1, HELP, curses.A_DIM)
        self.scr.refresh()

    def ask(self, prompt) -> str:
        h, w = self.scr.getmaxyx()
        self.put(h - 2, " " * (w - 1))
        self.put(h - 2, prompt)
        self.scr.timeout(-1)
        curses.echo()
        curses.curs_set(1)
        try:
            return self.scr.getstr(h - 2, min(len(prompt), w - 2), 200)\
                .decode(errors="replace").strip()
        finally:
            curses.noecho()
            curses.curs_set(0)

    def pager(self, text):
        self.scr.timeout(-1)
        lines = text.expandtabs().split("\n")
        top = max(0, len(lines) - self.scr.getmaxyx()[0] + 1)
        while True:
            h, _ = self.scr.getmaxyx()
            self.scr.erase()
            for i, ln in enumerate(lines[top:top + h - 1]):
                self.put(i, ln)
            self.put(h - 1, "up/down/space scroll, q back", curses.A_DIM)
            c = self.scr.getch()
            if c in (ord("q"), 27):
                return
            step = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, ord(" "): h - 2,
                    curses.KEY_NPAGE: h - 2, curses.KEY_PPAGE: 2 - h}.get(c, 0)
            top = max(0, min(top + step, max(0, len(lines) - 1)))

    def act(self, key):
        if not self.rows:
            return
        job = self.rows[self.sel]
        jid = job["job_id"]
        if key == "v":
            m = self.jobs.call("window", job_id=jid, tail=VIEW_LINES, max_bytes=READ_MAX,
                               strip_ansi=True)
            self.pager(m["text"])
        elif key == "g":
            pat = self.ask("grep: ")
            if pat:
                m = self.jobs.call("grep", job_id=jid, pattern=pat,
                                   before=VIEW_CONTEXT, after=VIEW_CONTEXT,
                                   max_matches=VIEW_MATCHES)
                self.pager(compact.grep_lines(m["groups"], offsets=False)
                           or "(no match)")
        elif key == "m":
            to = self.ask(f"move {jid} from {job['owner']} to agent: ")
            if to:
                self.jobs.call("transfer", job_id=jid, to=to)
                self.msg = f"moved to {to}"
        elif key == "e":
            self.msg = "exported " + self.jobs.export(jid)["path"]
        elif key in "prc":
            action = {"p": "pause", "r": "resume", "c": "cancel"}[key]
            if key == "c" and self.ask(f"cancel {jid}? [y/N] ").lower() != "y":
                return
            self.jobs.call("control", job_id=jid, action=action)
            self.msg = f"{action} sent"

    def loop(self):
        curses.curs_set(0)
        while True:
            self.refresh()
            self.draw()
            self.scr.timeout(REFRESH_MS)
            c = self.scr.getch()
            if c == ord("q"):
                return
            if c == curses.KEY_UP:
                self.sel = max(0, self.sel - 1)
            elif c == curses.KEY_DOWN:
                self.sel = min(len(self.rows) - 1, self.sel + 1)
            elif c == ord("a"):
                self.all = not self.all
            elif c > 0 and chr(c) in "vgmeprc":
                self.msg = ""
                try:
                    self.act(chr(c))
                except (PlutoError, OSError) as exc:
                    self.msg = f"error: {exc}"
