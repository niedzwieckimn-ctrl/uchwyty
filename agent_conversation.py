"""Durable verbatim turns. No entity, keyword or reference resolution."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import unicodedata
import uuid
from pathlib import Path
from internal_audit import SUCCESS, record_audit_event, sanitize_audit_text
from internal_rbac import load_actor_context, ALLOW

CONVERSATION_TTL_MINUTES = 45
TURN_LEASE_TTL = timedelta(minutes=5)
MAX_HISTORY_TURNS = 6
MAX_HISTORY_BYTES = 12000
MAX_MEMORY_BYTES = 4000
MAX_TERMS = 100
ALWAYS_APPLY_RELEVANCE_TERM = '__always_apply__'
SQLITE_MIGRATIONS = ('agent_runtime_history.sql', 'agent_durable_memory_sqlite.sql')
_connection_factory = None
_remote_memory_enabled = None
_remote_memory_select = None
_remote_memory_upsert = None
logger = logging.getLogger(__name__)

class ConversationAccessDenied(RuntimeError):
    pass

class ConversationBusy(RuntimeError):
    pass

def configure(connection_factory, *, remote_memory_enabled=None, remote_memory_select=None, remote_memory_upsert=None):
    global _connection_factory, _remote_memory_enabled, _remote_memory_select, _remote_memory_upsert
    if not callable(connection_factory):
        raise TypeError('connection_factory must be callable')
    _connection_factory = connection_factory
    _remote_memory_enabled = remote_memory_enabled
    _remote_memory_select = remote_memory_select
    _remote_memory_upsert = remote_memory_upsert

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
    migrations = Path(__file__).parent / 'migrations'
    for filename in SQLITE_MIGRATIONS:
        db.executescript((migrations / filename).read_text(encoding='utf-8'))
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
        acquired = db.execute('''INSERT INTO internal_agent_turn_leases(conversation_id,run_id,expires_at)
            VALUES(?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
            run_id=excluded.run_id,expires_at=excluded.expires_at
            WHERE internal_agent_turn_leases.expires_at<=?''',
            (cid, run_id, _iso(now+TURN_LEASE_TTL), _iso(now)))
        if acquired.rowcount != 1:
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


def release_turn(human, ai, cid, run_id):
    """Release only this run's lease when any later lifecycle step fails."""
    if not cid or not run_id or ai is None:
        return
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _owned(db, human, ai, cid)
        db.execute('DELETE FROM internal_agent_turn_leases WHERE conversation_id=? AND run_id=?', (cid, run_id))


def _tokens(value):
    return {token for token in re.findall(r'(?u)\b[\w-]{3,}\b', str(value).casefold())}


_POLISH_INFLECTION_CLASSES = (
    # Conservative, deterministic noun/adjective classes.  They intentionally
    # do not provide a generic "strip the ending" fallback.
    ('ska', ('ska', 'skiej', 'ską', 'skie', 'scy')),
    ('cka', ('cka', 'ckiej', 'cką', 'ckie', 'ccy')),
    ('dzka', ('dzka', 'dzkiej', 'dzką', 'dzkie', 'dzcy')),
    ('nia', ('nia', 'ni', 'nię', 'nią')),
    ('wa', ('wa', 'wy', 'wę', 'wie', 'wą', 'wach')),
    ('ra', ('ra', 'ry', 'rę', 'rze', 'rą')),
    ('ma', ('ma', 'my', 'mę', 'mie', 'mą')),
    ('na', ('na', 'nej', 'ną', 'ne', 'ni')),
    ('ka', ('ka', 'ki', 'kę', 'ce', 'ką')),
    ('ga', ('ga', 'gi', 'gę', 'dze', 'gą')),
    ('ca', ('ca', 'cy', 'cę', 'cą')),
    ('ów', ('ów', 'owa', 'owie', 'owem')),
    ('aw', ('aw', 'awia', 'awiu', 'awiem')),
    ('dź', ('dź', 'dzi', 'dzią')),
    ('ń', ('ń', 'nia', 'niu', 'niem')),
    ('sk', ('sk', 'ska', 'sku', 'skiem')),
    ('in', ('in', 'ina', 'inie', 'inem')),
    ('ice', ('ice', 'ic', 'icach', 'icami')),
)


def _terminology_words(value):
    normalized = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return re.findall(r'(?u)[\w]+', normalized)


def _inflection_identity(word):
    """Return bounded Polish inflection identities for one word."""
    identities = set()
    for class_name, variants in _POLISH_INFLECTION_CLASSES:
        for variant in variants:
            if word.endswith(variant) and len(word) - len(variant) >= 3:
                identities.add((class_name, word[:-len(variant)]))
    return identities


