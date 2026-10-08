import io
import pytest
from pypdf import PdfReader
import app as b
import payment_bank
from test_invoice_refactor import numbering_db

IBAN = 'DE89370400440532013000'  # Public example, never a production default.

@pytest.fixture
def foreign(monkeypatch):
    monkeypatch.setenv('FOREIGN_INVOICE_IBAN', IBAN)
    monkeypatch.setenv('FOREIGN_INVOICE_BIC', 'REVOLT21')
    monkeypatch.setenv('FOREIGN_INVOICE_BANK', 'Test Foreign Bank')

def test_missing_foreign_account_never_falls_back_to_domestic(monkeypatch):
    monkeypatch.delenv('FOREIGN_INVOICE_IBAN', raising=False)
    monkeypatch.setenv('PROFORMA_EUR_IBAN', IBAN)
    for kind in ('wdt', 'export'):
        with pytest.raises(payment_bank.BankConfigurationError):
            payment_bank.for_invoice({'bank_account':'DOMESTIC-ACCOUNT'}, {'invoice_type':kind})
    assert payment_bank.for_invoice({'bank_account':'DOMESTIC-ACCOUNT'}, {'invoice_type':'domestic'})['iban'] == 'DOMESTIC-ACCOUNT'

def test_invalid_foreign_account_is_rejected(foreign, monkeypatch):
    monkeypatch.setenv('FOREIGN_INVOICE_IBAN', 'DE00370400440532013000')
    with pytest.raises(payment_bank.BankConfigurationError): payment_bank.foreign_bank()

def test_foreign_xml_uses_same_bank_and_preserves_company(foreign):
    from test_invoice_refactor import foreign_invoice, COMPANY, ITEMS
    from ksef_module import FA3_NS, validate_fa3_xml
    from xml.etree import ElementTree as ET
    company=dict(COMPANY,bank_account='DOMESTIC-ACCOUNT',bank_swift='ALBPPLPWXXX')
    before=dict(company)
    xml=b.build_ksef_draft_xml(foreign_invoice(),company,ITEMS)
    root=ET.fromstring(xml)
    assert root.findtext('.//f:NrRB',namespaces={'f':FA3_NS})==IBAN
    assert root.findtext('.//f:SWIFT',namespaces={'f':FA3_NS})=='REVOLT21'
    assert company==before and 'DOMESTIC-ACCOUNT' not in xml
    assert validate_fa3_xml(xml,b.ksef_schema_path())==[]

@pytest.mark.parametrize('kind', ['wdt','export','domestic'])
def test_invoice_pdf_selects_one_bank(numbering_db, foreign, monkeypatch, kind):
    db=b.conn()
    db.execute("INSERT INTO company_profile(id,company_name,bank_account,bank_swift,updated_at) VALUES(1,'Test','DOMESTIC-ACCOUNT','ALBPPLPWXXX','2026-10-08')")
    db.commit(); db.close()
    monkeypatch.setattr(b,'DATA_DIR',str(numbering_db))
    monkeypatch.setattr(b,'find_logo_path',lambda:'')
    order={'id':1,'customer_name':'Test Buyer'}
    items=[{'sku':'TEST','name':'Uchwyt','model':'Test','qty':2,'net_price':10,'gross_price':12.3 if kind=='domestic' else 10,
            'line_value_net':20,'line_value_gross':24.6 if kind=='domestic' else 20,
            'vat_rate':23 if kind=='domestic' else 0,'currency':'PLN' if kind=='domestic' else 'EUR'}]
    meta={'invoice_no':'TEST-'+kind,'invoice_type':kind,'issue_date':'2026-10-08','sell_date':'2026-10-08',
          'payment_type':'transfer','payment_to':'2026-10-15','buyer_name':'Test Buyer','buyer_country':'DE',
          'buyer_tax_no':'DE123456789','paid':0}
    path,_,_=b.generate_order_invoice_pdf(order,items,meta)
    text=' '.join(p.extract_text() for p in PdfReader(path).pages)
    if kind=='domestic':
        assert 'DOMESTIC-ACCOUNT' in text and 'Test Foreign Bank' not in text
    else:
        assert payment_bank.display_iban(IBAN) in text and 'REVOLT21' in text and 'Test Foreign Bank' in text
        assert 'DOMESTIC-ACCOUNT' not in text and 'ALBPPLPWXXX' not in text

def test_proforma_route_uses_same_foreign_bank(numbering_db, foreign, monkeypatch):
    db=b.conn(); db.execute("UPDATE orders SET currency='EUR' WHERE id=1"); db.commit(); db.close()
    monkeypatch.setattr(b,'maybe_pull_shared_from_supabase',lambda *a,**kw:None)
    monkeypatch.setattr(b,'_client_order_items_local',lambda *a:[])
    monkeypatch.setattr(b,'_client_profile_for_email',lambda *a:{'language':'de'})
    monkeypatch.setattr(b,'find_logo_path',lambda:'')
    monkeypatch.setitem(b.app.config,'SECRET_KEY','test-only-key')
    client=b.app.test_client()
    with client.session_transaction() as s: s['admin_authenticated']=True
    response=client.get('/orders/1/proforma')
    assert response.status_code==200, response.text
    text=PdfReader(io.BytesIO(response.data)).pages[0].extract_text()
    assert payment_bank.display_iban(IBAN) in text and 'REVOLT21' in text
    monkeypatch.delenv('FOREIGN_INVOICE_IBAN')
    assert client.get('/orders/1/proforma').status_code==409
