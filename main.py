from __future__ import annotations

from utils import (
    APP_NAME,
    IS_WINDOWS,
    acquire_single_instance,
    enable_dpi_awareness,
    is_admin,
    native_message,
    relaunch_as_admin,
)


def main() -> int:
    if not IS_WINDOWS:
        native_message(APP_NAME, "This application runs on Windows only.", error=True)
        return 2

    enable_dpi_awareness()

    if not is_admin():
        # Administrator rights are required for DISM / SFC / CHKDSK.
        if relaunch_as_admin():
            return 0
        native_message(
            APP_NAME,
            "Administrator permission is required to run system maintenance.\n\n"
            "Please start the application again and approve the Windows prompt.",
            error=True,
        )
        return 1

    if not acquire_single_instance():
        native_message(APP_NAME, f"{APP_NAME} is already running.")
        return 0

    from gui import WinSmootherApp

    WinSmootherApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