def _terminology_match_rank(term, query):
    """Match a stored term in text, sharing semantics between prefetch and search."""
    term_words = _terminology_words(term)
    query_words = _terminology_words(query)
    if not term_words or not query_words:
        return None
    width = len(term_words)
    for start in range(len(query_words) - width + 1):
        candidate = query_words[start:start + width]
        if candidate == term_words:
            return 0
        if all(
            wanted == actual or bool(_inflection_identity(wanted) & _inflection_identity(actual))
            for wanted, actual in zip(term_words, candidate)
        ):
            return 1
    # Preserve the documented fragment lookup for a short explicit search,
    # without interpreting a whole sentence as a fragment.
    if len(query_words) == 1 and len(query_words[0]) >= 3:
        needle = query_words[0]
        if any(needle in word for word in term_words):
            return 2
    return None


def _matching_terminology(rows, query, limit):
    ranked = []
    for row in rows:
        item = dict(row)
        rank = _terminology_match_rank(item.get('term'), query)
        if rank is not None:
            ranked.append((rank, item))
    # Stable passes make the result deterministic: rank, newest definition,
    # then term.  All matching definitions remain candidates; none is silently
    # selected when more than one term matches.
    ranked.sort(key=lambda item: str(item[1].get('term') or '').casefold())
    ranked.sort(key=lambda item: str(item[1].get('updated_at') or ''), reverse=True)
    ranked.sort(key=lambda item: item[0])
    return [item for _rank, item in ranked[:limit]], len(ranked)


def _memory_write_failure(exc, data):
    status = None
    current = exc
    while current is not None:
        candidate = getattr(current, 'code', None) or getattr(current, 'status_code', None)
        if isinstance(candidate, int):
            status = candidate
            break
        current = getattr(current, '__cause__', None)
    message = sanitize_audit_text(exc)
    if status is None:
        match = re.search(r'\bHTTP\s+(\d{3})\b', message, re.IGNORECASE)
        status = int(match.group(1)) if match else None
    for private_value in [data.get('content'), data.get('memory_key'), *(data.get('relevance_terms') or [])]:
        if private_value:
            message = message.replace(str(private_value), '[REDACTED]')
    logger.error('MEMORY_WRITE_FAILURE %s', json.dumps({
        'stage':'supabase_authoritative_write',
        'exception_type':type(exc).__name__,
        'exception_message':message[:500],
        'supabase_http_status':status,
    }, ensure_ascii=False, sort_keys=True))


def _cache_memory_rows(rows):
    with connection() as db:
        for row in rows:
            terms = row.get('relevance_terms', row.get('relevance_terms_json', []))
            if not isinstance(terms, str):
                terms = json.dumps(terms, ensure_ascii=False, separators=(',', ':'))
            db.execute('''INSERT INTO internal_agent_memory(
                memory_id,memory_key,category,scope,human_actor_id,content,relevance_terms_json,
                source_run_id,confirmed_by_actor_id,version,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(memory_id) DO UPDATE SET memory_key=excluded.memory_key,category=excluded.category,
                scope=excluded.scope,human_actor_id=excluded.human_actor_id,content=excluded.content,
                relevance_terms_json=excluded.relevance_terms_json,source_run_id=excluded.source_run_id,
                confirmed_by_actor_id=excluded.confirmed_by_actor_id,version=excluded.version,updated_at=excluded.updated_at''',
                (row['memory_id'],row['memory_key'],row['category'],row['scope'],row.get('human_actor_id') or '',
                 row['content'],terms,row['source_run_id'],row['confirmed_by_actor_id'],row['version'],row['updated_at']))


def _memory_rows():
    remote = bool(_remote_memory_enabled and _remote_memory_enabled())
    if remote:
        try:
            rows = list(_remote_memory_select())
            _cache_memory_rows(rows)
            return rows
        except Exception:
            # Preferences are advisory only. The cache keeps them available during a transient outage.
            pass
    with connection() as db:
        rows = db.execute('SELECT * FROM internal_agent_memory ORDER BY updated_at DESC').fetchall()
    return [dict(row) for row in rows]


def _relevant_memory(rows, human, query, limit=8):
    query_tokens = _tokens(query)
    selected = []
    for row in rows:
        if row['scope'] == 'user' and (row.get('human_actor_id') or '') != human.actor_id:
            continue
        if row['scope'] not in {'company','user'}:
            continue
        raw_terms = row.get('relevance_terms', row.get('relevance_terms_json', []))
        try:
            terms = json.loads(raw_terms) if isinstance(raw_terms, str) else raw_terms
        except (TypeError, ValueError, json.JSONDecodeError):
            terms = []
        always_apply = any(
            str(term).casefold() == ALWAYS_APPLY_RELEVANCE_TERM
            for term in (terms or [])
        )
        memory_tokens = _tokens(row['memory_key']) | _tokens(row['content']) | _tokens(' '.join(terms or []))
        overlap = len(query_tokens & memory_tokens)
        if always_apply or overlap:
            selected.append((always_apply, overlap, row))
    selected.sort(key=lambda item: (item[0], item[1], str(item[2].get('updated_at',''))), reverse=True)
    return [item[2] for item in selected[:limit]]


