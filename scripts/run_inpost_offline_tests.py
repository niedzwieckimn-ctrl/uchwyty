"""Run the selected InPost acceptance/regression scope without external IO.

Install requirements-dev.txt in a disposable venv first. Optional --deps points
to a pip --target directory. Output contains only synthetic test data.
"""
import argparse
import os
from pathlib import Path
import socket
import sys
import tempfile
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--deps')
parser.add_argument('--output', help='New directory for synthetic SQLite, JUnit and pytest temporary files')
args = parser.parse_args()
source = Path(__file__).resolve().parents[1]
output = Path(args.output).resolve() if args.output else Path(tempfile.mkdtemp(prefix='inpost-offline-'))
if args.output:
    output.mkdir(parents=True, exist_ok=False)
for key in list(os.environ):
    if key.startswith(('SUPABASE_', 'OPENAI_', 'INPOST_', 'KSEF_', 'SMTP_', 'RESEND_',
                       'SEVENTEENTRACK_', 'ADMIN_', 'AI_', 'EMAIL_')):
        os.environ.pop(key, None)
os.environ.update(APP_DATA_DIR=str(output/'app-data'), INPOST_TRACKING_WORKER='0',
    INPOST_PICKUP_WORKER='0', INPOST_AUTO_PICKUP='0', KSEF_SCHEDULER_WORKER='0',
    EMAIL_ENABLED='0', AUDIT_OUTBOX_WORKER='0', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
    PYTHONDONTWRITEBYTECODE='1', FLASK_SECRET_KEY='synthetic-only')
sys.dont_write_bytecode = True
sys.path.insert(0, str(source))
if args.deps:
    sys.path.insert(1, str(Path(args.deps).resolve()))
def blocked(*args, **kwargs):
    raise AssertionError('NETWORK_BLOCKED_IN_TESTS')
socket.create_connection = socket.socket.connect = socket.socket.connect_ex = blocked
urllib.request.urlopen = blocked
os.chdir(source)
import pytest
print('Synthetic output:', output)
raise SystemExit(pytest.main(['-q', '--tb=short', '-p', 'no:cacheprovider',
    '--basetemp='+str(output/'tmp'), '--junitxml='+str(output/'results.xml'),
    'test_inpost_additional_review.py', 'test_inpost_completion.py',
    'test_inpost_tracking47.py', 'test_inpost_module.py', 'test_agent_shipping_draft47.py',
    'test_fulfillment_orchestrator.py', 'test_shipping_does_not_issue_stock.py',
    'test_remanent_integration.py', 'test_invoice_payment_sync.py']))
