# -*- coding: utf-8 -*-
"""Zadanie cykliczne Rendera dla przypomnień o płatności."""

import json
import sys

from app import app, app_now, pull_shared_tables_from_supabase
from app import send_automatic_payment_reminders


def main() -> int:
    now = app_now()
    force = "--force" in sys.argv
    # Cron działa co godzinę, dzięki czemu zmiana czasu lato/zima nie wymaga
    # edycji harmonogramu UTC po stronie Rendera.
    if not force and now.hour != 12:
        print(json.dumps({"ok": True, "skipped": True, "local_time": now.isoformat()}, ensure_ascii=False))
        return 0

    try:
        pull_shared_tables_from_supabase(force=True)
    except Exception as exc:
        app.logger.exception("Nie udało się pobrać danych przed przypomnieniami")
        print(json.dumps({"ok": False, "stage": "sync", "error": str(exc)}, ensure_ascii=False))
        return 1

    with app.app_context():
        result = send_automatic_payment_reminders(reference_time=now)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