def memory_for_model(human, ai, query=''):
    # The existing installation is single-company, one SQLite database per company.
    with connection() as db:
        term_rows = db.execute(
            'SELECT term,meaning,scope,source,version,updated_at FROM internal_agent_terminology'
        ).fetchall()
        style = db.execute('SELECT preferences_json FROM internal_agent_user_style WHERE human_actor_id=?', (human.actor_id,)).fetchone()
    terms, terminology_matched = _matching_terminology(term_rows, query, MAX_TERMS)
    durable = _relevant_memory(_memory_rows(), human, query)
    result = {'confirmed_terminology': [], 'user_style': json.loads(style[0]) if style else {},
              'relevant_company_memory': []}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 1000:
        result['user_style'] = {}
    terminology_included = 0
    for row in terms:
        candidate = {key: row[key] for key in ('term', 'meaning', 'scope', 'source', 'version')}
        result['confirmed_terminology'].append(candidate)
        if len(json.dumps(result,ensure_ascii=False).encode()) > MAX_MEMORY_BYTES:
            result['confirmed_terminology'].pop(); break
        terminology_included += 1
    for row in durable:
        result['relevant_company_memory'].append(
            {'memory_key':row['memory_key'],'category':row['category'],'scope':row['scope'],
             'content':row['content'],'version':row['version']})
        if len(json.dumps(result,ensure_ascii=False).encode()) > MAX_MEMORY_BYTES:
            result['relevant_company_memory'].pop()
            break
    logger.info('TERMINOLOGY_RETRIEVAL %s', json.dumps({
        'matched_count':terminology_matched,
        'included_count':terminology_included,
        'dropped_by_limit':max(0, terminology_matched-terminology_included),
        'ambiguous':terminology_matched > 1,
        'source':'prefetch',
    }, sort_keys=True))
    return result


