"""Read-only isolation of inconsistent parcel history in the overview.

Keep the strict amendment validator intact. A failed component is displayed as
unresolved, never repaired by choosing an arbitrary replica or revision.
"""
import json

from packing_versions import PackingConflict


def components(records):
    parents = {}

    def root(node):
        parents.setdefault(node, node)
        if parents[node] != node:
            parents[node] = root(parents[node])
        return parents[node]

    def connect(nodes):
        nodes = list(nodes)
        if nodes:
            first = root(nodes[0])
            for node in nodes[1:]:
                parents[root(node)] = first

    def lists(record):
        return {('list', r['packing_list_id']) for section in
                ('packing_lists', 'packing_batches', 'packing_shipments')
                for r in record.get(section) or [] if r.get('packing_list_id')}

    proofs = []
    for record in records:
        for node in lists(record):
            root(node)
        batches_by_id = {int(r['id']): r for r in record.get('packing_batches') or []}
        for row in record.get('packing_shipments') or []:
            nodes = [('shipment', row['shipment_key']), ('list', row['packing_list_id'])]
            batch = batches_by_id.get(int(row['final_batch_id']), {})
            if batch.get('packing_list_id'):
                nodes.append(('list', batch['packing_list_id']))
            connect(nodes)
        for row in record.get('packing_lists') or []:
            batch = batches_by_id.get(int(row.get('current_batch_id') or 0), {})
            if batch.get('packing_list_id'):
                connect([('list', row['packing_list_id']), ('list', batch['packing_list_id'])])
        for proof in record.get('fulfillment_verifications') or []:
            kind = str(proof.get('kind') or '')
            if not kind.startswith(('shipment_amendment:', 'join:')):
                continue
            node = ('proof', kind)
            nodes = [node]
            data = None
            try:
                data = json.loads(proof['payload'])
                if kind.startswith('shipment_amendment:'):
                    nodes += [('shipment', data[field]) for field in
                              ('original_shipment_key', 'predecessor_key', 'successor_key')]
                    nodes += [('list', data[field]['packing_list_id']) for field in ('before', 'after')]
                else:
                    nodes += [('shipment', kind), ('list', data['packing_list'])]
                    nodes += [('shipment', key) for key in data['parents']]
                if any(not isinstance(value, str) or not value for _, value in nodes):
                    raise ValueError('Invalid proof references')
            except (ValueError, TypeError, KeyError):
                # An unreadable proof cannot safely be assigned to one parcel.
                # Quarantine its source snapshot, but not unrelated snapshots.
                nodes = [node, *lists(record)]
                nodes += [('shipment', r['shipment_key']) for r in record.get('packing_shipments') or []]
                # Retain readable references even when another field is broken;
                # a malformed cross-snapshot proof must not free its predecessor.
                if isinstance(data, dict):
                    for field in ('original_shipment_key', 'predecessor_key', 'successor_key'):
                        if isinstance(data.get(field), str) and data[field]:
                            nodes.append(('shipment', data[field]))
                    for field in ('before', 'after'):
                        scope = data.get(field)
                        if isinstance(scope, dict) and isinstance(scope.get('packing_list_id'), str) and scope['packing_list_id']:
                            nodes.append(('list', scope['packing_list_id']))
            connect(nodes)
            proofs.append((record, proof, node))

    groups = {}
    for node in parents:
        groups.setdefault(root(node), set()).add(node)
    for nodes in groups.values():
        list_ids = {value for kind, value in nodes if kind == 'list'}
        shipment_keys = {value for kind, value in nodes if kind == 'shipment'}
        projected = []
        for record in records:
            logical = [r for r in record.get('packing_lists') or [] if r['packing_list_id'] in list_ids]
            finals = [r for r in record.get('packing_shipments') or [] if r['shipment_key'] in shipment_keys]
            referenced = {int(r['final_batch_id']) for r in finals}
            referenced.update(int(r['current_batch_id']) for r in logical if r.get('current_batch_id'))
            batches = [r for r in record.get('packing_batches') or []
                       if r.get('packing_list_id') in list_ids or int(r['id']) in referenced]
            batch_ids = {r['id'] for r in batches}
            selected_proofs = [p for source, p, node in proofs if source is record and node in nodes]
            if not (batches or logical or finals or selected_proofs):
                continue
            projected.append(dict(record, packing_lists=logical, packing_batches=batches,
                packing_shipments=finals, fulfillment_verifications=selected_proofs,
                packing_allocations=[r for r in record.get('packing_allocations') or [] if r['batch_id'] in batch_ids]))
        yield list_ids, projected


def read(records, build_cards):
    cards, conflicts = [], []
    for list_ids, rows in components(records):
        try:
            cards.extend(build_cards(rows))
        except PackingConflict:
            # Identifiers are references to evidence, not a claim about the
            # disputed parcel's contents, quantity, invoice or delivery status.
            order_ids = sorted({int(value) for row in rows for value in
                [row.get('order_id'),
                 *(p.get('root_order_id') for p in row.get('packing_lists') or []),
                 *(p.get('order_id') for p in row.get('packing_allocations') or [])]
                if value and int(value) > 0})
            conflicts.append({'list_ids': sorted(list_ids), 'order_ids': order_ids})
    return sorted(cards, key=lambda card: card['created'], reverse=True), conflicts
