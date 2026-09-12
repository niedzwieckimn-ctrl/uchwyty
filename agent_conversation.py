"""Durable verbatim turns. No entity, keyword or reference resolution."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid
from pathlib import Path
from internal_audit import SUCCESS, record_audit_event
from internal_rbac import ActorContext, load_actor_context, ALLOW

CONVERSATION_TTL_MINUTES = 45
MAX_HISTORY_TURNS = 6
MAX_HISTORY_BYTES = 12000
MAX_MEMORY_BYTES = 4000
MAX_TERMS = 100
_connection_factory = None

class ConversationAccessDenied(RuntimeError):
    pass

class ConversationBusy(RuntimeError):
    pass

def configure(connection_factory):
    global _connection_factory
    if not callable(connection_factory):
        raise TypeError('connection_factory must be callable')
    _connection_factory = connection_factory

@contextmanager
def connection():
    if _connection_factory is None:
        raise RuntimeError('Conversation storage not configured')
    db = _connection_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def _utc_now():
    return datetime.now(timezone.utc)

def _iso(value):
    return value.isoformat()

def initialize_schema(db):
    db.executescript((Path(__file__).parent / 'migrations' / 'agent_runtime_history.sql').read_text(encoding='utf-8'))
    db.commit()

def _audit(event, human, cid, ai_id):
    record_audit_event(event, result=SUCCESS, actor_context=human,
        entity_type='agent_conversation', entity_id=cid, correlation_id=cid,
        after_state={'initiated_by_actor_id': human.actor_id, 'executed_by_actor_id': ai_id},
        source='agent_conversation')

def _owned(db, human, ai, cid):
    try:
        cid = str(uuid.UUID(str(cid)))
    except (ValueError, TypeError):
        raise ConversationAccessDenied('Invalid conversation')
    row = db.execute('SELECT * FROM internal_agent_conversations WHERE conversation_id=?', (cid,)).fetchone()
    if row is None or row['human_actor_id'] != human.actor_id or row['ai_actor_id'] != ai.actor_id:
        raise ConversationAccessDenied('Conversation not owned by actor')
    return row

def open_conversation(human, ai, conversation_id=''):
    now = _utc_now()
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        status = 'created'
        if conversation_id:
            row = _owned(db, human, ai, conversation_id)
            conversation_id = row['conversation_id']
            status = 'resumed'
            if datetime.fromisoformat(row['expires_at']) <= now:
                db.execute('DELETE FROM internal_agent_turns WHERE conversation_id=?', (conversation_id,))
                status = 'expired'
            db.execute("UPDATE internal_agent_conversations SET state_json='{}',last_active_at=?,expires_at=? WHERE conversation_id=?",
                (_iso(now), _iso(now+timedelta(minutes=CONVERSATION_TTL_MINUTES)), conversation_id))
        else:
            conversation_id = str(uuid.uuid4())
            db.execute("INSERT INTO internal_agent_conversations(conversation_id,human_actor_id,ai_actor_id,created_at,last_active_at,expires_at) VALUES(?,?,?,?,?,?)",
                (conversation_id, human.actor_id, ai.actor_id, _iso(now), _iso(now), _iso(now+timedelta(minutes=CONVERSATION_TTL_MINUTES))))
    _audit('agent.conversation.'+status, human, conversation_id, ai.actor_id)
    return conversation_id, {}, status

def reset_conversation(human, ai, conversation_id):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _owned(db, human, ai, conversation_id)
        if db.execute('SELECT 1 FROM internal_agent_turn_leases WHERE conversation_id=? AND expires_at>?',
                      (conversation_id, _iso(_utc_now()))).fetchone():
            raise ConversationBusy('Conversation is running')
        db.execute('DELETE FROM internal_agent_turns WHERE conversation_id=?', (conversation_id,))
        db.execute("UPDATE internal_agent_conversations SET state_json='{}', reset_at=? WHERE conversation_id=?", (_iso(_utc_now()), conversation_id))
    _audit('agent.conversation.reset', human, conversation_id, ai.actor_id)

def begin_turn(human, ai, cid, run_id, message):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _owned(db, human, ai, cid)
        now = _utc_now()
        db.execute('DELETE FROM internal_agent_turn_leases WHERE expires_at<=?', (_iso(now),))
        try:
            db.execute('INSERT INTO internal_agent_turn_leases VALUES(?,?,?)',
                (cid, run_id, _iso(now+timedelta(minutes=5))))
        except sqlite3.IntegrityError:
            raise ConversationBusy('Conversation is running')
        db.execute('INSERT INTO internal_agent_turns(run_id,conversation_id,user_text,created_at) VALUES(?,?,?,?)',
            (run_id, cid, message, _iso(now)))


def history_for_model(human, ai, cid, run_id):
    with connection() as db:
        _owned(db, human, ai, cid)
        rows = db.execute('SELECT * FROM internal_agent_turns WHERE conversation_id=? AND run_id<>? AND assistant_text IS NOT NULL ORDER BY turn_id DESC LIMIT ?',
            (cid, run_id, MAX_HISTORY_TURNS)).fetchall()
    selected, used = [], 0
    for row in rows:
        pair = [{'role':'user','content':row['user_text']}, {'role':'assistant','content':row['assistant_text']}]
        full = [{'role':'user','content':row['user_text']}] + json.loads(row['evidence_json']) + [pair[-1]]
        # Prefer complete protocol groups. Never slice a function call/output pair.
        group = full if len(json.dumps(full,ensure_ascii=False).encode()) <= MAX_HISTORY_BYTES-used else pair
        size = len(json.dumps(group,ensure_ascii=False).encode())
        if used+size > MAX_HISTORY_BYTES:
            break
        selected.append(group); used += size
    return [item for group in reversed(selected) for item in group]


def finish_turn(human, ai, cid, run_id, answer, evidence):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _owned(db, human, ai, cid)
        lease = db.execute('SELECT run_id FROM internal_agent_turn_leases WHERE conversation_id=?', (cid,)).fetchone()
        if not lease or lease['run_id'] != run_id:
            raise ConversationBusy('Turn lease lost')
        db.execute('UPDATE internal_agent_turns SET assistant_text=?,evidence_json=? WHERE run_id=? AND conversation_id=?',
            (answer, json.dumps(evidence,ensure_ascii=False,separators=(',',':')), run_id, cid))
        db.execute('DELETE FROM internal_agent_turn_leases WHERE conversation_id=? AND run_id=?', (cid,run_id))


def memory_for_model(human, ai):
    # The existing installation is single-company, one SQLite database per company.
    with connection() as db:
        terms = db.execute('SELECT term,meaning,scope,source,version FROM internal_agent_terminology ORDER BY updated_at DESC LIMIT ?', (MAX_TERMS,)).fetchall()
        style = db.execute('SELECT preferences_json FROM internal_agent_user_style WHERE human_actor_id=?', (human.actor_id,)).fetchone()
    result = {'confirmed_terminology': [], 'user_style': json.loads(style[0]) if style else {}}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 1000:
        result['user_style'] = {}
    for row in terms:
        candidate = dict(row)
        result['confirmed_terminology'].append(candidate)
        if len(json.dumps(result,ensure_ascii=False).encode()) > MAX_MEMORY_BYTES:
            result['confirmed_terminology'].pop(); break
    return result


def remember_terminology(data, actor, correlation_id, transaction_connection=None):
    """LLM decides semantic confirmation; storage checks provenance and permissions only."""
    from business_operations import ControlledOperationError
    human = load_actor_context(actor.delegated_by_actor_id)
    if human is None or human.actor_type != 'HUMAN' or human.permission_decision('agent.terminology.remember') != ALLOW:
        raise ControlledOperationError('PERMISSION_DENIED', 'Brak uprawnień do pamięci firmy')
    if data['confirmed_by_user'] is not True:
        raise ControlledOperationError('CONFIRMATION_REQUIRED', 'Termin wymaga potwierdzenia użytkownika')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM internal_agent_turns WHERE run_id=?', (data['source_run_id'],)).fetchone()
        if row is None:
            raise ControlledOperationError('INVALID_MEMORY_SOURCE', 'Brak źródła potwierdzenia')
        _owned(db,human,actor,row['conversation_id'])
        lease = db.execute('SELECT run_id FROM internal_agent_turn_leases WHERE conversation_id=?', (row['conversation_id'],)).fetchone()
        if not lease or lease['run_id'] != data['source_run_id'] or row['assistant_text'] is not None:
            raise ControlledOperationError('INVALID_MEMORY_SOURCE', 'Potwierdzenie nie pochodzi z aktywnego turnu')
        previous = db.execute('SELECT * FROM internal_agent_terminology WHERE term=? COLLATE NOCASE', (data['term'],)).fetchone()
        if previous and previous['source_run_id'] == data['source_run_id'] and previous['meaning'] == data['meaning']:
            return {'ok':True,'term':previous['term'],'version':previous['version']}
        version = previous['version'] if previous else 0
        if version != data['expected_version']:
            raise ControlledOperationError('MEMORY_VERSION_CONFLICT', 'Termin zmienił się; odczytaj jego aktualną wersję')
        if not previous and db.execute('SELECT COUNT(*) FROM internal_agent_terminology').fetchone()[0] >= MAX_TERMS:
            raise ControlledOperationError('MEMORY_LIMIT', 'Osiągnięto limit pamięci terminologii')
        db.execute('''INSERT INTO internal_agent_terminology(term,meaning,scope,source,source_run_id,confirmed_by_actor_id,version,updated_at)
            VALUES(?,?,'company','confirmed_by_user',?,?,?,?) ON CONFLICT(term) DO UPDATE SET
            meaning=excluded.meaning,source_run_id=excluded.source_run_id,confirmed_by_actor_id=excluded.confirmed_by_actor_id,
            version=excluded.version,updated_at=excluded.updated_at''',
            (data['term'],data['meaning'],data['source_run_id'],human.actor_id,version+1,_iso(_utc_now())))
        record_audit_event('agent.terminology.remembered', result=SUCCESS, actor_context=actor,
            entity_type='agent_terminology', entity_id=data['term'],correlation_id=correlation_id,
            before_state={'meaning':previous['meaning'],'version':version} if previous else None,
            after_state={'meaning':data['meaning'],'version':version+1,'confirmed_by_actor_id':human.actor_id,'source_run_id':data['source_run_id']},
            source='agent_conversation',transaction_connection=db)
    return {'ok':True,'term':data['term'],'version':version+1}


def search_terminology(data, actor, correlation_id, transaction_connection=None):
    with connection() as db:
        rows = db.execute("SELECT term,meaning,scope,source,version FROM internal_agent_terminology WHERE instr(lower(term),lower(?))>0 ORDER BY term LIMIT 10", (data['query'],)).fetchall()
    return {'ok':True,'results':[dict(row) for row in rows]}
