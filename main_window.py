from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMainWindow, QMessageBox, QPushButton, QSpinBox, QStackedWidget,
    QTableWidget, QVBoxLayout, QWidget,
)

from annual_inventory.application import InventoryApplication
from annual_inventory.models import AnnualSkuAggregate, CalculatedImport

from .common import money, set_table, show_error
from .forms import OpeningBalanceDialog, ProductDialog
from .import_form import ManualImportDialog
from .spreadsheet_dialog import SpreadsheetImportDialog
from .warehouse_check_page import WarehouseCheckPage


def _page(title: str) -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    layout = QVBoxLayout(page)
    heading = QLabel(title)
    heading.setObjectName("PageTitle")
    layout.addWidget(heading)
    return page, layout


class DashboardPage(QWidget):
    def __init__(self, service: InventoryApplication) -> None:
        super().__init__()
        self.service = service
        layout = QVBoxLayout(self)
        heading = QLabel("Pulpit")
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)
        self.counts = QLabel()
        self.counts.setObjectName("DashboardCounts")
        layout.addWidget(self.counts)
        layout.addWidget(QLabel("Dane poniżej pochodzą wyłącznie z Twojej lokalnej bazy."))
        layout.addStretch()

    def refresh(self) -> None:
        products = self.service.products()
        imports = self.service.imports()
        self.counts.setText(
            f"Produkty: {len(products)} (aktywne: {sum(item.is_active for item in products)})\n"
            f"Zapisane importy: {len(imports)}\n"
            f"Kupione sztuki we wszystkich zapisanych importach: "
            f"{sum(line.quantity for item in imports for line in item.lines)}"
        )


class ProductsPage(QWidget):
    def __init__(self, service: InventoryApplication) -> None:
        super().__init__()
        self.service = service
        self.records = ()
        layout = QVBoxLayout(self)
        title = QLabel("Produkty / kartoteka")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        actions = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Szukaj po SKU lub modelu…")
        self.search.textChanged.connect(self.refresh)
        add = QPushButton("Dodaj produkt")
        add.clicked.connect(self.add)
        edit = QPushButton("Edytuj zaznaczony")
        edit.clicked.connect(self.edit)
        import_button = QPushButton("Importuj kartotekę CSV/XLSX")
        import_button.clicked.connect(self.import_sheet)
        actions.addWidget(self.search, 1)
        actions.addWidget(add)
        actions.addWidget(edit)
        actions.addWidget(import_button)
        layout.addLayout(actions)
        self.table = QTableWidget()
        self.table.doubleClicked.connect(self.edit)
        layout.addWidget(self.table, 1)

    def refresh(self) -> None:
        self.records = self.service.products(self.search.text())
        set_table(self.table, ["SKU", "Model", "Masa jedn. (kg)", "Status"], [
            [item.product.sku, item.product.model, str(item.product.unit_mass_kg),
             "Aktywny" if item.is_active else "Nieaktywny"] for item in self.records
        ])

    def add(self) -> None:
        if ProductDialog(self.service, parent=self).exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def edit(self, *_: object) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.records):
            QMessageBox.information(self, "Produkt", "Zaznacz produkt do edycji.")
            return
        if ProductDialog(self.service, self.records[row], self).exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def import_sheet(self) -> None:
        if SpreadsheetImportDialog(self.service, "products", self).exec() == QDialog.DialogCode.Accepted:
            self.refresh()


