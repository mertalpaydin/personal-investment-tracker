"""Registering the daily update with Windows Task Scheduler, from the page.

The owner of this tool does not use a terminal, so "run this schtasks command"
is not an instruction they can act on. This module is what a button on the
settings page calls.

Four decisions worth stating, because each has an obvious-looking alternative
that fails quietly:

**``/XML``, not ``/TR``.** Embedding a quoted command inside a quoted argument
is fragile the moment a path contains a space, and ``/TR`` cannot express
``<WorkingDirectory>``, ``<Hidden>`` or -- the important one --
``<StartWhenAvailable>``, which is what makes the task run after a laptop that
was closed at the scheduled time.

**The XML must be UTF-16LE with a BOM.** ``schtasks /Create /XML`` rejects
UTF-8 with "The task XML is malformed", which is a misleading message for a
well-formed document in the wrong encoding.

**``.venv\\Scripts\\pythonw.exe``, not ``uv run``.** ``uv run`` re-resolves the
lockfile and may reach the network, so a task built on it fails exactly when
the machine is offline -- which is one of the times a scheduled catch-up
matters most. ``pythonw`` also means no console window appears each evening.

**Registration state is read from ``/XML``, never from ``/Query /FO LIST``.**
Those field names are localised: on a German Windows the parser would look for
"Last Run Time" and find "Letzte Laufzeit". Element names in the XML are not
localised. Run *history* is read from our own ``update_run`` table, which is
more informative anyway.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

TASK_NAME = "PortfolioTrackerDailyUpdate"

#: The only user-supplied value that reaches a command line. Validated against
#: this before it is formatted into the XML, and the call uses an argv list
#: with shell=False, so there is no shell to inject into either way.
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_TIME = "18:30"

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class TaskError(RuntimeError):
    """Task Scheduler refused, and said why."""


@dataclass(frozen=True)
class TaskStatus:
    supported: bool
    registered: bool
    task_name: str = TASK_NAME
    time: str | None = None
    command: str | None = None
    working_dir: str | None = None
    problem: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "registered": self.registered,
            "task_name": self.task_name,
            "time": self.time,
            "command": self.command,
            "working_dir": self.working_dir,
            "problem": self.problem,
        }


def supported() -> bool:
    return sys.platform == "win32"


def python_exe(repo_root: Path) -> Path:
    """The interpreter the task should run.

    Prefers the project's own virtual environment: the task must keep working
    after a system Python upgrade, and it needs this project's dependencies,
    which a bare system Python does not have.
    """
    candidates = [
        repo_root / ".venv" / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def build_xml(*, repo_root: Path, db_path: Path, at_time: str, start_date: str) -> str:
    """The task definition. ``start_date`` is passed in so this stays pure."""
    if not TIME_PATTERN.match(at_time):
        raise TaskError(f"{at_time!r} is not a time of day; use HH:MM, for example 18:30.")

    exe = python_exe(repo_root)
    arguments = f'-m portfolio_tracker.jobs.daily_update --db "{db_path}" --trigger SCHEDULED'
    description = (
        "Fetches missing prices and exchange rates for Portfolio Tracker, then recomputes."
    )

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{TASK_NS}">
  <RegistrationInfo>
    <Description>{description}</Description>
    <URI>\\{TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start_date}T{at_time}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{repo_root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    """Run schtasks with an argv list and no shell."""
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )


def register(
    *,
    repo_root: Path,
    db_path: Path,
    at_time: str = DEFAULT_TIME,
    start_date: str | None = None,
) -> TaskStatus:
    """Create or replace the daily task."""
    if not supported():
        return TaskStatus(supported=False, registered=False, problem="Windows only.")

    from datetime import date

    xml = build_xml(
        repo_root=repo_root,
        db_path=db_path,
        at_time=at_time,
        start_date=start_date or date.today().isoformat(),
    )

    # NamedTemporaryFile with delete=False: on Windows a file cannot be
    # reopened by another process while the handle is held.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", encoding="utf-16", delete=False, newline=""
    ) as handle:
        handle.write(xml)
        temp = Path(handle.name)

    try:
        result = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(temp), "/F")
        if result.returncode != 0:
            raise TaskError((result.stderr or result.stdout or "schtasks refused").strip())
    finally:
        temp.unlink(missing_ok=True)

    return status(repo_root=repo_root)


def unregister() -> TaskStatus:
    if not supported():
        return TaskStatus(supported=False, registered=False, problem="Windows only.")
    result = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if result.returncode != 0 and "cannot find" not in (result.stderr + result.stdout).lower():
        raise TaskError((result.stderr or result.stdout or "schtasks refused").strip())
    return TaskStatus(supported=True, registered=False)


def status(*, repo_root: Path | None = None) -> TaskStatus:
    """Whether the task exists, and what it is set to do."""
    if not supported():
        return TaskStatus(
            supported=False,
            registered=False,
            problem="Scheduled updates are set up through Windows Task Scheduler, "
            "which this machine does not have.",
        )

    result = _schtasks("/Query", "/TN", TASK_NAME, "/XML")
    if result.returncode != 0:
        return TaskStatus(supported=True, registered=False)

    try:
        # schtasks emits a BOM and sometimes trailing nulls; both upset the parser.
        root = ET.fromstring(result.stdout.lstrip("﻿").strip("\x00").strip())
    except ET.ParseError as exc:
        return TaskStatus(supported=True, registered=True, problem=f"unreadable task: {exc}")

    def find(path: str) -> str | None:
        node = root.find(path, {"t": TASK_NS})
        return node.text if node is not None else None

    boundary = find(".//t:CalendarTrigger/t:StartBoundary")
    return TaskStatus(
        supported=True,
        registered=True,
        time=boundary.split("T")[1][:5] if boundary and "T" in boundary else None,
        command=find(".//t:Exec/t:Command"),
        working_dir=find(".//t:Exec/t:WorkingDirectory"),
    )


def run_now() -> None:
    """Trigger the task immediately, proving the registration actually works."""
    if not supported():
        raise TaskError("Windows only.")
    result = _schtasks("/Run", "/TN", TASK_NAME)
    if result.returncode != 0:
        raise TaskError((result.stderr or result.stdout or "schtasks refused").strip())
