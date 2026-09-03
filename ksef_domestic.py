"""Domestic FA(3) entry point preserving the existing 23% implementation."""


def validate(invoice, company, items, *, validator):
    return validator(invoice, company, items)


def generate(invoice, company, items, *, generator):
    return generator(invoice, company, items)

