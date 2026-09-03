"""Domestic invoice PDF entry point.

The callable is injected to retain exact historical layout during the staged
extraction. No database, order, stock, allocation or status write is performed
by this module.
"""


def generate(order, items, invoice, *, renderer):
    return renderer(order, items, invoice)

