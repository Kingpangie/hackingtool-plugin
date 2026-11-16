#!/usr/bin/env python3
"""Local dashboard and process manager for installed CPU miners."""
import argparse
import collections
import hmac
import json
import ipaddress
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'cpu-miner'
SETTINGS_PATH = CONFIG_DIR / 'settings.json'
CPU_COUNT = os.cpu_count() or 1
DEFAULTS = {'pool': '', 'wallet': '', 'password': 'x', 'worker': '', 'tls': True,
            'threads': max(1, CPU_COUNT // 2), 'xmrig': 'xmrig',
            'coin': 'monero', 'xds_address': '', 'xds_node': 'Daemon', 'xds_peers': '',
            'btc_miner': 'cpuminer', 'btc_user': '', 'btc_password': 'x',
            'btc_pools': 'stratum+tcp://us.emcd.network:3333\nstratum+tcp://eu.emcd.network:3333\nstratum+tcp://gate.emcd.network:3333'}


def validate(data, require_ready=False):
    if not isinstance(data, dict):
        raise ValueError('Settings must be an object.')
    result = {}
    for key, limit in [('pool', 300), ('wallet', 256), ('password', 256), ('worker', 128), ('xmrig', 1024),
                       ('xds_address', 128), ('xds_node', 1024), ('xds_peers', 2000),
                       ('btc_miner', 1024), ('btc_user', 256), ('btc_password', 256), ('btc_pools', 2000)]:
        value = data.get(key, DEFAULTS[key])
        if not isinstance(value, str) or len(value) > limit or any((ord(c) < 32 and not (key == 'btc_pools' and c in '\r\n')) or ord(c) == 127 for c in value):
            raise ValueError(f'Invalid {key}.')
        result[key] = value.strip()
    coin = data.get('coin', DEFAULTS['coin'])
    if coin not in ('monero', 'xds', 'bitcoin'):
        raise ValueError('Choose Monero, XDS, or Bitcoin.')
    result['coin'] = coin
    if coin == 'monero' and not result['xmrig']:
        raise ValueError('Enter the path to XMRig.')
    if coin == 'bitcoin' and not result['btc_miner']:
        raise ValueError('Enter the path to cpuminer-opt.')
    if coin == 'xds' and not result['xds_node']:
        raise ValueError('Enter the path to the XDS node executable.')
    if result['xds_address'] and (not result['xds_address'].lower().startswith('xds1') or any(c.isspace() for c in result['xds_address'])):
        raise ValueError('Enter an XDS bech32 address beginning with xds1.')
    for peer in filter(None, (p.strip() for p in result['xds_peers'].split(','))):
        try:
            # XDS accepts IP addresses and optional ports. IPv6 with a port uses brackets.
            if peer.startswith('['):
                end = peer.index(']')
                ipaddress.ip_address(peer[1:end])
                suffix = peer[end + 1:]
                if suffix and (not suffix.startswith(':') or not suffix[1:].isdigit() or not 1 <= int(suffix[1:]) <= 65535):
                    raise ValueError()
            else:
                try:
                    ipaddress.ip_address(peer)
                except ValueError:
                    host, port = peer.rsplit(':', 1)
                    if ipaddress.ip_address(host).version != 4 or not port.isdigit() or not 1 <= int(port) <= 65535:
                        raise ValueError()
        except (ValueError, IndexError):
            raise ValueError('XDS peers must be comma-separated IP addresses with optional ports.') from None
    if not isinstance(data.get('tls', DEFAULTS['tls']), bool):
        raise ValueError('TLS must be enabled or disabled.')
    result['tls'] = data.get('tls', DEFAULTS['tls'])
    threads = data.get('threads', DEFAULTS['threads'])
    if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= CPU_COUNT:
        raise ValueError(f'Threads must be between 1 and {CPU_COUNT}.')
    result['threads'] = threads
    if result['pool']:
        try:
            parsed = urlsplit('//' + result['pool'])
            if not parsed.hostname or not parsed.port or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or any(c.isspace() for c in result['pool']):
                raise ValueError()
        except ValueError:
            raise ValueError('Pool must be a hostname and port, such as pool.example.com:3333.') from None
    if require_ready and coin == 'monero' and (not result['pool'] or not result['wallet']):
        raise ValueError('Enter a pool address and wallet before starting.')
    if result['btc_pools']:
        pools = result['btc_pools'].splitlines()
        if len(pools) > 10:
            raise ValueError('Use at most 10 Bitcoin pool addresses.')
        for pool in pools:
            try:
                parsed = urlsplit(pool.strip())
                if (parsed.scheme not in ('stratum+tcp', 'stratum+ssl') or not parsed.hostname or not parsed.port
                    or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                    or any(c.isspace() for c in pool.strip())):
                    raise ValueError()
            except ValueError:
                raise ValueError('Bitcoin pools must use stratum+tcp:// or stratum+ssl:// with a host and port.') from None
    if result['btc_user'] and not re.fullmatch(r'[A-Za-z0-9._-]+', result['btc_user']):
        raise ValueError('Bitcoin worker login may contain letters, numbers, dots, underscores, and hyphens.')
    if any(c in result['btc_password'] for c in ('"', '\\')):
        raise ValueError('Bitcoin pool password cannot contain quotes or backslashes.')
    if require_ready and coin == 'bitcoin' and (not result['btc_user'] or not result['btc_pools']):
        raise ValueError('Enter a Bitcoin pool address and worker login.')
    if require_ready and coin == 'xds' and not result['xds_address']:
        raise ValueError('Enter an XDS reward address before starting.')
    return result


def load_settings():
    try:
        return validate(json.loads(SETTINGS_PATH.read_text()))
    except (OSError, ValueError, json.JSONDecodeError):
        return DEFAULTS.copy()


def save_settings(settings):
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    fd, path = tempfile.mkstemp(prefix='.settings-', dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(settings, stream)
        os.replace(path, SETTINGS_PATH)
    finally:
        if os.path.exists(path):
            os.unlink(path)


class Miner:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.logs = collections.deque(maxlen=100)
        self.error = None
        self.started = None
        self.api_port = None
        self.token = None
        self.temp = None
        self.stopping = False
        self.coin = None
        self.btc_commands = []
        self.btc_index = 0
        self.btc_connected = False

    def start(self, settings):
        settings = validate(settings, require_ready=True)
        with self.lock:
            if self.process is not None:
                raise ValueError('The miner is already running.')
            coin = settings['coin']
            label = {'monero': 'XMRig', 'xds': 'XDS node', 'bitcoin': 'cpuminer-opt'}[coin]
            executable = shutil.which(os.path.expanduser(settings[{'monero': 'xmrig', 'xds': 'xds_node', 'bitcoin': 'btc_miner'}[coin]]))
            if not executable:
                raise ValueError(f'{label} executable was not found. Install it or enter its path.')
            # Reserve an available loopback port; XMRig must bind it after the socket is closed.
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            token = secrets.token_urlsafe(32) if coin == 'monero' else None
            temp = tempfile.TemporaryDirectory(prefix='cpu-miner-')
            if coin == 'monero':
                config = {'autosave': False, 'background': False, 'colors': False,
                          'http': {'enabled': True, 'host': '127.0.0.1', 'port': port,
                                   'access-token': token, 'restricted': True},
                          'cpu': {'enabled': True}, 'opencl': {'enabled': False}, 'cuda': {'enabled': False},
                          'randomx': {'wrmsr': False},
                          'pools': [{'url': settings['pool'], 'user': settings['wallet'],
                                     'pass': settings['password'] or 'x', 'rig-id': settings['worker'] or None,
                                     'coin': 'monero', 'tls': settings['tls'], 'enabled': True}]}
                config_path = Path(temp.name) / 'xmrig.json'
                config_path.write_text(json.dumps(config))
                os.chmod(config_path, 0o600)
                command = [executable, '--config', str(config_path), '--threads', str(settings['threads'])]
            elif coin == 'bitcoin':
                commands = []
                for index, pool in enumerate(settings['btc_pools'].splitlines()):
                    config = {'algo': 'sha256d', 'url': pool.strip(), 'user': settings['btc_user'],
                              'pass': settings['btc_password'] or 'x', 'threads': settings['threads'],
                              'api-bind': f'127.0.0.1:{port}', 'retries': 2, 'retry-pause': 5,
                              'no-color': True, 'no-redirect': True}
                    config_path = Path(temp.name) / f'bitcoin-{index}.json'
                    config_path.write_text(json.dumps(config))
                    os.chmod(config_path, 0o600)
                    commands.append([executable, '--config', str(config_path)])
                command = commands[0]
            else:
                command = [executable, '-mine=1', '-mineaddress=' + settings['xds_address'],
                           '-stake=0', '-server=0', '-apiuri=http://127.0.0.1',
                           '-apiport=' + str(port), '-usehttps=0']
                command += ['-addnode=' + peer.strip() for peer in settings['xds_peers'].split(',') if peer.strip()]
            try:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, errors='replace', bufsize=1)
            except OSError as exc:
                temp.cleanup()
                raise ValueError(f'Unable to start {label}: {exc}') from exc
            self.logs.clear()
            self.error = None
            self.started = time.monotonic()
            self.api_port, self.token, self.temp, self.process, self.coin = port, token, temp, process, coin
            self.btc_commands = commands if coin == 'bitcoin' else []
            self.btc_index = 0
            self.btc_connected = False
            self.stopping = False
            threading.Thread(target=self._collect, args=(process, temp), daemon=True).start()

    def _collect(self, process, temp):
        network_failure = False
        authentication_failure = False
        for line in process.stdout:
            line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line.rstrip())[:2000]
            with self.lock:
                self.logs.append(line)
                if self.coin == 'bitcoin':
                    if 'Stratum connection established' in line:
                        self.btc_connected = True
                    elif any(message in line for message in ('Stratum connection failed', 'Stratum connection reset', 'Stratum connection timeout', 'Stratum authentication failed')):
                        self.btc_connected = False
                    if 'workio thread dead, exiting' in line:
                        network_failure = True
                    if 'Stratum authentication failed' in line:
                        authentication_failure = True
        process.stdout.close()
        code = process.wait()
        with self.lock:
            if self.process is process:
                if (self.coin == 'bitcoin' and not self.stopping and network_failure and not authentication_failure
                    and self.btc_index + 1 < len(self.btc_commands)):
                    self.btc_index += 1
                    self.btc_connected = False
                    self.logs.append(f'Trying Bitcoin pool {self.btc_index + 1} of {len(self.btc_commands)}.')
                    try:
                        replacement = subprocess.Popen(self.btc_commands[self.btc_index], stdout=subprocess.PIPE,
                                                       stderr=subprocess.STDOUT, text=True, errors='replace', bufsize=1)
                        self.process = replacement
                        threading.Thread(target=self._collect, args=(replacement, temp), daemon=True).start()
                        return
                    except OSError as exc:
                        self.error = f'Unable to start backup miner: {exc}'
                elif not self.stopping:
                    label = {'monero': 'XMRig', 'xds': 'XDS node', 'bitcoin': 'cpuminer-opt'}[self.coin]
                    if self.coin == 'bitcoin' and authentication_failure:
                        self.error = 'Pool rejected the worker login. Check your account and password.'
                    elif self.coin == 'bitcoin' and network_failure:
                        self.error = 'Bitcoin pool connection failed. Check the pool addresses and miner output.'
                    else:
                        self.error = f'{label} exited unexpectedly (exit code {code}).'
                self.process = None
                self.temp = None
                self.btc_connected = False
        temp.cleanup()

    def stop(self):
        with self.lock:
            process = self.process
            if not process:
                return
            self.stopping = True
            if process.poll() is not None:
                return
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def status(self):
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            port, token, coin, process = self.api_port, self.token, self.coin, self.process
            connected = self.btc_connected
            result = {'running': running, 'coin': coin, 'error': self.error, 'logs': list(self.logs),
                      'uptime': int(time.monotonic() - self.started) if running else 0,
                      'stats': None, 'api_error': None}
        if running:
            try:
                if coin == 'monero':
                    req = urllib.request.Request(f'http://127.0.0.1:{port}/2/summary', headers={'Authorization': f'Bearer {token}'})
                    with urllib.request.urlopen(req, timeout=1) as response:
                        data = json.load(response)
                    result['stats'] = {'hashrate': data.get('hashrate', {}).get('total', [None])[0],
                                       'connection': data.get('connection', {}), 'results': data.get('results', {})}
                elif coin == 'bitcoin':
                    with socket.create_connection(('127.0.0.1', port), timeout=1) as sock:
                        sock.sendall(b'summary')
                        chunks = bytearray()
                        while len(chunks) < 8192:
                            chunk = sock.recv(8192 - len(chunks))
                            if not chunk:
                                break
                            chunks.extend(chunk)
                            if b'\x00' in chunk:
                                break
                        response = chunks.decode('utf-8', errors='replace').strip('\x00|')
                    data = dict(item.split('=', 1) for item in response.split(';') if '=' in item)
                    if data.get('ALGO', '').lower() != 'sha256d':
                        raise ValueError('Unexpected miner API response')
                    accepted, rejected = int(data['ACC']), int(data['REJ'])
                    result['stats'] = {'hashrate': float(data['HS']),
                                       'connection': {'pool': data.get('URL'), 'uptime': int(connected)},
                                       'results': {'shares_good': accepted, 'shares_total': accepted + rejected}}
                else:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/node/status', timeout=1) as response:
                        data = json.load(response)
                    if data.get('processId') != process.pid or data.get('coinTicker') != 'XDS':
                        raise ValueError('Unexpected node API response')
                    result['stats'] = {'height': data.get('consensusHeight'),
                                       'peers': len(data.get('inboundPeers', [])) + len(data.get('outboundPeers', [])),
                                       'node_state': data.get('state')}
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                result['api_error'] = {'monero': 'Waiting for the XMRig status API.', 'xds': 'Waiting for the XDS node status API.', 'bitcoin': 'Waiting for the cpuminer-opt status API.'}[coin]
        return result


