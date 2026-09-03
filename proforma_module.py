import io
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm


TEXTS = {
    "de": {"title":"Proformarechnung", "place":"Ort", "issue":"Ausstellungsdatum", "shipping":"Voraussichtlicher Versand", "shipping_value":"innerhalb von ca. 14 Tagen", "payment":"Zahlungsart", "payment_value":"Vorkasse / Überweisung", "due":"Zahlungsfrist", "seller":"Verkäufer", "buyer":"Käufer", "bank":"Bankverbindung (EUR / SEPA)", "pos":"Pos.", "article":"Artikel / SKU", "qty":"Menge", "unit":"Netto/Stk.", "value":"Nettowert", "vat":"USt.", "net":"Nettosumme", "shipping_cost":"Versandkosten", "total":"Gesamtbetrag", "note":"Hinweis: Dieses Dokument ist eine Proformarechnung und keine umsatzsteuerliche Rechnung. Die endgültige Rechnung wird im Zusammenhang mit der Ausführung der innergemeinschaftlichen Lieferung ausgestellt. Zahlung bitte in EUR per SEPA-Überweisung auf das oben angegebene Konto."},
    "en": {"title":"Proforma invoice", "place":"Place", "issue":"Issue date", "shipping":"Expected dispatch", "shipping_value":"within approximately 14 days", "payment":"Payment method", "payment_value":"Prepayment / bank transfer", "due":"Payment due", "seller":"Seller", "buyer":"Buyer", "bank":"Bank details (EUR / SEPA)", "pos":"No.", "article":"Item / SKU", "qty":"Qty", "unit":"Net/unit", "value":"Net value", "vat":"VAT", "net":"Net total", "shipping_cost":"Shipping", "total":"Total amount", "note":"Note: This document is a proforma invoice and is not a VAT invoice. The final invoice will be issued in connection with the intra-Community supply. Please pay in EUR by SEPA transfer to the account shown above."},
    "es": {"title":"Factura proforma", "place":"Lugar", "issue":"Fecha de emisión", "shipping":"Envío previsto", "shipping_value":"en aproximadamente 14 días", "payment":"Forma de pago", "payment_value":"Pago anticipado / transferencia", "due":"Fecha de pago", "seller":"Vendedor", "buyer":"Comprador", "bank":"Datos bancarios (EUR / SEPA)", "pos":"Pos.", "article":"Artículo / SKU", "qty":"Cant.", "unit":"Neto/ud.", "value":"Valor neto", "vat":"IVA", "net":"Total neto", "shipping_cost":"Transporte", "total":"Importe total", "note":"Nota: Este documento es una factura proforma y no una factura fiscal. La factura definitiva se emitirá en relación con la entrega intracomunitaria. El pago debe realizarse en EUR mediante transferencia SEPA a la cuenta indicada arriba."},
    "it": {"title":"Fattura proforma", "place":"Luogo", "issue":"Data emissione", "shipping":"Spedizione prevista", "shipping_value":"entro circa 14 giorni", "payment":"Metodo di pagamento", "payment_value":"Pagamento anticipato / bonifico", "due":"Scadenza pagamento", "seller":"Venditore", "buyer":"Acquirente", "bank":"Coordinate bancarie (EUR / SEPA)", "pos":"Pos.", "article":"Articolo / SKU", "qty":"Q.tà", "unit":"Netto/pz.", "value":"Valore netto", "vat":"IVA", "net":"Totale netto", "shipping_cost":"Spedizione", "total":"Importo totale", "note":"Nota: Il presente documento è una fattura proforma e non una fattura fiscale. La fattura definitiva sarà emessa in relazione alla cessione intracomunitaria. Pagamento in EUR tramite bonifico SEPA sul conto sopra indicato."},
}


def _money(value):
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " EUR"


