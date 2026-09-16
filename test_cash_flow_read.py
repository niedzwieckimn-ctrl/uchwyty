from datetime import datetime
from zoneinfo import ZoneInfo

import app as backend
import business_operations as operations
import cash_flow_module
from test_agent_runtime import isolated, owner


def _deps():
    return {
        'conn': backend.conn, 'now_iso': backend.now_iso, 'app_now': backend.app_now,
        'to_float': backend.to_float,
        'maybe_pull_shared_from_supabase': lambda: None,
        'supabase_enabled': lambda: False,
        'BASE_URL': backend.BASE_URL, 'DB_PATH': backend.DB_PATH,
    }


def test_cashflow_panel_and_agent_read_use_same_calculator(monkeypatch):
    cash_flow_module.ensure_cash_flow_tables(backend.conn, backend.now_iso)
    now = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo('Europe/Warsaw'))
    monkeypatch.setattr(backend, 'app_now', lambda: now)
    deps = _deps()
    cash_flow_module._runtime_deps = deps
    db = backend.conn()
    db.execute("UPDATE cash_flow_settings SET value='10000' WHERE key='account_balance'")
    for index in range(30):
        db.execute('''INSERT INTO cash_flow_expenses(
            expense_date,category,description,document_no,amount,created_at)
            VALUES(?,?,?,?,?,?)''',
            ('2026-09-10', 'Test', '', f'KOSZT-{index}', index + 1, backend.now_iso()))
    db.commit(); db.close()

    panel = cash_flow_module.calculate_cash_flow_snapshot(deps, current_time=now)
    result = operations.execute_business_operation(
        owner(), cash_flow_module.CASHFLOW_READ_OPERATION,
        {'section': 'sources', 'limit': 100})
    assert result.status == 'SUCCESS'
    read = result.data
    assert read['kpis'] == panel['kpis']
    assert read['sales_chart'] == panel['sales_chart']
    assert read['sources_total'] == len(panel['sources'])
    assert len(read['sources']) == len(panel['sources']) > 25
    assert read['sources'][0].keys() >= {
        'source_type', 'source_id', 'date', 'amount_original', 'currency',
        'conversion_rate', 'inclusion_rules', 'contribution'}
    assert read['capabilities']['daily_balance_timeline'] is False
    assert read['capabilities']['daily_balance_explanation'] is False
    assert read['scope']['kind'] == 'panel_source_of_truth'
    settings = [row for row in read['sources'] if row['source_type'] == 'cashflow_setting']
    assert {row['source_id'] for row in settings} >= {
        'account_balance', 'monthly_zus', 'cash_buffer', 'planned_china_budget'}
    descriptor = operations.operation_descriptor(
        operations.OPERATION_REGISTRY[cash_flow_module.CASHFLOW_READ_OPERATION])
    assert descriptor['capability_contract']['daily_balance_timeline'] is False
