"""Durable, resumable, single-job inventory sync with conservative failure handling.

Basis = local available + cumulative non-cancelled Orderchamp quantities.
Orderchamp sales reduce its available stock themselves; importing the same sale
reduces local available and increases cumulative quantities by equal amounts.
Only the *change* of basis is exported using ADJUST, preserving concurrent sales.
An ambiguous ADJUST is quarantined, never retried (clientMutationId is not assumed
to be an idempotency key). A first anchor checks remote reservations before use.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import copy
import time
import uuid
from orderchamp_client import OrderchampError


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def integer(value, minimum=0):
    if type(value) is not int or not minimum<=value<=2147483647:
        raise OrderchampError('INVALID_QUANTITY')
    return value


def stamp(value):
    if not isinstance(value,str): raise OrderchampError('INVALID_ORDER')
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None: raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError: raise OrderchampError('INVALID_ORDER') from None


def money(value):
    if not isinstance(value,str): raise OrderchampError('INVALID_ORDER_PRICE')
    try:
        result=Decimal(value)
        if not result.is_finite() or result<0: raise ValueError()
        return result
    except (ValueError,InvalidOperation): raise OrderchampError('INVALID_ORDER_PRICE') from None


def address(value):
    if not isinstance(value,dict): raise OrderchampError('BILLING_DATA_MISSING')
    return '\n'.join(filter(None,[str(value.get('name') or ''),
      ' '.join(str(value.get(k) or '') for k in ('street','houseNumber')).strip(),
      ' '.join(str(value.get(k) or '') for k in ('postalCode','city')).strip(),str(value.get('country') or '')]))


def normalize_order(order):
    for key in ('isCancelled','isConfirmed','isFulfilled','isTest'):
        if type(order.get(key)) is not bool: raise OrderchampError('INVALID_ORDER')
    if order['isTest']: raise OrderchampError('TEST_ORDER_REQUIRES_REVIEW')
    for key in ('id','number','companyName','currency'):
        if not isinstance(order.get(key),str) or not order[key]: raise OrderchampError('INVALID_ORDER')
    if order['currency'] not in ('PLN','EUR'): raise OrderchampError('CURRENCY_NOT_SUPPORTED')
    items=[]
    for line in order.get('lines',[]):
        qty=integer(line.get('quantity'),1)
        remaining=integer(line.get('unshippedQuantity'))
        if remaining>qty or not isinstance(line.get('id'),str) or not isinstance(line.get('sku'),str) or not line['sku'].strip():
            raise OrderchampError('INVALID_ORDER')
        items.append({'line_id':line['id'],'sku':line['sku'],'qty':qty,'unshipped_qty':remaining,
            'unit_net':str(money(line.get('subtotalPrice'))/qty),
            'unit_gross':str(money(line.get('totalPrice'))/qty),
            'subtotal':str(money(line.get('subtotalPrice'))),'tax':str(money(line.get('taxPrice'))),
            'total':str(money(line.get('totalPrice')))})
    if not items or len({r['line_id'] for r in items})!=len(items): raise OrderchampError('INVALID_ORDER')
    billing=order.get('billingAddress') or {}
    return {'id':order['id'],'number':order['number'],'company':billing.get('companyName') or order['companyName'],
        'email':order.get('email') or '', 'phone':order.get('companyPhone') or '',
        'vat_number':order.get('vatNumber') or '', 'currency':order['currency'],
        'billing_address':address(order.get('billingAddress')),
        'billing_country':order['billingAddress'].get('country') or '',
        'shipping_address':address(order.get('shippingAddress')),
        'created_at':stamp(order.get('createdAt')),'updated_at':stamp(order.get('updatedAt')),
        'cancelled':order['isCancelled'],'confirmed':order['isConfirmed'],'fulfilled':order['isFulfilled'],
        'source':order.get('source') or '', 'items':items,
        'subtotal':str(money(order.get('subtotalPrice'))),'tax':str(money(order.get('taxPrice'))),
        'total':str(money(order.get('totalPrice')))}


def remote_level(variant):
    levels=variant.get('inventoryLevels',{})
    if (variant.get('inventoryPolicy')!='DENY' or levels.get('pageInfo',{}).get('hasNextPage') is not False
        or len(levels.get('nodes',[]))!=1): raise OrderchampError('REMOTE_STOCK_SCOPE_UNSAFE')
    level=levels['nodes'][0]
    quantity=integer(level.get('quantity'),-2147483647)
    available=integer(level.get('availableQuantity'),-2147483647)
    if (level.get('location',{}).get('isPrimary') is not True or quantity-available<0
        or variant.get('inventoryQuantity')!=quantity or not isinstance(level.get('id'),str)):
        raise OrderchampError('REMOTE_STOCK_SCOPE_UNSAFE')
    return {'level_id':level['id'],'quantity':quantity,'available':available,'reserved':quantity-available}


class SyncEngine:
    def __init__(self,store,client_factory,*,clock=utcnow,cache_import=lambda rows:None):
        self.store,self.client_factory,self.clock,self.cache_import=store,client_factory,clock,cache_import

    def run(self):
        began=time.perf_counter()
        token=str(uuid.uuid4())
        state=self.store.control('claim',token)
        if state is None: return {'claimed':False}
        state=copy.deepcopy(state)
        client=None
        def save(): self.store.control('save',token,state)
        try:
            client=self.client_factory()
            if state.get('phase') in ('catalog','write'):
                # A plan computed before a process restart may be obsolete. Keep
                # confirmed anchors, discard pending calculations and read afresh.
                state['phase']='idle'
                state.pop('plan',None)
            # A restart before a write resumes at durable cursors; a persisted intent
            # is rejected by the store before claiming, so it cannot run twice.
            if state.get('phase') not in ('orders','catalog','write'):
                state.update(phase='orders',started_at=self.clock(),order_cursor=None,seen_cursors=[],seen_orders={},
                    report={'imported_orders':0,'changed_sku':0,'unchanged_sku':0,'skipped':[]},error=None)
                save()
            timings=state['report'].setdefault('seconds',{})
            if state['phase']=='orders':
                phase_started=time.perf_counter()
                for _ in range(100):
                    orders,cursor=client.orders_page(state.get('since'),state.get('order_cursor'))
                    normalized=[normalize_order(o) for o in orders]
                    state.setdefault('seen_orders',{}).update({o['id']:o['updated_at'] for o in normalized})
                    if normalized:
                        state['report']['pending_orders']=[o['number'] for o in normalized]
                        save()  # renew lease before an atomic import
                        imported=self.store.import_orders(token,normalized)
                        self.cache_import(imported)
                        state['report']['imported_orders']+=imported.get('changed_count',len(imported['order_ids']))
                        state['report'].pop('pending_orders',None)
                    if cursor and cursor in state['seen_cursors']: raise OrderchampError('REPEATED_CURSOR')
                    state['order_cursor']=cursor
                    if cursor: state['seen_cursors'].append(cursor)
                    else: state['phase']='catalog'
                    save()
                    if not cursor: break
                else: raise OrderchampError('ORDER_SCAN_LIMIT')
                timings['orders_and_import']=round(time.perf_counter()-phase_started,3)
            if state['phase']=='catalog':
                phase_started=time.perf_counter()
                snapshot=self.store.availability()
                timings['availability']=round(time.perf_counter()-phase_started,3)
                if snapshot.get('conflicts'):
                    raise OrderchampError('LOCAL_ORDER_CONFLICT_REVIEW')
                rows=snapshot['rows']
                if state.get('only_sku'):
                    rows=[r for r in rows if r.get('sku')==state['only_sku']]
                    if not rows: raise OrderchampError('LOCAL_SKU_NOT_FOUND')
                state['report']['only_sku']=state.get('only_sku')
                if not rows: raise OrderchampError('EMPTY_LOCAL_CATALOG')
                counts=Counter(r['sku'] for r in rows)
                if any(not isinstance(k,str) or not k or v!=1 for k,v in counts.items()):
                    raise OrderchampError('DUPLICATE_OR_INVALID_SKU')
                anchors=state.setdefault('anchors',{})
                for removed in (set(anchors)-set(counts) if not state.get('only_sku') else set()):
                    state['report']['skipped'].append({'sku':removed,'reason':'LOCAL_SKU_REMOVED_REVIEW'})
                # Normal cycles use saved anchors. A bounded catalogue reconciliation
                # every 15 minutes repairs missed changes and caches absent variants.
                now=datetime.fromisoformat(self.clock())
                checked=state.get('catalog_checked_at')
                audit_due=not checked or now-datetime.fromisoformat(checked)>=timedelta(minutes=15)
                deferred=state.setdefault('deferred',{})
                missing={r['sku'] for r in rows if r['sku'] not in anchors and r['sku'] not in deferred}
                inspect=set(counts) if audit_due else missing
                remote={}
                phase_started=time.perf_counter()
                if inspect:
                    cursor=None
                    seen=set()
                    for _ in range(100):
                        variants,cursor=client.variants_page(cursor,sorted(inspect))
                        for v in variants:
                            sku=v.get('sku')
                            if sku in inspect:
                                if sku in remote: raise OrderchampError('REMOTE_DUPLICATE_SKU')
                                remote[sku]=v
                        save()
                        if not cursor: break
                        if cursor in seen: raise OrderchampError('REPEATED_CURSOR')
                        seen.add(cursor)
                    else: raise OrderchampError('CATALOG_SCAN_LIMIT')
                    # Validate that the first remote anchor did not observe a sale
                    # missing from the imported order snapshot. ADJUST then preserves
                    # any sale/shipment that happens *after* the inventory read.
                    recent,more=client.orders_page(state['started_at'],None)
                    if more or any(state.get('seen_orders',{}).get(o.get('id'))!=stamp(o.get('updatedAt')) for o in recent):
                        state.update(phase='orders',order_cursor=None,seen_cursors=[],seen_orders={},started_at=self.clock())
                        raise OrderchampError('ORDERS_CHANGED_DURING_CATALOG')
                    if audit_due:
                        state['catalog_checked_at']=self.clock()
                    for sku in inspect: deferred.pop(sku,None)
                timings['catalog_and_order_recheck']=round(time.perf_counter()-phase_started,3)
                plan=[]
                for row in rows:
                    sku=row['sku']
                    if sku in deferred:
                        state['report']['skipped'].append({'sku':sku,'reason':deferred[sku]})
                        continue
                    available=integer(row['available_qty'])
                    basis=available+integer(row['oc_ordered'])
                    anchor=anchors.get(sku)
                    if anchor and sku not in inspect:
                        delta=basis-anchor['basis']
                        level_id=anchor['level_id']
                    else:
                        if sku not in remote:
                            deferred[sku]='REMOTE_SKU_NOT_FOUND'
                            state['report']['skipped'].append({'sku':sku,'reason':'REMOTE_SKU_NOT_FOUND'})
                            continue
                        try:
                            level=remote_level(remote[sku])
                        except OrderchampError as exc:
                            deferred[sku]=exc.code
                            state['report']['skipped'].append({'sku':sku,'reason':exc.code})
                            continue
                        if level['reserved']!=integer(row['oc_unshipped']):
                            deferred[sku]='ORDERS_NOT_YET_RECONCILED'
                            # Sales arriving between reads are reconciled next cycle.
                            state['report']['skipped'].append({'sku':sku,'reason':'ORDERS_NOT_YET_RECONCILED'})
                            continue
                        delta=available-level['available']
                        level_id=level['level_id']
                    if delta:
                        if delta>0 and integer(row.get('shortage_qty',0))>0:
                            state['report']['skipped'].append({'sku':sku,'reason':'OVERCOMMITTED_STOCK_REVIEW'})
                            continue
                        integer(delta,-2147483647)
                        plan.append({'sku':sku,'level_id':level_id,'delta':delta,'basis':basis})
                    else:
                        anchors[sku]={'level_id':level_id,'basis':basis}
                        state['report']['unchanged_sku']+=1
                state.update(plan=plan,phase='write')
                save()
            phase_started=time.perf_counter()
            while state.get('plan'):
                batch=state['plan'][:20]
                mutation_id=str(uuid.uuid4())
                # Must be durable BEFORE sending a non-idempotent adjustment.
                state['intent']={'id':mutation_id,'rows':batch,'at':self.clock()}
                save()
                client.adjust_batch(batch,mutation_id)
                for row in batch:
                    state['anchors'][row['sku']]={'basis':row['basis'],'level_id':row['level_id']}
                state['report']['changed_sku']+=len(batch)
                state['plan']=state['plan'][len(batch):]
                del state['intent']
                save()
            state.update(phase='idle',since=(datetime.fromisoformat(state['started_at'])-timedelta(minutes=5)).isoformat(),error=None)
            timings['writes_and_checkpoints']=round(time.perf_counter()-phase_started,3)
            timings['total']=round(time.perf_counter()-began,3)
            timings['api_wait_included_above']=round(getattr(client,'wait_seconds',0),3)
            state['report'].update(completed_at=self.clock(),orderchamp_requests=client.request_count,
                catalog_checked_at=state.get('catalog_checked_at'))
            state.pop('plan',None)
            self.store.control('release',token,state)
            return {'claimed':True,'report':state['report']}
        except Exception as exc:
            code=exc.code if isinstance(exc,OrderchampError) else 'SYNC_FAILED'
            state['error']=code
            if state.get('intent'):
                state['review_required']=True
                state['error']='MUTATION_OUTCOME_UNKNOWN'
            # Do not persist partially advanced in-memory anchors if saving failed.
            # The store's previous durable intent still blocks restart.
            try: self.store.control('fail',token,state)
            except Exception: pass
            return {'claimed':True,'error':state['error']}
        finally:
            if client: client.close()
