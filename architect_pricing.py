"""Versioned architect promotion arithmetic. All monetary inputs are PLN amounts.

The customer receives a discount on retail gross. Manufacturer and distributor
each bear half of its net value. The wholesale unit price is reduced only by
the manufacturer's share. Order and invoice snapshots use the resulting unit
price, so quantity changes must call this function again on the server.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def dec(value) -> Decimal:
    return Decimal(str(value))


def money(value) -> Decimal:
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ProgramRules:
    version: int
    customer_discount: Decimal
    manufacturer_share: Decimal
    commission: Decimal

    def __post_init__(self):
        if self.version <= 0 or any(not Decimal(0) <= value <= Decimal(1) for value in
                (self.customer_discount, self.manufacturer_share, self.commission)):
            raise ValueError("Invalid architect program rules")


@dataclass(frozen=True)
class PricedLine:
    qty: int
    vat_rate: Decimal
    wholesale_unit_before: Decimal
    wholesale_unit_after: Decimal
    retail_unit_gross_before: Decimal
    retail_unit_gross_after: Decimal
    retail_unit_net_after: Decimal
    manufacturer_discount_unit_net: Decimal
    commission_estimate: Decimal
    program_version: int

    @property
    def wholesale_net_after(self):
        return money(self.wholesale_unit_after * self.qty)


def price_line(*, wholesale_unit_net, retail_unit_gross, vat_rate,
               qty: int, rules: ProgramRules) -> PricedLine:
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        raise ValueError("Quantity must be a positive integer")
    wholesale = money(wholesale_unit_net)
    retail_gross = money(retail_unit_gross)
    vat = dec(vat_rate)
    if wholesale <= 0 or retail_gross <= 0 or vat < 0 or vat > 1:
        raise ValueError("Invalid price or VAT")
    gross_discount = money(retail_gross * rules.customer_discount)
    retail_after = retail_gross - gross_discount
    net_discount = money(gross_discount / (1 + vat))
    manufacturer_share = money(net_discount * rules.manufacturer_share)
    wholesale_after = wholesale - manufacturer_share
    if wholesale_after < 0:
        raise ValueError("Architect discount exceeds wholesale price; manual review required")
    retail_net_after = money(retail_after / (1 + vat))
    commission = money(retail_net_after * rules.commission * qty)
    return PricedLine(
        qty=qty, vat_rate=vat, wholesale_unit_before=wholesale,
        wholesale_unit_after=wholesale_after,
        retail_unit_gross_before=retail_gross,
        retail_unit_gross_after=retail_after,
        retail_unit_net_after=retail_net_after,
        manufacturer_discount_unit_net=manufacturer_share,
        commission_estimate=commission,
        program_version=rules.version,
    )
