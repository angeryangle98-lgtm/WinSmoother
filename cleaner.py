from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
import time
from pathlib import Path

from utils import (
    Canceller,
    Logger,
    app_data_dir,
    delete_tree_contents,
    human_bytes,
    run_command,
    system32,
)

THUMBNAILS = "Windows thumbnail cache"


def _chromium_profiles(root: Path) -> list[Path]:
    profiles: list[Path] = []
    if not root.is_dir():
        return profiles
    try:
        for p in root.iterdir():
            if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile ")):
                profiles.append(p)
    except OSError:
        pass
    return profiles


def existing_paths() -> list[tuple[str, Path]]:
    """Known temp/cache locations only. Nothing here holds documents or settings."""
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))

    paths: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add(label: str, p: Path | str | None):
        if not p:
            return
        p = Path(p)
        key = str(p).lower()
        if key not in seen:  # %TEMP% and %TMP% usually point to the same folder
            seen.add(key)
            paths.append((label, p))

    add("User TEMP", os.environ.get("TEMP"))
    add("User TMP", os.environ.get("TMP"))
    add("Windows TEMP", windows / "Temp")
    add("WER report queue", program_data / "Microsoft" / "Windows" / "WER" / "ReportQueue")
    add("WER temp", program_data / "Microsoft" / "Windows" / "WER" / "Temp")
    add(
        "Delivery Optimization cache",
        windows / "ServiceProfiles" / "NetworkService" / "AppData" / "Local"
        / "Microsoft" / "Windows" / "DeliveryOptimization" / "Cache",
    )
    add("Internet cache", local / "Microsoft" / "Windows" / "INetCache")
    add(THUMBNAILS, local / "Microsoft" / "Windows" / "Explorer")
    add("D3D shader cache", local / "D3DSCache")
    add("NVIDIA DX cache", local / "NVIDIA" / "DXCache")
    add("NVIDIA GL cache", local / "NVIDIA" / "GLCache")
    add("AMD DX cache", local / "AMD" / "DxCache")
    add("User crash dumps", local / "CrashDumps")
    add("Windows minidumps", windows / "Minidump")

    browsers = (
        ("Chrome", local / "Google" / "Chrome" / "User Data"),
        ("Edge", local / "Microsoft" / "Edge" / "User Data"),
        ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
    )
    for name, root in browsers:
        for profile in _chromium_profiles(root):
            tag = f"{name} [{profile.name}]"
            add(f"{tag} HTTP cache", profile / "Cache")
            add(f"{tag} code cache", profile / "Code Cache")
            add(f"{tag} GPU cache", profile / "GPUCache")
            add(f"{tag} service worker cache", profile / "Service Worker" / "CacheStorage")

    firefox = local / "Mozilla" / "Firefox" / "Profiles"
    if firefox.is_dir():
        try:
            for prof in firefox.iterdir():
                add(f"Firefox [{prof.name[:8]}] cache", prof / "cache2")
        except OSError:
            pass

    # Only cache folders - never IndexedDB / Local Storage (those hold app data).
    for cache in ("Cache", "Code Cache", "GPUCache"):
        add(f"Teams (classic) {cache}", roaming / "Microsoft" / "Teams" / cache)
    new_teams = (
        local / "Packages" / "MSTeams_8wekyb3d8bbwe" / "LocalCache" / "Microsoft" / "MSTeams"
    )
    for cache in ("Cache", "Code Cache", "GPUCache"):
        add(f"Teams {cache}", new_teams / cache)

    return paths


def clean_thumbnail_files(logger: Logger, explorer_dir: Path) -> tuple[int, int, int]:
    count = size = skipped = 0
    if not explorer_dir.exists():
        return count, size, skipped
    try:
        for p in explorer_dir.glob("thumbcache_*.db"):
            try:
                if p.is_file():
                    s = p.stat().st_size
                    p.unlink()
                    count += 1
                    size += s
            except OSError as exc:  # Explorer keeps some of these open
                skipped += 1
                logger.write(f"SKIP thumbnail: {p} -> {exc}")
    except OSError as exc:
        skipped += 1
        logger.write(f"Thumbnail enumeration failed: {exc}")
    logger.write(f"{THUMBNAILS}: removed {count} files / {human_bytes(size)} / skipped {skipped}")
    return count, size, skipped


