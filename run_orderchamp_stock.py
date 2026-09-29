"""Run in the existing Render web service Shell, never in a separate diskless job."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from orderchamp_client import OrderchampClient, OrderchampError
from orderchamp_stock_sync import dry_run_stock_sync


def main(argv=None):
    parser = argparse.ArgumentParser(description="Orderchamp: odczyt i dry run; bez zapisu stanów.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("test-connection", help="Test backendowego tokenu i odczytu produktów")
    dry = commands.add_parser("dry-run", help="Porównaj wszystkie lokalne SKU z Orderchamp")
    dry.add_argument("--db", type=Path, help="Domyślnie APP_DATA_DIR/app.db, jak w aplikacji")
    dry.add_argument("--sku", help="Opcjonalnie jedno dokładne SKU")
    dry.add_argument("--report", type=Path, help="Nowy plik .json; istniejącego pliku nie nadpisujemy")
    args = parser.parse_args(argv)
    client = None
    try:
        if args.command == "dry-run" and args.report is not None:
            if args.report.suffix.lower() != ".json" or args.report.exists():
                raise OrderchampError("REPORT_PATH_MUST_BE_NEW_JSON")
        client = OrderchampClient.from_env()
        if args.command == "test-connection":
            print(json.dumps(client.test_connection(), ensure_ascii=False))
            return 0
        default_dir = Path(os.environ.get("APP_DATA_DIR") or Path(__file__).resolve().parent / "data")
        db = args.db if args.db is not None else default_dir / "app.db"
        report = dry_run_stock_sync(client, db, sku=args.sku)
        text = client.redact(json.dumps(report, ensure_ascii=False, indent=2))
        if args.report is not None:
            try:
                # Exclusive creation also prevents overwriting after the early check.
                with args.report.open("x", encoding="utf-8") as stream:
                    stream.write(text + "\n")
            except OSError:
                print(text)
                raise OrderchampError("REPORT_WRITE_FAILED_RESULT_ON_STDOUT") from None
            print(client.redact(json.dumps({"mode": "dry_run", "writes_enabled": False,
                                           "summary": report["summary"],
                                           "report": str(args.report)}, ensure_ascii=False)))
        else:
            print(text)
        if report["summary"]["errors"] or not report["summary"]["local_sku"]:
            return 2
        if report["summary"]["missing"] or report["summary"]["warning_rows"]:
            return 1
        return 0
    except OrderchampError as exc:
        print(json.dumps({"ok": False, "mode": "read_only", "error_code": exc.code}), file=sys.stderr)
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
