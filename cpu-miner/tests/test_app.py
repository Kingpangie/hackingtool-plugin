import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('miner_app', ROOT / 'app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

FAKE = '''#!/usr/bin/env python3
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
with open(sys.argv[sys.argv.index('--config') + 1]) as f:
    config = json.load(f)
assert config['pools'][0]['coin'] == 'monero'
assert config['opencl']['enabled'] is False
assert config['cuda']['enabled'] is False
assert int(sys.argv[sys.argv.index('--threads') + 1]) > 0
print('Fake miner started', flush=True)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer ' + config['http']['access-token']:
            self.send_error(401); return
        body = json.dumps({'hashrate': {'total': [42.5, 40, 38]}, 'connection': {'pool': 'test:3333', 'uptime': 10}, 'results': {'shares_good': 3, 'shares_total': 4}}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', config['http']['port']), Handler).serve_forever()
'''


FAKE_XDS = """#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
args = sys.argv[1:]
assert '-mine=1' in args
assert '-mineaddress=xds1testaddress' in args
assert '-stake=0' in args and '-server=0' in args
assert '-apiuri=http://127.0.0.1' in args and '-usehttps=0' in args
assert '-addnode=192.0.2.1:38333' in args
port = int(next(arg.split('=', 1)[1] for arg in args if arg.startswith('-apiport=')))
print('\\x1b[32mMining enabled.\\x1b[0m', flush=True)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/api/node/status': self.send_error(404); return
        body = json.dumps({'coinTicker':'XDS', 'processId':os.getpid(), 'consensusHeight':42,
                           'inboundPeers':[{}], 'outboundPeers':[{}, {}], 'state':'Started'}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', port), Handler).serve_forever()
"""


FAKE_BITCOIN = """#!/usr/bin/env python3
import json, socket, stat, sys
from pathlib import Path
config_path = Path(sys.argv[sys.argv.index('--config') + 1])
assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
config = json.loads(config_path.read_text())
assert config['algo'] == 'sha256d'
assert config['user'] == 'account.worker' and config['pass'] == 'x'
assert config['threads'] == 1 and config['no-redirect'] is True
if 'fail.test' in config['url']:
    print('Stratum connection failed: simulated', flush=True)
    print('workio thread dead, exiting.', flush=True)
    sys.exit(0)
if 'auth.test' in config['url']:
    print('Stratum authentication failed', flush=True)
    print('workio thread dead, exiting.', flush=True)
    sys.exit(0)
port = int(config['api-bind'].split(':')[1])
print('Stratum connection established', flush=True)
with socket.socket() as s:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('127.0.0.1', port))
    s.listen()
    while True:
        conn, _ = s.accept()
        with conn:
            assert conn.recv(100) == b'summary'
            data = ('NAME=cpuminer-opt;ALGO=sha256d;URL=' + config['url'] + ';HS=123.5;ACC=2;REJ=1;|').encode() + bytes([0])
            conn.sendall(data)
"""


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        app.CONFIG_DIR = self.base / 'config'
        app.SETTINGS_PATH = app.CONFIG_DIR / 'settings.json'
        self.fake = self.base / 'fake-miner'
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        app.miner = app.Miner()
        self.settings = {**app.DEFAULTS, 'pool': 'test:3333', 'wallet': 'test-wallet', 'xmrig': str(self.fake), 'threads': 1}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.server.access_code = 'test-access-code'
        self.server.session_token = 'test-session-token'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        app.miner.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, data=None, origin=None, header=True, authenticated=True):
        headers = {'Cookie': 'ore_session=' + self.server.session_token} if authenticated else {}
        if data is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': origin or self.url})
            if header: headers['X-CPU-Miner'] = '1'
            data = json.dumps(data).encode()
        with urllib.request.urlopen(urllib.request.Request(self.url + path, data=data, headers=headers), timeout=3) as response:
            return json.load(response)

    def wait_for(self, predicate):
        for _ in range(60):
            value = self.request('/api/status')
            if predicate(value): return value
            time.sleep(.05)
        self.fail(f'Status did not reach expected state: {value}')

    def test_settings_and_permissions(self):
        self.request('/api/settings', self.settings)
        self.assertEqual(self.request('/api/settings')['settings'], self.settings)
        self.assertEqual(stat.S_IMODE(app.SETTINGS_PATH.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(app.CONFIG_DIR.stat().st_mode), 0o700)
        for invalid in ({**self.settings, 'pool': 'invalid'}, {**self.settings, 'threads': 0}, {**self.settings, 'wallet': 'x\n'}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/start', invalid)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        self.assertFalse(self.request('/api/status')['running'])

    def test_dashboard_authentication(self):
        for path, data in (('/api/settings', None), ('/api/status', None), ('/api/start', self.settings), ('/api/stop', {})):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path, data, authenticated=False)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        self.assertFalse(app.SETTINGS_PATH.exists())
        for invalid_code in ('wrong', '🔑', None):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/session', {'code': invalid_code}, authenticated=False)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/session', {'code': self.server.access_code}, origin='https://attacker.example', authenticated=False)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        req = urllib.request.Request(self.url + '/api/session', data=json.dumps({'code': self.server.access_code}).encode(),
            headers={'Content-Type': 'application/json', 'Origin': self.url, 'X-CPU-Miner': '1'})
        with urllib.request.urlopen(req) as response:
            cookie = response.headers['Set-Cookie']
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=Strict', cookie)
        req = urllib.request.Request(self.url + '/api/settings', headers={'Cookie': cookie.split(';', 1)[0]})
        with urllib.request.urlopen(req) as response:
            self.assertEqual(json.load(response)['settings'], app.DEFAULTS)
        with urllib.request.urlopen(self.url + '/') as response:
            html = response.read().decode()
            self.assertNotIn(self.server.access_code, html)
            self.assertNotIn(self.server.session_token, html)

    def test_origin_and_host_protection(self):
        for origin, header in [('https://attacker.example', True), (self.url, False)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/start', self.settings, origin, header)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        req = urllib.request.Request(self.url + '/api/settings', headers={'Host': 'attacker.example'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        self.assertFalse(app.SETTINGS_PATH.exists())

    def test_start_status_duplicate_and_stop(self):
        self.settings.update(btc_user='legacy"login', btc_password='legacy\\password')
        self.request('/api/start', self.settings)
        status = self.wait_for(lambda value: value['stats'] is not None)
        self.assertEqual(status['stats']['hashrate'], 42.5)
        self.assertEqual(status['stats']['results']['shares_good'], 3)
        self.assertEqual(status['stats']['results']['shares_total'], 4)
        self.assertTrue(any('Fake miner started' in line for line in status['logs']))
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/start', self.settings)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.request('/api/stop', {})
        status = self.wait_for(lambda value: not value['running'])
        self.assertIsNone(status['error'])

    def test_xds_start_status_and_stop(self):
        self.fake.write_text(FAKE_XDS)
        settings = {**self.settings, 'coin': 'xds', 'xds_address': 'xds1testaddress',
                    'xds_node': str(self.fake), 'xds_peers': '192.0.2.1:38333',
                    'btc_user': 'legacy"login', 'btc_password': 'legacy\\password'}
        self.request('/api/start', settings)
        status = self.wait_for(lambda value: value['stats'] is not None)
        self.assertEqual(status['coin'], 'xds')
        self.assertEqual(status['stats'], {'height': 42, 'peers': 3, 'node_state': 'Started'})
        self.assertIn('Mining enabled.', status['logs'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/start', self.settings)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.request('/api/stop', {})
        self.assertFalse(self.wait_for(lambda value: not value['running'])['running'])
        self.assertEqual(self.request('/api/settings')['settings'], settings)

    def test_xds_validation_and_legacy_settings(self):
        app.CONFIG_DIR.mkdir(mode=0o700)
        old_settings = {key: self.settings[key] for key in ('pool', 'wallet', 'password', 'worker', 'tls', 'threads', 'xmrig')}
        app.SETTINGS_PATH.write_text(json.dumps(old_settings))
        self.assertEqual(self.request('/api/settings')['settings']['coin'], 'monero')
        for overrides in ({'xds_address': ''}, {'xds_address': 'not-xds'}, {'xds_peers': 'example.com'},
                          {'xds_peers': '192.0.2.1:99999'}, {'xds_node': ''}):
            settings = {**self.settings, 'coin': 'xds', 'xds_address': 'xds1testaddress', 'xds_node': str(self.fake), **overrides}
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/start', settings)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        self.assertFalse(self.request('/api/status')['running'])

    def test_bitcoin_pool_failover_and_stats(self):
        self.fake.write_text(FAKE_BITCOIN)
        settings = {**self.settings, 'coin': 'bitcoin', 'btc_miner': str(self.fake),
                    'btc_user': 'account.worker', 'btc_pools': 'stratum+tcp://fail.test:3333\nstratum+tcp://ok.test:3333'}
        self.request('/api/start', settings)
        status = self.wait_for(lambda value: value['stats'] is not None)
        self.assertEqual(status['coin'], 'bitcoin')
        self.assertEqual(status['stats']['hashrate'], 123.5)
        self.assertEqual(status['stats']['results'], {'shares_good': 2, 'shares_total': 3})
        self.assertIn('ok.test', status['stats']['connection']['pool'])
        self.assertTrue(any('Trying Bitcoin pool 2' in line for line in status['logs']))
        self.request('/api/stop', {})
        self.assertIsNone(self.wait_for(lambda value: not value['running'])['error'])

    def test_bitcoin_credentials_round_trip(self):
        login = 'account+tag@example.com.worker:/é'
        password = 'p@ssword:secret'
        self.fake.write_text(FAKE_BITCOIN.replace(
            "assert config['user'] == 'account.worker' and config['pass'] == 'x'",
            f"assert config['user'] == {login!r} and config['pass'] == {password!r}"))
        settings = {**self.settings, 'coin': 'bitcoin', 'btc_miner': str(self.fake),
                    'btc_user': login, 'btc_password': password,
                    'btc_pools': 'stratum+tcp://ok.test:3333'}
        self.request('/api/start', settings)
        self.assertEqual(self.wait_for(lambda value: value['stats'] is not None)['stats']['hashrate'], 123.5)
        saved = self.request('/api/settings')['settings']
        self.assertEqual(saved['btc_user'], login)
        self.assertEqual(saved['btc_password'], password)
        self.request('/api/stop', {})
        self.assertFalse(self.wait_for(lambda value: not value['running'])['running'])
        for key in ('btc_user', 'btc_password'):
            for char in ('"', '\\'):
                with self.subTest(field=key, char=char):
                    invalid = {**settings, key: 'a' + char + 'b'}
                    for endpoint in ('/api/settings', '/api/start'):
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            self.request(endpoint, invalid)
                        self.assertEqual(error.exception.code, 400)
                        error.exception.close()
                    self.assertFalse(self.request('/api/status')['running'])
                    self.assertEqual(self.request('/api/settings')['settings'], settings)
        for key in ('btc_user', 'btc_password'):
            for char in ('"', '\\'):
                with self.subTest(legacy_field=key, char=char):
                    legacy = {**settings, key: 'a' + char + 'b'}
                    app.save_settings(legacy)
                    self.assertEqual(self.request('/api/settings')['settings'], legacy)
                    for endpoint in ('/api/settings', '/api/start'):
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            self.request(endpoint, legacy)
                        self.assertEqual(error.exception.code, 400)
                        error.exception.close()
                    self.assertEqual(json.loads(app.SETTINGS_PATH.read_text()), legacy)
                    self.assertFalse(self.request('/api/status')['running'])
        for char in (' ', '\t', '\n', '\x00', '\x7f', '\x85', '\x9f', '\u00a0', '\u2003'):
            for invalid in (char + login, login + char, 'account' + char + 'worker'):
                with self.subTest(login=invalid), self.assertRaises(ValueError):
                    app.validate({**settings, 'btc_user': invalid}, require_ready=True)

    def test_bitcoin_all_pools_fail(self):
        self.fake.write_text(FAKE_BITCOIN)
        settings = {**self.settings, 'coin': 'bitcoin', 'btc_miner': str(self.fake),
                    'btc_user': 'account.worker', 'btc_pools': 'stratum+tcp://fail.test:3333\nstratum+tcp://fail.test:4444'}
        self.request('/api/start', settings)
        status = self.wait_for(lambda value: bool(value['error']))
        self.assertIn('pool connection failed', status['error'])
        self.assertFalse(status['running'])
        self.assertEqual(sum('Trying Bitcoin pool' in line for line in status['logs']), 1)

    def test_bitcoin_auth_failure_and_validation(self):
        self.fake.write_text(FAKE_BITCOIN)
        settings = {**self.settings, 'coin': 'bitcoin', 'btc_miner': str(self.fake),
                    'btc_user': 'account.worker', 'btc_pools': 'stratum+tcp://auth.test:3333\nstratum+tcp://ok.test:3333'}
        self.request('/api/start', settings)
        status = self.wait_for(lambda value: bool(value['error']))
        self.assertIn('rejected the worker login', status['error'])
        self.assertFalse(any('Trying Bitcoin pool 2' in line for line in status['logs']))
        for overrides in ({'btc_user': ''}, {'btc_user': 'invalid user'}, {'btc_pools': 'http://example.com:3333'},
                          {'btc_pools': 'stratum+tcp://user:pass@example.com:3333'}, {'btc_miner': ''}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/start', {**settings, **overrides})
            self.assertEqual(error.exception.code, 400)
            error.exception.close()

    def test_missing_executable_and_unexpected_exit(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/start', {**self.settings, 'xmrig': str(self.base / 'missing')})
        error.exception.close()
        self.fake.write_text('#!/usr/bin/env python3\nimport sys\nprint("broken", flush=True)\nsys.exit(7)\n')
        self.request('/api/start', self.settings)
        status = self.wait_for(lambda value: bool(value['error']))
        self.assertIn('exit code 7', status['error'])
        self.assertIn('broken', status['logs'])


if __name__ == '__main__':
    unittest.main()