class ImportDetailsDialog(QDialog):
    def __init__(self, item: CalculatedImport, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Import {item.import_id}")
        self.resize(1050, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"{item.import_date.isoformat()}  |  Faktura {item.invoice_number}  |  {item.supplier}\n"
            f"Waluta towaru: {item.goods_currency}; kurs: {item.goods_rate_to_pln}\n"
            f"Masa z kartoteki: {item.calculated_package_mass_kg} kg; "
            f"masa rzeczywista: {item.actual_package_mass_kg if item.actual_package_mass_kg is not None else '—'} kg"
        ))
        table = QTableWidget()
        set_table(table, ["SKU", "Model", "Ilość", "Masa jedn. kg", "Masa pozycji kg", "Towar netto PLN", "Transport PLN", "Z transportem PLN"], [
            [line.sku, line.model, str(line.quantity), str(line.unit_mass_kg_snapshot),
             str(line.line_mass_kg), money(line.goods_value_pln), money(line.transport_allocated_pln),
             money(line.value_with_transport_pln)] for line in item.lines
        ])
        layout.addWidget(table, 1)
        layout.addWidget(QLabel(
            f"Towar netto: {money(item.total_goods_value_pln)} PLN     "
            f"Transport: {money(item.total_transport_allocated_pln)} PLN     "
            f"Z transportem: {money(item.total_value_with_transport_pln)} PLN"
        ))
        close = QPushButton("Zamknij")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class ImportsPage(QWidget):
    def __init__(self, service: InventoryApplication) -> None:
        super().__init__()
        self.service = service
        self.records = ()
        layout = QVBoxLayout(self)
        heading = QLabel("Importy / dostawy")
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)
        actions = QHBoxLayout()
        new_button = QPushButton("Dodaj dostawę ręcznie")
        new_button.clicked.connect(self.add)
        details = QPushButton("Otwórz szczegóły")
        details.clicked.connect(self.open_details)
        actions.addWidget(new_button)
        actions.addWidget(details)
        actions.addStretch()
        layout.addLayout(actions)
        self.table = QTableWidget()
        self.table.doubleClicked.connect(self.open_details)
        layout.addWidget(self.table, 1)

    def refresh(self) -> None:
        self.records = self.service.imports()
        set_table(self.table, ["Data", "Nr faktury", "Dostawca", "Pozycji", "Sztuk", "Netto PLN", "Transport PLN", "Z transportem PLN", "Kod importu"], [
            [item.import_date.isoformat(), item.invoice_number, item.supplier,
             str(len(item.lines)), str(sum(line.quantity for line in item.lines)),
             money(item.total_goods_value_pln), money(item.total_transport_allocated_pln),
             money(item.total_value_with_transport_pln), item.import_id] for item in self.records
        ])

    def add(self) -> None:
        if ManualImportDialog(self.service, self).exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def open_details(self, *_: object) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.records):
            QMessageBox.information(self, "Import", "Zaznacz import.")
            return
        ImportDetailsDialog(self.records[row], self).exec()


class OpeningPage(QWidget):
    def __init__(self, service: InventoryApplication) -> None:
        super().__init__()
        self.service = service
        layout = QVBoxLayout(self)
        heading = QLabel("Magazyn — stan początkowy")
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)
        controls = QHBoxLayout()
        self.year = QSpinBox()
        self.year.setRange(1900, 9999)
        self.year.setValue(date.today().year)
        self.year.valueChanged.connect(self.refresh)
        add = QPushButton("Dodaj ręcznie")
        add.clicked.connect(self.add)
        import_button = QPushButton("Importuj CSV/XLSX")
        import_button.clicked.connect(self.import_sheet)
        controls.addWidget(QLabel("Rok:"))
        controls.addWidget(self.year)
        controls.addWidget(add)
        controls.addWidget(import_button)
        controls.addStretch()
        layout.addLayout(controls)
        layout.addWidget(QLabel("Wartość otwarcia jest zachowana osobno; nie jest automatycznie mieszana z zakupami roku."))
        self.table = QTableWidget()
        layout.addWidget(self.table, 1)

    def refresh(self) -> None:
        records = self.service.opening_balances(self.year.value())
        set_table(self.table, ["Rok", "SKU", "Ilość", "Wartość jedn. PLN", "Wartość łączna PLN", "Źródło / referencja"], [
            [str(item.year), item.sku, str(item.quantity), str(item.unit_value_pln),
             money(item.total_value_pln), item.source_reference] for item in records
        ])

    def add(self) -> None:
        if OpeningBalanceDialog(self.service, self.year.value(), self).exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def import_sheet(self) -> None:
        if SpreadsheetImportDialog(self.service, "opening", self).exec() == QDialog.DialogCode.Accepted:
            self.refresh()


class SkuDetailsDialog(QDialog):
    def __init__(self, aggregate: AnnualSkuAggregate, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Źródła roczne SKU {aggregate.sku}")
        self.resize(900, 460)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{aggregate.sku} — {aggregate.model} | importów: {aggregate.import_count} | kupiono: {aggregate.total_purchased_quantity} szt."))
        table = QTableWidget()
        set_table(table, ["Data", "Import", "Faktura", "Ilość", "Netto PLN", "Transport PLN", "Z transportem PLN"], [
            [contribution.calculated_import.import_date.isoformat(), contribution.calculated_import.import_id,
             contribution.calculated_import.invoice_number, str(contribution.line.quantity),
             money(contribution.line.goods_value_pln), money(contribution.line.transport_allocated_pln),
             money(contribution.line.value_with_transport_pln)]
            for contribution in aggregate.contributions
        ])
        layout.addWidget(table, 1)
        close = QPushButton("Zamknij")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class AnnualPage(QWidget):
    def __init__(self, service: InventoryApplication) -> None:
        super().__init__()
        self.service = service
        self.records = ()
        layout = QVBoxLayout(self)
        heading = QLabel("Rozliczenie roczne — techniczne koszty zakupów")
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)
        controls = QHBoxLayout()
        self.year = QSpinBox()
        self.year.setRange(1900, 9999)
        self.year.setValue(date.today().year)
        self.year.valueChanged.connect(self.refresh)
        controls.addWidget(QLabel("Rok:"))
        controls.addWidget(self.year)
        controls.addWidget(QLabel("Dwuklik SKU pokazuje importy źródłowe."))
        controls.addStretch()
        layout.addLayout(controls)
        self.table = QTableWidget()
        self.table.doubleClicked.connect(self.open_details)
        layout.addWidget(self.table, 1)
        layout.addWidget(QLabel("To nie jest jeszcze końcowa księgowa ani podatkowa wycena magazynu."))

    def refresh(self) -> None:
        self.records = self.service.annual_summary(self.year.value())
        set_table(self.table, ["SKU", "Model", "Importów", "Kupiono szt.", "Wartość netto PLN", "Transport PLN", "Z transportem PLN", "Śr. netto/szt. PLN", "Śr. z transportem/szt. PLN"], [
            [item.sku, item.model, str(item.import_count), str(item.total_purchased_quantity),
             money(item.total_goods_value_pln), money(item.total_transport_allocated_pln),
             money(item.total_value_with_transport_pln), money(item.average_net_unit_value_pln),
             money(item.average_unit_value_with_transport_pln)] for item in self.records
        ])

    def open_details(self, *_: object) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.records):
            SkuDetailsDialog(self.records[row], self).exec()


