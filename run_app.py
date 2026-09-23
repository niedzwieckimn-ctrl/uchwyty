"""Entry point for the windowed Windows executable; no local server."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

def _startup_error(exc: BaseException) -> None:
    try:
        from annual_inventory.persistence import AppPaths
        paths = AppPaths.for_current_user()
        paths.ensure_directories()
        logger = logging.getLogger("annual_inventory.startup")
        logger.setLevel(logging.ERROR)
        handler = RotatingFileHandler(paths.logs_dir / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        logger.addHandler(handler)
        logger.error("Nieoczekiwany błąd aplikacji", exc_info=(type(exc), exc, exc.__traceback__))
        logger.removeHandler(handler)
        handler.close()
        location = str(paths.logs_dir / "app.log")
    except Exception:
        fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "RoczneRozliczenie" / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        location = str(fallback / "app.log")
        with (fallback / "app.log").open("a", encoding="utf-8") as log_file:
            log_file.write(f"Błąd uruchamiania: {type(exc).__name__}: {exc}\n")
    message = f"Aplikacja napotkała błąd. Szczegóły zapisano w:\n{location}\n\n{exc}"
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "Nie udało się uruchomić Rocznego Rozliczenia", message)
            return
    except Exception:
        pass
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Roczne Rozliczenie — błąd", 0x10)


def main() -> int:
    runtime = None
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from annual_inventory.application import InventoryApplication
        from annual_inventory.persistence import initialize_app
        from annual_inventory.ui.main_window import MainWindow

        app = QApplication(sys.argv)
        app.setApplicationName("Roczne Rozliczenie")
        app.setOrganizationName("RoczneRozliczenie")
        runtime = initialize_app()
        service = InventoryApplication(runtime)
        window = MainWindow(service)
        window.show()

        def handle_exception(kind: type[BaseException], value: BaseException, tb: object) -> None:
            _startup_error(value)

        sys.excepthook = handle_exception
        if "--smoke-test" in sys.argv:
            service.products()
            service.imports()
            QTimer.singleShot(200, app.quit)
        return app.exec()
    except Exception as exc:
        _startup_error(exc)
        return 1
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
