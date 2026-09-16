import json
from datetime import datetime, timedelta, timezone

import app as backend
import business_operations as operations
import search_analytics
from test_agent_runtime import isolated, owner


def _event(event_id, *, at, query, customer='client-1', results=0,
           model_id=None, model_name='', selected_sku='', resolution='unresolved'):
    return {
        'id': event_id, 'customer_id': customer, 'customer_name': 'Klient Testowy',
        'model_id': model_id, 'model_name': model_name, 'query': query,
        'edit_id': event_id, 'sequence': 1, 'results_count': results,
        'resolution': resolution, 'catalog_results': results,
        'skus': [selected_sku] if selected_sku else [],
        'selected_sku': selected_sku, 'created_at': at.isoformat(),
    }


def test_panel_projection_and_agent_read_share_identical_aggregates():
    now = datetime.now(timezone.utc)
    model_id = search_analytics.family_id('Leo')
    events = [
        _event('missing', at=now - timedelta(minutes=20), query='nieistniejący model'),
        _event('leo-1', at=now - timedelta(minutes=2), query='Leo', results=1,
               model_id=model_id, model_name='Leo', selected_sku='CH032-AB-N29',
               resolution='selection'),
        _event('leo-2', at=now - timedelta(minutes=1), query='Leo uchwyt', results=1,
               model_id=model_id, model_name='Leo', resolution='query'),
    ]
    db = backend.conn()
    for event in events:
        db.execute(
            'INSERT INTO search_analytics_records(id,kind,created_at,payload) VALUES(?,?,?,?)',
            (event['id'], 'event', event['created_at'], json.dumps(event, ensure_ascii=False)))
    db.commit(); db.close()

    panel_data = search_analytics.analytics_snapshot(backend, days=30, now=now)
    result = operations.execute_business_operation(
        owner(), search_analytics.OPERATION, {'days': 30, 'limit': 100})
    assert result.status == 'SUCCESS'
    read = result.data
    assert read['totals'] == {
        'intents': len(panel_data['filtered']),
        'active_customers': len(panel_data['clients']),
        'no_result_intents': sum(row['no_result'] for row in panel_data['filtered']),
    }
    assert read['models'][0]['model_name'] == panel_data['models'][0]['name'] == 'Leo'
    assert read['models'][0]['intent_count'] == panel_data['models'][0]['count'] == 1
    assert read['models'][0]['explicit_sku_selections'] == [
        {'sku': 'CH032-AB-N29', 'count': 1}]
    assert read['missing'] == panel_data['missing']
    assert read['scope']['entity_existence_authoritative'] is True
    descriptor = operations.operation_descriptor(
        operations.OPERATION_REGISTRY[search_analytics.OPERATION])
    assert descriptor['capability_contract']['authoritative_source'] == (
        'search_analytics_records_shared_projection')
