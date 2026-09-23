from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QMessageBox, QTableWidget, QTableWidgetItem


def decimal_input(text: str, label: str, *, optional: bool = False) -> Decimal | None:
    cleaned = text.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if optional and not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"{label}: wpisz poprawną liczbę.") from exc
    if not value.is_finite():
        raise ValueError(f"{label}: liczba musi być skończona.")
    return value


def integer_input(text: str, label: str) -> int:
    cleaned = text.strip()
    if not cleaned.isdecimal():
        raise ValueError(f"{label}: wpisz liczbę całkowitą.")
    return int(cleaned)


def money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):,.2f}".replace(",", " ")


def set_table(table: QTableWidget, headers: list[str], rows: list[list[str]]) -> None:
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setRowCount(len(rows))
    for row_number, values in enumerate(rows):
        for column_number, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_number, column_number, item)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.resizeColumnsToContents()


def show_error(parent: object, error: Exception) -> None:
    QMessageBox.warning(parent, "Nie można wykonać operacji", str(error))
