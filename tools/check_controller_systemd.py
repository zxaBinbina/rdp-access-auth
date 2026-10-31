"""Test installed socket activation only on a disposable GitHub Actions runner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import atexit

if os.environ.get('GITHUB_ACTIONS') != 'true' or os.geteuid() == 0:
    raise SystemExit('Only run as an ordinary user on a disposable GitHub Actions runner')
if Path('/etc/rdp-access-auth-control/authority.json').exists():
    raise SystemExit('Refusing to overwrite an initialized installation')
atexit.register(lambda: subprocess.run(['sudo', 'journalctl', '-u', 'rdp-access-auth-control.service', '-n', '30', '--no-pager']))
sys.path.insert(0, '/usr/lib/rdp-access-auth')
import package_bootstrap
from desktop.control import Client
from management import ManagementError

password = 'Disposable-CI-management-123!'
subprocess.run(['sudo', '/usr/bin/rdp-auth', 'desktop-bootstrap'], input=json.dumps({'password': password}), text=True, check=True)
client = Client()
assert client.request('status')['initialized']
try:
    client.request('preferences.save', {'changes': {'show_tray': False}})
    raise AssertionError('locked write accepted')
except ManagementError as error:
    assert error.status == 401
session = client.request('security.login', {'password': password})['session']
client.request('preferences.save', {'changes': {'theme': 'dark', 'show_tray': False}}, session=session)
params = {'name': 'CI Agent'}
confirmation = client.request('security.confirm', {'password': password, 'method': 'agents.register', 'params': params}, session=session)['confirmation']
identity = client.request('agents.register', params, session=session, confirmation=confirmation)
request = client.request('agents.request', identity=identity)
client.request('agents.approve', {'request_id': request['request_id'], 'allow': True}, session=session)
client.request('preferences.save', {'changes': {'palette': 'hutao-red'}}, identity=identity)
subprocess.run(['sudo', 'systemctl', 'restart', 'rdp-access-auth-control.service'], check=True)
assert client.request('status')['preferences']['palette'] == 'hutao-red'
try:
    client.request('preferences.save', {'changes': {'theme': 'light'}}, session=session)
    raise AssertionError('old window session survived controller restart')
except ManagementError as error:
    assert error.status == 401
client.request('preferences.save', {'changes': {'theme': 'light'}}, identity=identity)
assert client.request('status')['preferences']['theme'] == 'light'
print('Installed systemd socket activation, first initialization, locked writes, Agent grant persistence and session reset passed.')
