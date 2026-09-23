"""Read-only warehouse reconciliation screen and explicit CSV handoff."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSpinBox, QTableWidget, QVBoxLayout, QWidget,
)

from annual_inventory.remanent_bridge import export_remanent_csv
from annual_inventory.warehouse_check import (
    STATUSES, MATCHED, PROBABLE_MATCH, annual_records, compare, identifier,
    load_receipt_snapshot, summary,
)
from .common import set_table, show_error


class WarehouseCheckPage(QWidget):
    def __init__(self, service) -> None:
        super().__init__()
        self.service = service
        self.receipts = None
        self.rows = ()
        layout = QVBoxLayout(self)
        heading = QLabel("Kontrola zgodności z magazynem")
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)
        layout.addWidget(QLabel(
            "Porównanie jest kontrolne. Snapshot pochodzi z kopii bazy ZIP46; ten ekran nie zmienia magazynu ani remanentu."))

        controls = QHBoxLayout()
        self.year = QSpinBox()
        self.year.setRange(2000, 2100)
        self.year.setValue(date.today().year)
        self.year.valueChanged.connect(self.refresh)
        load_button = QPushButton("Wczytaj snapshot przyjęć JSON")
        load_button.clicked.connect(self.load_snapshot)
        self.status_filter = QComboBox()
        self.status_filter.addItem("Wszystkie statusy", "")
        for status in STATUSES:
            self.status_filter.addItem(status, status)
        self.status_filter.currentIndexChanged.connect(self.show_rows)
        controls.addWidget(QLabel("Rok:"))
        controls.addWidget(self.year)
        controls.addWidget(load_button)
        controls.addWidget(QLabel("Status:"))
        controls.addWidget(self.status_filter)
        controls.addStretch()
        layout.addLayout(controls)
        self.snapshot_info = QLabel("Nie wczytano snapshotu. Brak danych do kontroli zgodności.")
        layout.addWidget(self.snapshot_info)
        self.totals = QLabel("")
        layout.addWidget(self.totals)
        self.table = QTableWidget()
        layout.addWidget(self.table, 1)

        export_controls = QHBoxLayout()
        self.basis = QComboBox()
        self.basis.addItem("Pełny landed cost", "landed_cost")
        self.basis.addItem("Towar + transport", "goods_transport")
        self.basis.addItem("Sam towar netto", "goods_net")
        export_button = QPushButton("Eksport CSV do podglądu remanentu")
        export_button.clicked.connect(self.export_csv)
        export_controls.addWidget(QLabel("Podstawa wyceny:"))
        export_controls.addWidget(self.basis)
        export_controls.addWidget(export_button)
        export_controls.addStretch()
        layout.addLayout(export_controls)

    def load_snapshot(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Snapshot przyjęć ZIP46", "", "JSON (*.json)")
        if not filename:
            return
        try:
            loaded = load_receipt_snapshot(Path(filename))
            self.receipts = loaded
            self.snapshot_info.setText(f"Snapshot: {filename} | pozycji przyjęć: {len(loaded)}")
            self.refresh()
        except Exception as exc:
            show_error(self, exc)

    def refresh(self) -> None:
        if self.receipts is None:
            self.rows = ()
            self.totals.setText("Wczytaj snapshot, aby zobaczyć statusy.")
        else:
            year = self.year.value()
            start, end = date(year, 1, 1) - timedelta(days=2), date(year, 12, 31) + timedelta(days=2)
            annual = annual_records(self.service.imports(), year)
            annual_documents = {identifier(row.document_no) for row in annual if row.document_no}
            annual_imports = {identifier(row.import_id) for row in annual if row.import_id}
            # Keep strong identifiers even when receipt and invoice fall in
            # different years; limit unrelated receipts to the selected year.
            source = tuple(row for row in self.receipts if
                start <= row.date <= end or identifier(row.document_no) in annual_documents
                or identifier(row.package_no) in annual_imports)
            self.rows = compare(annual, source)
            totals = summary(self.rows)
            self.totals.setText("  |  ".join(f"{status}: {totals[status]}" for status in STATUSES))
        self.show_rows()

    def show_rows(self) -> None:
        status = self.status_filter.currentData() or ""
        visible = [row for row in self.rows if not status or row.status == status]
        set_table(self.table, ["Status", "SKU", "Nazwa produktu", "Ilość RoczneRozliczenie",
            "Ilość magazyn", "Numer dokumentu", "Data", "Import / P/O", "Wartość PLN (landed)",
            "Źródło", "Uzasadnienie", "Pewność", "Uwagi"], [
            [row.status, row.sku, row.product_name,
             "" if row.annual_quantity is None else str(row.annual_quantity),
             "" if row.warehouse_quantity is None else str(row.warehouse_quantity),
             row.document_no, row.date, row.import_po, row.value_pln, row.source,
             row.match_reason, format(row.match_confidence, ".2f"), row.notes]
            for row in visible])

    def export_csv(self) -> None:
        try:
            year = self.year.value()
            imports = tuple(item for item in self.service.imports() if item.import_date.year == year)
            if not imports:
                QMessageBox.information(self, "Eksport remanentu", "Brak importów w wybranym roku.")
                return
            counts = summary(self.rows) if self.receipts is not None else {}
            matched = counts.get(MATCHED, 0)
            probable = counts.get(PROBABLE_MATCH, 0)
            warning = ("Ta pozycja prawdopodobnie została już uwzględniona w danych magazynowych.\n"
                       f"Dopasowane: {matched}; prawdopodobne: {probable}.\n"
                       "Eksport może podwoić zakupy w remanencie. Sprawdź pozycje przed importem.") if matched else (
                       "Brak wczytanego snapshotu — nie można ocenić ryzyka duplikacji.\n"
                       "Eksport może podwoić zakupy w remanencie." if self.receipts is None else
                       f"Kontrola: dopasowania prawdopodobne {probable}. Sprawdź pozycje przed importem.")
            answer = QMessageBox.warning(self, "Kontrola przed eksportem", warning,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            data = export_remanent_csv(imports, basis=self.basis.currentData(), year=year)
            filename, _ = QFileDialog.getSaveFileName(self, "Eksport CSV do remanentu",
                f"remanent_zakupy_{year}.csv", "CSV (*.csv)")
            if filename:
                Path(filename).write_bytes(data)
                QMessageBox.information(self, "Eksport remanentu",
                    "Zapisano CSV. W głównej aplikacji użyj podglądu importu i sprawdź duplikaty; nic nie zostało automatycznie zaimportowane.")
        except Exception as exc:
            show_error(self, exc)
