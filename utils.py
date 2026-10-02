from __future__ import annotations

import codecs
import ctypes
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

APP_NAME = "WinSmoother"
APP_VERSION = "5.2.0"
APP_TAGLINE = "Windows cleanup & repair utility"

IS_WINDOWS = os.name == "nt"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
STATUS_CONTROL_C_EXIT = 0xC000013A

RC_TIMEOUT = 124
RC_CANCELLED = 130
RC_LAUNCH_FAILED = 127

_SPLIT_RE = re.compile(r"[\r\n]+")


# --------------------------------------------------------------------------
# Paths / resources
# --------------------------------------------------------------------------
def resource_path(relative: str) -> Path:
    """Works both from source and from a PyInstaller one-file build."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def app_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "WinSmootherBot"
    return Path.home() / "AppData" / "Local" / "WinSmootherBot"


def log_dir() -> Path:
    return app_data_dir() / "Logs"


def system32(name: str) -> str:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    candidate = Path(windir) / "System32" / name
    return str(candidate) if candidate.exists() else name


def human_bytes(n: int) -> str:
    value = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def system_drive() -> str:
    return os.environ.get("SystemDrive", "C:")


def system_drive_usage() -> tuple[int, int] | None:
    """(total, free) bytes of the Windows drive - the *real* numbers from the file system."""
    try:
        usage = shutil.disk_usage(system_drive() + "\\")
        return usage.total, usage.free
    except OSError:
        return None


# --------------------------------------------------------------------------
# Logging (file only, never shown in the UI)
# --------------------------------------------------------------------------
class Logger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.path.open("a", encoding="utf-8", errors="replace")

    def write(self, message: str) -> None:
        try:
            self.fp.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
            self.fp.flush()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.fp.close()
        except Exception:
            pass


def prune_logs(keep: int = 15) -> None:
    """Keep only the newest `keep` runs (each run = .log + .json)."""
    try:
        logs = sorted(log_dir().glob("run_*.log"), key=lambda p: p.stat().st_mtime)
        for old in logs[:-keep]:
            for p in (old, old.with_suffix(".json")):
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


# --------------------------------------------------------------------------
# Windows helpers
# --------------------------------------------------------------------------
def enable_dpi_awareness() -> None:
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def native_message(title: str, text: str, error: bool = False) -> None:
    if IS_WINDOWS:
        try:
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
            return
        except Exception:
            pass
    print(f"{title}: {text}")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    try:
        if getattr(sys, "frozen", False):
            executable = sys.executable
            params = " ".join(f'"{a}"' for a in sys.argv[1:])
        else:
            executable = sys.executable
            script = str(Path(sys.argv[0]).resolve())
            params = " ".join([f'"{script}"', *(f'"{a}"' for a in sys.argv[1:])])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
        return rc > 32
    except Exception:
        return False


_mutex_handle = None


def acquire_single_instance() -> bool:
    """Prevents two copies running DISM/SFC at the same time."""
    global _mutex_handle
    if not IS_WINDOWS:
        return True
    try:
        _mutex_handle = ctypes.windll.kernel32.CreateMutexW(
            None, False, "Global\\WinSmootherBot_SingleInstance"
        )
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _oem_encoding() -> str:
    if IS_WINDOWS:
        try:
            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except Exception:
            pass
    return "utf-8"


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------
class Canceller:
    """Lets the UI stop the current child process (and its children)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def attach(self, proc: subprocess.Popen | None) -> None:
        with self._lock:
            self._proc = proc

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            kill_process_tree(proc)


def kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is not None:
            return
        if IS_WINDOWS:
            subprocess.run(
                [system32("taskkill.exe"), "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Running external tools
# --------------------------------------------------------------------------
def subprocess_kwargs(mode: str = "nowindow") -> dict:
    """
    mode "nowindow":   CREATE_NO_WINDOW (default).
    mode "newconsole": hidden dedicated console. Used for the retry when a
                       tool exits with STATUS_CONTROL_C_EXIT (0xC000013A).
    Both put the child in its own process group so CTRL+C / console events
    of the GUI process are not delivered to it.
    """
    kwargs: dict = {"stdin": subprocess.DEVNULL}
    if IS_WINDOWS:
        flags = CREATE_NEW_PROCESS_GROUP
        flags |= CREATE_NEW_CONSOLE if mode == "newconsole" else CREATE_NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs["creationflags"] = flags
        kwargs["startupinfo"] = si
    return kwargs


def _make_decoder(first_chunk: bytes):
    # sfc.exe writes UTF-16LE even when redirected; DISM/CHKDSK use the OEM code page.
    if b"\x00" in first_chunk[:64]:
        return codecs.getincrementaldecoder("utf-16-le")(errors="replace")
    return codecs.getincrementaldecoder(_oem_encoding())(errors="replace")


def run_command(
    logger: Logger,
    command: list[str],
    timeout: int | None = None,
    on_output=None,
    cancel: Canceller | None = None,
    mode: str = "nowindow",
) -> int:
    """
    Runs a tool hidden and streams its output line-by-line while it runs.
    DISM/SFC/CHKDSK redraw progress with '\\r', so both '\\r' and '\\n' end a line.
    """
    logger.write(f"COMMAND ({mode}): " + " ".join(command))

    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            **subprocess_kwargs(mode),
        )
    except Exception as exc:
        logger.write(f"COMMAND FAILED TO START: {type(exc).__name__}: {exc}")
        return RC_LAUNCH_FAILED

    if cancel:
        cancel.attach(proc)

    last_logged = {"text": ""}

    def emit(line: str) -> None:
        line = line.strip()
        if not line:
            return
        # Progress redraws repeat the same text; keep the log readable.
        if line != last_logged["text"]:
            logger.write(line)
            last_logged["text"] = line
        if on_output:
            try:
                on_output(line)
            except Exception:
                pass

    def reader() -> None:
        decoder = None
        pending = ""
        try:
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                if decoder is None:
                    decoder = _make_decoder(chunk)
                pending += decoder.decode(chunk)
                parts = _SPLIT_RE.split(pending)
                pending = parts.pop()
                for part in parts:
                    emit(part)
            pending += decoder.decode(b"", final=True) if decoder else ""
            emit(pending)
        except Exception as exc:
            logger.write(f"OUTPUT READER ERROR: {exc}")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout if timeout else None
    timed_out = False
    try:
        while proc.poll() is None:
            if cancel and cancel.cancelled:
                kill_process_tree(proc)
                break
            if deadline and time.monotonic() > deadline:
                timed_out = True
                kill_process_tree(proc)
                break
            time.sleep(0.2)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        thread.join(timeout=5)
    finally:
        if cancel:
            cancel.attach(None)

    if cancel and cancel.cancelled:
        logger.write("COMMAND CANCELLED BY USER")
        return RC_CANCELLED
    if timed_out:
        logger.write("COMMAND TIMED OUT")
        return RC_TIMEOUT

    rc = proc.returncode if proc.returncode is not None else 1
    logger.write(f"EXIT CODE: {rc}")
    return rc


# --------------------------------------------------------------------------
# Safe deletion
# --------------------------------------------------------------------------
def _is_reparse(path: str) -> bool:
    """Symlink or NTFS junction / mount point - never followed or removed."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _under(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def delete_tree_contents(
    logger: Logger,
    directory: Path,
    label: str,
    protected_paths: tuple[Path, ...] = (),
    cancel: Canceller | None = None,
) -> tuple[int, int, int]:
    """
    Deletes everything *inside* `directory` (never the directory itself).
    Returns (files_removed, bytes_removed, skipped). Files that are locked or
    denied are skipped; junctions/symlinks are never followed.
    """
    removed = freed = skipped = 0

    if not directory.exists():
        logger.write(f"{label}: not present -> {directory}")
        return 0, 0, 0

    roots = []
    for p in protected_paths:
        try:
            roots.append(p.resolve())
        except OSError:
            roots.append(p)

    def protected(full: str) -> bool:
        try:
            return _under(Path(full).resolve(), roots)
        except OSError:
            return True

    dirs_seen: list[str] = []
    try:
        for root, dirs, files in os.walk(directory, topdown=True, followlinks=False):
            if cancel and cancel.cancelled:
                break

            keep = []
            for d in dirs:
                full = os.path.join(root, d)
                if _is_reparse(full) or protected(full):
                    skipped += 1
                    logger.write(f"SKIP (link/protected): {full}")
                else:
                    keep.append(d)
                    dirs_seen.append(full)
            dirs[:] = keep

            for name in files:
                full = os.path.join(root, name)
                if _is_reparse(full) or protected(full):
                    skipped += 1
                    continue
                try:
                    size = os.lstat(full).st_size
                    try:
                        os.unlink(full)
                    except PermissionError:
                        os.chmod(full, stat.S_IWRITE)
                        os.unlink(full)
                    removed += 1
                    freed += size
                except OSError as exc:
                    skipped += 1
                    logger.write(f"SKIP (locked/denied): {full} -> {exc}")
    except OSError as exc:
        logger.write(f"Cannot enumerate {directory}: {exc}")
        skipped += 1

    for d in reversed(dirs_seen):  # remove now-empty sub-folders, deepest first
        try:
            os.rmdir(d)
        except OSError:
            pass

    logger.write(
        f"{label}: removed {removed} files / {human_bytes(freed)} / skipped {skipped}"
    )
    return removed, freed, skipped
