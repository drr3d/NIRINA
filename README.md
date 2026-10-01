# nigate

A small, self-hosted AI gateway for OpenAI-compatible clients, written in Rust.

Put nigate between your application and your LLM providers. Your application only changes its base URL and uses a **virtual key** instead of a provider key. In return you get per-client keys, rate limits, provider failover, a two-way secret filter, usage statistics, an admin API and a dashboard. Any client that can call `POST /v1/chat/completions` can use it.

> **Status:** version 0.1.0, built and run from source. Chat completions only, no streaming, one instance. See [Good to know](#good-to-know-before-you-rely-on-it) before you rely on it.

## What you get

| You want to... | nigate gives you |
|---|---|
| Give each client or app its own credential | **Virtual keys** (`ngk_...`). Revocable, individually limited. Provider keys never leave the gateway's environment |
| Stop one client from burning the whole quota | **Rate limits** per key: requests per minute (RPM) and tokens per minute (TPM) |
| Survive a provider outage or rate limit | **Failover**: an alias maps to an ordered list of providers, with retries, cooldown and a total time budget |
| Keep credentials out of prompts and answers | **Secret filter** in both directions: redact, block or just log. 15 built-in rules plus an entropy detector |
| See who uses what | **Statistics** (metadata only, never prompts or answers), an **admin API** and a **Streamlit dashboard** |
| Change models without touching clients | **Aliases**: clients send an alias in `model`, nigate maps it to real models |

## How it works

```
 your app                      nigate (one process)                          providers
 --------                      ----------------------------------            ---------
 POST /v1/chat/completions --> auth -> guardrail -> rate limit -> failover --> provider A
 Authorization: Bearer ngk_..                                              --> provider B (backup)
        <--------------------- guardrail on the answer <-------------------

 operators / dashboard -------> admin API (separate port, separate token)
```

One binary, one TOML config file, two SQLite files (key store and statistics). No external database.

## Quick start

You need a Rust toolchain (stable, edition 2024) and a C compiler (for the bundled SQLite). Run these from the folder that contains `Cargo.toml`.

```bash
# 1. Build
cargo build --release --locked          # binary: target/release/nigate  (nigate.exe on Windows)

# 2. Configure: copy the template, then edit the [[model]] blocks to point at your provider(s)
cp nigate.example.toml nigate.toml

# 3. Create an admin token and export the secrets the gateway reads from its environment
./target/release/nigate admin token                     # prints a random token
export NIGATE_ADMIN_TOKEN="<token printed above>"
export CEREBRAS_API_KEY="<your provider key>"           # the variable named by api_key_env in nigate.toml

# 4. Start the gateway
./target/release/nigate -c nigate.toml

# 5. In another terminal: create a virtual key for your app (shown once) and try it
./target/release/nigate -c nigate.toml key create my-app
export NIGATE_KEY="ngk_..."
curl http://127.0.0.1:4000/healthz
curl -H "Authorization: Bearer $NIGATE_KEY" http://127.0.0.1:4000/v1/models
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $NIGATE_KEY" -H "Content-Type: application/json" \
  -d '{"model":"chat-main","messages":[{"role":"user","content":"Say hello"}]}'
```

On PowerShell use `$env:NIGATE_ADMIN_TOKEN = "<token>"` and `.\target\release\nigate.exe -c nigate.toml`.

`chat-main` is an alias defined in `nigate.example.toml`. Use an alias from your own `[[model]]` blocks. Startup logs go to stderr and are in Indonesian (see the [Language note](#language-note)).

## Connect a client

To a client, nigate is one more OpenAI-compatible endpoint. Nothing nigate-specific is installed on the client.

| Client setting | Value |
|---|---|
| Base URL | `http://<gateway-host>:4000/v1` (must end in `/v1`) |
| API key | the `ngk_...` virtual key. Never the provider's own key |
| `model` | a nigate **alias** from a `[[model]]` block (`GET /v1/models` lists them) |
| `stream` | omit it or `false`. `true` is rejected with `400 stream_unsupported` |
| Timeout | larger than nigate's `total_timeout_secs` (300 by default) |
| Client retries | off (`max_retries=0`): retries and failover are nigate's job |

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:4000/v1", api_key="ngk_...", timeout=330, max_retries=0)
resp = client.chat.completions.create(model="chat-main", messages=[{"role": "user", "content": "Say hello"}])
print(resp.choices[0].message.content)
```

Using NIRINA? Follow [NIRINA integration](docs/NIRINA_INTEGRATION.md), which walks through a real client end to end.

## Dashboard

`ui/` is a standalone Streamlit app that only talks to the admin API (it never reads the databases).

```bash
pip install -r ui/requirements.txt
export NIGATE_ADMIN_TOKEN="<the same token the gateway got>"
cd ui && python -m streamlit run app.py --server.port 8502 --server.address 127.0.0.1
# open http://127.0.0.1:8502
```

Tabs (labels are Indonesian): *Ringkasan* (summary), *Key & Limit*, *Upstream*, *Guardrail*, *Konfigurasi*. On Windows, `ui\jalankan.cmd` does the same.

## Documentation

| I want to... | Read |
|---|---|
| Understand every setting, with defaults and ranges | [Reference: Configuration](docs/REFERENCE.md#configuration) |
| Know exactly what happens to a request, and every error code | [Reference: Architecture and request path](docs/REFERENCE.md#architecture-and-request-path) |
| Manage keys and limits | [Reference: Virtual keys and rate limits](docs/REFERENCE.md#virtual-keys-and-rate-limits) |
| Tune or test the secret filter | [Reference: Guardrail](docs/REFERENCE.md#guardrail) |
| Understand retries, failover and cooldown | [Reference: Failover and health](docs/REFERENCE.md#failover-and-health) |
| Script the gateway | [Reference: Admin API](docs/REFERENCE.md#admin-api) |
| Run it for real: reload, backups, upgrades, systemd | [Reference: Operations](docs/REFERENCE.md#operations) |
| Review the security model | [Reference: Security notes](docs/REFERENCE.md#security-notes) |
| Fix a problem | [Reference: Troubleshooting](docs/REFERENCE.md#troubleshooting) |
| Connect NIRINA | [NIRINA integration](docs/NIRINA_INTEGRATION.md) |

## Good to know before you rely on it

- **Chat completions only, no streaming.** There is no embeddings, completions, Responses or audio endpoint.
- **One instance.** Rate-limit buckets and upstream health live in memory and reset on restart.
- **No fail-open.** If nigate is down, its clients fail. A client that adds a direct-to-provider fallback must trigger it only when *no HTTP response at all* came back, never on a nigate rejection (`403 guardrail_blocked`, `429`, `502`), or the fallback defeats the guardrail and the key limits.
- **The secret filter is a heuristic**, not a data-loss-prevention product. It targets credentials, not personal data.
- **Plain HTTP.** The data and admin listeners have no TLS. Keep them on loopback, or put a TLS-terminating proxy in front.
- **The admin API is off until you export a token**, and the dashboard has no login of its own. Keep both on loopback.

The full list is in [Reference: Known limitations](docs/REFERENCE.md#known-limitations).

## Development

```bash
cargo test                                      # integration tests in tests/
cargo clippy --all-targets -- -D warnings       # lint (warnings are errors)
cargo fmt                                       # rustfmt.toml: max_width = 140
python -m unittest discover -s ui -p "tes_*.py" -v    # dashboard tests (needs Streamlit)
```

Source layout, test design and the benchmark command are in [Reference: Development and tests](docs/REFERENCE.md#development-and-tests).

## Language note

The project grew in Indonesian. Identifiers, log lines, CLI output, error messages, the dashboard and several admin-API JSON field names (`jam`, `hari`, `temuan`, `hasil`, `kelompok`, ...) are in Indonesian. The [Glossary](docs/REFERENCE.md#glossary) translates all of them.

## License

MIT. See the [LICENSE](LICENSE) file.