def clean_windows_update_download(
    logger: Logger, cancel: Canceller | None = None
) -> tuple[int, int, int]:
    target = Path(os.environ.get("WINDIR", r"C:\Windows")) / "SoftwareDistribution" / "Download"
    logger.write("Windows Update downloaded payload cleanup.")
    sc = system32("sc.exe")
    stopped: list[str] = []
    try:
        for service in ("wuauserv", "bits"):
            if run_command(logger, [sc, "stop", service], timeout=45) == 0:
                stopped.append(service)
        time.sleep(3)
        return delete_tree_contents(
            logger, target, "Windows Update download cache", cancel=cancel
        )
    finally:
        for service in stopped:  # always give the services back, even on error/cancel
            run_command(logger, [sc, "start", service], timeout=45)


def empty_recycle_bin(logger: Logger) -> None:
    logger.write("Emptying Recycle Bin (irreversible).")
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHEmptyRecycleBinW.restype = ctypes.c_int
        # NOCONFIRMATION | NOPROGRESSUI | NOSOUND
        result = shell32.SHEmptyRecycleBinW(None, None, 0x7)
        logger.write(f"Recycle Bin result code: {result & 0xFFFFFFFF:#x}")
    except Exception as exc:
        logger.write(f"Recycle Bin cleanup failed: {exc}")


def system_inventory(logger: Logger) -> dict:
    data = {"computer": platform.node(), "windows": platform.platform(), "drives": []}
    logger.write(f"Computer: {data['computer']}")
    logger.write(f"Windows: {data['windows']}")

    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{drive}:\\")
        if root.exists():
            try:
                usage = shutil.disk_usage(root)
                data["drives"].append(
                    {"drive": drive, "total": usage.total, "used": usage.used, "free": usage.free}
                )
            except OSError:
                pass
    return data


def _protected_paths() -> tuple[Path, ...]:
    """Never delete the running app (PyInstaller extracts itself into %TEMP%) or our logs."""
    paths = [Path(__file__).resolve().parent, app_data_dir()]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass))
    return tuple(paths)


def run_cleaner(
    logger: Logger,
    progress=None,
    cancel: Canceller | None = None,
    empty_bin: bool = True,
    span: int = 50,
) -> dict:
    """Cleanup occupies 0..span percent of the overall progress."""
    inventory = system_inventory(logger)
    paths = existing_paths()
    protected = _protected_paths()

    result = {
        "items_removed": 0,
        "bytes_removed": 0,
        "skipped": 0,
        "targets": len(paths),
        "recycle_bin_emptied": False,
        "inventory": inventory,
    }

    total_steps = len(paths) + 1 + (1 if empty_bin else 0)
    step = 0

    def tick(label: str) -> None:
        nonlocal step
        step += 1
        if progress:
            progress(int(step / total_steps * span), label, "cleanup")

    def add(c: int, b: int, s: int) -> None:
        result["items_removed"] += c
        result["bytes_removed"] += b
        result["skipped"] += s

    for label, path in paths:
        if cancel and cancel.cancelled:
            return result
        tick(f"Cleaning {label}")
        if label == THUMBNAILS:
            add(*clean_thumbnail_files(logger, path))
        else:
            add(*delete_tree_contents(logger, path, label, protected, cancel))

    if cancel and cancel.cancelled:
        return result
    tick("Cleaning Windows Update download cache")
    add(*clean_windows_update_download(logger, cancel))

    if empty_bin and not (cancel and cancel.cancelled):
        tick("Emptying Recycle Bin")
        empty_recycle_bin(logger)
        result["recycle_bin_emptied"] = True

    logger.write(
        f"Cleanup summary: removed {result['items_removed']} files; "
        f"reclaimed {human_bytes(result['bytes_removed'])}; skipped {result['skipped']}"
    )
    return result
