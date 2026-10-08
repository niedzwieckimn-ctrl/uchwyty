from pathlib import Path
import shutil

from pypdf import PdfReader
from pypdf import PdfWriter
from io import BytesIO
import json
import pytest
import packing_history
import app as b
import packing_pdf


def sample():
    numbers=['ZAM-2609221','ZAM-2609222','ZAM-2609241','ZAM-2610071']
    rows=[('CH020-BB-6490','Lynd',0,'Chaciński',3),
          ('CH102-MB-320360','Victor',1,'Mazek',2),
          ('CH102-MB-224264','Victor',1,'Mazek',3),
          ('CH102-MB-192232','Victor',1,'Mazek',13),
          ('CH102-MB-128168','Victor',1,'Mazek',1),
          ('CH010-AB-192232','Winsor',2,'Gruchoła',3),
          ('CH101-BLK-128164','Andre',3,'RACHWALSKI',2),
          ('CH101-BLK-T50','Andre',3,'RACHWALSKI',1)]
    return [dict(sku=sku,model=model,source_order_no=numbers[n],source_order_note=note,qty=qty)
            for sku,model,n,note,qty in rows]


def render(tmp_path, monkeypatch, items):
    monkeypatch.setattr(b,'DATA_DIR',str(tmp_path))
    order=dict(order_no='ZAM-2610071',created_at='2026-09-24 10:00:00',
               customer_name='ARTYSTYCZNA MANUFAKTURA AGNIESZKA WOJEWÓDKA',customer_email='')
    return packing_pdf.generate(b,order,items,dict(invoice_no=order['order_no'],
         document_label_key='order',buyer_name=order['customer_name'],language='pl'))


def test_sample_full_customer_and_one_order_summary(tmp_path,monkeypatch):
    path=render(tmp_path,monkeypatch,sample())
    pdf=PdfReader(path)
    assert len(pdf.pages)==1
    text=' '.join(pdf.pages[0].extract_text().split())
    assert 'ARTYSTYCZNA MANUFAKTURA AGNIESZKA WOJEWÓDKA' in text
    assert text.count('ZAM-2609221')==2  # summary + its line, no duplicate heading
    assert '28' in text and '...' not in text
    rows, count, qty = packing_history._document_rows(Path(path).read_bytes())
    assert count == 8 and qty == 28 and rows[0]['order_number'] == 'ZAM-2609221'
    target=Path(__file__).parents[1]/'pdf-review'
    target.mkdir(exist_ok=True)
    shutil.copyfile(path,target/'lista-pakowa-28.pdf')


def test_long_text_and_repeated_headers_fit_multiple_pages(tmp_path,monkeypatch):
    items=sample()*6
    items[0].update(model='Długi model produktu z wykończeniem szczotkowanym bez obcinania tekstu',
                    source_order_note='Długa notatka dostawy z informacjami dla osoby pakującej, bez utraty treści')
    path=render(tmp_path,monkeypatch,items)
    pdf=PdfReader(path)
    assert len(pdf.pages)>1
    text=' '.join(' '.join(p.extract_text().split()) for p in pdf.pages)
    assert 'bez utraty treści' in text
    assert 'bez obcinania tekstu' in text
    assert '168' in text
    rows, count, qty = packing_history._document_rows(Path(path).read_bytes())
    assert count == 48 and qty == 168
    assert rows[0]['note'] == items[0]['source_order_note']
    for page in pdf.pages:
        assert 'LISTA PAKOWANIA' in page.extract_text()
    target=Path(__file__).parents[1]/'pdf-review'
    target.mkdir(exist_ok=True)
    shutil.copyfile(path,target/'lista-pakowa-wiele-stron.pdf')


def test_invalid_document_snapshot_fails_closed(tmp_path, monkeypatch):
    path = render(tmp_path, monkeypatch, sample())
    writer = PdfWriter(clone_from=path)
    rows = json.loads(PdfReader(path).metadata.subject.removeprefix('packing-rows-v2:'))
    rows[0]['packed_qty'] = -1
    writer.add_metadata({'/Subject': 'packing-rows-v2:' + json.dumps(rows)})
    stream = BytesIO()
    writer.write(stream)
    with pytest.raises(packing_history.PackingHistoryError):
        packing_history._document_rows(stream.getvalue())
