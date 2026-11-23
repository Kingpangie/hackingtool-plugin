'use strict';
const $ = id => document.getElementById(id);
const form = $('settings-form');
let running = false;
let actionPending = false;
let lastError = '';
let noticeTimer;
let lastStatus = {running: false, logs: [], uptime: 0};

function notice(message, success = false) {
  $('notice').textContent = message;
  $('notice').className = success ? 'success' : '';
  $('notice').hidden = false;
  clearTimeout(noticeTimer);
  if (success) noticeTimer = setTimeout(() => $('notice').hidden = true, 4000);
}
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-CPU-Miner': '1'}, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) { const error = new Error(result.error || 'Request failed.'); error.status = response.status; throw error; }
  return result;
}
function values() {
  return {pool: form.elements.pool.value, wallet: form.elements.wallet.value,
    worker: form.elements.worker.value, password: form.elements.password.value,
    xmrig: form.elements.xmrig.value, threads: Number(form.elements.threads.value), tls: form.elements.tls.checked,
    coin: form.elements.coin.value, xds_address: form.elements.xds_address.value,
    xds_node: form.elements.xds_node.value, xds_peers: form.elements.xds_peers.value,
    btc_miner: form.elements.btc_miner.value, btc_user: form.elements.btc_user.value,
    btc_password: form.elements.btc_password.value, btc_pools: form.elements.btc_pools.value};
}
function updateButtons() {
  $('start').disabled = running || actionPending;
  $('stop').disabled = !running || actionPending;
  form.elements.coin.disabled = running || actionPending;
}
function duration(seconds) {
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds / 60) % 60, s = seconds % 60;
  return [h, m, s].map(n => String(n).padStart(2, '0')).join(':');
}
function render(data) {
  lastStatus = data;
  running = data.running;
  const coin = running ? data.coin : form.elements.coin.value;
  const xds = coin === 'xds', bitcoin = coin === 'bitcoin';
  for (const el of document.querySelectorAll('[data-coin]')) el.hidden = !el.dataset.coin.split(' ').includes(coin);
  $('engine-name').textContent = xds ? 'XDS node' : bitcoin ? 'cpuminer-opt' : 'XMRig';
  $('network-label').textContent = xds ? 'XDS NETWORK' : bitcoin ? 'BITCOIN NETWORK' : 'MONERO NETWORK';
  $('coin-mark').textContent = xds ? 'X' : bitcoin ? '₿' : 'ɱ';
  $('config-description').textContent = xds ? 'Configure your local XDS node for solo mining.' : bitcoin ? 'Connect your CPU miner to a Bitcoin pool.' : 'Connect your wallet to a mining pool to get started.';
  $('help-text').textContent = xds ? 'XDS needs a synced node and reachable peers. Mining rewards go to your XDS address.' : bitcoin ? 'Enter your pool worker login. Bitcoin CPU hashrates are very low compared with ASIC hardware.' : "You'll need an XMR wallet and a pool address before getting started. Your pool can provide connection details.";
  updateButtons();
  $('status-label').textContent = running ? (xds ? 'NODE RUNNING' : 'RUNNING') : 'OFFLINE';
  $('status-dot').classList.toggle('online', running);
  $('hero-title').textContent = running ? (xds ? 'Your XDS node is running.' : 'Your CPU is working.') : 'Ready when you are.';
  $('hero-description').textContent = running ? (xds ? 'The node will attempt solo mining when it is synced. Check its output below.' : 'Your miner is running. Follow its progress and pool connection below.') : (xds ? 'Enter your XDS address and node executable to start solo mining.' : bitcoin ? 'Enter your worker login and cpuminer-opt path to start mining Bitcoin.' : 'Set up your pool and wallet below to start mining Monero with your CPU.');
  const stats = data.stats;
  $('metric-one-label').textContent = xds ? 'CHAIN HEIGHT' : 'HASHRATE';
  $('metric-unit').textContent = xds ? '' : 'H/s';
  $('metric-two-label').textContent = xds ? 'CONNECTED PEERS' : 'ACCEPTED SHARES';
  $('metric-three-label').textContent = xds ? 'NODE STATUS' : 'REJECTED SHARES';
  $('metric-two-caption').textContent = xds ? 'Inbound and outbound peers' : 'Shares accepted by pool';
  $('metric-three-caption').textContent = xds ? 'Reported by the XDS node' : 'Shares rejected by pool';
  const rate = xds ? stats?.height : stats?.hashrate;
  $('hashrate').textContent = typeof rate === 'number' ? rate.toLocaleString(undefined, {maximumFractionDigits: 1}) : '—';
  $('hash-caption').textContent = running ? (stats ? (xds ? 'Node consensus height' : 'Live hashrate') : 'Waiting for miner') : 'Waiting for miner';
  document.querySelector('.tiny-dot').classList.toggle('online', running && !!stats);
  const accepted = xds ? stats?.peers : stats?.results?.shares_good;
  const total = stats?.results?.shares_total;
  $('accepted').textContent = typeof accepted === 'number' ? accepted.toLocaleString() : '—';
  $('rejected').textContent = xds ? (stats?.node_state || '—') : (typeof total === 'number' && typeof accepted === 'number' ? (total - accepted).toLocaleString() : '—');
  $('uptime').textContent = duration(data.uptime || 0);
  const connection = stats?.connection;
  $('connection').textContent = xds ? (running ? (stats ? `${stats.peers} connected peer(s); syncing may be required` : data.api_error || 'Waiting for XDS node…') : 'XDS node is offline') : (running ? (connection?.uptime > 0 ? `Connected to ${connection.pool || 'pool'}` : (data.api_error || 'Connecting to pool…')) : 'Not connected to a pool');
  const log = $('logs');
  log.classList.toggle('empty-log', !data.logs?.length);
  if (data.logs?.length) {
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 35;
    log.textContent = data.logs.join('\n');
    if (nearBottom) log.scrollTop = log.scrollHeight;
  } else {
    log.textContent = 'Your miner is quiet for now.\nStart mining to see activity here.';
  }
  if (data.error && data.error !== lastError) notice(data.error);
  lastError = data.error || '';
}
async function refresh() {
  try { render(await api('/api/status')); }
  catch (error) {
    if (error.status === 403) {
      clearInterval(refreshTimer);
      $('dashboard').hidden = true;
      $('access-screen').hidden = false;
      $('access-code').focus();
    } else notice('Could not connect to the local dashboard.');
  }
}
form.elements.coin.addEventListener('change', () => render(lastStatus));
form.elements.threads.addEventListener('input', () => $('thread-count').textContent = form.elements.threads.value);
form.addEventListener('submit', async event => {
  event.preventDefault();
  try { await api('/api/settings', values()); notice('Settings saved on this machine.', true); }
  catch (error) { notice(error.message); }
});
$('start').addEventListener('click', async () => {
  actionPending = true; updateButtons();
  try { await api('/api/start', values()); lastError = ''; $('notice').hidden = true; }
  catch (error) { notice(error.message); }
  finally { actionPending = false; await refresh(); }
});
$('stop').addEventListener('click', async () => {
  actionPending = true; updateButtons();
  try { await api('/api/stop', {}); }
  catch (error) { notice(error.message); }
  finally { actionPending = false; await refresh(); }
});
let refreshTimer;
async function initialize() {
  try {
    const data = await api('/api/settings');
    $('access-screen').hidden = true;
    $('dashboard').hidden = false;
    for (const [key, value] of Object.entries(data.settings)) {
      if (key === 'tls') form.elements[key].checked = value;
      else form.elements[key].value = value;
    }
    form.elements.threads.max = data.cpu_count;
    $('thread-count').textContent = data.settings.threads;
    $('max-threads').textContent = `${data.cpu_count} AVAILABLE`;
    await refresh();
    clearInterval(refreshTimer);
    refreshTimer = setInterval(refresh, 3000);
  } catch (error) {
    if (error.status === 403) {
      $('dashboard').hidden = true;
      $('access-screen').hidden = false;
      $('access-code').focus();
    } else {
      $('access-screen').hidden = false;
      $('access-error').hidden = false;
      $('access-error').textContent = 'Could not load dashboard settings.';
    }
  }
}
$('access-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api('/api/session', {code: $('access-code').value});
    $('access-code').value = '';
    $('access-error').hidden = true;
    await initialize();
  } catch (error) {
    $('access-error').hidden = false;
    $('access-error').textContent = error.message;
  }
});
initialize();
