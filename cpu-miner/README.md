<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:ddf8cbd23b4c18d9fe94d9ddc0ae64ce7d989786fe2a32bc347e6688d546dc13 -->

# Ore: local CPU mining dashboard

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:c2b515987794bd1faa4eb963d0d2f140c4f84e6046202c9f4dfae70916be5f00 -->

Ore controls one local miner at a time: [XMRig](https://github.com/xmrig/xmrig) for Monero, [cpuminer-opt](https://github.com/JayDDee/cpuminer-opt) for Bitcoin pool mining, or the [XDS full node](https://github.com/sonofsatoshi2020/xds) for XDS solo mining. The CPU belongs to the machine running the dashboard. Linux and macOS are supported, subject to each miner’s platform requirements.

<!-- /coderabbit-ai-generated:v1 -->

## Start the dashboard

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:b89f577b831e3e3597a42ee5e106947be0cf99f1c18ca9e834fb4b108c711bfb -->

Install Python 3, then run `python3 cpu-miner/app.py` from the repository root. Open `http://127.0.0.1:8765/` on the same machine. Read the access code from the private file whose path appears in the terminal, and enter it to unlock the dashboard. The code changes each time the dashboard starts. Pass `--port 8765` to select another port. Choose the coin and configure it as described below. Select **Start mining**; **Stop** or Ctrl+C stops the miner. Mining never starts automatically.

<!-- /coderabbit-ai-generated:v1 -->

### Monero

Install XMRig from its [official releases](https://github.com/xmrig/xmrig/releases) or build it using its official instructions. Enter the pool host and port, Monero wallet address, XMRig executable path, thread count, and TLS setting required by the pool. Worker name and pool password are optional. XMRig's default developer donation is 1%; the dashboard does not change it.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:a307c0afe92bb2724002eadcd23abe97ca8651b33fe353df434630a0395beabf -->

### Bitcoin

Install [cpuminer-opt](https://github.com/JayDDee/cpuminer-opt) and set its executable path. Enter the pool's full worker login, password if required, and CPU thread count. EMCD endpoints are prefilled; edit the list for your pool, with one `stratum+tcp://` or `stratum+ssl://` URL per line. On repeated connection failure, the dashboard tries each address in order, then stops with an error. A rejected login stops immediately. Hashrate and share counts reset when the miner switches pools. Bitcoin mining on a CPU is much slower than on an ASIC.

### XDS

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:6ecf586e9a7937c1ae44390f1b24b4cb6bcfa971c2e1433d6d11f54c0c30c559 -->

Build or install the published `Daemon` executable from the [XDS project](https://github.com/sonofsatoshi2020/xds) and its required .NET runtime. The upstream source targets unsupported .NET Core 2.2, and its dependency restore reports known vulnerabilities, including in LiteDB 4.1.4. Enter the executable path and an XDS bech32 reward address (`xds1...`). The dashboard starts the node with proof-of-work mining enabled, staking and RPC disabled, and its API bound to loopback. It uses the node's normal data directory and configuration; it may need to download and sync the chain. Stop any other XDS node using the same data directory before starting.

<!-- /coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:50942d7b11105d55516dfe9ad42ded41467690db43eb9bec7e1ecf546aa29cdd -->

For example, from the XDS source directory, build a self-contained Linux x64 node with `dotnet publish src/daemon/Daemon.csproj -c Release -r linux-x64 --self-contained true`. On Intel macOS, use `osx-x64` instead of `linux-x64`. Point the dashboard at the `Daemon` executable in the publish directory. The node still requires compatible native libraries for its .NET Core 2.2 runtime.

The linked XDS source has no built-in DNS seeds or seed nodes.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:a5e381a2b29e241014283af21c938678263238e3d5973eed41a7984dbb84d0b3 -->

<!-- /coderabbit-ai-generated:v1 -->

Supply reachable peer IP addresses in the dashboard or configure them in the node's config; there is no guarantee that peers or an active network are available. Peer entries use comma-separated IP addresses, optionally with ports (default 38333). The node mines solo rather than through a pool; its API does not expose a mining hashrate, pool shares, or a thread limit. The dashboard shows chain height, connected peers, node state, uptime, and node output instead. A running node may still be syncing or waiting for peers.

<!-- /coderabbit-ai-generated:v1 -->

## Settings and security

Settings, including any pool password, are stored as plaintext with owner-only permissions in `$XDG_CONFIG_HOME/cpu-miner/settings.json` or `~/.config/cpu-miner/settings.json`. The dashboard listens only on loopback. Its temporary XMRig configuration and authenticated status API are local. <!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:ef2d2409ea1505774b8e699dbc0f76ddfabad286cc9fc6acb3d77bbb7d30c44e -->

The cpuminer-opt API is unauthenticated, bound to loopback, and started without remote controls; its temporary configuration has owner-only permissions. The XDS node API is unauthenticated and bound to loopback; the dashboard checks that the response belongs to its child node.

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:e73472aafe92a7909eaf2d12c4cc47c6d8a98b9c2100caf4018cfd39e23ecbbd -->

<!-- /coderabbit-ai-generated:v1 -->

Normal dashboard shutdown stops the child; abrupt termination may require stopping it separately.

<!-- /coderabbit-ai-generated:v1 -->

## Test

Run `python3 -W error::ResourceWarning -m unittest discover -s cpu-miner/tests -v` from the repository root. Tests use simulated miners and do not connect to a pool or network.


<!-- /coderabbit-ai-generated:v1 -->