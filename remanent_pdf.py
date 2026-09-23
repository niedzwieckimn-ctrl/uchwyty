"""Printable reports exclusively from completed inventory snapshots."""
from io import BytesIO
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle, KeepTogether


def _font():
    if 'RemanentFont' not in pdfmetrics.getRegisteredFontNames():
        paths = [Path('C:/Windows/Fonts/arial.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
        path = next((item for item in paths if item.exists()), None)
        if path is None:
            raise RuntimeError('Brak czcionki z polskimi znakami dla arkusza remanentu')
        pdfmetrics.registerFont(TTFont('RemanentFont', str(path)))
    return 'RemanentFont'


def _table(rows, widths, font):
    table = LongTable(rows, colWidths=widths, repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([
        ('FONTNAME',(0,0),(-1,-1),font),('FONTSIZE',(0,0),(-1,-1),7),
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e9eef8')),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f8faff')]),
        ('VALIGN',(0,0),(-1,-1),'TOP'),('GRID',(0,0),(-1,-1),.3,colors.HexColor('#d0d7e2')),
        ('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4),
        ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
    ]))
    return table


def render_pdf(session, items, total, kind, company):
    if session['status'] != 'COMPLETED':
        raise ValueError('PDF powstaje wyłącznie z zamkniętego snapshotu')
    if kind not in {'internal','sheet'}:
        raise ValueError('Nieznany typ raportu')
    font = _font()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='RemTitle', fontName=font, fontSize=16, leading=20, spaceAfter=9))
    styles.add(ParagraphStyle(name='RemText', fontName=font, fontSize=9, leading=13, spaceAfter=5))
    styles.add(ParagraphStyle(name='RemSmall', fontName=font, fontSize=7, leading=10))
    styles.add(ParagraphStyle(name='RemCenter', parent=styles['RemSmall'], alignment=TA_CENTER))
    width, height = landscape(A3 if kind=='internal' else A4)
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer,pagesize=(width,height),leftMargin=13*mm,rightMargin=13*mm,
                                 topMargin=13*mm,bottomMargin=14*mm,
                                 title=('Raport remanentu' if kind=='internal' else 'Arkusz spisu z natury'))
    story = [Paragraph('Raport wewnętrzny remanentu' if kind=='internal' else 'Arkusz spisu z natury',styles['RemTitle']),
             Paragraph(escape(f"{company.get('company_name') or 'Dane firmy nieuzupełnione'} · NIP: {company.get('nip') or '-'} · {company.get('address') or ''}"), styles['RemText']),
             Paragraph(f"Numer: {session['remanent_no']} · Data stanu/spisu: {session['as_of_date']} · "
                       f"Rozpoczęto: {session['snapshot_at']} · Zamknięto: {session['completed_at']}",styles['RemText']),
             Spacer(1,5*mm)]
    p = lambda value: Paragraph(escape(str(value if value is not None else '—')), styles['RemSmall'])
    if kind == 'sheet':
        rows = [[p(x) for x in ('Lp.','Towar / SKU / wariant','J.m.','Ilość','Cena jedn. PLN','Wartość PLN')]]
        for n,item in enumerate(items,1):
            description = f"{item['name']} / {item['model']} / {item['sku']}"
            if item['variant']:
                description += f" / {item['variant']}"
            rows.append([p(n),p(description),p(item['unit']),p(item['counted_qty']),
                         p(item['unit_value_pln']),p(item['counted_stock_value'])])
        rows.append([p(''),p('ŁĄCZNA WARTOŚĆ SPISU'),p(''),p(total['counted_qty']),p(''),p(total['counted_value'])])
        story.extend([_table(rows,[12*mm,139*mm,15*mm,23*mm,31*mm,48*mm],font),Spacer(1,7*mm),
                      Paragraph(f"Spis zakończono na pozycji {len(items)}.",styles['RemText']),
                      Spacer(1,13*mm),Paragraph('Osoby sporządzające spis: ______________________________________________',styles['RemText']),
                      Spacer(1,8*mm),Paragraph('Podpis właściciela / wspólników: _________________________________________',styles['RemText'])])
    else:
        headings = ('Lp.','SKU','Nazwa / model','01.01','Zakupy CSV/ręczne','Zakupy app','Sprzedaż ręczna','Sprzedaż CSV','Sprzedaż app','Stan dokument.','System start','Ostatni spis','Policzono','Różn. dok.','Różn. syst.')
        rows = [[p(x) for x in headings]]
        for n,i in enumerate(items,1):
            rows.append([p(x) for x in (n,i['sku'],f"{i['name']} {i['model']}",i['opening_stock'],
                i['historical_purchases'],i['purchases_from_app'],i['historical_sales_manual'],
                i['historical_sales_import'],i['sales_from_app'],i['document_stock'],
                i['system_stock_at_start'],i['last_inventory_count'],
                ('0 (przyjęte)' if i['assumed_zero'] else i['counted_qty']),
                i['difference_document_vs_count'],i['difference_system_vs_count'])])
        story.extend([Paragraph('Ilości według SKU',styles['RemText']),
            _table(rows,[13*mm,28*mm,45*mm]+[20*mm]*12,font),Spacer(1,8*mm)])
        values = [[p(x) for x in ('Lp.','SKU','Cena jednostkowa PLN','Wartość dokumentowa PLN',
                                 'Wartość fizyczna PLN','Różnica wartości PLN')]]
        for n,i in enumerate(items,1):
            values.append([p(x) for x in (n,i['sku'],i['unit_value_pln'],i['document_stock_value'],
                                            i['counted_stock_value'],i['difference_value'])])
        story.extend([Paragraph('Wycena pozycji',styles['RemText']),
                      _table(values,[13*mm,45*mm,66*mm,75*mm,75*mm,76*mm],font),Spacer(1,8*mm)])
        fields = [('Stan 01.01',total['opening_stock']),('Zakupy',total['purchases_total']),
                  ('Sprzedaż',total['sales_total']),('Stan dokumentowy',total['document_stock']),
                  ('Wartość dokumentowa PLN',total['document_value']),('Stan fizyczny',total['counted_qty']),
                  ('Wartość fizyczna PLN',total['counted_value']),('Niedobory szt.',total['shortage_qty']),
                  ('Niedobory PLN',total['shortage_value']),('Nadwyżki szt.',total['surplus_qty']),
                  ('Nadwyżki PLN',total['surplus_value']),('Różnica netto szt.',total['difference_qty']),
                  ('Różnica netto PLN',total['difference_value']),('Liczba SKU',total['sku_count']),
                  ('Pozycje policzone',total['counted_count']),('Pozycje niepoliczone',total['uncounted_count'])]
        story.append(KeepTogether([Paragraph('Podsumowanie',styles['RemText'])] +
                                  [Paragraph(f'{key}: {value}',styles['RemText']) for key,value in fields]))
    document.build(story)
    buffer.seek(0)
    return buffer
