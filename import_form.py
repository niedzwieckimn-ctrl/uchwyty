from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QDialog, QFileDialog, QFormLayout, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from annual_inventory.application import InventoryApplication, new_import_code
from annual_inventory.models import (
    AllocationMethod, CostComponent, CostType, ImportLine, ImportPackage,
)

from .common import decimal_input, money, show_error


class ManualImportDialog(QDialog):
    COLUMNS = ["SKU", "Model", "Masa jedn. kg", "Ilość", "Rodzaj ceny", "Cena/wartość netto", "Masa kg", "Netto PLN", "Udział masy", "Transport PLN", "Z transportem PLN"]

    def __init__(self, service: InventoryApplication, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.code = new_import_code()
        self.catalog = {item.sku: item for item in service.active_products()}
        self._updating = False
        self.setWindowTitle("Nowy import / faktura — ręcznie")
        self.resize(1250, 770)
        root = QVBoxLayout(self)
        root.addWidget(QLabel(f"Nowa dostawa: {self.code}"))
        grid = QGridLayout()
        self.date = QDateEdit(QDate.currentDate())
        self.date.setCalendarPopup(True)
        self.invoice = QLineEdit()
        self.supplier = QComboBox()
        self.supplier.setEditable(True)
        self.supplier.addItems(service.suppliers())
        self.goods_currency = QComboBox()
        self.goods_currency.setEditable(True)
        self.goods_currency.addItems(["PLN", "USD", "EUR"])
        self.goods_rate = QLineEdit("1")
        self.actual_mass = QLineEdit()
        self.actual_mass.setPlaceholderText("opcjonalnie")
        self.transport = QLineEdit("0")
        self.transport_currency = QComboBox()
        self.transport_currency.setEditable(True)
        self.transport_currency.addItems(["PLN", "USD", "EUR"])
        self.transport_rate = QLineEdit("1")
        fields = [
            ("Data", self.date), ("Nr faktury", self.invoice), ("Dostawca", self.supplier),
            ("Waluta towaru", self.goods_currency), ("Kurs towaru do PLN", self.goods_rate),
            ("Rzeczywista masa paczki (kg)", self.actual_mass),
            ("Transport", self.transport), ("Waluta transportu", self.transport_currency),
            ("Kurs transportu do PLN", self.transport_rate),
        ]
        for index, (label, widget) in enumerate(fields):
            column = index // 5
            row = index % 5
            grid.addWidget(QLabel(label), row, column * 2)
            grid.addWidget(widget, row, column * 2 + 1)
        root.addLayout(grid)
        pdf_line = QHBoxLayout()
        pdf_line.addWidget(QLabel("PDF źródłowy"))
        self.pdf = QLineEdit()
        self.pdf.setReadOnly(True)
        select_pdf = QPushButton("Wybierz PDF…")
        select_pdf.clicked.connect(self.choose_pdf)
        pdf_line.addWidget(self.pdf, 1)
        pdf_line.addWidget(select_pdf)
        root.addLayout(pdf_line)
        buttons = QHBoxLayout()
        add_line = QPushButton("Dodaj pozycję")
        add_line.clicked.connect(self.add_line)
        remove_line = QPushButton("Usuń zaznaczoną")
        remove_line.clicked.connect(self.remove_line)
        buttons.addWidget(add_line)
        buttons.addWidget(remove_line)
        buttons.addStretch()
        root.addLayout(buttons)
        self.lines = QTableWidget(0, len(self.COLUMNS))
        self.lines.setHorizontalHeaderLabels(self.COLUMNS)
        self.lines.setAlternatingRowColors(True)
        self.lines.setMinimumHeight(310)
        root.addWidget(self.lines, 1)
        self.status = QLabel("Dodaj pozycję i uzupełnij dane dostawy.")
        root.addWidget(self.status)
        self.summary = QLabel("Masa: —    Netto PLN: —    Transport PLN: —    Razem: —")
        root.addWidget(self.summary)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("Anuluj")
        cancel.clicked.connect(self.reject)
        self.save = QPushButton("Sprawdź i zatwierdź import")
        self.save.clicked.connect(self.approve)
        actions.addWidget(cancel)
        actions.addWidget(self.save)
        root.addLayout(actions)
        for widget in (self.invoice, self.goods_rate, self.actual_mass, self.transport, self.transport_rate):
            widget.textChanged.connect(self.recalculate)
        for widget in (self.date, self.supplier, self.goods_currency, self.transport_currency):
            if isinstance(widget, QDateEdit):
                widget.dateChanged.connect(self.recalculate)
            else:
                widget.currentTextChanged.connect(self.recalculate)
        self.goods_currency.currentTextChanged.connect(lambda value: self._currency_changed(value, self.goods_rate))
        self.transport_currency.currentTextChanged.connect(lambda value: self._currency_changed(value, self.transport_rate))
        if self.catalog:
            self.add_line()
        else:
            self.status.setText("Brak aktywnych produktów. Najpierw dodaj lub importuj kartotekę.")
            self.save.setEnabled(False)

    def _currency_changed(self, value: str, rate: QLineEdit) -> None:
        rate.setText("1" if value.strip().upper() == "PLN" else "")

    def choose_pdf(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Wybierz oryginalną fakturę PDF", "", "PDF (*.pdf)")
        if filename:
            self.pdf.setText(filename)

    def _readonly_item(self, value: str = "") -> QTableWidgetItem:
        item = QTableWidgetItem(value)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def add_line(self) -> None:
        if not self.catalog:
            return
        row = self.lines.rowCount()
        self.lines.insertRow(row)
        sku = QComboBox()
        sku.addItems(sorted(self.catalog))
        sku.currentTextChanged.connect(lambda _=None, widget=sku: self._sku_changed(self._row_of_widget(widget)))
        quantity = QSpinBox()
        quantity.setRange(1, 10_000_000)
        quantity.valueChanged.connect(self.recalculate)
        mode = QComboBox()
        mode.addItems(["Cena jednostkowa", "Wartość całej pozycji"])
        mode.currentIndexChanged.connect(self.recalculate)
        price = QLineEdit()
        price.setPlaceholderText("np. 12,50")
        price.textChanged.connect(self.recalculate)
        self.lines.setCellWidget(row, 0, sku)
        self.lines.setCellWidget(row, 3, quantity)
        self.lines.setCellWidget(row, 4, mode)
        self.lines.setCellWidget(row, 5, price)
        for col in (1, 2, 6, 7, 8, 9, 10):
            self.lines.setItem(row, col, self._readonly_item())
        self._sku_changed(row)
        self.lines.resizeColumnsToContents()

    def remove_line(self) -> None:
        row = self.lines.currentRow()
        if row >= 0:
            self.lines.removeRow(row)
            self.recalculate()

    def _row_of_widget(self, widget: QComboBox) -> int:
        return next((row for row in range(self.lines.rowCount()) if self.lines.cellWidget(row, 0) is widget), -1)

    def _sku_changed(self, row: int) -> None:
        if row < 0 or row >= self.lines.rowCount():
            return
        product = self.catalog[self.lines.cellWidget(row, 0).currentText()]
        self.lines.item(row, 1).setText(product.model)
        self.lines.item(row, 2).setText(str(product.unit_mass_kg))
        self.recalculate()

    def _package(self) -> ImportPackage:
        if self.lines.rowCount() == 0:
            raise ValueError("Dodaj przynajmniej jedną pozycję.")
        rows = []
        for row in range(self.lines.rowCount()):
            sku = self.lines.cellWidget(row, 0).currentText()
            product = self.catalog[sku]
            quantity = self.lines.cellWidget(row, 3).value()
            mode = self.lines.cellWidget(row, 4).currentIndex()
            price = decimal_input(self.lines.cellWidget(row, 5).text(), f"Pozycja {row + 1}: cena/wartość")
            rows.append(ImportLine(
                f"L-{row + 1}", product, quantity,
                unit_price_source=price if mode == 0 else None,
                total_value_source=price if mode == 1 else None,
            ))
        qdate = self.date.date()
        transport = CostComponent(
            "TRANSPORT", CostType.TRANSPORT,
            decimal_input(self.transport.text(), "Transport"),
            self.transport_currency.currentText().strip().upper(),
            decimal_input(self.transport_rate.text(), "Kurs transportu"),
            AllocationMethod.MASS,
        )
        return ImportPackage(
            self.code, date(qdate.year(), qdate.month(), qdate.day()),
            self.goods_currency.currentText().strip().upper(),
            decimal_input(self.goods_rate.text(), "Kurs towaru"),
            tuple(rows), (transport,),
            decimal_input(self.actual_mass.text(), "Masa rzeczywista", optional=True),
            invoice_number=self.invoice.text(), supplier=self.supplier.currentText(),
        )

    def recalculate(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            package = self._package()
            result = self.service.preview_import(package)
            for row, line in enumerate(result.lines):
                transport_share = next((allocation.share for allocation in line.allocations if allocation.component_id == "TRANSPORT"), None)
                values = {
                    6: str(line.line_mass_kg),
                    7: money(line.goods_value_pln),
                    8: f"{transport_share * 100:.2f}%" if transport_share is not None else "—",
                    9: money(line.transport_allocated_pln),
                    10: money(line.value_with_transport_pln),
                }
                for col, value in values.items():
                    self.lines.item(row, col).setText(value)
            self.summary.setText(
                f"Masa: {result.calculated_package_mass_kg} kg    "
                f"Netto: {money(result.total_goods_value_pln)} PLN    "
                f"Transport: {money(result.total_transport_allocated_pln)} PLN    "
                f"Razem: {money(result.total_value_with_transport_pln)} PLN"
            )
            self.status.setText("Podgląd policzony przez silnik ETAPU 1. Sprawdź dane przed zatwierdzeniem.")
            self.save.setEnabled(True)
        except (ValueError, TypeError) as exc:
            self.status.setText(str(exc))
            self.summary.setText("Uzupełnij poprawnie pola, aby zobaczyć wyliczenia.")
            self.save.setEnabled(False)
            for row in range(self.lines.rowCount()):
                for col in (6, 7, 8, 9, 10):
                    item = self.lines.item(row, col)
                    if item is not None:
                        item.setText("")
        finally:
            self._updating = False

    def approve(self) -> None:
        try:
            package = self._package()
            result = self.service.preview_import(package)
            response = QMessageBox.question(
                self, "Zatwierdź dostawę",
                f"Faktura: {package.invoice_number}\nDostawca: {package.supplier}\n"
                f"Pozycji: {len(result.lines)}; sztuk: {sum(line.quantity for line in result.lines)}\n"
                f"Masa: {result.calculated_package_mass_kg} kg\n"
                f"Netto: {money(result.total_goods_value_pln)} PLN\n"
                f"Transport: {money(result.total_transport_allocated_pln)} PLN\n"
                f"Razem: {money(result.total_value_with_transport_pln)} PLN\n\nZapisać całą dostawę?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if response != QMessageBox.StandardButton.Yes:
                return
            pdf = Path(self.pdf.text()) if self.pdf.text().strip() else None
            self.service.post_import(package, pdf)
            self.accept()
        except (ValueError, TypeError, OSError) as exc:
            show_error(self, exc)
