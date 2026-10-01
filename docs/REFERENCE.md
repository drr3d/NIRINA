# nigate reference

The complete reference for nigate: how a request flows, every configuration option, virtual keys and rate limits, the guardrail, failover, the admin API, the dashboard, operations, security notes, known limitations and troubleshooting.

- New to nigate? Start with the [README](../README.md): what it is and a 5-minute quick start.
- Connecting NIRINA to nigate? See [NIRINA integration](NIRINA_INTEGRATION.md).

Every command in this reference runs from the folder that contains `Cargo.toml` unless stated otherwise. Read [Known limitations](#known-limitations) before putting nigate in front of anything important.

**Language note.** The project grew in Indonesian. Identifiers, log lines, CLI output, error messages, the dashboard and several admin-API JSON field names are in Indonesian. This reference translates what you will meet, and the [Glossary](#glossary) at the end collects the vocabulary.

**Contents**

1. [What nigate is](#what-nigate-is)
2. [Features](#features)
3. [Architecture and request path](#architecture-and-request-path)
4. [Prerequisites](#prerequisites)
5. [Build and run from source](#build-and-run-from-source)
6. [Configuration](#configuration)
7. [Virtual keys and rate limits](#virtual-keys-and-rate-limits)
8. [Using nigate from a client](#using-nigate-from-a-client)
9. [Guardrail](#guardrail)
10. [Failover and health](#failover-and-health)
11. [Admin API](#admin-api)
12. [Dashboard](#dashboard)
13. [Operations](#operations)
14. [Security notes](#security-notes)
15. [Known limitations](#known-limitations)
16. [Development and tests](#development-and-tests)
17. [Troubleshooting](#troubleshooting)
18. [Glossary](#glossary)
19. [License](#license)

---

## What nigate is

nigate is one process that speaks the OpenAI chat-completions protocol on the front and talks to one or more real providers on the back. Your application only changes its `base_url` and uses a **virtual key** (`ngk_...`) instead of a provider key. Provider keys never leave the gateway's environment.

It is deliberately small. It consists of one binary, two SQLite files and one TOML config file. It exposes three data-plane routes: `GET /healthz`, `GET /v1/models` and `POST /v1/chat/completions`.

### When to use it

- You want your applications' LLM calls to go through one controlled door. That gives per-client keys, rate limits, failover across cloud and local models, and usage statistics.
- You send prompts or tool results to a cloud provider and want a safety net that redacts or blocks credentials. It scans prompts on the way out and completions on the way back.
- You want to change providers or fallback order without touching the clients. Clients only know a model alias, and nigate maps the alias to real models.
- You want to keep provider keys out of application environments. Clients hold revocable virtual keys, and the provider keys stay in the gateway's environment.
- Your clients call chat completions without streaming (see [When not to use it](#when-not-to-use-it) if they stream).

### When not to use it

- You need streaming (`stream: true` is rejected with `400 stream_unsupported`).
- You need embeddings, `/v1/completions`, the Responses API, audio or images. Only chat completions are proxied.
- You need high availability across several gateway instances. Limiter and health state live in memory per process.
- You need price or cost accounting. Only token counts are recorded.
- You need PII detection (names, emails, phone numbers). The guardrail targets credentials and secrets only.

---

## Features

- **OpenAI-compatible surface:** `POST /v1/chat/completions` (non-streaming), `GET /v1/models` (lists your aliases) and an open `GET /healthz`. Tool calling (`tools`, `tool_calls`) passes through.
- **Virtual keys:** `ngk_` plus 64 hex characters. Only a SHA-256 hash is stored, and the plaintext is shown once at creation. Keys can be revoked, re-enabled and limited individually.
- **Rate limits:** a per-key token bucket for requests per minute (RPM) and tokens per minute (TPM), with optional defaults in the config.
- **Model aliases with ordered upstreams:** one alias maps to a prioritised list of providers. Failures trigger retries with backoff, failover, a passive cooldown and a total time budget.
- **Two-way guardrail:** 15 built-in secret rules plus an entropy detector, optional custom regex rules, and `redact`, `block` or `log_only` modes (per rule if you like).
- **Usage statistics:** one metadata row per authenticated chat request in a separate SQLite file, written in batches by a background thread, with automatic retention. Prompts and completions are never stored.
- **Admin API:** a separate token-protected listener for keys, upstream status, effective config, stats, guardrail events and hot reload.
- **Dashboard:** a standalone Streamlit app (`ui/`) that talks only to the admin API.
- **Secrets stay in the environment:** provider keys are referenced by env-var name (`api_key_env`), never written in the config.
- **Defensive limits:** request body and upstream response size caps, a graceful-shutdown timer, and `usage` numbers from upstreams treated as untrusted.

---

## Architecture and request path

```
 client                       nigate (one process)                         providers
 ------                       ---------------------------------------      ---------
 POST /v1/chat/completions    data listener  127.0.0.1:4000 (default)
 Authorization: Bearer ngk_.. |
        |                     v
        +-------------------> [1 auth] -> [2 validate] -> [3 guardrail: request]
                                                                  |
                                                                  v
                                                           [4 rate limit RPM/TPM]
                                                                  |
                                                                  v
                              [5 failover: order, retry, cooldown] ---> upstream #1
                                                                  |  ---> upstream #2 ...
                                                                  v
        <-------------------- [6 guardrail: response] <- [usage / TPM correction]
                                                                  |
                                                                  v
                                                          [7 stats (async, SQLite)]

 operators / dashboard ------> admin listener 127.0.0.1:4001 (default; separate; admin token)
```

The order matters, because it decides what costs quota:

1. **Auth.** `GET /healthz` is open. Everything under `/v1/` needs `Authorization: Bearer <virtual key>` (the scheme is case-insensitive).
   - A missing key gives `401 missing_api_key`.
   - An unknown or revoked key gives `401 invalid_api_key` (same message for both).
   - Both 401s carry `WWW-Authenticate: Bearer`.
   - With `auth.required = false` every request is anonymous: no key check and no limits at all.
2. **Validate.**
   - Bad JSON gives `400 invalid_json`.
   - A non-object body gives `400 invalid_body`.
   - A missing `model` gives `400 missing_model`.
   - `"stream": true` (JSON boolean) gives `400 stream_unsupported`.
   - An unknown alias gives `404 model_not_found`.
   - Upstreams whose `api_key_env` is set but empty are skipped. If none remain, you get `503 upstream_not_configured`.
3. **Request guardrail.** Secrets are redacted, or the request is rejected with `403 guardrail_blocked`. This runs before the limiter, so rejected requests spend no quota.
4. **Rate limit.** The per-key RPM/TPM bucket is checked. A rejection gives `429 rate_limit_exceeded` plus `Retry-After`.
5. **Failover.** The request is sent to the upstreams in order. `model` is overwritten with each upstream's real model name, and a `Bearer` header with the provider key is added if one is configured. Client headers are not forwarded, and the virtual key is never sent upstream.
6. **Response guardrail and accounting.** For a successful JSON answer, nigate reads `usage`, corrects the TPM bucket, scans the completion, and then returns it.
7. **Stats.** A metadata row is queued. This covers every request that passed auth on the chat route, including 400, 429 and 5xx results. 401s and `/v1/models` calls are not recorded.

Successful responses carry `x-nigate-upstream` (the upstream's `name`, never its URL) and `x-nigate-attempts` (number of upstream calls, including retries).

**State.**
- *On disk (SQLite):*
  - Key store at `storage.db_path`: hashes, limits, active flag.
  - Stats at `stats.db_path`.
- *In memory only, reset on restart:*
  - Limiter buckets.
  - Upstream health and cooldowns.
  - The active-key cache.

**Error envelope.** Errors are OpenAI-style: `{"error": {"message": "...", "type": "...", "code": "..."}}`. `type` is `invalid_request_error` for every 4xx except 429 (this includes 401, 403 and 404), `rate_limit_error` for 429, and `api_error` for 5xx.

| Status | `code` | Meaning |
|---|---|---|
| 401 | `missing_api_key`, `invalid_api_key` | No key, or unknown/revoked key |
| 400 | `invalid_json`, `invalid_body`, `missing_model`, `stream_unsupported` | Bad request |
| 404 | `model_not_found` | Alias not in the config |
| 503 | `upstream_not_configured` | Every upstream of the alias lacks its provider key |
| 403 | `guardrail_blocked` | Request blocked by a `block`-mode rule |
| 429 | `rate_limit_exceeded` | Per-key RPM/TPM exceeded, with `Retry-After` |
| 502 | `guardrail_blocked` | Provider response blocked by a `block`-mode rule |
| 504 | `upstream_timeout` | Upstream timed out |
| 502 | `upstream_unreachable`, `upstream_read_failed`, `upstream_response_too_large` | Transport-level upstream failures |
| 500 | `internal` | Unexpected internal error |
| any | (provider's own body) | If every upstream fails with an HTTP status, the last provider status and body are forwarded verbatim. See [Failover and health](#failover-and-health) |

---

## Prerequisites

- **Rust toolchain** (stable, via `rustup`). The crate uses edition 2024 and let-chains, so Rust 1.88 or newer is a safe lower bound. `Cargo.toml` declares no `rust-version`, and the exact minimum was not determined. Install a current stable release.
- **A C toolchain** (compiler and linker for your platform). It is needed for the bundled SQLite (`rusqlite` with the `bundled` feature) and for `aws-lc-sys`, the rustls crypto backend that the HTTPS client pulls in. On Windows you may also need CMake and NASM.
- **Network egress** to your providers. HTTPS upstreams use rustls and verify certificates against the system trust store, so keep CA certificates installed.
- Optional:
  - `curl` for smoke tests.
  - `sqlite3` for backups and ad-hoc queries.
  - Python 3 with `pip` for the dashboard (Streamlit and pandas).

No external database or message queue is needed.

---

## Build and run from source

### Build

```bash
cd path/to/nigate        # the folder that contains Cargo.toml
cargo build --release --locked
# binary: target/release/nigate   (nigate.exe on Windows)
```

`Cargo.lock` is committed, and `--locked` makes the build fail instead of silently updating it. The release profile uses opt-level 3, thin LTO and a single codegen unit and strips the binary, so a release build takes a while. For quick iterations, `cargo run -- <args>` builds a debug binary, and `cargo run --release -- -c nigate.toml` builds and starts the optimised one.

The result is a single binary. At run time it needs only its TOML config file and write access to the directory where it creates its two SQLite files (`nigate.db` and `nigate-stats.db` by default). You can copy the binary anywhere on the host.

### First run

```bash
# 1. Create your config from the template, then edit upstreams (see "Configuration")
cp nigate.example.toml nigate.toml

# 2. Generate an admin token (works without a config; prints 64 hex characters)
./target/release/nigate admin token

# 3. Export the secrets the gateway will read from its environment
export NIGATE_ADMIN_TOKEN="<token printed above>"
export CEREBRAS_API_KEY="<your provider key>"       # the variable named by api_key_env in your first upstream

# 4. Start the gateway (serve is the default command)
./target/release/nigate -c nigate.toml
```

On PowerShell, use `$env:NIGATE_ADMIN_TOKEN = "<token>"` and `.\target\release\nigate.exe -c nigate.toml`.

Startup logs go to stderr (Indonesian). You should see lines like `nigate mulai`, `mendengarkan di 127.0.0.1:4000` and `API admin di 127.0.0.1:4001`. Typical warnings:

| Log message (translated) | Meaning |
|---|---|
| `belum ada key aktif` (no active key yet) | Create a key (next step) |
| `API admin TIDAK dijalankan: env token admin kosong` | `NIGATE_ADMIN_TOKEN` is empty, so no admin listener was started |
| `model '<alias>': env X kosong, request ke model ini akan dijawab 503` | The provider key variable for one upstream is empty. That upstream is skipped. Requests get 503 only if every upstream of the alias is unconfigured |

Create a virtual key and make a request. The key is displayed once:

```bash
./target/release/nigate -c nigate.toml key create my-app      # prints ngk_...
export NIGATE_KEY="ngk_..."                                    # your client's copy

curl http://127.0.0.1:4000/healthz
curl -H "Authorization: Bearer $NIGATE_KEY" http://127.0.0.1:4000/v1/models

curl -i http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $NIGATE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"chat-main","messages":[{"role":"user","content":"Say hello"}]}'
```

With `-i` you can see `x-nigate-upstream` and `x-nigate-attempts`. `chat-main` is one of the aliases defined in the shipped `nigate.example.toml`. If you edited the config, use an alias from your own `[[model]]` blocks as `model`.

### CLI reference

```
nigate [-c|--config <file>] [serve]                        run the gateway (default command)
nigate [-c <file>] key create <name>                       create a virtual key (shown once)
nigate [-c <file>] key list                                list keys (rpm/tpm: "-" = not set)
nigate [-c <file>] key limit <name> [--rpm N|none] [--tpm N|none]
nigate [-c <file>] key revoke <name>                       deactivate a key
nigate [-c <file>] key enable <name>                       re-activate a revoked key
nigate [-c <file>] stats [--jam N | --hari N] [--per semua|key|alias|upstream|hari|jam]
nigate [-c <file>] guardrail cek [file]                    test guardrail rules on stdin or a file
nigate admin token                                         print a new random admin token (no config needed)
nigate [-c <file>] healthcheck                             GET http://127.0.0.1:<port>/healthz, 2 s timeout, exit 0 = healthy
nigate help | --help | -h
```

Notes:
- **Config path.** The config path is the first of `-c`/`--config <file>`, the `NIGATE_CONFIG` env var, then `./nigate.toml`. `--config=path` is not supported. A trailing `-c` with no value silently falls back to `nigate.toml` and ignores `NIGATE_CONFIG`.
- **Config validation.** Every command except `help` and `admin token` loads and validates the config first. A missing or invalid config, or an admin-token env var shorter than 24 characters, makes `key ...`, `stats` and `healthcheck` fail too.
- **Stats command.** `stats` reads the stats database file directly, so the server does not need to be running. It defaults to the last 24 hours with `--per semua`. The built-in help text omits `jam` for `--per`, but it is accepted. Columns: `kelompok` (group), `request`, `ok`, `klien` (client errors), `limit`, `guard`, `upstream`, `gw` (gateway), `temuan` (guardrail findings), `tok_masuk`/`tok_keluar` (input/output tokens), `rata(ms)`/`maks(ms)` (mean/max latency).

---

## Configuration

Copy `nigate.example.toml` to `nigate.toml` and edit it. The file contains no secrets: provider keys are referenced by the *name* of an environment variable.

A trimmed walkthrough (placeholders only):

```toml
[server]
listen = "127.0.0.1:4000"      # data listener. Expose beyond loopback only deliberately
max_body_mb = 10               # max request body

[auth]
required = true                # false = /v1/* open, no keys, no limits (local development only)

[storage]
db_path = "nigate.db"          # key store (hashes only). Use an absolute path when run as a service

[limits]                       # per-key defaults per minute; delete a line = unlimited
# default_rpm = 60
# default_tpm = 100000

[resilience]
max_retries = 1                # extra attempts on the same upstream for transient errors
retry_backoff_ms = 200         # doubles per retry: 200, 400, 800 ...
cooldown_secs = 30             # how long a failed upstream is deprioritised
total_timeout_secs = 300       # budget for all attempts and failovers of one request

[stats]
enabled = true
db_path = "nigate-stats.db"
retention_days = 30

[guardrail]
enabled = true
mode = "redact"                # redact | block | log_only
scan_request = true
scan_response = true
entropy = true
entropy_min_length = 32
entropy_threshold = 4.5

[guardrail.aksi]               # per-rule overrides ("aksi" = action)
private_key = "block"

[admin]
enabled = true
listen = "127.0.0.1:4001"
token_env = "NIGATE_ADMIN_TOKEN"

[[model]]                      # one [[model]] = one alias clients put in "model"
alias = "chat-main"

  [[model.upstream]]           # order = priority
  name = "primary"             # shown in x-nigate-upstream and logs
  base_url = "https://api.provider-a.example/v1"
  model = "<provider-model-name>"
  api_key_env = "PROVIDER_A_API_KEY"
  timeout_secs = 120

  [[model.upstream]]
  name = "local-backup"
  base_url = "http://127.0.0.1:11434/v1"      # e.g. a local OpenAI-compatible server
  model = "<local-model-name>"
  timeout_secs = 300
```

The shipped `nigate.example.toml` defines two aliases: `chat-main` (a cloud upstream with a local backup) and `chat-local` (local only). An alias is just a name you choose. Clients pass it in the `model` field, and nothing in nigate depends on the name. See [Using nigate from a client](#using-nigate-from-a-client).

### All options

"Reload" means the value can change through `POST /admin/reload` without a restart.

| Key | Default | Valid range | Reload |
|---|---|---|---|
| `server.listen` | `127.0.0.1:4000` | `host:port` | restart |
| `server.max_body_mb` | `10` | 1 to 256 | restart |
| `server.max_response_mb` | `32` | 1 to 256 | yes |
| `server.shutdown_grace_secs` | `30` | 1 to 300 | restart (read once at startup; a reload accepts it but does not apply it, and does not report it) |
| `auth.required` | `true` | bool | yes |
| `storage.db_path` | `nigate.db` | non-empty | restart |
| `limits.default_rpm`, `limits.default_tpm` | unset (unlimited) | at least 1 | yes |
| `resilience.max_retries` | `1` | 0 to 5 | yes |
| `resilience.retry_backoff_ms` | `200` | 0 to 10000 | yes |
| `resilience.cooldown_secs` | `30` | 1 to 3600 | yes |
| `resilience.total_timeout_secs` | `300` | 1 to 1800 | yes |
| `stats.enabled` | `true` | bool | restart |
| `stats.db_path` | `nigate-stats.db` | non-empty | restart |
| `stats.retention_days` | `30` | 1 to 3650 | restart |
| `guardrail.enabled`, `scan_request`, `scan_response`, `entropy` | `true` | bool | yes |
| `guardrail.mode` | `redact` | `redact`, `block`, `log_only` | yes |
| `guardrail.entropy_min_length` | `32` | 16 to 256 | yes |
| `guardrail.entropy_threshold` | `4.5` | 3.0 to 6.0 | yes |
| `guardrail.aksi.<rule>` | none | a mode; rule must exist | yes |
| `[[guardrail.rule]]` `name`, `pattern`, `mode` | none | name: 1 to 40 chars of `[a-z0-9_]`; `mode` optional | yes |
| `admin.enabled` | `true` | bool | restart |
| `admin.listen` | `127.0.0.1:4001` | `host:port` | restart |
| `admin.token_env` | `NIGATE_ADMIN_TOKEN` | env-var name | restart |
| `[[model]]` `alias` | none | trimmed, non-empty, unique | yes |
| `[[model.upstream]]` `base_url` | none | must start with `http://` or `https://` | yes |
| `[[model.upstream]]` `model` | none | non-empty | yes |
| `[[model.upstream]]` `api_key_env` | none | env-var name (optional) | yes |
| `[[model.upstream]]` `timeout_secs` | `120` | 1 to 600 | yes |
| `[[model.upstream]]` `name` | `upstream-N` | 1 to 64 chars of `[A-Za-z0-9_.-]`, unique per alias | yes |

### Rules to remember

- **Each alias needs at least one `[[model.upstream]]`.** `base_url` has trailing `/` characters trimmed and `/chat/completions` appended, so include `/v1` (or whatever prefix the provider needs) in `base_url`.
- **Provider keys come from the environment only.** The variable named by `api_key_env` is read when the config is loaded or reloaded and trimmed. If it is empty, that upstream counts as *not configured* and is skipped. Because a reload re-reads the same process environment, changing a key's value requires restarting nigate.
- **Reload keeps running values for restart-only settings.** `POST /admin/reload` keeps the running value of any restart-only setting and lists the affected section in `perlu_restart` (`server.listen`, `server.max_body_mb`, `storage.db_path`, `stats`, `admin`).
- **Admin token validation applies to every command.** If the env var named by `admin.token_env` is set but shorter than 24 characters, config loading fails. This applies to every command and to reload, even with `admin.enabled = false`.
- **Typos are mostly silent.** Unknown keys are rejected only inside `[admin]`, `[guardrail]` and `[[guardrail.rule]]`. In every other section a misspelt key such as `max_retires` or `api_key_envv` is silently ignored and the default applies. After editing, check the effective values with `GET /admin/config` (or the dashboard's Konfigurasi tab); keys it does not list must be checked in the file itself.
- **Guardrail rule names are validated.** A name under `[guardrail.aksi]` that is not a real rule (built-in, custom, or `high_entropy`) fails startup or reload.
- **Relative `db_path` values resolve against the working directory.** Use absolute paths if you run nigate as a service.

### Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `NIGATE_CONFIG` | gateway, CLI | Config path if `-c` is not given (default `./nigate.toml`) |
| `NIGATE_ADMIN_TOKEN` | gateway, dashboard | Admin token (at least 24 chars). The variable name can be changed with `admin.token_env` |
| `<your api_key_env names>` | gateway | One variable per upstream key, for example `CEREBRAS_API_KEY` in the shipped example or `PROVIDER_A_API_KEY` in the walkthrough above |
| `RUST_LOG` | gateway, CLI | Log filter (tracing `EnvFilter` syntax, default `info`, written to stderr) |
| `NIGATE_ADMIN_URL` | dashboard | Admin API address (default `http://127.0.0.1:4001`) |

---

## Virtual keys and rate limits

**Format and storage.** A key is `ngk_` followed by 64 lowercase hex characters (32 bytes from the OS CSPRNG). The database stores only its SHA-256 hash plus an 8-character prefix for display. The plaintext is returned exactly once, by `nigate key create` or by the response of `POST /admin/keys`. Key names are 1 to 64 characters of `[A-Za-z0-9_.-]`.

```bash
nigate -c nigate.toml key create svc-example
nigate -c nigate.toml key limit svc-example --rpm 60 --tpm 100000
nigate -c nigate.toml key list
nigate -c nigate.toml key revoke svc-example     # takes effect within about 2 seconds
nigate -c nigate.toml key enable svc-example
```

CLI changes made while the server runs are picked up within about 2 seconds, because the server polls SQLite's `data_version`. Changes made through the admin API apply immediately. A revoked key gets the same `401 invalid_api_key` as an unknown one. Permanent deletion (`DELETE /admin/keys/{name}`) exists only in the admin API; statistics history keeps the key's name.

**Limits.**
- Each key can have its own `rpm` and `tpm`. A key without its own value uses `limits.default_rpm` / `default_tpm`, and with neither set it is unlimited.
- `none` on the CLI, or `null` in an admin `PATCH`, clears the key's own value so it falls back to the default. There is no way to make one key unlimited while a default is set. Set a large explicit value instead.
- Limits are token buckets, per key, in memory.
  - The capacity equals the per-minute limit and refills continuously at `capacity / 60` per second. For example, `rpm = 60` allows a burst of 60 requests, then one per second.
  - **RPM** needs at least one token in the bucket.
  - **TPM** is soft:
    - Before the request, nigate estimates input tokens as `body bytes / 4` (at least 1). An 8000-byte body counts as 2000 tokens.
    - After a successful response, if the key has a TPM limit and the response has `usage`, the bucket is corrected by the real total minus the estimate. The real total is `total_tokens`, or prompt plus completion tokens if no total is given.
    - A request larger than the whole capacity can pass on a full bucket and drive the balance negative. The debt is capped at 60 minutes of refill.
  - If either check fails, nothing is consumed. The result is `429 rate_limit_exceeded` with `Retry-After` set to the wait in whole seconds (at least 1, at most 3600).
  - If the upstream fails or returns a non-success status, the TPM estimate is refunded, but the RPM token stays spent.
  - Requests rejected earlier (invalid JSON, unknown alias, guardrail block) consume nothing.
- Limiter state resets on restart; every key then starts with a full bucket. Entries are never pruned while running.

**Anonymous mode.** With `auth.required = false`, requests are anonymous (recorded in stats as key `anonim`) and no limits apply, not even the defaults. Use it for local development only.

---

## Using nigate from a client

To a client, nigate is one more OpenAI-compatible endpoint. Any client or SDK that can set a base URL, an API key and a model name works. There is nothing nigate-specific to install on the client side.

### 1. On the gateway

1. Define an alias in `nigate.toml` with its upstreams (see [Configuration](#configuration)).
2. Create a key with `nigate key create <name>` and note the `ngk_...` value. Use **one key per client process** (or per application), so limits and statistics are separate. Clients that share a key share its RPM/TPM bucket.
3. Hand the key to the client through its environment or secret store, not through source code or a committed file, and restart the client if it reads the variable only at startup.

### 2. In the client

| Client setting | Value for nigate |
|---|---|
| Base URL | `http://<gateway-host>:4000/v1`. It must end in `/v1`, because clients append `/chat/completions`. Left at its default, a client talks to the provider and not to nigate |
| API key | the `ngk_...` virtual key, sent as `Authorization: Bearer ngk_...` (the scheme is case-insensitive). Never use the provider's own key here |
| `model` | the nigate **alias** from a `[[model]]` block, not the provider's model name. `GET /v1/models` lists the aliases |
| `stream` | omit it or send `false`. `"stream": true` is rejected with `400 stream_unsupported` |
| Timeout | larger than nigate's `total_timeout_secs` (300 by default), plus a little for guardrail processing. Otherwise the client gives up while nigate is still retrying or failing over |
| Client-side retries | off (for example `max_retries=0`), so retries and failover are left to nigate. Otherwise the client's own retries stack on top of nigate's |
| Other parameters | `temperature`, `max_tokens`, `tools` and the like are forwarded to the upstream. Only `model` is overwritten with the upstream's real model name |

Use the real address as the base URL. A client on the same host can use `127.0.0.1`. If the client runs on another machine, use an address it can reach and set `server.listen` in `nigate.toml` to bind to that address, because the default listens on loopback only. See [Security notes](#security-notes) about plain HTTP on the data listener.

**curl**

```bash
curl -i http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $NIGATE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"chat-main","messages":[{"role":"user","content":"Say hello"}]}'
```

**Python (`openai` SDK)**

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:4000/v1",
    api_key=os.environ["NIGATE_KEY"],   # the ngk_... virtual key
    timeout=330,                        # more than total_timeout_secs (300 by default)
    max_retries=0,                      # retries and failover are nigate's job
)

resp = client.chat.completions.create(
    model="chat-main",                  # a nigate alias
    messages=[{"role": "user", "content": "Say hello"}],
)
print(resp.choices[0].message.content)
```

### 3. What a client will see

Errors use the OpenAI-style envelope described in [Architecture and request path](#architecture-and-request-path). The table says what each one means for a caller.

| Status and `code` | Meaning for the client | Retry? |
|---|---|---|
| `400` `invalid_json`, `invalid_body`, `missing_model`, `stream_unsupported` | The request itself is wrong. For `stream_unsupported`, turn streaming off | No, fix the request |
| `401` `missing_api_key`, `invalid_api_key` | No key, or an unknown or revoked key. The response carries `WWW-Authenticate: Bearer` | No, fix the key |
| `403` `guardrail_blocked` | A `block`-mode guardrail rule matched the request. Nothing was sent to a provider and no quota was spent | No: the same body is blocked again. Remove the secret first |
| `404` `model_not_found` | The `model` value is not an alias of this gateway | No, fix the alias |
| `429` `rate_limit_exceeded` | The key's own RPM or TPM is exhausted. `Retry-After` gives the wait in whole seconds (1 to 3600) | Yes, after `Retry-After` |
| `502` `guardrail_blocked` | A `block`-mode rule matched the provider's *response* (the provider was already billed) | Usually not: the same prompt tends to give the same result |
| `502` `upstream_unreachable`, `upstream_read_failed`, `upstream_response_too_large`; `504` `upstream_timeout` | Every upstream tried failed at transport level. nigate has already retried and failed over within its budget | Yes, later, with backoff |
| `503` `upstream_not_configured` | Every upstream of the alias lacks its provider key. This is an operator problem | Not until the operator fixes it |
| `500` `internal` | Unexpected gateway error | Maybe once |
| any status, provider's own body | See below | Depends on the status |

**Provider statuses pass through.** If an upstream answers with a non-retryable error status (for example `400` or `422`), or if every upstream fails with an HTTP status, nigate forwards that provider status and body unchanged. A provider `401`, `403` or `429` can therefore reach the client without being nigate's own answer (a wrong provider key inside nigate shows up this way). A client that classifies failures by HTTP status alone can misread such a reply, so check whether the body carries one of nigate's `code` values (see the table in [Architecture and request path](#architecture-and-request-path)).

**Response headers.**
- `x-nigate-upstream`: the `name` of the upstream that produced the answer (never its URL). It is present on successful responses and on a non-retryable provider error that a specific upstream returned. It is absent on nigate's own JSON errors and when every upstream failed with an HTTP status.
- `x-nigate-attempts`: the number of upstream calls made for the request, including retries. It is present on every response that comes from an upstream (successful or forwarded), but not on nigate's own JSON errors.
- `Retry-After`: set on `429 rate_limit_exceeded` (whole seconds).

### Things to know

- **No fail-open.** If nigate is down, calls through it fail. Do not chain a direct-to-provider fallback behind the gateway unless the caller accepts bypassing the guardrail and the key limits. A router that falls back on *any* error would also send a `403 guardrail_blocked`, a `429` or a `502` from the gateway straight to the provider, defeating both. If you do add a fallback, trigger it only when no HTTP response came back at all (connection refused, DNS failure), never on a nigate rejection.
- **Timeout stacking.** Set the client timeout and retry count explicitly. Otherwise the client library's own defaults apply on top of nigate's retries and failover (the `openai` Python SDK, for example, waits up to 600 s and retries twice by default). The client timeout must exceed nigate's worst case (`total_timeout_secs`, plus a little for guardrail processing), or the client will time out first.
- **Size the limits.** A single user action can make several LLM calls (agent steps, tool loops, summaries). If several processes share one key, they share its RPM/TPM, and a `429` can arrive quickly. The shipped `nigate.example.toml` sets no default limit (`default_rpm` and `default_tpm` are commented out), so a new key is unlimited until you set one. Set per-key limits deliberately (`key limit <name> --rpm N`), and leave enough headroom for busy clients.
- **Guardrail side effects.** The guardrail scans message `content` of *every* role, including role `tool` messages, and tool-call arguments. In `redact` mode, anything in a tool result that matches a rule (for example `password=...`, `ghp_...`) or is a high-entropy token of at least 32 characters mixing at least two of lower case, upper case and digits (for example a base64 id) is replaced with `[REDACTED:<rule>]` before the model sees it. Pure hex hashes and commit ids are not flagged at the default threshold. This can change what the model reads. Use `guardrail cek` on realistic tool output.
- **Scope.** Only chat completions go through nigate. Embeddings and other endpoints are not proxied, so a client that needs them must reach its provider another way.
- **Test the setup.** Send the `curl` request above, or the one in [First run](#first-run), with the same base URL, key and alias, and look at `x-nigate-upstream` in the response.

---

## Guardrail

The guardrail is a secret and credential filter, not a general content filter. It has **no PII detection** (names, emails, phone numbers, national IDs) and no prompt-injection or semantic filtering.

**What is scanned.** Request side, in `messages[*]`:
- the fields `content`, `reasoning_content`, `reasoning` and `refusal`;
- `tool_calls[].function.arguments`;
- `function_call.arguments`.

Response side, in `choices[*].message` (same fields) and `choices[*].text`. A `content` value can be a string or a list of parts, and only each part's `text` string is scanned (image parts are skipped). Fields such as `id`, `name`, `role`, `tool_call_id`, `model` and the tool schemas are deliberately left alone so tool-call matching is not broken.

**Modes.**

| Mode | Effect |
|---|---|
| `redact` (default) | The finding is replaced with `[REDACTED:<rule>]` and the request or response continues |
| `block` | The whole request (`403 guardrail_blocked`, before any upstream call and before quota is spent) or response (`502 guardrail_blocked`, after the upstream already answered) is rejected. The error lists rule names, never values |
| `log_only` | Findings are counted and logged; the content is untouched |

A rule's effective mode is the first of: its entry in `[guardrail.aksi]`, then the `mode` of a custom rule, then the global `guardrail.mode`.

**Built-in rules.** The 15 regex rules, plus the entropy detector `high_entropy`:

`private_key`, `aws_access_key`, `aws_secret_key`, `github_token`, `sk_api_key`, `cerebras_key`, `groq_key`, `nigate_key`, `slack_token`, `google_api_key`, `stripe_key`, `jwt`, `bearer_token`, `url_credentials`, `secret_assignment`

- If a rule has a capture group, only group 1 is replaced. For example `DB_PASSWORD=[REDACTED:secret_assignment]` keeps the variable name.
- `bearer_token` requires letters and digits in the value. `url_credentials` and `secret_assignment` ignore obvious placeholders (`none`, `null`, `changeme`, `password` and similar), values containing any of `( ) { } $ < > [ ] *`, and values with two or fewer distinct characters.
- **Entropy detector.** It looks at tokens made of `[A-Za-z0-9+/_-]`. A token is flagged when:
  - its length is at least `entropy_min_length`;
  - it contains at least two character classes (lower case, upper case, digit);
  - its Shannon entropy is at least `entropy_threshold` bits per character.

  Pure hex tops out at 4.0 bits per character, so hashes and commit ids pass at the default threshold. Entropy hits that overlap a specific rule's match are dropped. Raise the threshold if you see false positives, and lower it or use `entropy_min_length` if known secrets slip through.
- **Overlaps.** When findings overlap, the one that starts first (then the longer one) wins, and a later hit that starts inside it is ignored. This is decided by position, not severity. A `block` hit inside an earlier `redact` hit is not counted.

**Custom rules.**

```toml
[[guardrail.rule]]
name = "ticket_id"                   # 1-40 chars [a-z0-9_]; must not reuse a built-in name
pattern = "TKT-[0-9]{8}"             # Rust regex syntax; case-sensitive unless you add (?i)
mode = "block"                       # optional

[[guardrail.rule]]
name = "service_token"
pattern = "service_token=([A-Za-z0-9]{16,})"   # group 1 only is redacted
```

Avoid patterns that can match an empty string. They are not rejected and would fire at every position.

**Test rules without running the gateway.**

```bash
echo 'DB_PASSWORD=Xk29sLq0pZ' | nigate -c nigate.toml guardrail cek
```

It prints the findings per rule, which rules would block, and the text after redaction. It uses the configured rules and modes but ignores `enabled = false`, so it shows what the rules *would* do.

**What gets recorded.** Statistics store only rule names and counts (`temuan_masuk`, `temuan_keluar`, `jenis_temuan`), never matched text. Logs contain rule names and counts as well.

**Gaps to know (see also [Known limitations](#known-limitations)).**
- Not scanned: tool and function *definitions*, `response_format`, `stop`, `metadata`, `user`, message `name`, non-`text` content parts (image URLs, audio, files), and a request with no `messages` key.
- Response scanning covers only `choices[*].message` and `choices[*].text`. A non-JSON success body or a JSON body without `choices` passes unscanned. Non-2xx upstream bodies are forwarded without scanning.
- `"stream": "true"` (a string) is not rejected, and a streamed reply would not be scanned.
- It is a heuristic. Secrets without a known prefix, a label such as `password=`, or enough length and entropy can slip through.
- The policy is global: no per-key or per-alias override.

---

## Failover and health

The order of `[[model.upstream]]` entries is the priority order.

| What the upstream does | What nigate does |
|---|---|
| Connection error, timeout, body read error, 5xx, 408 | Retry the same upstream (up to `max_retries`), then move to the next |
| 429, 401, 403 | Move to the next upstream immediately (no retry) |
| Response larger than `server.max_response_mb` | Move to the next upstream (no retry) |
| Any other 4xx (400, 404, 422, ...) | Return it to the client as-is; counts as a healthy upstream |
| 2xx | Return it (after the response guardrail) |

- **Per-attempt time.** Each attempt is limited to `min(timeout_secs of the upstream, time left of total_timeout_secs)`. Connecting has a fixed 10 s timeout.
- **Backoff.** Backoff is `retry_backoff_ms * 2^(n-1)` (200, 400, 800 ms ...) with no jitter, and never sleeps beyond the remaining total budget.
- **Total budget.** When `total_timeout_secs` runs out, remaining upstreams are not tried. The last failure is returned, or `504 upstream_timeout` if there was none. Keep `total_timeout_secs` comfortably above `timeout_secs * (1 + max_retries)` of your primary upstream plus the time you want the backup to have. Otherwise a hung primary can eat the entire budget and the backup is never reached. With the defaults, a primary that hangs on both attempts uses about 240 s, leaving about 60 s for the next upstream.
- **Cooldown and ordering.** Health tracking is passive; there are no active probes.
  - After an upstream has used up its attempts, it is marked failed for `cooldown_secs` (flat, not growing). For a `429` carrying a `Retry-After` in integer seconds, the cooldown is that value clamped to 1 to 300 s. HTTP-date values are not read.
  - A cooled-down upstream is not removed. It only moves behind the healthy ones (soonest-to-recover first) and gets a single attempt without retries. A request is never failed without trying everything.
  - Any success or pass-through response clears an upstream's failure state. After the cooldown it returns to its configured position immediately.
- **Health keys.** Health state is keyed by `base_url|model` and is shared by every alias that points at the same endpoint and model. State is in memory and per process; it resets on restart. See the current state with `GET /admin/upstreams` or the Upstream tab.
- **`/healthz` is process liveness only.** It does not reflect provider or database state.
- **What the client sees when everything fails.**
  - If the last failure was an HTTP status from a provider (5xx, 401, 403, 408 or 429), that provider's status, content type and body are forwarded verbatim, with `x-nigate-attempts` but without `x-nigate-upstream`. The provider's `Retry-After` is not forwarded. A provider-side 401 or 429 therefore looks like nigate's own by status; the body tells them apart (nigate's carry codes such as `invalid_api_key` or `rate_limit_exceeded`).
  - Other failures become `504 upstream_timeout`, `502 upstream_unreachable`, `502 upstream_read_failed` or `502 upstream_response_too_large`.
- **Duplicates.** Retries and failover resend the complete request. There is no idempotency key, so a retry after a timeout can cause duplicate generation on the provider side (and duplicate billing).

---

## Admin API

A separate listener (`admin.listen`, default `127.0.0.1:4001`) serves a JSON API.

- **When it starts.** It starts only if `admin.enabled` is true **and** the env var named by `admin.token_env` is set and non-empty. Without a token there is no admin door at all. The default behaviour is therefore "no admin API until a token is exported".
- **Authentication.** Send `Authorization: Bearer <admin token>`. A wrong or missing token gives `401 invalid_admin_token`. Failed attempts are logged at most once per 10 s.
- **Response headers and limits.** Every response carries `Cache-Control: no-store`. Request bodies are limited to 64 KiB.
- **Token requirements.** The token must be at least 24 characters. Generate one with `nigate admin token`.

| Method and path | Purpose |
|---|---|
| `GET /admin/health` | Status, version (`versi`), uptime (`uptime_detik`), `auth_required`, number of models, `stats_aktif`, `statistik_dibuang`, `guardrail_aktif`, `reload_tersedia` |
| `GET /admin/config` | Effective config without secrets (a subset: it does not show `max_response_mb`, `shutdown_grace_secs`, the two `db_path` values, `admin.enabled`/`token_env` or custom-rule patterns). Upstream URLs have any `user:password@` removed; env-var names are shown, values never |
| `POST /admin/reload` | Re-read the config file and apply it. An invalid file is rejected whole (`400 config_invalid`) and the old config keeps running. Returns `{"status","jumlah_model","perlu_restart"}` |
| `GET /admin/keys` | List keys with limits and effective limits (`rpm_efektif`, `tpm_efektif`) |
| `POST /admin/keys` | Create a key. Body `{"name": "...", "rpm"?: N, "tpm"?: N}`. Returns `201` with `{"key": "ngk_...", "info": {...}}`. This is the only time the key is returned |
| `PATCH /admin/keys/{nama}` | Change `active`, `rpm`, `tpm`. A field that is absent stays unchanged. `null` clears a limit. Unknown fields are rejected |
| `DELETE /admin/keys/{nama}` | Delete a key permanently (statistics history stays). Returns `{"dihapus": "<name>"}` |
| `GET /admin/upstreams` | Per alias and upstream: order, `name`, model, URL, `key_env`, `terkonfigurasi`, timeout, `gagal_beruntun`, `dalam_cooldown`, `sisa_cooldown_detik` |
| `GET /admin/stats?jam=24&per=semua` | Aggregated usage. `jam` is 1 to 8760 (default 24). `per` is `semua` (default), `key`, `alias`, `upstream`, `hari` or `jam` |
| `GET /admin/guardrail/events?jam=24&limit=100` | Recent requests that triggered the guardrail (metadata only). `jam` is 1 to 8760 (default 24), `limit` is 1 to 1000 (default 100) |

Examples (POSIX shell):

```bash
export A=http://127.0.0.1:4001
export H="Authorization: Bearer $NIGATE_ADMIN_TOKEN"

curl -H "$H" "$A/admin/health"
curl -H "$H" "$A/admin/stats?jam=6&per=alias"

curl -X POST  -H "$H" -H "Content-Type: application/json" -d '{"name":"svc-example","rpm":60}' "$A/admin/keys"
curl -X PATCH -H "$H" -H "Content-Type: application/json" -d '{"rpm":null}'   "$A/admin/keys/svc-example"   # clear limit
curl -X PATCH -H "$H" -H "Content-Type: application/json" -d '{"active":false}' "$A/admin/keys/svc-example"  # revoke
curl -X POST  -H "$H" "$A/admin/reload"
```

**Stats rows** contain: `kelompok` (the group label), `request`, `ok`, `klien`, `limit`, `guardrail`, `upstream`, `gateway` (outcome counts), `temuan` (guardrail findings), `token_masuk`, `token_keluar`, `latensi_rata_ms` and `latensi_maks_ms`. The response wraps them as `{"aktif","jam","per","dari_ms","sampai_ms","dibuang","baris": [...]}`. Notes on stats:
- **Outcomes.** `hasil` values: `ok`, `klien` (client-side error), `limit`, `guardrail`, `upstream`, `gateway`. A 401, 403, 408 or 429 forwarded from a provider counts as `upstream`; a gateway-generated 429 counts as `limit`.
- **Time and buckets.** Timestamps (`dari_ms`, `sampai_ms`) are milliseconds, while `created_at` on keys is Unix seconds. `hari` and `jam` buckets are UTC. Rows are bucketed by request start time.
- **Token sums.** Token counts come from the provider's `usage` on 2xx JSON upstream responses, including ones nigate then blocks with `502 guardrail_blocked`. If a provider reports only `total_tokens`, the input and output columns stay empty.
- **Query parsing.** Parameters are not percent-decoded, unknown parameters are ignored, and `per` is case-sensitive.

**Admin error codes.** `invalid_admin_token` (401); `invalid_body`, `invalid_name`, `invalid_limit`, `no_changes` (an empty JSON object on PATCH), `invalid_query`, `config_invalid` (400); `key_not_found` (404); `name_taken`, `reload_unavailable` (409, raised when the gateway was not started from a config file); `internal` (500).

---

## Dashboard

`ui/` is a standalone Streamlit app. It never reads nigate's databases; it only calls the admin API.

```bash
pip install -r ui/requirements.txt
export NIGATE_ADMIN_TOKEN="<same token the gateway got>"
export NIGATE_ADMIN_URL="http://127.0.0.1:4001"      # only if different from this default

cd ui
python -m streamlit run app.py --server.port 8502 --server.address 127.0.0.1
# open http://127.0.0.1:8502
```

On Windows, `ui\jalankan.cmd` runs the same command, bound to `127.0.0.1:8502`, after you `set NIGATE_ADMIN_TOKEN=...`. If the token variable is not set, the sidebar shows a password field instead.

**Sidebar.** Admin API address, token, period (1 hour, 6 hours, 24 hours by default, 7 days, 30 days) and an optional 10-second auto-refresh (off by default). It also shows the gateway version and uptime and warns when stats are disabled or records were dropped.

**Tabs** (labels are Indonesian):

| Tab | What it does |
|---|---|
| Ringkasan (Summary) | Request totals and outcome counts, outcome chart over time, latency and token charts, breakdown by key, alias and upstream |
| Key & Limit | List keys, create one (the plaintext key is shown once until you click "Sudah saya simpan"), activate or revoke, set RPM/TPM or follow the default, delete after confirming |
| Upstream | Each alias's upstreams in failover order: "Sehat" (healthy), "Cooldown N dtk" (cooling down, N seconds left) or "Belum dikonfigurasi (env X kosong)" (provider env var empty) |
| Guardrail | Effective guardrail settings and recent findings (rule names and counts only) |
| Konfigurasi (Configuration) | Effective config, and a button that calls `POST /admin/reload` and warns about `perlu_restart` sections |

**Things to know.**
- The dashboard has no login of its own. Whoever can reach port 8502 has the admin token's powers (create or delete keys, reload). Keep it on loopback, or put your own authentication in front of it.
- The "Alamat API admin" (admin address) field in the sidebar is free text. The token from the environment is sent to whatever address is typed there. Only an `http://` or `https://` prefix is checked.
- The gateway groups hourly statistics in UTC. The dashboard converts them to the local time zone of the machine running the dashboard.
- Each page render makes about a dozen admin calls, five of them to `/admin/stats`. Auto-refresh covers the Ringkasan, Upstream and Guardrail tabs.
- `ui/requirements.txt` pins `streamlit==1.59.1` (the version the dashboard was tested with) and `pandas>=2.0`. The page uses newer Streamlit options (for example `width="stretch"` on `st.dataframe`), so do not downgrade Streamlit.
- The launcher does not change Streamlit's own usage-statistics settings. See Streamlit's documentation (`browser.gatherUsageStats`) if you want them off.
- Run the UI tests from the NIGATE root (not from `ui/`) with `python -m unittest discover -s ui -p "tes_*.py" -v` (needs Streamlit installed).

---

## Operations

**Reload versus restart.** Edit `nigate.toml`, then call `POST /admin/reload` (or use the dashboard button). Changes to `server.listen`, `server.max_body_mb`, `storage.db_path`, `stats.*`, `admin.*` and `shutdown_grace_secs` need a restart. There is no `SIGHUP` or file watching.

**Rotating secrets.**
- *Admin token:* run `nigate admin token`, set the new value in the gateway's environment, restart, then update the dashboard's environment. The token is one static shared secret; there is no overlap period.
- *Provider key:* change the environment variable and restart. A reload cannot see changed environment values.
- *Virtual key:* revoke the old key, then create a new one. Revocation applies within about 2 s from the CLI, or immediately through the admin API.

**Logging.** Logs go to stderr. Control verbosity with `RUST_LOG` (`info` by default; for example `RUST_LOG=debug`). The log statements we reviewed carry key names, aliases, upstream names and guardrail rule names, and not prompts, completions, key values or matched secrets. Review the output for your own deployment before shipping logs elsewhere.

**Stopping.** `Ctrl-C` (all platforms) or `SIGTERM` (Unix) starts a graceful shutdown. In-flight requests get `server.shutdown_grace_secs` (default 30) and are then dropped with a warning. Statistics are flushed before exit. If your supervisor sends `SIGKILL` after a timeout, make that timeout longer than the grace period.

**Backups.** Two SQLite files matter:
- The **key store** (`storage.db_path`) holds virtual-key hashes, limits and active flags. It is the one to back up.
- The **stats database** (`stats.db_path`) is history. It uses WAL mode, so copying only the main file while the gateway runs can give an inconsistent copy.

Use SQLite's online backup, which is safe while the gateway runs. The `backup/` directory must exist first, or `.backup` fails:

```bash
mkdir -p backup
sqlite3 nigate.db       ".backup 'backup/nigate.db'"
sqlite3 nigate-stats.db ".backup 'backup/nigate-stats.db'"
```

Also keep a copy of `nigate.toml`. It holds no secrets, but you will want it.

**Retention and disk.** Statistics older than `stats.retention_days` are deleted when the gateway starts and then about hourly. The code does not run `VACUUM` or a WAL checkpoint, so the file does not shrink after a purge.

**Upgrades.** Pull the new sources, rebuild with `cargo build --release --locked`, and restart the process. Both databases migrate on open (key store schema 2, stats schema 2). A database written by a newer nigate than the running binary is refused, so a downgrade needs a restore.

**Running as a service.** nigate does not daemonise itself, so run it under a process manager. Any one will do, provided it starts the binary with an absolute config path, supplies the secrets through the process environment, restarts it on failure, and waits longer than `server.shutdown_grace_secs` before force-killing it. An illustrative systemd unit (comments sit on their own lines because systemd does not support trailing comments):

```ini
[Unit]
Description=nigate AI gateway
After=network-online.target

[Service]
User=nigate
WorkingDirectory=/var/lib/nigate
# nigate.env holds NIGATE_ADMIN_TOKEN=... and one line per provider key variable
EnvironmentFile=/etc/nigate/nigate.env
ExecStart=/usr/local/bin/nigate -c /etc/nigate/nigate.toml
Restart=on-failure
# longer than server.shutdown_grace_secs (30 by default)
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
```

Use absolute database paths in the config. Restrict `nigate.env` and the SQLite files with file permissions.

**Monitoring.** `GET /healthz` or `nigate healthcheck` answers "is the process up". `GET /admin/upstreams` shows cooldowns. `GET /admin/stats` and the dashboard show outcome mix, latency and dropped-statistics counts (`statistik_dibuang`).

---

## Security notes

- **Virtual keys.** They are random 256-bit tokens, stored as unsalted SHA-256 hashes (adequate for random tokens of that size). Authentication is a memory lookup on the digest. The gateway's own key is never forwarded upstream, and neither are any client headers.
- **Provider keys.** They exist only in the environment named by `api_key_env`. They do not appear in the config, logs, statistics or admin responses.
- **Admin API.** It is plain HTTP with a single static token, no TLS, no lockout and no scoping. Keep `admin.listen` on loopback. A non-loopback bind only logs a warning. For remote administration, use an SSH tunnel or a TLS-terminating reverse proxy with its own access control. A holder of the admin token plus write access to the config file can switch off client authentication (`auth.required`) through a reload.
- **Data listener.** It also speaks plain HTTP. If clients are on another machine, terminate TLS in front of it and restrict who can connect.
- **Do not disable `auth.required`** outside local experiments. The data routes are then open and unlimited.
- **Statistics.** Rows hold metadata only: key name, alias, upstream name, status, outcome, error code, token counts, latency, attempts, and guardrail rule names and counts. Anyone who can read the SQLite file sees who used what, and when.
- **`/admin/config`.** It strips `user:password@` from upstream URLs but nothing else. Do not put credentials in `base_url` query strings.
- **Guardrail.** It is a heuristic safety net and not a data-loss-prevention product. See [Guardrail](#guardrail) for what it does not cover.
- **Size caps.** Request bodies are capped by `server.max_body_mb`, upstream responses by `server.max_response_mb`, and admin bodies at 64 KiB. Regex matching uses the `regex` crate (linear time), and custom patterns are compiled with a 1 MiB size limit each.
- **Untrusted `usage`.** Numbers reported by upstreams are clamped, and TPM debt is capped at 60 minutes of refill, so a bad response cannot lock a key out for long.
- **Never commit** `nigate.toml` with real hostnames you consider private, `.env` files, SQLite files, or logs.

---

## Known limitations

- **Scope.** Chat completions only, non-streaming. There is no embeddings, completions, Responses or audio endpoint.
- **Single instance.** Rate-limit buckets, upstream health and cooldowns are in memory. They reset on restart and are not shared between instances.
- **No cost accounting and no export.** Only token counts are stored. There is no price table and no CSV or raw-row export endpoint. `nigate stats` prints a table, and the stats file is plain SQLite.
- **No fail-open.** If nigate is down, its clients fail. Fail-open belongs in the client or its router, and it must not treat gateway rejections (403, 429, 502) as "gateway unreachable".
- **Provider errors pass through.** The last provider HTTP error is forwarded verbatim (status and body) when all upstreams fail. Clients that classify errors by status alone can misread a provider 401 or 429 as their own.
- **Failover details.**
  - There are no active health probes and no half-open trial: a recovered upstream gets full traffic at once.
  - Concurrent requests keep hitting a failing upstream until the first one finishes its retries and cooldown is recorded.
  - There is no backoff jitter.
  - Permanent 5xx errors (such as 501) are retried like transient ones.
- **Request rewriting.** The request is parsed and re-serialised before forwarding (after redaction), so key order and number formatting can differ from the client's bytes. `model` is replaced. The TPM estimate uses the original body size.
- **TPM is approximate.** It is estimated from body size and corrected only from the upstream's reported `usage`, and only for keys that have a TPM limit. A guardrail-blocked *response* is not refunded.
- **Guardrail coverage.** See [Guardrail](#guardrail): unscanned fields, unscanned non-JSON or non-`choices` responses, no PII, heuristic entropy, position-based overlap resolution, and a global policy.
- **Statistics are lossy by design.** The queue holds 20,000 records. When it is full or a write fails, records are dropped and only counted in memory (`statistik_dibuang`, reset on restart). `/admin/guardrail/events` is therefore not an audit log. Statistics are visible within about a second, not instantly. A deleted and recreated key name merges into the old name's history.
- **Key id reuse.** `api_keys.id` is a plain `INTEGER PRIMARY KEY`, so SQLite may reuse an id after the newest key is deleted. The limiter is keyed by that id and never pruned, so a recreated key could inherit an old bucket until restart. Prefer revoking over deleting.
- **Admin mutations are not transactional.** Creating a key with limits is two steps, and `PATCH` is read-then-write. Concurrent edits can overwrite each other.
- **Silent config typos** outside `[admin]` and `[guardrail]` (see [Configuration](#configuration)).
- **Oversized bodies.** A request or admin body over its size limit is rejected by the HTTP layer. The reply may not use the OpenAI JSON error format; no nigate code or test handles it.
- **Very large `stats` arguments.** `nigate stats --jam/--hari` has no upper bound (the API caps at 8760 hours), so absurdly large values may overflow.
- **Language.** Messages, logs, CLI output and the dashboard are Indonesian only.
- **Maturity.** There is no CI configuration and no declared minimum Rust version. Benchmarks exist as ignored tests (`tests/bench.rs`), and their figures have not been independently reproduced.

---

## Development and tests

```bash
cd path/to/nigate        # the folder that contains Cargo.toml
cargo test                                         # integration tests in tests/
cargo clippy --all-targets -- -D warnings          # lint, warnings are errors
cargo fmt                                          # rustfmt.toml: max_width = 140, use_small_heuristics = "Max"
cargo test --release --test bench -- --ignored --nocapture      # ignored benchmarks
```

- **Tests.** They run in-process: they build the axum `Router` from a config string with injected environment values and drive it with `tower::ServiceExt::oneshot`. Fake upstreams are real local servers on ephemeral ports, and `127.0.0.1:1` stands in for a dead upstream. A few tests wait on real timers (cooldown, timeouts, stats flush), so the suite takes a few seconds longer than a pure unit run.
- **Coverage gaps.** The CLI entry point, `serve()`, graceful shutdown, the 413 body-limit path and any CI or packaging have no tests. The dashboard has its own Python tests (see [Dashboard](#dashboard)).

Source layout (`src/`; module names are Indonesian where noted):

| Module | Role |
|---|---|
| `main.rs` | CLI and `serve()`: listeners, signal handling, graceful shutdown |
| `config.rs` | TOML schema, defaults, validation, hot-reload merge |
| `proxy.rs` | Router and the chat-completions handler |
| `auth.rs` | Bearer authentication for `/v1/*` |
| `keys.rs` | Virtual-key store (SQLite plus in-memory cache) |
| `limiter.rs` | Per-key RPM/TPM token buckets (`pembatas` = limiter) |
| `failover.rs`, `kesehatan.rs` | Retry, failover and cooldown (`kesehatan` = health) |
| `guardrail.rs` | Secret rules, entropy detector, JSON walkers |
| `stats.rs` | Batched statistics recorder and aggregation |
| `admin.rs` | Admin API |
| `error.rs`, `util.rs` | Error envelope and small helpers |

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401 missing_api_key` | No `Authorization: Bearer ngk_...` header |
| `401 invalid_api_key` | Key mistyped, revoked, or created in a different key store (check `storage.db_path`). CLI changes can take up to about 2 s |
| `401`, `403` or `429` with a body that is not nigate's format | It came from the *provider*, forwarded as-is. Check the provider key and quota, and `GET /admin/upstreams` |
| `400 stream_unsupported` | The client sent `"stream": true`. Disable streaming in the client |
| `404 model_not_found` | `model` is not an alias in `nigate.toml` |
| `503 upstream_not_configured` | Every upstream of that alias has an `api_key_env` whose variable is empty in nigate's environment |
| `429 rate_limit_exceeded` | The key's RPM or TPM is exhausted. See `Retry-After`, and raise the limit with `key limit` |
| `403 guardrail_blocked` | A `block`-mode rule matched the request. Remove the secret, or lower that rule's mode in `[guardrail.aksi]` |
| `502 guardrail_blocked` | A `block`-mode rule matched the provider's *response* (the provider was already billed) |
| `502 upstream_unreachable` | Wrong `base_url`, provider down, no network or DNS, or the address is not reachable from where nigate runs |
| `504 upstream_timeout` | Upstream slower than its `timeout_secs`, or the total budget ran out |
| `502 upstream_read_failed` | The connection broke while reading the body. A slow body that exceeds the timeout may also surface this way |
| `502 upstream_response_too_large` | The response exceeded `server.max_response_mb` |
| Admin API unreachable, log says `API admin TIDAK dijalankan` | The env var for the admin token is empty. Export it and restart |
| Startup, reload or any CLI command fails mentioning the admin token being too short | The env var named by `admin.token_env` is set but under 24 characters. Generate a proper token |
| Failed to bind (`gagal mendengarkan ... port sudah dipakai?`) | Another process uses the port, or it is already running |
| `key create` and similar commands fail on a missing config | The CLI always loads the config. Pass `-c <file>` or set `NIGATE_CONFIG` |
| A setting seems ignored | A misspelt key outside `[admin]` and `[guardrail]` is silently ignored. Compare against `GET /admin/config`; keys it does not list (see the Admin API table) must be checked in the file itself |
| `POST /admin/reload` reports `perlu_restart` | Those settings are pinned until restart. Restart nigate |
| Statistics are empty | `stats.enabled = false`, no authenticated chat requests yet, or the stats file was deleted while running (`/admin/stats` then returns an error) |
| `nigate healthcheck` fails | The config does not load, or `server.listen` has no valid port, or the gateway is not listening on loopback at that port |
| Dashboard says it cannot reach the gateway | The admin listener is not running, or the address in the sidebar is wrong |
| Dashboard says the token was rejected | `NIGATE_ADMIN_TOKEN` differs from the gateway's value |
| A client gets `401 invalid_api_key` although it should have a key | Besides a wrong or revoked key: the client's key variable may be empty or unset, so it sends a placeholder key (for example `sk-dummy`). Check the `Authorization` header the client really sends |
| A client times out while nigate is still working | The client timeout is shorter than `total_timeout_secs`, or the client's own retries stack on nigate's. Raise the timeout and turn client retries off (see [Using nigate from a client](#using-nigate-from-a-client)) |
| A client keeps failing after one blocked prompt | Check whether the client or its router pauses or switches on `403`, `404` or `429`. A nigate `403 guardrail_blocked` only concerns that one request, and a `404 model_not_found` means a wrong alias |

---

## Glossary

| Term | Meaning |
|---|---|
| `alias` | The model name clients use. Maps to an ordered list of upstreams |
| `upstream` | A real provider endpoint plus model behind an alias |
| `aksi` | "action": `[guardrail.aksi]` sets a per-rule mode |
| `temuan` | findings (guardrail hits) |
| `kejadian` | events |
| `hasil` | outcome (`ok`, `klien`, `limit`, `guardrail`, `upstream`, `gateway`) |
| `klien` | client-side error (for example a 4xx caused by the request) |
| `kelompok` | group (the label for a stats row) |
| `kesehatan` | health (upstream health and cooldown tracking) |
| `cek` | check (`guardrail cek`) |
| `semua` | all (`--per semua`) |
| `jam`, `hari` | hours, days (`--jam`, `--hari`) |
| `perlu_restart` | needs restart (settings a reload could not apply) |
| `dihapus` | deleted |
| `statistik_dibuang` | statistics records dropped |
| `terkonfigurasi` | configured |
| `dalam_cooldown`, `sisa_cooldown_detik` | in cooldown, seconds of cooldown left |
| `gagal_beruntun` | consecutive failures |
| `rpm_efektif`, `tpm_efektif` | effective RPM and TPM (own value, or the default) |
| `token_masuk`, `token_keluar` | input and output tokens |
| `latensi_rata_ms`, `latensi_maks_ms` | mean and maximum latency in milliseconds |
| `rule_kustom` | custom rules |
| `anonim` | anonymous (the key name recorded when `auth.required = false`) |
| `galat` | error |
| `dtk` | seconds (abbreviation used in the dashboard) |

---

## License

MIT. See the `LICENSE` file in the repository root.
