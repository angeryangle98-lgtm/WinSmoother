"""Installed-program discovery and forced removal.

Forced removal means, in order:
  1. stop the program's services and kill every process running from its folder
  2. run its own uninstaller silently (with a timeout, so a stuck one cannot block us)
  3. stop/kill again, delete leftover services, wipe the install folder
     (take ownership, `rd`, and finally schedule deletion at reboot for locked files)
  4. delete the program's entry from the registry so it disappears from Windows

Store (UWP) apps are removed with Remove-AppxPackage.
"""
from __future__ import annotations

import base64
import ctypes
import json
import ntpath
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from utils import (
    RC_CANCELLED,
    RC_LAUNCH_FAILED,
    RC_TIMEOUT,
    Canceller,
    Logger,
    _is_reparse,
    app_data_dir,
    delete_tree_contents,
    kill_process_tree,
    run_command,
    subprocess_kwargs,
    system32,
)

try:
    import winreg  # type: ignore
except ImportError:  # not Windows (only happens when testing the logic elsewhere)
    winreg = None

_UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

_RISKY_RE = re.compile(
    r"redistributable|vcredist|visual c\+\+|\.net (core|runtime|sdk|desktop|framework)|"
    r"asp\.net|webview2|directx|windows sdk|windows driver|\bdriver\b|chipset|"
    r"nvidia (graphics|physx|hd audio)|realtek|intel\(r\)|amd (software|chipset)|"
    r"microsoft edge|windows (defender|security)",
    re.IGNORECASE,
)
_UPDATE_NAME_RE = re.compile(
    r"^(kb\d{6,}|(security |hotfix |cumulative )?update for |hotfix for )", re.IGNORECASE
)
_MSI_GUID_RE = re.compile(r"\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}")
_INNO_RE = re.compile(r"^unins\d*\.exe$", re.IGNORECASE)

# Folder names that are shared containers, never one program's own folder.
_SHARED_NAMES = {
    "microsoft", "windowsapps", "modifiablewindowsapps", "common files", "windows nt",
    "windows defender", "windows defender advanced threat protection", "internet explorer",
    "windowspowershell", "dotnet", "reference assemblies", "msbuild", "microsoft.net",
    "windows kits", "windows mail", "windows media player", "windows photo viewer",
    "windows portable devices", "windows security", "windows sidebar",
    "uninstall information", "packages", "programs", "temp", "google", "adobe",
    "package cache", "installer", "system32", "syswow64", "winsxs",
}
_TOP_LEVEL_DENY = {
    "windows", "users", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery", "boot", "perflogs",
    "documents and settings", "config.msi",
}

ROOTS_ENV = (
    "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData",
    "CommonProgramFiles", "CommonProgramFiles(x86)", "LOCALAPPDATA", "APPDATA",
    "PUBLIC", "ALLUSERSPROFILE", "USERPROFILE", "OneDrive", "OneDriveConsumer",
)


# ==========================================================================
# Model
# ==========================================================================
@dataclass
class AppEntry:
    key: str
    kind: str                      # "desktop" | "store"
    name: str
    version: str = ""
    publisher: str = ""
    location: str = ""
    location_guessed: bool = False
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""
    est_size: int = 0              # bytes, as declared by the installer (often missing)
    package_full_name: str = ""
    reg_hive: int = 0
    reg_view: int = 0
    reg_path: str = ""
    risky: bool = False            # runtimes / drivers other programs may rely on
    locked: bool = False           # this very app (or the Python it runs on)
    size: int | None = None        # measured size in bytes
    size_done: bool = False
    icon_file: str = ""            # file the icon comes from (.ico / .exe / .dll)
    icon_index: int = 0            # icon index inside that file


# ==========================================================================
# Path safety (pure string logic - unit-testable anywhere)
# ==========================================================================
def _norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(path))).rstrip("\\/")


