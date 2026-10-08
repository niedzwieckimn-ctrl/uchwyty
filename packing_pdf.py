"""Print layout only: consume the supplied packing snapshot without business writes."""
from html import escape
import json
import os
import tempfile

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, KeepTogether
from reportlab.lib.utils import ImageReader


def generate(b, order_row, items, meta, invoice_pdf_path=''):
    order = dict(order_row or {})
    items = [dict(item) for item in items if int(item.get('qty') or 0) > 0]
    customer = b.norm(meta.get('buyer_name') or order.get('customer_name') or '-')
    directory = b.invoice_dir_for_customer(customer)
    name = str(meta['invoice_no'])
    if meta.get('packing_document_token'):
        name += '_' + b.safe_filename(str(meta['packing_document_token']))
    path = b.packing_list_pdf_path_for_invoice(
        invoice_pdf_path or os.path.join(directory, b.safe_filename(name) + '.pdf'), name)
    regular, bold = b.get_pdf_font_names()
    language = b.normalize_client_language(meta.get('language') or order.get('language'))
    if not (meta.get('language') or order.get('language')):
        email = b.norm(order.get('customer_email') or meta.get('buyer_email'))
        if email:
            try:
                language = b.normalize_client_language(b._client_profile_for_email(email).get('language'))
            except Exception:
                b.app.logger.warning('PACKING_PDF_LANGUAGE_FALLBACK')
    tr = lambda key: b.packing_list_text(language, key)
    normal = ParagraphStyle('packing', fontName=bold, fontSize=9, leading=12,
                            textColor=colors.black, splitLongWords=True)
    small = ParagraphStyle('packing-small', parent=normal, fontSize=8, leading=11)
    label = ParagraphStyle('packing-label', parent=normal, fontSize=8, leading=11)
    quantity = ParagraphStyle('packing-quantity', parent=normal, fontSize=11, leading=13, alignment=2)
    def p(value, style=normal):
        return Paragraph(escape(str(value or '-')).replace('\n', '<br/>'), style)
    def source(item):
        number, note = b.norm(item.get('source_order_no')), b.norm(item.get('source_order_note'))
        if note and number.casefold().endswith((' ' + note).casefold()):
            number = number[:-(len(note) + 1)].strip()
        return number, note
    numbers = list(dict.fromkeys(source(item)[0] for item in items if source(item)[0]))
    order_numbers = ', '.join(numbers) or b.norm(order.get('order_no') or '-')
    order_date = str(order.get('created_at') or b.app_now().date())[:10]
    # Keep a machine-readable copy of these exact printed rows in the verified
    # PDF. Wrapped text cannot be recovered reliably by the legacy line parser.
    snapshot = [dict(order_number=source(item)[0], note=source(item)[1],
                     sku=str(item.get('sku') or '-'),
                     model_name=str(item.get('model') or item.get('name') or '-'),
                     packed_qty=int(item['qty'])) for item in items]
    descriptor, temporary_path = tempfile.mkstemp(prefix='.lp-', suffix='.tmp', dir=os.path.dirname(path))
    os.close(descriptor)
    doc = SimpleDocTemplate(temporary_path, pagesize=(210 * mm, 297 * mm),
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=49 * mm, bottomMargin=19 * mm,
                            title=tr('title'), author='Niedźwieccy')
    def page(canvas, document):
        canvas.saveState()
        canvas.setSubject('packing-rows-v2:' + json.dumps(snapshot, ensure_ascii=False))
        logo = b.find_logo_path()
        if logo:
            canvas.drawImage(ImageReader(logo), 15 * mm, 257 * mm, 54.4 * mm, 34 * mm,
                             preserveAspectRatio=True, anchor='w', mask='auto')
        canvas.setFont(bold, 17)
        canvas.drawRightString(195 * mm, 280 * mm, tr('title'))
        # Order numbers occur once in the summary, never again under the title.
        subtitle = ''
        if (meta.get('document_label_key') or 'invoice') != 'order':
            subtitle = f"{tr('invoice')}: {meta['invoice_no']}"
        if document.page > 1:
            subtitle = (subtitle + ' | ' if subtitle else '') + tr('continued')
        if subtitle:
            block = p(subtitle, small)
            _, height = block.wrap(105 * mm, 24 * mm)
            block.drawOn(canvas, 90 * mm, 273 * mm - height)
        canvas.setStrokeColor(colors.HexColor('#666666'))
        canvas.line(15 * mm, 253 * mm, 195 * mm, 253 * mm)
        canvas.setFont(regular, 8)
        canvas.drawRightString(195 * mm, 10 * mm, str(document.page))
        canvas.restoreState()
    summary = Table([
        [p(tr('customer'), label), p(customer), p(tr('date'), label), p(order_date)],
        [p(tr('order'), label), p(order_numbers), '', ''],
    ], colWidths=[25 * mm, 109 * mm, 16 * mm, 30 * mm], hAlign='LEFT')
    summary.setStyle(TableStyle([
        ('SPAN', (1, 1), (3, 1)), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
    ]))
    rows = [[p(tr('checked'), label), p(tr('line'), label), p('SKU', label),
             p(tr('product'), label), p(tr('source'), label), p(tr('quantity'), label)]]
    for index, item in enumerate(items, 1):
        number, note = source(item)
        rows.append([p('□'), p(index), p(item.get('sku')), p(item.get('model') or item.get('name')),
                     p('\n'.join(x for x in (number, note) if x), small), p(item['qty'], quantity)])
    table = Table(rows, colWidths=[10 * mm, 10 * mm, 47 * mm, 40 * mm, 53 * mm, 20 * mm],
                  repeatRows=1, hAlign='LEFT', splitInRow=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8e8e8')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 0), (-1, -1), .5, colors.HexColor('#777777')),
    ]))
    totals = Table([[p(f"{tr('positions')}: {len(items)}"),
                     p(f"{tr('total_qty')}: {sum(int(i['qty']) for i in items)}"),
                     p(f"{tr('packages')}: ______")]], colWidths=[60 * mm] * 3)
    totals.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#e8e8e8')),
                               ('TOPPADDING', (0, 0), (-1, -1), 10),
                               ('BOTTOMPADDING', (0, 0), (-1, -1), 10)]))
    signatures = Table([[p(tr('packed_by') + ':', small), p(tr('date').title() + ':', small),
                         p(tr('signature') + ':', small)],
                        [p('______________________', small), p(str(b.app_now().date()), small),
                         p('______________________', small)]], colWidths=[76 * mm, 45 * mm, 59 * mm])
    try:
        doc.build([summary, Spacer(1, 4 * mm), table, Spacer(1, 5 * mm),
                   KeepTogether([totals, Spacer(1, 7 * mm), signatures])], onFirstPage=page, onLaterPages=page)
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
    return path
