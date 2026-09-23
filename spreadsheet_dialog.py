from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QListWidget, QPushButton, QTableWidget, QVBoxLayout,
)

from annual_inventory.application import InventoryApplication
from annual_inventory.spreadsheet import Workbook, load_workbook, suggest_mapping, validate_rows

from .common import set_table, show_error


FIELDS = {
    "products": (
        ("sku", "SKU"), ("model", "Model"), ("mass", "Masa jednostkowa (kg)"),
    ),
    "opening": (
        ("year", "Rok"), ("sku", "SKU"), ("quantity", "Ilość"),
        ("unit_value", "Wartość jednostkowa PLN"),
        ("total_value", "Wartość całkowita PLN"),
        ("source", "Źródło / referencja"),
    ),
}


class SpreadsheetImportDialog(QDialog):
    def __init__(self, service: InventoryApplication, kind: str, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.kind = kind
        self.workbook: Workbook | None = None
        self.records: tuple[object, ...] = ()
        self.mapping: dict[str, QComboBox] = {}
        self.setWindowTitle("Importuj kartotekę" if kind == "products" else "Importuj stan początkowy")
        self.resize(960, 660)
        root = QVBoxLayout(self)
        pick = QHBoxLayout()
        self.file_label = QLabel("Nie wybrano pliku")
        choose = QPushButton("Wybierz CSV / XLSX…")
        choose.clicked.connect(self.choose_file)
        pick.addWidget(choose)
        pick.addWidget(self.file_label, 1)
        root.addLayout(pick)
        sheet_line = QHBoxLayout()
        sheet_line.addWidget(QLabel("Arkusz:"))
        self.sheets = QComboBox()
        self.sheets.currentIndexChanged.connect(self.sheet_changed)
        sheet_line.addWidget(self.sheets, 1)
        root.addLayout(sheet_line)
        self.mapping_form = QFormLayout()
        for field, label in FIELDS[kind]:
            box = QComboBox()
            box.currentIndexChanged.connect(self.validate)
            self.mapping[field] = box
            self.mapping_form.addRow(label, box)
        root.addLayout(self.mapping_form)
        root.addWidget(QLabel("Podgląd danych (pierwsze 200 wierszy):"))
        self.preview = QTableWidget()
        root.addWidget(self.preview, 1)
        self.status = QLabel("Wybierz plik, a następnie sprawdź mapowanie kolumn.")
        root.addWidget(self.status)
        self.errors = QListWidget()
        self.errors.setMaximumHeight(115)
        root.addWidget(self.errors)
        actions = QHBoxLayout()
        self.import_button = QPushButton("Zatwierdź i zapisz całość")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self.commit)
        cancel = QPushButton("Anuluj")
        cancel.clicked.connect(self.reject)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(self.import_button)
        root.addLayout(actions)

    def choose_file(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Wybierz arkusz", "", "Arkusze (*.xlsx *.csv);;Wszystkie pliki (*)"
        )
        if not filename:
            return
        try:
            workbook = load_workbook(Path(filename))
        except (ValueError, OSError) as exc:
            show_error(self, exc)
            return
        self.workbook = workbook
        self.file_label.setText(filename)
        self.sheets.blockSignals(True)
        self.sheets.clear()
        self.sheets.addItems(sheet.name for sheet in workbook.sheets)
        self.sheets.blockSignals(False)
        self.sheet_changed()

    def sheet_changed(self) -> None:
        if self.workbook is None or self.sheets.currentIndex() < 0:
            return
        sheet = self.workbook.sheets[self.sheets.currentIndex()]
        guesses = suggest_mapping(sheet.headers, self.kind)
        for field, box in self.mapping.items():
            box.blockSignals(True)
            box.clear()
            box.addItem("— wybierz kolumnę —", -1)
            for index, header in enumerate(sheet.headers):
                box.addItem(f"{index + 1}. {header or '(bez nagłówka)'}", index)
            box.setCurrentIndex(guesses[field] + 1)
            box.blockSignals(False)
        set_table(self.preview, list(sheet.headers), [list(row) for _, row in sheet.rows[:200]])
        self.validate()

    def validate(self) -> None:
        self.import_button.setEnabled(False)
        self.records = ()
        self.errors.clear()
        if self.workbook is None or self.sheets.currentIndex() < 0:
            return
        sheet = self.workbook.sheets[self.sheets.currentIndex()]
        mapping = {field: box.currentData() if box.currentData() is not None else -1 for field, box in self.mapping.items()}
        try:
            if self.kind == "products":
                existing = {item.product.normalized_sku for item in self.service.products()}
                result = validate_rows(sheet, mapping, "products", existing)
            else:
                existing = self.service.existing_opening_keys()
                catalog = {item.product.normalized_sku for item in self.service.products()}
                result = validate_rows(sheet, mapping, "opening", existing, catalog)
        except Exception as exc:
            self.errors.addItem(str(exc))
            self.status.setText("Nie udało się sprawdzić pliku.")
            return
        self.records = result.records
        self.errors.addItems(result.errors[:200])
        if len(result.errors) > 200:
            self.errors.addItem(f"… i {len(result.errors) - 200} dalszych błędów")
        self.status.setText(f"Poprawnych: {len(result.records)}. Błędów: {len(result.errors)}. Zapis jest atomowy.")
        self.import_button.setEnabled(bool(result.records) and not result.errors)

    def commit(self) -> None:
        self.validate()
        if not self.import_button.isEnabled():
            return
        try:
            if self.kind == "products":
                self.service.import_products(self.records)
            else:
                self.service.import_opening_balances(self.records)
            self.accept()
        except Exception as exc:
            show_error(self, exc)
            self.validate()