def remember_memory(data, actor, correlation_id, transaction_connection=None):
    """Persist a confirmed work preference/procedure remotely first, then update SQLite cache."""
    from business_operations import ControlledOperationError
    human = load_actor_context(actor.delegated_by_actor_id)
    if human is None or human.actor_type != 'HUMAN' or human.permission_decision('agent.terminology.remember') != ALLOW:
        raise ControlledOperationError('PERMISSION_DENIED', 'Brak uprawnień do pamięci firmy')
    if data['confirmed_by_user'] is not True:
        raise ControlledOperationError('CONFIRMATION_REQUIRED', 'Zasada wymaga potwierdzenia użytkownika')
    terms = data['relevance_terms']
    if (not isinstance(terms, list) or not 1 <= len(terms) <= 12
            or any(not isinstance(term, str) or not 2 <= len(term) <= 60 for term in terms)):
        raise ControlledOperationError('INVALID_INPUT', 'Hasła relewancji mają nieprawidłową wartość')
    with connection() as db:
        row = db.execute('SELECT * FROM internal_agent_turns WHERE run_id=?', (data['source_run_id'],)).fetchone()
        if row is None:
            raise ControlledOperationError('INVALID_MEMORY_SOURCE', 'Brak źródła potwierdzenia')
        _owned(db,human,actor,row['conversation_id'])
        lease = db.execute('SELECT run_id FROM internal_agent_turn_leases WHERE conversation_id=?', (row['conversation_id'],)).fetchone()
        if not lease or lease['run_id'] != data['source_run_id'] or row['assistant_text'] is not None:
            raise ControlledOperationError('INVALID_MEMORY_SOURCE', 'Potwierdzenie nie pochodzi z aktywnego turnu')

    actor_scope = human.actor_id if data['scope'] == 'user' else ''
    rows = _memory_rows()
    previous = next((row for row in rows if row['category']==data['category'] and row['scope']==data['scope']
                     and (row.get('human_actor_id') or '')==actor_scope
                     and row['memory_key'].casefold()==data['memory_key'].casefold()), None)
    version = int(previous['version']) if previous else 0
    previous_terms = previous.get('relevance_terms', previous.get('relevance_terms_json', [])) if previous else []
    try:
        previous_terms = json.loads(previous_terms) if isinstance(previous_terms, str) else previous_terms
    except (TypeError, ValueError, json.JSONDecodeError):
        previous_terms = []
    if (previous and previous['content'] == data['content']
            and list(previous_terms or []) == data['relevance_terms']):
        _cache_memory_rows([previous])
        with connection() as db:
            record_audit_event('agent.memory.remembered', result=SUCCESS, actor_context=actor,
                entity_type='agent_memory', entity_id=previous['memory_id'], correlation_id=correlation_id,
                before_state={'content':previous['content'],'version':version},
                after_state={'memory_key':previous['memory_key'],'category':previous['category'],'scope':previous['scope'],
                             'content':previous['content'],'version':version,'confirmed_by_actor_id':human.actor_id,
                             'source_run_id':data['source_run_id']}, source='agent_conversation',is_replay=True,
                transaction_connection=db)
        return {'ok':True,'memory_key':previous['memory_key'],'version':version}
    if version != data['expected_version']:
        raise ControlledOperationError('MEMORY_VERSION_CONFLICT', 'Zasada zmieniła się; odczytaj jej aktualną wersję')
    memory_id = previous['memory_id'] if previous else str(uuid.uuid4())
    saved = {
        'memory_id':memory_id,'memory_key':data['memory_key'],'category':data['category'],'scope':data['scope'],
        'human_actor_id':actor_scope,'content':data['content'],'relevance_terms':data['relevance_terms'],
        'source_run_id':data['source_run_id'],'confirmed_by_actor_id':human.actor_id,
        'version':version+1,'updated_at':_iso(_utc_now()),
    }
    reconciled = False
    if _remote_memory_enabled and _remote_memory_enabled():
        try:
            _remote_memory_upsert(saved)
        except Exception as exc:
            authoritative = []
            try:
                authoritative = list(_remote_memory_select())
            except Exception:
                pass
            matched = next((row for row in authoritative
                if row['category']==data['category'] and row['scope']==data['scope']
                and (row.get('human_actor_id') or '')==actor_scope
                and row['memory_key'].casefold()==data['memory_key'].casefold()), None)
            matched_terms = matched.get('relevance_terms', matched.get('relevance_terms_json', [])) if matched else []
            try:
                matched_terms = json.loads(matched_terms) if isinstance(matched_terms, str) else matched_terms
            except (TypeError, ValueError, json.JSONDecodeError):
                matched_terms = []
            if (matched and matched['content'] == data['content']
                    and list(matched_terms or []) == data['relevance_terms']):
                previous = matched
                saved = dict(matched)
                memory_id = saved['memory_id']
                version = int(saved['version'])
                reconciled = True
            else:
                _memory_write_failure(exc, data)
                if matched:
                    raise ControlledOperationError('MEMORY_VERSION_CONFLICT', 'Zasada zmieniła się; odczytaj jej aktualną wersję') from exc
                raise ControlledOperationError('MEMORY_STORAGE_UNAVAILABLE', 'Nie udało się zapisać pamięci firmy w Supabase') from exc
    _cache_memory_rows([saved])
    saved_version = int(saved['version'])
    with connection() as db:
        record_audit_event('agent.memory.remembered', result=SUCCESS, actor_context=actor,
            entity_type='agent_memory', entity_id=memory_id, correlation_id=correlation_id,
            before_state={'content':previous['content'],'version':version} if previous else None,
            after_state={'memory_key':data['memory_key'],'category':data['category'],'scope':data['scope'],
                         'content':data['content'],'version':saved_version,'confirmed_by_actor_id':human.actor_id,
                         'source_run_id':data['source_run_id']}, source='agent_conversation',is_replay=reconciled,
                         transaction_connection=db)
    return {'ok':True,'memory_key':data['memory_key'],'version':saved_version}


def search_memory(data, actor, correlation_id, transaction_connection=None):
    human = load_actor_context(actor.delegated_by_actor_id) if actor.actor_type == 'AI_AGENT' else actor
    rows = _relevant_memory(_memory_rows(), human, data['query'], limit=10)
    if data.get('category'):
        rows = [row for row in rows if row['category'] == data['category']]
    return {'ok':True,'results':[{'memory_key':row['memory_key'],'category':row['category'],
            'scope':row['scope'],'content':row['content'],'version':row['version']} for row in rows]}


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
        term_rows = db.execute(
            'SELECT term,meaning,scope,source,version,updated_at FROM internal_agent_terminology'
        ).fetchall()
    rows, matched = _matching_terminology(term_rows, data['query'], 10)
    logger.info('TERMINOLOGY_RETRIEVAL %s', json.dumps({
        'matched_count':matched,
        'included_count':len(rows),
        'dropped_by_limit':max(0, matched-len(rows)),
        'ambiguous':matched > 1,
        'source':'explicit_search',
    }, sort_keys=True))
    return {'ok':True,'results':[
        {key: row[key] for key in ('term', 'meaning', 'scope', 'source', 'version')}
        for row in rows
    ]}