def _contains(parent: str, child: str) -> bool:
    """True if `child` is `parent` itself or lives below it (both already normalised)."""
    return child == parent or child.startswith(parent + "\\")


def _protected_roots(env) -> list[str]:
    get = env.get
    roots: list[str] = []

    def add(p):
        if p:
            roots.append(_norm(p))

    windir = get("WINDIR") or get("SYSTEMROOT") or r"C:\Windows"
    add(windir)
    add(ntpath.join(windir, "System32"))
    add(ntpath.join(windir, "SysWOW64"))
    for name in ROOTS_ENV:
        add(get(name))
    home = get("USERPROFILE")
    if home:
        for sub in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos",
                    "Favorites", "Links", "Contacts", "Saved Games", r"AppData\LocalLow"):
            add(ntpath.join(home, sub))
        add(ntpath.dirname(home))
    local, roaming, pdata = get("LOCALAPPDATA"), get("APPDATA"), get("PROGRAMDATA")
    if local:
        for sub in ("Programs", "Microsoft", "Packages", "Temp"):
            add(ntpath.join(local, sub))
    if roaming:
        add(ntpath.join(roaming, "Microsoft"))
    if pdata:
        add(ntpath.join(pdata, "Microsoft"))
        add(ntpath.join(pdata, "Package Cache"))
    add((get("SystemDrive") or "C:") + "\\")
    return roots


def wipe_check(path: str, *, for_size: bool = False, env=None, isdir=os.path.isdir) -> tuple[bool, str]:
    """
    Decides whether `path` is one program's own folder that may be measured
    (for_size=True) or deleted (for_size=False). Anything that equals or contains a
    system / profile / shared folder is refused, so a badly written InstallLocation
    can never cause a wipe of Program Files, Users, Documents, etc.
    """
    env = os.environ if env is None else env
    if not path:
        return False, "no folder"
    p = ntpath.normpath(os.path.expandvars(str(path).strip().strip('"')))
    if not ntpath.isabs(p):
        return False, "not an absolute path"
    n = _norm(p)
    _drive, tail = ntpath.splitdrive(n)
    parts = [x for x in re.split(r"[\\/]+", tail) if x]
    if not parts:
        return False, "drive root"
    if not isdir(p):
        return False, "folder not found"

    for root in _protected_roots(env):
        if root and _contains(n, root):
            return False, "contains a system or user folder"

    last = parts[-1]
    if last in _SHARED_NAMES:
        return False, "shared folder"
    if len(parts) == 1 and last in _TOP_LEVEL_DENY:
        return False, "system folder"

    if not for_size:
        windir = _norm(env.get("WINDIR") or env.get("SYSTEMROOT") or r"C:\Windows")
        if _contains(windir, n):
            return False, "inside Windows"
        if "windowsapps" in parts:
            return False, "Store package folder"
        try:
            if _is_reparse(p):
                return False, "link"
        except Exception:
            pass
    return True, ""


def _self_paths() -> list[str]:
    paths = [Path(__file__).resolve().parent, app_data_dir(), Path(sys.prefix), Path(sys.base_prefix)]
    try:
        paths.append(Path(sys.executable).resolve().parent)
    except OSError:
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass))
    return [_norm(str(p)) for p in paths]


def split_command(cmd: str) -> tuple[str, str]:
    """'"C:\\x y\\a.exe" /arg' -> ('C:\\x y\\a.exe', '/arg'); also copes with unquoted paths."""
    cmd = os.path.expandvars((cmd or "").strip())
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end != -1:
            return cmd[1:end], cmd[end + 1:].strip()
    m = re.search(r"\.(exe|bat|cmd|com)\b", cmd, re.IGNORECASE)
    if m:
        return cmd[:m.end()].strip('"'), cmd[m.end():].strip()
    parts = cmd.split(None, 1)
    if not parts:
        return "", ""
    return parts[0].strip('"'), (parts[1] if len(parts) > 1 else "")


