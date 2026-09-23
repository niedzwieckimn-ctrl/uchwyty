from __future__ import annotations

from datetime import date
from decimal import Decimal

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QLineEdit, QVBoxLayout,
)

from annual_inventory.application import InventoryApplication, ProductListing
from annual_inventory.models import OpeningBalance

from .common import decimal_input, integer_input, show_error


class ProductDialog(QDialog):
    def __init__(self, service: InventoryApplication, existing: ProductListing | None = None, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.existing = existing
        self.setWindowTitle("Edytuj produkt" if existing else "Dodaj produkt")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.sku = QLineEdit(existing.product.sku if existing else "")
        self.sku.setReadOnly(existing is not None)
        self.model = QLineEdit(existing.product.model if existing else "")
        self.mass = QLineEdit(str(existing.product.unit_mass_kg) if existing else "")
        self.mass.setPlaceholderText("np. 0,184")
        self.active = QCheckBox("Aktywny")
        self.active.setChecked(existing.is_active if existing else True)
        self.active.setEnabled(existing is not None)
        form.addRow("SKU", self.sku)
        form.addRow("Model", self.model)
        form.addRow("Masa jedn. (kg)", self.mass)
        form.addRow("Status", self.active)
        layout.addLayout(form)
        layout.addWidget(QLabel("Zmiana masy nie przelicza historycznych importów."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self) -> None:
        try:
            mass = decimal_input(self.mass.text(), "Masa jednostkowa")
            if self.existing:
                self.service.update_product(self.existing.product.sku, self.model.text(), mass, self.active.isChecked())
            else:
                self.service.add_product(self.sku.text(), self.model.text(), mass)
            self.accept()
        except (ValueError, TypeError) as exc:
            show_error(self, exc)


class OpeningBalanceDialog(QDialog):
    def __init__(self, service: InventoryApplication, year: int, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Dodaj stan początkowy")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.year = QLineEdit(str(year))
        self.sku = QComboBox()
        self.sku.setEditable(True)
        self.sku.addItems(item.product.sku for item in service.products())
        self.quantity = QLineEdit()
        self.unit_value = QLineEdit()
        self.total_value = QLineEdit()
        self.source = QLineEdit()
        self.unit_value.setPlaceholderText("PLN/szt.")
        self.total_value.setPlaceholderText("PLN")
        form.addRow("Rok", self.year)
        form.addRow("SKU", self.sku)
        form.addRow("Ilość", self.quantity)
        form.addRow("Wartość jedn. PLN", self.unit_value)
        form.addRow("Wartość łączna PLN", self.total_value)
        form.addRow("Źródło / referencja", self.source)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self) -> None:
        try:
            balance = OpeningBalance(
                integer_input(self.year.text(), "Rok"), self.sku.currentText(),
                integer_input(self.quantity.text(), "Ilość"),
                decimal_input(self.unit_value.text(), "Wartość jednostkowa"),
                decimal_input(self.total_value.text(), "Wartość łączna"),
                self.source.text(),
            )
            self.service.add_opening_balance(balance)
            self.accept()
        except (ValueError, TypeError) as exc:
            show_error(self, exc)
