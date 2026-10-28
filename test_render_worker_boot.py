"""Import the real app in a fresh process, as a pre-fork server may do."""
import os
from pathlib import Path
import subprocess
import sys
import pytest


def test_render_import_does_not_start_sqlite_background_threads(tmp_path):
    env = dict(os.environ, RENDER='true', APP_DATA_DIR=str(tmp_path),
               SUPABASE_URL='https://startup.example.invalid',
               SUPABASE_SERVICE_ROLE_KEY='test-only-not-a-credential',
               SUPABASE_ANON_KEY='', SUPABASE_AUTO_SYNC_ON_WRITE='0')
    script = '''
import inpost_pickups, inpost_tracking, ksef_scheduler, internal_audit_outbox
def premature(*args, **kwargs):
    raise AssertionError('Background worker started during pre-fork import')
for module in (inpost_pickups, inpost_tracking, ksef_scheduler, internal_audit_outbox):
    module.start_worker = premature
import app
assert app.supabase_enabled()
assert app._APP_IMPORT_PID == __import__('os').getpid()
print('IMPORT_WITHOUT_BACKGROUND_WORKERS_OK')
'''
    result = subprocess.run([sys.executable, '-c', script],
                            cwd=Path(__file__).parent, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'IMPORT_WITHOUT_BACKGROUND_WORKERS_OK' in result.stdout


@pytest.mark.parametrize('started', [True, False])
def test_gunicorn_hook_accepts_audit_worker_boolean_contract(monkeypatch, started):
    import logging
    import runpy
    from types import SimpleNamespace
    import app
    import inpost_pickups, inpost_tracking, ksef_scheduler
    monkeypatch.setattr(app, 'supabase_enabled', lambda: True)
    monkeypatch.setattr(app, 'start_audit_outbox_worker', lambda: started)
    for module in (inpost_pickups, inpost_tracking, ksef_scheduler):
        monkeypatch.setattr(module, 'start_worker', lambda backend: None)
    hook = runpy.run_path(str(Path(__file__).with_name('gunicorn.conf.py')))['post_worker_init']
    hook(SimpleNamespace(pid=os.getpid(), cfg=SimpleNamespace(preload_app=False),
                         log=logging.getLogger('boot-test')))
