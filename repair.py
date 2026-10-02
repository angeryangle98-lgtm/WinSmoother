from __future__ import annotations

import re

from utils import (
    RC_CANCELLED,
    RC_LAUNCH_FAILED,
    RC_TIMEOUT,
    STATUS_CONTROL_C_EXIT,
    Canceller,
    Logger,
    run_command,
    system32,
)

_PERCENT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%")

# Repair occupies 50..100 % of the overall progress bar.
PLAN = [
    {
        "name": "DISM",
        "exe": "DISM.exe",
        "args": ["/Online", "/Cleanup-Image", "/RestoreHealth"],
        "stage": "Repairing Windows component store",
        "desc": "Repair the Windows component store",
        "start": 50, "end": 74, "timeout": 60 * 60,
        "ok": {0, 3010},
    },
    {
        "name": "SFC",
        "exe": "sfc.exe",
        "args": ["/scannow"],
        "stage": "Scanning and repairing protected system files",
        "desc": "Verify and repair protected system files",
        "start": 74, "end": 87, "timeout": 60 * 60,
        "ok": {0},
    },
    {
        "name": "DISM Cleanup",
        "exe": "DISM.exe",
        "args": ["/Online", "/Cleanup-Image", "/StartComponentCleanup"],
        "stage": "Removing superseded Windows component versions",
        "desc": "Remove superseded components",
        "start": 87, "end": 95, "timeout": 60 * 60,
        "ok": {0, 3010},
    },
    {
        "name": "CHKDSK",
        "exe": "chkdsk.exe",
        "args": ["C:", "/scan"],
        "stage": "Scanning the C: file system",
        "desc": "Scan the C: file system online",
        "start": 95, "end": 100, "timeout": 30 * 60,
        "ok": {0, 1, 2},  # 1 = errors found and fixed, 2 = cleanup performed
    },
]


def _percent_from_line(line: str) -> float | None:
    matches = _PERCENT_RE.findall(line)
    if not matches:
        return None
    try:
        return max(0.0, min(100.0, float(matches[-1])))
    except ValueError:
        return None


def _map(local_pct: float, start: float, end: float) -> int:
    return round(start + (max(0.0, min(100.0, local_pct)) / 100.0) * (end - start))


def describe_result(name: str, rc: int) -> str:
    if rc == 0:
        return "Completed successfully"
    if rc == 3010:
        return "Completed - restart required"
    if name == "CHKDSK" and rc == 1:
        return "Errors were found and fixed"
    if name == "CHKDSK" and rc == 2:
        return "Completed - cleanup performed"
    if rc == RC_CANCELLED:
        return "Stopped by user"
    if rc == RC_TIMEOUT:
        return "Timed out"
    if rc == RC_LAUNCH_FAILED:
        return "Could not start the Windows tool"
    if (rc & 0xFFFFFFFF) == STATUS_CONTROL_C_EXIT:
        return "Interrupted by Windows (0xC000013A)"
    return f"Failed (exit code {rc})"


def run_repair(logger: Logger, progress=None, cancel: Canceller | None = None) -> dict:
    """
    Runs Microsoft's own tools. Output is streamed while they run, so the
    percentage shown is the tool's real progress, mapped into its stage range.
    """
    results = []

    for step in PLAN:
        if cancel and cancel.cancelled:
            break

        name, stage = step["name"], step["stage"]
        start, end = step["start"], step["end"]
        command = [system32(step["exe"]), *step["args"]]

        if progress:
            progress(start, stage, name)

        child_pct = {"value": 0.0}

        def on_output(line: str, _s=start, _e=end, _n=name, _st=stage):
            value = _percent_from_line(line)
            if value is not None and value >= child_pct["value"]:
                child_pct["value"] = value
                if progress:
                    progress(_map(value, _s, _e), f"{_st} ({value:.0f}%)", _n)

        rc = run_command(
            logger, command, timeout=step["timeout"], on_output=on_output, cancel=cancel
        )

        retried = False
        if (rc & 0xFFFFFFFF) == STATUS_CONTROL_C_EXIT and not (cancel and cancel.cancelled):
            retried = True
            logger.write("STATUS_CONTROL_C_EXIT detected; retrying once in a hidden console.")
            child_pct["value"] = 0.0
            if progress:
                progress(start, f"Retrying {name}...", name)
            rc = run_command(
                logger, command, timeout=step["timeout"], on_output=on_output,
                cancel=cancel, mode="newconsole",
            )

        success = rc in step["ok"]
        results.append(
            {
                "name": name,
                "stage": stage,
                "return_code": rc,
                "success": success,
                "retried": retried,
                "message": describe_result(name, rc),
            }
        )

        if progress and rc != RC_CANCELLED:
            progress(
                end if success else max(start, end - 1),
                f"{name} {'complete' if success else 'finished with an error'}",
                name,
            )

    succeeded = sum(1 for r in results if r["success"])
    failed = len(results) - succeeded
    logger.write(f"Repair summary: {succeeded} succeeded, {failed} failed/stopped.")
    return {"results": results, "succeeded": succeeded, "failed": failed}
