"""Read-only counting worksheet from an already refreshed physical-stock snapshot."""
from html import escape
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle,
)


def render_stock_count_pdf(items, generated_at, font_names, company=None):
    items = [dict(item) for item in items]
    regular, bold = font_names
    company = company or {}
    text = ParagraphStyle('stock-text', fontName=regular, fontSize=9, leading=13)
    small = ParagraphStyle('stock-small', parent=text, fontSize=8, leading=11)
    label = ParagraphStyle('stock-label', parent=small, fontName=bold)
    quantity = ParagraphStyle('stock-qty', parent=text, alignment=TA_RIGHT)
    center = ParagraphStyle('stock-center', parent=small, alignment=TA_CENTER)

    def p(value, style=small):
        return Paragraph(escape(str(value if value is not None else '')).replace('\n', '<br/>'), style)

    buffer = BytesIO()
    document = SimpleDocTemplate(
        # Platypus frames add 6 pt of inner padding on either side.
        buffer, pagesize=A4, leftMargin=12*mm-6, rightMargin=12*mm-6,
        topMargin=29*mm, bottomMargin=17*mm,
        title='Arkusz spisu z natury', author=company.get('company_name') or 'Niedźwieccy',
    )
    story = [
        p(company.get('company_name') or 'Niedźwieccy', label),
        p('Data spisu: ................................    Miejsce: ................................................', text),
        p('Stan systemowy to ilość zapisana w magazynie. Ilość faktycznie policzoną wpisz w kolumnie „Policzono”.', small),
        Spacer(1, 4*mm),
    ]
    headers = ('Lp.', 'SKU / EAN', 'Model / nazwa', 'J.m.', 'Stan\nsystemowy', 'Policzono', 'Uwagi')
    rows = [[p(value, label) for value in headers]]
    for index, item in enumerate(items, 1):
        sku = str(item.get('sku') or '')
        ean = str(item.get('ean') or '').strip()
        model = str(item.get('model') or '').strip()
        name = str(item.get('name') or '').strip()
        description = model
        if name and name.casefold() not in {model.casefold(), 'produkt'}:
            description = '\n'.join(filter(None, (model, name)))
        rows.append([
            p(index, center), p(sku + ('\n' + ean if ean else '')),
            p(description), p('szt.', center), p(item['stock_qty'], quantity), '', '',
        ])
    if not items:
        rows.append(['', p('Brak aktywnych pozycji.'), '', '', '', '', ''])
    table = LongTable(rows, colWidths=[10*mm, 37*mm, 51*mm, 10*mm, 23*mm, 25*mm, 30*mm],
                      repeatRows=1, hAlign='LEFT', minRowHeights=[10*mm] + [12*mm]*(len(rows)-1))
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#edf0f4')),
        ('GRID', (0,0), (-1,-1), .4, colors.HexColor('#9aa3ad')),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 4), ('RIGHTPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 5), ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ]))
    story.extend([table, Spacer(1, 5*mm), KeepTogether([
        p(f"Liczba pozycji: {len(items)}. Łączny stan systemowy: {sum(item['stock_qty'] for item in items)} szt.", label),
        Spacer(1, 7*mm),
        p('Spis sporządził(a): .......................................................................................', text),
        Spacer(1, 5*mm),
        p('Podpis: ........................................................................................................', text),
    ])])

    def page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(colors.HexColor('#17283f'))
        canvas.setFont(bold, 16)
        canvas.drawString(12*mm, 281*mm, 'ARKUSZ SPISU Z NATURY')
        canvas.setFont(regular, 8)
        canvas.drawString(12*mm, 275*mm, f"Stan systemowy - wydruk: {generated_at:%d.%m.%Y %H:%M:%S} (czas polski)")
        canvas.setStrokeColor(colors.HexColor('#9aa3ad'))
        canvas.line(12*mm, 272*mm, 198*mm, 272*mm)
        canvas.setFont(regular, 8)
        canvas.drawString(12*mm, 9*mm, 'Arkusz do liczenia - wszystkie aktywne pozycje magazynu')
        canvas.drawRightString(198*mm, 9*mm, f'Strona {doc.page}')
        canvas.restoreState()

    document.build(story, onFirstPage=page, onLaterPages=page)
    buffer.seek(0)
    return buffer
