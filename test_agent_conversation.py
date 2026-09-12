from datetime import timedelta
import json
import uuid
import pytest
import app as backend
import agent_conversation as conversations
import agent_runtime as runtime
import internal_rbac as rbac

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(backend,'DB_PATH',str(tmp_path/'conversation.db'))
    backend.init_db()

def actors():
    return (rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID),rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID))

def saved_turn(cid,text='Pokaż Avery.',answer='Avery 128 i Avery 160.',evidence=None):
    human,ai=actors(); run=str(uuid.uuid4())
    conversations.begin_turn(human,ai,cid,run,text)
    conversations.finish_turn(human,ai,cid,run,answer,evidence or [])
    return run

def test_verbatim_history_survives_storage_reopen():
    human,ai=actors();cid=conversations.open_conversation(human,ai)[0]
    saved_turn(cid,'Pokaż Avery.','Avery 128 i Avery 160.')
    conversations.configure(backend.conn)
    assert conversations.history_for_model(human,ai,cid,'next')==[
        {'role':'user','content':'Pokaż Avery.'},{'role':'assistant','content':'Avery 128 i Avery 160.'}]
    db=backend.conn();assert db.execute('SELECT state_json FROM internal_agent_conversations').fetchone()[0]=='{}';db.close()

def test_history_keeps_complete_tool_protocol():
    human,ai=actors();cid=conversations.open_conversation(human,ai)[0]
    evidence=[{'type':'function_call','name':'orders__get','call_id':'c','arguments':'{"id":31}'},
              {'type':'function_call_output','call_id':'c','output':'{"items":[{"product_id":2}]}'}]
    saved_turn(cid,evidence=evidence)
    assert conversations.history_for_model(human,ai,cid,'next')[1:3]==evidence

def test_large_evidence_falls_back_to_verbatim_pair_and_window_is_bounded():
    human,ai=actors();cid=conversations.open_conversation(human,ai)[0]
    for i in range(12):
        saved_turn(cid,'q'+str(i),'a'+str(i),[{'role':'assistant','content':'x'*15000}])
    history=conversations.history_for_model(human,ai,cid,'next')
    assert history[0]['content']=='q6' and history[-1]['content']=='a11'
    assert len(json.dumps(history).encode())<=conversations.MAX_HISTORY_BYTES
    assert len(history)==12

def test_other_human_cannot_read_append_or_reset():
    from dataclasses import replace
    human,ai=actors();cid=conversations.open_conversation(human,ai)[0]
    other=replace(human,actor_id=str(uuid.uuid4()))
    for action in [lambda:conversations.open_conversation(other,ai,cid),
                   lambda:conversations.history_for_model(other,ai,cid,'x'),
                   lambda:conversations.begin_turn(other,ai,cid,'x','test'),
                   lambda:conversations.reset_conversation(other,ai,cid)]:
        with pytest.raises(conversations.ConversationAccessDenied):action()

def test_expiry_and_reset_remove_history_keep_audit():
    human,ai=actors();cid=conversations.open_conversation(human,ai)[0];saved_turn(cid)
    db=backend.conn();db.execute('UPDATE internal_agent_conversations SET expires_at=?',
        ((conversations._utc_now()-timedelta(seconds=1)).isoformat(),));db.commit();db.close()
    assert conversations.open_conversation(human,ai,cid)[2]=='expired'
    assert conversations.history_for_model(human,ai,cid,'x')==[]
    saved_turn(cid);conversations.reset_conversation(human,ai,cid)
    assert conversations.history_for_model(human,ai,cid,'x')==[]
    db=backend.conn();assert db.execute('SELECT COUNT(*) FROM internal_audit_log').fetchone()[0]>0;db.close()

def test_concurrent_turn_and_reset_are_rejected():
    human,ai=actors();cid=conversations.open_conversation(human,ai)[0]
    conversations.begin_turn(human,ai,cid,'one','hello')
    with pytest.raises(conversations.ConversationBusy):conversations.begin_turn(human,ai,cid,'two','hello')
    with pytest.raises(conversations.ConversationBusy):conversations.reset_conversation(human,ai,cid)
    conversations.finish_turn(human,ai,cid,'one','hi',[])
    saved_turn(cid)

def test_expired_lease_recovers_after_worker_crash():
    human,ai=actors();cid=conversations.open_conversation(human,ai)[0]
    conversations.begin_turn(human,ai,cid,'one','hello')
    db=backend.conn();db.execute('UPDATE internal_agent_turn_leases SET expires_at=?',
        ((conversations._utc_now()-timedelta(seconds=1)).isoformat(),));db.commit();db.close()
    saved_turn(cid)


def test_schema_is_additive_and_repeatable():
    human,ai=actors();cid=conversations.open_conversation(human,ai)[0];saved_turn(cid)
    db=backend.conn();conversations.initialize_schema(db);conversations.initialize_schema(db);db.close()
    assert conversations.history_for_model(human,ai,cid,'x')