miner = Miner()


class Handler(BaseHTTPRequestHandler):
    def allowed_host(self):
        host = self.headers.get('Host', '')
        return host in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

    def authenticated(self):
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get('Cookie', ''))
            session = cookies.get('ore_session')
            return bool(session and hmac.compare_digest(session.value, self.server.session_token))
        except (CookieError, TypeError):
            return False

    def respond(self, status, payload, content_type='application/json; charset=utf-8', cookie=None):
        body = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else payload
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed_host():
            return self.respond(403, {'error': 'Invalid host.'})
        if self.path.startswith('/api/') and not self.authenticated():
            return self.respond(403, {'error': 'Dashboard authentication required.'})
        if self.path == '/api/settings':
            return self.respond(200, {'settings': load_settings(), 'cpu_count': CPU_COUNT})
        if self.path == '/api/status':
            return self.respond(200, miner.status())
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if self.path not in files:
            return self.respond(404, {'error': 'Not found.'})
        name, mime = files[self.path]
        return self.respond(200, (ROOT / 'static' / name).read_bytes(), mime)

    def do_POST(self):
        origin = self.headers.get('Origin', '')
        if not self.allowed_host() or origin != 'http://' + self.headers.get('Host', '') or self.headers.get('X-CPU-Miner') != '1':
            return self.respond(403, {'error': 'Request origin rejected.'})
        if self.path != '/api/session' and not self.authenticated():
            return self.respond(403, {'error': 'Dashboard authentication required.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192 or self.headers.get_content_type() != 'application/json':
                raise ValueError('Invalid request.')
            data = json.loads(self.rfile.read(length))
            if self.path == '/api/session':
                code = data.get('code') if isinstance(data, dict) else None
                if not isinstance(code, str) or not hmac.compare_digest(code.encode('utf-8'), self.server.access_code.encode('utf-8')):
                    return self.respond(403, {'error': 'Invalid access code.'})
                cookie = f'ore_session={self.server.session_token}; HttpOnly; SameSite=Strict; Path=/'
                return self.respond(200, {'ok': True}, cookie=cookie)
            if self.path == '/api/settings':
                save_settings(validate(data))
            elif self.path == '/api/start':
                settings = validate(data, require_ready=True)
                with miner.lock:
                    if miner.process is not None:
                        raise ValueError('The miner is already running.')
                    save_settings(settings)
                    miner.start(settings)
            elif self.path == '/api/stop':
                miner.stop()
            else:
                return self.respond(404, {'error': 'Not found.'})
            self.respond(200, {'ok': True})
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            self.respond(400, {'error': str(exc)})

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description='Run the local CPU miner dashboard')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.access_code = secrets.token_urlsafe(32)
    server.session_token = secrets.token_urlsafe(32)
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    access_path = CONFIG_DIR / f'access-{server.server_port}.txt'
    fd, tmp = tempfile.mkstemp(prefix='.access-', dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(server.access_code)
        os.replace(tmp, access_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f'Dashboard: http://127.0.0.1:{server.server_port}', flush=True)
    print(f'Access code file: {access_path}', flush=True)
    try:
        server.serve_forever()
    finally:
        miner.stop()
        server.server_close()
        try:
            access_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == '__main__':
    main()
