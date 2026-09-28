"""Durable, session/conversation scoped clarification; no approval authority."""

import json


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS internal_inventory_voice_clarifications(
        session_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        context_json TEXT NOT NULL,
        PRIMARY KEY(session_id,conversation_id)
    )''')


def _scope(operations, db, ai_actor, human_actor, conversation_id):
    ai = operations._trusted_actor(ai_actor)
    human = operations.load_actor_context(human_actor.actor_id)
    if (human is None or human.actor_type != 'HUMAN'
            or ai.delegated_by_actor_id != human.actor_id):
        raise operations.ControlledOperationError('UNTRUSTED_ACTOR', 'Brak właściciela sesji', status=operations.DENIED)
    if (ai.permission_decision('inventory.read') == operations.PERMISSION_DENY
            or human.permission_decision('inventory.read') == operations.PERMISSION_DENY):
        raise operations.ControlledOperationError('PERMISSION_DENIED', 'Brak dostępu do liczenia', status=operations.DENIED)
    operations._assert_count_conversation(db, ai, conversation_id)
    session = operations._open_count_session(db, human.actor_id, conversation_id)
    if not session:
        raise operations.ControlledOperationError('COUNT_SESSION_NOT_FOUND', 'Brak otwartej sesji', status=operations.CONFLICT)
    return str(session['session_id'])


def load(operations, ai_actor, human_actor, conversation_id):
    db = operations._factory()()
    try:
        session_id = _scope(operations, db, ai_actor, human_actor, conversation_id)
        row = db.execute('''SELECT context_json FROM internal_inventory_voice_clarifications
                            WHERE session_id=? AND conversation_id=?''', (session_id, conversation_id)).fetchone()
        return json.loads(row['context_json']) if row else {}
    finally:
        db.close()


def timing(operations, ai_actor, human_actor, conversation_id):
    import inventory_count_lifecycle
    db = operations._factory()()
    try:
        session_id = _scope(operations, db, ai_actor, human_actor, conversation_id)
        return inventory_count_lifecycle.timing(db, session_id)
    finally:
        db.close()


def save(operations, ai_actor, human_actor, conversation_id, context):
    candidates = []
    for item in context.get('candidates', [])[:20]:
        candidates.append({key:item[key] for key in ('id','sku','model','name','ean') if key in item})
    quantity = context.get('quantity')
    if quantity is not None and (type(quantity) is not int or not 0 <= quantity <= 9999999):
        raise ValueError('Invalid pending count')
    safe = {'candidates':candidates, 'quantity':quantity,
            'quantity_options':list(context.get('quantity_options', ()))[:10]}
    encoded = json.dumps(safe, ensure_ascii=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > 20000:
        raise ValueError('Inventory clarification too large')
    db = operations._factory()()
    try:
        session_id = _scope(operations, db, ai_actor, human_actor, conversation_id)
        if not context:
            db.execute('DELETE FROM internal_inventory_voice_clarifications WHERE session_id=? AND conversation_id=?',
                       (session_id, conversation_id))
        else:
            db.execute('''INSERT INTO internal_inventory_voice_clarifications VALUES(?,?,?)
                ON CONFLICT(session_id,conversation_id) DO UPDATE SET context_json=excluded.context_json''',
                (session_id, conversation_id, encoded))
        db.commit()
    finally:
        db.close()