def generate_proforma_pdf(order, items, company, language="de", logo_path="", iban="", bic="", bank_name="", place="Kotuszów"):
    language = language if language in TEXTS else "en"
    t = TEXTS[language]
    issue = date.today()
    created = str(order.get("created_at") or "")[:10]
    try:
        issue = datetime.strptime(created, "%Y-%m-%d").date()
    except Exception:
        pass
    order_id = int(order.get("id") or 0)
    proforma_no = f"PRO-{language.upper()}/{order_id}/{issue:%m/%Y}"
    regular, bold = company.get("pdf_font", "Helvetica"), company.get("pdf_font_bold", "Helvetica-Bold")
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(210 * mm, 297 * mm))
    left, right, height = 15 * mm, 195 * mm, 297 * mm
    navy, muted, pale, line = (0.07,0.13,0.24), (0.34,0.39,0.49), (0.94,0.96,1), (0.84,0.88,0.94)

    pdf.setFillColorRGB(*navy); pdf.setFont(bold, 18)
    pdf.drawString(left, height-18*mm, f"{t['title']}: {proforma_no}")
    if logo_path:
        try: pdf.drawImage(ImageReader(logo_path), 165*mm, height-29*mm, 30*mm, 20*mm, preserveAspectRatio=True, anchor="e", mask="auto")
        except Exception: pass
    pdf.setStrokeColorRGB(*line); pdf.line(left, height-34*mm, right, height-34*mm)
    y = height-45*mm
    pdf.setFillColorRGB(*muted); pdf.setFont(regular, 9)
    pdf.drawString(left,y,f"{t['place']}: {place}"); pdf.drawString(68*mm,y,f"{t['issue']}: {issue:%d.%m.%Y}"); pdf.drawString(130*mm,y,t['shipping'])
    pdf.drawString(130*mm,y-5*mm,t['shipping_value']); y-=15*mm
    pdf.drawString(left,y,f"{t['payment']}: {t['payment_value']}"); pdf.drawString(110*mm,y,f"{t['due']}: {(issue+timedelta(days=7)):%d.%m.%Y}")
    y-=16*mm
    pdf.setFillColorRGB(*navy); pdf.setFont(bold,10); pdf.drawString(left,y,t['seller']); pdf.drawString(110*mm,y,t['buyer'])
    pdf.setFont(regular,9); y-=5*mm
    seller_lines=[company.get('company_name') or 'Niedźwieccy', company.get('nip') or '', company.get('address') or '', company.get('phone') or '', company.get('email') or '']
    buyer_lines=[order.get('customer_name') or '', order.get('customer_tax_no') or '', order.get('customer_address') or '', order.get('customer_phone') or '', order.get('customer_email') or '']
    for idx in range(max(len(seller_lines),len(buyer_lines))):
        if idx<len(seller_lines) and seller_lines[idx]: pdf.drawString(left,y,str(seller_lines[idx]))
        if idx<len(buyer_lines) and buyer_lines[idx]: pdf.drawString(110*mm,y,str(buyer_lines[idx]))
        y-=4.5*mm
    y-=3*mm; pdf.setFont(bold,9); pdf.drawString(left,y,t['bank']); y-=5*mm; pdf.setFont(regular,9)
    for bank_line in (f"IBAN: {iban}" if iban else "IBAN: -", f"BIC: {bic}" if bic else "BIC: -", f"Bank: {bank_name}" if bank_name else ""):
        if bank_line: pdf.drawString(left,y,bank_line); y-=4.5*mm
    y-=7*mm
    widths=[12,75,18,32,32,18]; xs=[left]
    for width in widths: xs.append(xs[-1]+width*mm)
    pdf.setFillColorRGB(*pale); pdf.roundRect(left,y-8*mm,180*mm,11*mm,2*mm,fill=1,stroke=0)
    pdf.setFillColorRGB(*navy); pdf.setFont(bold,8)
    for label,x in zip((t['pos'],t['article'],t['qty'],t['unit'],t['value'],t['vat']),xs[:-1]): pdf.drawString(x+2*mm,y-4.5*mm,label)
    y-=14*mm; total=Decimal('0')
    for pos,item in enumerate(items,1):
        qty=int(item.get('qty') or 0); unit=Decimal(str(item.get('unit_net_price') or item.get('net_price') or 0)); value=unit*qty; total+=value
        pdf.setFont(regular,9); pdf.setFillColorRGB(*navy)
        pdf.drawString(xs[0]+2*mm,y,str(pos)); pdf.drawString(xs[1]+2*mm,y,f"{item.get('sku') or '-'}  {item.get('model') or item.get('name') or ''}"[:58])
        pdf.drawRightString(xs[3]-2*mm,y,str(qty)); pdf.drawRightString(xs[4]-2*mm,y,_money(unit)); pdf.drawRightString(xs[5]-2*mm,y,_money(value)); pdf.drawRightString(xs[6]-2*mm,y,"0%")
        pdf.setStrokeColorRGB(*line); pdf.line(left,y-5*mm,right,y-5*mm); y-=10*mm
    y-=4*mm; pdf.setFont(bold,10)
    for label,value in ((t['net'],total),(f"{t['vat']} 0%",0),(t['shipping_cost'],0),(t['total'],total)):
        pdf.drawRightString(165*mm,y,f"{label}:"); pdf.drawRightString(right,y,_money(value)); y-=6*mm
    y-=8*mm; pdf.setFont(regular,8.5); pdf.setFillColorRGB(*muted)
    words=t['note'].split(); line_text='';
    for word in words:
        candidate=(line_text+' '+word).strip()
        if pdf.stringWidth(candidate,regular,8.5)>180*mm:
            pdf.drawString(left,y,line_text); y-=4.5*mm; line_text=word
        else: line_text=candidate
    if line_text: pdf.drawString(left,y,line_text)
    pdf.save(); buffer.seek(0)
    return buffer, f"{t['title'].replace(' ','_')}_{proforma_no.replace('/','_')}.pdf"