def build_uninstall_command(app: AppEntry) -> tuple[str, int] | None:
    """Returns (command line, timeout seconds) for a silent uninstall, or None."""
    quiet = (app.quiet_uninstall_string or "").strip()
    raw = (app.uninstall_string or "").strip()
    if quiet:
        exe, args = split_command(quiet)
        return (f'"{exe}" {args}'.strip() if exe else quiet), 300
    if not raw:
        return None
    if "msiexec" in raw.lower():
        m = _MSI_GUID_RE.search(raw)
        if not m:
            return None
        msi = system32("msiexec.exe")
        return f'"{msi}" /x {m.group(0)} /qn /norestart', 300
    exe, args = split_command(raw)
    if not exe:
        return None
    base = f'"{exe}" {args}'.strip()
    if _INNO_RE.match(ntpath.basename(exe)):
        return f"{base} /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-", 300
    if "--uninstall" in args:  # Chromium-style installers (Chrome, Brave, ...)
        return f"{base} --force-uninstall", 180
    return f"{base} /S", 90  # NSIS and most others; a hidden GUI is killed on timeout


# ==========================================================================
# Discovery
# ==========================================================================
def _reg_value(key, name, default=""):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def _reg_int(key, name) -> int:
    try:
        return int(_reg_value(key, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _icon_dir(display_icon: str) -> str:
    icon = os.path.expandvars((display_icon or "").strip())
    if not icon:
        return ""
    icon = re.sub(r",\s*-?\d+$", "", icon).strip().strip('"')
    if icon.lower().endswith((".exe", ".ico", ".dll")):
        return ntpath.dirname(icon)
    return ""


def _parse_display_icon(raw: str) -> tuple[str, int]:
    """'"C:\\x\\a.exe",2' -> ('C:\\x\\a.exe', 2). Index defaults to 0."""
    raw = os.path.expandvars((raw or "").strip())
    if not raw:
        return "", 0
    index = 0
    m = re.search(r",\s*(-?\d+)\s*$", raw)
    if m:
        index = int(m.group(1))
        raw = raw[:m.start()]
    return raw.strip().strip('"').strip(), index


def _pick_icon_source(display_icon: str, uninstall_cmd: str, location: str) -> tuple[str, int]:
    """Best file to take an app's icon from: DisplayIcon, else the uninstaller's exe."""
    path, index = _parse_display_icon(display_icon)
    if path and path.lower().endswith((".exe", ".ico", ".dll")):
        return path, index
    if uninstall_cmd:
        exe, _args = split_command(uninstall_cmd)
        if exe and not re.search(r"msiexec|rundll32|cmd\.exe", exe, re.IGNORECASE):
            return exe, 0
    return "", 0


def _registry_apps() -> list[AppEntry]:
    apps: list[AppEntry] = []
    if winreg is None:
        return apps
    sources = (
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        ("HKLM32", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        ("HKCU", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
    )
    seen: set[tuple] = set()
    for tag, hive, view in sources:
        try:
            root = winreg.OpenKey(hive, _UNINSTALL_KEY, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        names: list[str] = []
        with root:
            i = 0
            while True:
                try:
                    names.append(winreg.EnumKey(root, i))
                except OSError:
                    break
                i += 1
        for sub in names:
            path = f"{_UNINSTALL_KEY}\\{sub}"
            try:
                with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as k:
                    name = str(_reg_value(k, "DisplayName")).strip()
                    if not name or _UPDATE_NAME_RE.match(name):
                        continue
                    if _reg_int(k, "SystemComponent") == 1:
                        continue
                    if _reg_value(k, "ParentKeyName") or _reg_value(k, "ParentDisplayName"):
                        continue
                    if str(_reg_value(k, "ReleaseType")).lower() in (
                            "update", "hotfix", "security update", "update rollup", "service pack"):
                        continue
                    uninstall = str(_reg_value(k, "UninstallString")).strip()
                    quiet = str(_reg_value(k, "QuietUninstallString")).strip()
                    location = os.path.expandvars(str(_reg_value(k, "InstallLocation")).strip().strip('"'))
                    guessed = False
                    if not location:
                        location = _icon_dir(str(_reg_value(k, "DisplayIcon")))
                        guessed = bool(location)
                    if not location and (quiet or uninstall):
                        exe, _args = split_command(quiet or uninstall)
                        if exe and not re.search(r"msiexec|rundll32|cmd\.exe", exe, re.IGNORECASE):
                            location = ntpath.dirname(exe)
                            guessed = bool(location)
                    if not (uninstall or quiet or location):
                        continue
                    version = str(_reg_value(k, "DisplayVersion")).strip()
                    ident = (name.lower(), version, (quiet or uninstall).lower())
                    if ident in seen:
                        continue
                    seen.add(ident)
                    est_kb = _reg_int(k, "EstimatedSize")
                    app = AppEntry(
                        key=f"reg|{tag}|{sub}",
                        kind="desktop",
                        name=name,
                        version=version,
                        publisher=str(_reg_value(k, "Publisher")).strip(),
                        location=location,
                        location_guessed=guessed,
                        uninstall_string=uninstall,
                        quiet_uninstall_string=quiet,
                        est_size=max(0, est_kb) * 1024,
                        reg_hive=hive,
                        reg_view=view,
                        reg_path=path,
                    )
                    app.risky = bool(_RISKY_RE.search(name)) or _reg_int(k, "NoRemove") == 1
                    app.icon_file, app.icon_index = _pick_icon_source(
                        str(_reg_value(k, "DisplayIcon")), quiet or uninstall, location)
                    apps.append(app)
            except OSError:
                continue
    return apps


def _prettify_store_name(name: str) -> str:
    n = re.sub(r"^Microsoft\.", "", name)
    n = n.replace(".", " ")
    n = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[a-z])(?=\d)|(?<=\d)(?=[A-Z][a-z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", n)
    n = re.sub(r"\s+", " ", n)
    return n.strip() or name


def _powershell_path() -> str:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    candidate = Path(windir) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.exists() else "powershell.exe"


def run_powershell(logger: Logger | None, script: str, timeout: int = 120) -> tuple[int, str]:
    """Runs a script hidden and returns (exit code, stdout). Encoded so quoting can't break it."""
    full = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + script
    encoded = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    cmd = [_powershell_path(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-EncodedCommand", encoded]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, **subprocess_kwargs())
    except subprocess.TimeoutExpired:
        if logger:
            logger.write("POWERSHELL TIMED OUT")
        return RC_TIMEOUT, ""
    except Exception as exc:
        if logger:
            logger.write(f"POWERSHELL FAILED TO START: {type(exc).__name__}: {exc}")
        return RC_LAUNCH_FAILED, ""
    out = proc.stdout.decode("utf-8-sig", errors="replace").strip()
    if logger and out:
        logger.write("POWERSHELL: " + out[:2000])
    return proc.returncode, out


_STORE_LIST_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$items = Get-AppxPackage | Where-Object {
    -not $_.IsFramework -and -not $_.NonRemovable -and -not $_.IsResourcePackage -and
    $_.SignatureKind -ne 'System' -and $_.Name -notlike 'Microsoft.Windows.*' -and
    $_.Name -notlike 'MicrosoftWindows.*' -and $_.Name -notlike 'Windows.*'
} | ForEach-Object {
    [pscustomobject]@{ n = $_.Name; f = $_.PackageFullName; l = $_.InstallLocation;
                       p = $_.PublisherId; v = $_.Version.ToString() }
}
ConvertTo-Json -InputObject @($items) -Compress
"""


def _store_apps(logger: Logger | None) -> list[AppEntry]:
    rc, out = run_powershell(None, _STORE_LIST_SCRIPT, timeout=90)
    if rc != 0 or not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    apps = []
    for item in data:
        full = item.get("f") or ""
        name = item.get("n") or ""
        if not full or not name:
            continue
        apps.append(AppEntry(
            key=f"store|{full}",
            kind="store",
            name=_prettify_store_name(name),
            version=item.get("v") or "",
            publisher="Microsoft Store",
            location=item.get("l") or "",
            package_full_name=full,
        ))
    return apps


def list_installed_apps(logger: Logger | None = None) -> list[AppEntry]:
    apps = _registry_apps() + _store_apps(logger)
    selfp = _self_paths()
    for app in apps:
        if app.location:
            loc = _norm(os.path.expandvars(app.location))
            if any(_contains(loc, sp) for sp in selfp):
                app.locked = True
    apps.sort(key=lambda a: a.name.lower())
    return apps


# ==========================================================================
# Icons
# ==========================================================================
_ICON_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class WsIcon {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern uint PrivateExtractIcons(string file, int index, int cx, int cy,
        IntPtr[] handles, uint[] ids, uint count, uint flags);
    [DllImport("user32.dll")]
    public static extern bool DestroyIcon(IntPtr h);
}
'@
$size = __SIZE__
$items = @(ConvertFrom-Json @'
__JSON__
'@)
$i = 0
foreach ($it in $items) {
    try {
        $h = New-Object IntPtr[] 1
        $ids = New-Object UInt32[] 1
        $n = [WsIcon]::PrivateExtractIcons($it.p, [int]$it.n, $size, $size, $h, $ids, 1, 0)
        if ($n -ge 1 -and $h[0] -ne [IntPtr]::Zero) {
            $icon = [System.Drawing.Icon]::FromHandle($h[0])
            $bmp = New-Object System.Drawing.Bitmap($icon.ToBitmap())
            $ms = New-Object System.IO.MemoryStream
            $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
            Write-Output ($it.i.ToString() + "`t" + [Convert]::ToBase64String($ms.ToArray()))
            $bmp.Dispose(); $ms.Dispose(); $icon.Dispose()
            [WsIcon]::DestroyIcon($h[0]) | Out-Null
        }
    } catch { }
}
"""


def extract_desktop_icons(items: list[tuple[int, str, int]], size: int) -> dict[int, str]:
    """
    items: (number, icon file, icon index). Returns {number: base64 PNG}.
    Uses the Windows shell through PowerShell, so no third-party imaging library is needed.
    """
    todo = [{"i": i, "p": p, "n": n} for i, p, n in items if p and os.path.isfile(p)]
    if not todo:
        return {}
    script = (_ICON_SCRIPT
              .replace("__SIZE__", str(int(size)))
              .replace("__JSON__", json.dumps(todo, ensure_ascii=True)))
    rc, out = run_powershell(None, script, timeout=90)
    result: dict[int, str] = {}
    for line in out.splitlines():
        num, _tab, data = line.partition("\t")
        if num.strip().isdigit() and data.strip():
            result[int(num)] = data.strip()
    return result


def store_logo_path(app: AppEntry, size: int) -> str:
    """PNG of a Store app's logo (read from its AppxManifest), or ''."""
    root = app.location
    if not root or not os.path.isdir(root):
        return ""
    try:
        tree = ET.parse(os.path.join(root, "AppxManifest.xml"))
    except (OSError, ET.ParseError):
        return ""
    logo = ""
    for want in ("Square44x44Logo", "Square150x150Logo", "Logo"):
        for el in tree.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag in ("VisualElements", "Properties", "DefaultTile"):
                value = el.attrib.get(want) or (el.text.strip() if tag == "Logo" and el.text else "")
                if value:
                    logo = value
                    break
        if logo:
            break
    if not logo:
        return ""
    logo = logo.replace("/", "\\")
    full = os.path.join(root, *[x for x in logo.split("\\") if x])
    folder, fname = os.path.split(full)
    stem, ext = os.path.splitext(fname)
    try:
        names = os.listdir(folder)
    except OSError:
        return ""
    best, best_score = "", None
    for name in names:
        low = name.lower()
        if not low.endswith(ext.lower() or ".png") or not low.startswith(stem.lower()):
            continue
        rest = low[len(stem):-len(ext)] if ext else low[len(stem):]
        score = 1000
        m = re.search(r"targetsize-(\d+)", rest)
        if m:
            score = abs(int(m.group(1)) - size)
            if "altform" in rest:
                score += 5
        else:
            m = re.search(r"scale-(\d+)", rest)
            if m:  # scale-100 of a 44px logo is 44px wide
                score = abs(round(44 * int(m.group(1)) / 100) - size) + 20
            elif rest == "":
                score = 500
        if best_score is None or score < best_score:
            best, best_score = os.path.join(folder, name), score
    return best


# ==========================================================================
# Sizes
# ==========================================================================
def dir_size(path: str, should_stop=None) -> int:
    total = 0
    stack = [path]
    while stack:
        if should_stop and should_stop():
            return total
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        if getattr(st, "st_file_attributes", 0) & 0x400:  # junction / symlink
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += st.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def measure_app_size(app: AppEntry, should_stop=None) -> int | None:
    """Real size on disk of the install folder; falls back to the installer's own estimate."""
    est = app.est_size or None
    measured = None
    if app.location:
        ok, _why = wipe_check(app.location, for_size=True)
        if ok:
            measured = dir_size(app.location, should_stop) or None
    if measured is None:
        return est
    if app.location_guessed and est:
        return max(measured, est)
    return measured


# ==========================================================================
# Registry / file-system helpers used by the uninstaller
# ==========================================================================
def _key_exists(app: AppEntry) -> bool:
    if winreg is None or not app.reg_path:
        return False
    try:
        with winreg.OpenKey(app.reg_hive, app.reg_path, 0, winreg.KEY_READ | app.reg_view):
            return True
    except OSError:
        return False


def _delete_key_tree(hive: int, path: str, view: int) -> None:
    with winreg.OpenKey(hive, path, 0, winreg.KEY_ALL_ACCESS | view) as k:
        subs = []
        i = 0
        while True:
            try:
                subs.append(winreg.EnumKey(k, i))
            except OSError:
                break
            i += 1
    for sub in subs:
        _delete_key_tree(hive, f"{path}\\{sub}", view)
    winreg.DeleteKeyEx(hive, path, view, 0)


def _wait_until(predicate, seconds: float, cancel: Canceller | None) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        if cancel and cancel.cancelled:
            return False
        time.sleep(0.5)
    return predicate()


def _run_cmdline(logger: Logger, cmdline: str, timeout: int, cancel: Canceller | None) -> int:
    logger.write("UNINSTALL COMMAND: " + cmdline)
    try:
        proc = subprocess.Popen(
            cmdline, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False, **subprocess_kwargs(),
        )
    except Exception as exc:
        logger.write(f"UNINSTALLER FAILED TO START: {type(exc).__name__}: {exc}")
        return RC_LAUNCH_FAILED
    if cancel:
        cancel.attach(proc)
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if cancel and cancel.cancelled:
                kill_process_tree(proc)
                return RC_CANCELLED
            if time.monotonic() > deadline:
                logger.write("UNINSTALLER TIMED OUT - killing it and continuing with forced removal")
                kill_process_tree(proc)
                return RC_TIMEOUT
            time.sleep(0.3)
    finally:
        if cancel:
            cancel.attach(None)
    rc = proc.returncode if proc.returncode is not None else 1
    logger.write(f"UNINSTALLER EXIT CODE: {rc}")
    return rc


_STOP_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$prefix = ('__LOC__').TrimEnd('\') + '\'
$self = __PID__
$svcs = @(Get-CimInstance Win32_Service | Where-Object {
    $_.PathName -and $_.PathName.Replace('"','').StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) })
foreach ($s in $svcs) {
    Stop-Service -Name $s.Name -Force
    __DELETE_SERVICES__
}
$procs = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $self -and $_.ExecutablePath -and
    $_.ExecutablePath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) })
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force }
Write-Output ('services=' + $svcs.Count + ' processes=' + $procs.Count)
"""


def stop_everything_under(logger: Logger, folder: str, delete_services: bool = False) -> None:
    """Stops services and kills processes whose executable lives inside `folder`."""
    script = (
        _STOP_SCRIPT
        .replace("__LOC__", folder.replace("'", "''"))
        .replace("__PID__", str(os.getpid()))
        .replace("__DELETE_SERVICES__", "sc.exe delete $s.Name | Out-Null" if delete_services else "")
    )
    run_powershell(logger, script, timeout=90)


def _schedule_delete_on_reboot(path: str) -> bool:
    try:
        return bool(ctypes.windll.kernel32.MoveFileExW(str(path), None, 0x4))  # DELAY_UNTIL_REBOOT
    except Exception:
        return False


def wipe_folder(logger: Logger, folder: str, cancel: Canceller | None = None) -> str:
    """Returns 'gone', 'reboot' (locked files will be removed at next restart) or 'failed'."""
    target = Path(folder)
    protected = tuple(Path(p) for p in _self_paths())

    def attempt() -> None:
        delete_tree_contents(logger, target, "Program folder", protected, cancel)
        try:
            os.rmdir(target)
        except OSError:
            pass

    attempt()
    if not target.exists():
        return "gone"
    if cancel and cancel.cancelled:
        return "failed"

    logger.write("Folder still present - taking ownership and retrying")
    run_command(logger, [system32("takeown.exe"), "/f", str(target), "/r", "/d", "y"],
                timeout=180, cancel=cancel)
    run_command(logger, [system32("icacls.exe"), str(target), "/grant",
                         "*S-1-5-32-544:(OI)(CI)F", "/t", "/c", "/q"], timeout=180, cancel=cancel)
    attempt()
    if not target.exists():
        return "gone"

    logger.write("Falling back to rd /s /q")
    run_command(logger, [system32("cmd.exe"), "/c", "rd", "/s", "/q", str(target)],
                timeout=180, cancel=cancel)
    if not target.exists():
        return "gone"

    logger.write("Locked files remain - scheduling deletion at next restart")
    files, dirs = [], []
    for root, dnames, fnames in os.walk(target, topdown=True, followlinks=False):
        dnames[:] = [d for d in dnames if not _is_reparse(os.path.join(root, d))]
        dirs.extend(os.path.join(root, d) for d in dnames)
        files.extend(os.path.join(root, f) for f in fnames)
    scheduled = 0
    for f in files:
        scheduled += _schedule_delete_on_reboot(f)
    for d in sorted(dirs, key=len, reverse=True):
        scheduled += _schedule_delete_on_reboot(d)
    scheduled += _schedule_delete_on_reboot(str(target))
    return "reboot" if scheduled else "failed"


# ==========================================================================
# Uninstall
# ==========================================================================
_STORE_REMOVE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$pkg = '__PKG__'
try { Remove-AppxPackage -Package $pkg -AllUsers }
catch {
    try { Remove-AppxPackage -Package $pkg }
    catch { Write-Output ('ERR: ' + $_.Exception.Message); exit 1 }
}
Write-Output 'OK'
"""


def uninstall_app(app: AppEntry, logger: Logger, cancel: Canceller | None = None, step=None) -> dict:
    """
    Forcefully removes one program. Returns
    {"key", "name", "status": "removed"|"reboot"|"failed"|"stopped", "message"}.
    `step(text, fraction)` is called with progress messages.
    """
    def say(text: str, frac: float) -> None:
        logger.write(text)
        if step:
            try:
                step(text, frac)
            except Exception:
                pass

    result = {"key": app.key, "name": app.name, "status": "failed", "message": ""}
    logger.write(f"=== UNINSTALL: {app.name} {app.version} ({app.kind}) ===")
    try:
        if app.locked:
            result["message"] = "This is the program (or Python) that WinSmoother is running from."
            return result
        if app.kind == "store":
            return _uninstall_store(app, logger, cancel, say, result)
        return _uninstall_desktop(app, logger, cancel, say, result)
    except Exception as exc:
        logger.write(f"UNINSTALL ERROR: {type(exc).__name__}: {exc}")
        result["message"] = f"{type(exc).__name__}: {exc}"
        return result


def _uninstall_store(app, logger, cancel, say, result) -> dict:
    if app.location:
        say(f"Closing {app.name}...", 0.1)
        stop_everything_under(logger, app.location)
    say(f"Removing Store app {app.name}...", 0.4)
    script = _STORE_REMOVE_SCRIPT.replace("__PKG__", app.package_full_name.replace("'", "''"))
    rc, out = run_powershell(logger, script, timeout=300)
    if cancel and cancel.cancelled:
        result.update(status="stopped", message="Stopped by user")
    elif rc == 0:
        result.update(status="removed", message="Removed")
    else:
        result["message"] = out.replace("ERR: ", "")[:200] or f"Windows refused (code {rc})"
    return result


def _uninstall_desktop(app, logger, cancel, say, result) -> dict:
    wipe_ok, why = wipe_check(app.location) if app.location else (False, "no install folder")
    loc = os.path.expandvars(app.location) if wipe_ok else ""
    if not wipe_ok:
        logger.write(f"Folder will NOT be force-deleted ({why}): {app.location or '-'}")

    if loc:
        say(f"Closing {app.name} and stopping its services...", 0.10)
        stop_everything_under(logger, loc)

    uninstaller_ok = False
    rc = None
    cmd = build_uninstall_command(app)
    if cmd:
        cmdline, timeout = cmd
        say(f"Running the uninstaller of {app.name}...", 0.30)
        rc = _run_cmdline(logger, cmdline, timeout, cancel)
        if cancel and cancel.cancelled:
            result.update(status="stopped", message="Stopped by user")
            return result
        # Some uninstallers hand over to a temporary copy of themselves and return at once.
        _wait_until(lambda: not _key_exists(app), 20, cancel)
        # The registry entry vanishing is the real proof; 1605 = "not installed" (a ghost entry).
        uninstaller_ok = (not _key_exists(app)) or rc == 1605
    else:
        logger.write("No usable uninstall command - going straight to forced removal")

    wiped = "gone"
    if loc:
        say(f"Removing leftovers of {app.name}...", 0.65)
        stop_everything_under(logger, loc, delete_services=True)
        wiped = wipe_folder(logger, loc, cancel)
        if cancel and cancel.cancelled:
            result.update(status="stopped", message="Stopped by user")
            return result

    folder_gone = bool(loc) and wiped in ("gone", "reboot")
    if _key_exists(app) and (uninstaller_ok or folder_gone):
        say(f"Cleaning the registry entry of {app.name}...", 0.9)
        try:
            _delete_key_tree(app.reg_hive, app.reg_path, app.reg_view)
        except OSError as exc:
            logger.write(f"Registry cleanup failed: {exc}")

    if not (uninstaller_ok or folder_gone):
        result["message"] = (
            f"Uninstaller did not finish (code {rc}) and its folder could not be removed safely"
            if cmd else "No uninstaller found and the install folder is unknown or protected")
        return result

    if wiped == "reboot":
        result.update(status="reboot", message="Removed - restart Windows to finish deleting locked files")
    elif wiped == "failed":
        result.update(status="failed", message="Uninstalled, but some files could not be deleted")
    else:
        result.update(status="removed", message="Removed")
    return result