class SettingsPage(QWidget):
    def __init__(self, service: InventoryApplication) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        heading = QLabel("Ustawienia")
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)
        paths = service.runtime.paths
        layout.addWidget(QLabel(
            f"Katalog danych użytkownika:\n{paths.root}\n\n"
            f"Baza danych:\n{paths.database_file}\n\n"
            f"Dokumenty PDF:\n{paths.documents_dir}\n\n"
            f"Log błędów:\n{paths.logs_dir / 'app.log'}"
        ))
        layout.addWidget(QLabel("Dane pozostają w tym katalogu po wymianie programu na nowszą wersję."))
        layout.addStretch()


class MainWindow(QMainWindow):
    NAVIGATION = ["Pulpit", "Produkty", "Importy", "Zakupy", "Sprzedaż", "Magazyn", "Remanent", "Rozliczenie roczne", "Ustawienia", "Kontrola zgodności z magazynem"]

    def __init__(self, service: InventoryApplication) -> None:
        super().__init__()
        self.service = service
        self.setWindowTitle("Roczne Rozliczenie — wersja testowa ETAP 3")
        self.resize(1380, 820)
        container = QWidget()
        layout = QHBoxLayout(container)
        self.navigation = QListWidget()
        self.navigation.addItems(self.NAVIGATION)
        self.navigation.setFixedWidth(250)
        self.pages = QStackedWidget()
        self.dashboard = DashboardPage(service)
        self.products = ProductsPage(service)
        self.imports = ImportsPage(service)
        self.opening = OpeningPage(service)
        self.annual = AnnualPage(service)
        self.warehouse_check = WarehouseCheckPage(service)
        screens = [
            self.dashboard, self.products, self.imports,
            self._placeholder("Zakupy", "Dokumenty zakupowe są zapisywane przy importach. Osobny ekran zakupów powstanie później."),
            self._placeholder("Sprzedaż", "Wprowadzanie sprzedaży będzie dostępne w kolejnym etapie."),
            self.opening,
            self._placeholder("Remanent", "Spis z natury i końcowa wycena będą dostępne w kolejnym etapie."),
            self.annual, SettingsPage(service), self.warehouse_check,
        ]
        for screen in screens:
            self.pages.addWidget(screen)
        layout.addWidget(self.navigation)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(container)
        self.navigation.currentRowChanged.connect(self._change_page)
        self.navigation.setCurrentRow(0)
        self.setStyleSheet("""
            QMainWindow { background: #f7f8fa; }
            QListWidget { background: #eaf0f6; border: 0; font-size: 14px; padding: 10px; }
            QListWidget::item { padding: 11px; border-radius: 5px; }
            QListWidget::item:selected { background: #d3e4f5; color: #12324f; }
            QLabel#PageTitle { font-size: 22px; font-weight: 600; margin: 8px 0 16px 0; }
            QLabel#DashboardCounts { font-size: 18px; line-height: 1.7; padding: 16px; }
            QPushButton { padding: 7px 11px; }
            QTableWidget { background: white; gridline-color: #e6e9ec; }
        """)

    def _placeholder(self, title: str, message: str) -> QWidget:
        page, layout = _page(title)
        layout.addWidget(QLabel(message))
        layout.addStretch()
        return page

    def _change_page(self, index: int) -> None:
        if index < 0:
            return
        self.pages.setCurrentIndex(index)
        page = self.pages.widget(index)
        if hasattr(page, "refresh"):
            try:
                page.refresh()
            except Exception as exc:
                show_error(self, exc)
