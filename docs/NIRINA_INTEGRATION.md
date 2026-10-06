# Using nigate with NIRINA

This guide connects a running NIRINA chatbot to a running nigate gateway, one LLM slot at a time, **without changing any NIRINA code**. Everything is done in nigate's config and on NIRINA's *Kelola LLM* (Manage LLM) page, plus one environment variable.

It was written from a real integration: the `chat` slot and the `analitik` slot were moved behind the gateway and verified end to end (connection test, live statistics, failover when the free OpenRouter tier answered `429`).

**Contents**

1. [How NIRINA chooses an LLM](#1-how-nirina-chooses-an-llm)
2. [Before you start](#2-before-you-start)
3. [Step 1: configure the gateway](#3-step-1-configure-the-gateway)
4. [Step 2: configure NIRINA (Kelola LLM)](#4-step-2-configure-nirina-kelola-llm)
5. [Step 3: verify](#5-step-3-verify)
6. [Recommended rollout](#6-recommended-rollout)
7. [Timeouts and retries](#7-timeouts-and-retries)
8. [Failure behaviour, and why there is no direct fallback](#8-failure-behaviour-and-why-there-is-no-direct-fallback)
9. [The guardrail and NIRINA's data](#9-the-guardrail-and-nirinas-data)
10. [Observability](#10-observability)
11. [Rollback](#11-rollback)
12. [Operations](#12-operations)
13. [Troubleshooting](#13-troubleshooting)
14. [Checklist](#14-checklist)

---

## 1. How NIRINA chooses an LLM

NIRINA's LLM settings have three layers, edited on the *Kelola LLM* page (a button in the Streamlit sidebar, which opens `/teams/chatbot/llm/`). Every change is saved as a numbered **version** (`v0001`, `v0002`, ...) and applies from the **next chat message**, with no restart. Older versions stay available under the *Riwayat* (history) tab.

```
Profile  ───────►  Chain  ───────►  Slot
one model +        ordered list      which agent uses
its endpoint       of profiles       which chain
                   (fallback order)
```

- **Profile** (*Profil model*): one model plus how to reach it: provider, model name, base URL, the **name** of the environment variable that holds the API key (the key itself is never stored), temperature, timeout, retries, max tokens.
- **Chain** (*Rantai failover*): an ordered list of profiles. NIRINA's router tries them in order and moves to the next one when a call fails.
- **Slot** (*Slot agen*): a point in the bot where an LLM is needed. Each slot uses exactly one chain.

The deployment this guide was written against (`v0001`, the default) looked like this:

| Slot | Needs tools | Chain | Profiles in the chain, in order |
|---|---|---|---|
| `umum`, `analitik`, `data`, `rcs`, `penilai_voyager` (penilai_voyager runs without tools) | yes (except `penilai_voyager`) | `utama` | `main` (OpenRouter `qwen/qwen3.8-27b:free`), `fallback` (Cerebras `qwen-3.8-27b`), `fallback2` (Cerebras `gpt-oss-120b`) |
| `chat` (conversation gate: small talk and off-topic) | no | `obrolan` | `chat` (Cerebras `gpt-oss-120b`), `chat_fallback` (Cerebras `qwen-3.8-27b`) |
| `automation_extract` | no | `otomasi` | `automation_extract` (local Ollama `qwen2.5:3b`) |

Your own versions may differ. Open *Slot agen* to see what you have.

### Two rules that shape this whole guide

1. **A profile's "verified" status comes from its model name.** NIRINA marks a profile *terverifikasi* (verified) when the model name is recognised by one of its output-format handlers. A slot that needs tools **rejects** unverified profiles; a slot without tools accepts them only after you confirm. The format handlers also depend on the model name (they normalise reasoning output and tool calls).
   **Consequence: the gateway aliases you give NIRINA must be the real model names**, for example `gpt-oss-120b` and `qwen/qwen3.8-27b:free`. An alias such as `nirina-chat` works for a connection test but shows a red "not verified" dot and cannot be used on tool slots.
2. **NIRINA's router moves to the next profile on any error** (see [section 8](#8-failure-behaviour-and-why-there-is-no-direct-fallback)). So never place a direct-to-provider profile behind a gateway profile in the same chain.

---

## 2. Before you start

You need:

- A running nigate with at least one provider key in its environment, and its **admin token** if you want the dashboard. See the [README](../README.md) quick start.
- A running NIRINA (`python app.py`) and access to its *Kelola LLM* page.
- The ability to set an environment variable **in the same shell that starts NIRINA**. NIRINA reads the gateway key from its process environment when it builds a profile, so a variable set in another window, or after NIRINA started, is not seen until NIRINA is restarted.

Addresses (the gateway's data port is `4000` by default):

| NIRINA runs... | Gateway runs... | Base URL to put in the profile |
|---|---|---|
| directly on the host | directly on the host, or in a container with port 4000 published | `http://127.0.0.1:4000/v1` |
| in a container | on the host | `http://host.docker.internal:4000/v1` (on Linux add `extra_hosts: host.docker.internal:host-gateway`), and bind the gateway to an address the container can reach (`server.listen`) |
| in a container | in a container on the same Docker network | `http://<gateway-service-name>:4000/v1` |

The base URL must end in `/v1`.

---

## 3. Step 1: configure the gateway

### 3.1 Aliases named after the real models

Add one `[[model]]` per model NIRINA uses, with the **real model name as the alias**. For the default NIRINA chains:

```toml
[resilience]
max_retries = 0            # NIRINA's chain already provides fallback; avoid stacking retries
total_timeout_secs = 95    # must be >= the largest upstream timeout below

# Matches NIRINA profile `main`
[[model]]
alias = "qwen/qwen3.8-27b:free"
  [[model.upstream]]
  name = "openrouter-qwen"
  base_url = "https://openrouter.ai/api/v1"
  model = "qwen/qwen3.8-27b:free"
  api_key_env = "OPENROUTER_API_KEY"
  timeout_secs = 90

# Matches NIRINA profiles `fallback` and `chat_fallback`
[[model]]
alias = "qwen-3.8-27b"
  [[model.upstream]]
  name = "cerebras-qwen"
  base_url = "https://api.cerebras.ai/v1"
  model = "qwen-3.8-27b"
  api_key_env = "CEREBRAS_API_KEY"
  timeout_secs = 30

# Matches NIRINA profiles `fallback2` and `chat`
[[model]]
alias = "gpt-oss-120b"
  [[model.upstream]]
  name = "cerebras-gptoss"
  base_url = "https://api.cerebras.ai/v1"
  model = "gpt-oss-120b"
  api_key_env = "CEREBRAS_API_KEY"
  timeout_secs = 60
```

Provider keys are read from the gateway's environment (`OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`). If the gateway runs in a container, make sure those variables are passed into the container.

The `[resilience]` values and the per-upstream `timeout_secs` interact with NIRINA's profile timeouts. See [section 7](#7-timeouts-and-retries).

### 3.2 Apply the config

Restart the gateway, or, if it is already running and you only changed models or upstreams, call `POST /admin/reload` (or press the button in the dashboard's *Konfigurasi* tab). A reload re-reads the config file but **not** the process environment, so a newly added provider key variable needs a restart of the gateway.

Check that the upstreams are usable:

```bash
curl -H "Authorization: Bearer $NIGATE_ADMIN_TOKEN" http://127.0.0.1:4001/admin/upstreams
# every upstream should show  "terkonfigurasi": true
```

`terkonfigurasi: false` means the provider key variable named by `api_key_env` is empty in the gateway's environment.

### 3.3 Create a key for NIRINA

```bash
nigate -c nigate.toml key create nirina-dev      # prints ngk_..., shown once
```

Use **one key per NIRINA environment** (dev, production) so limits and statistics stay separate. Keys created from the CLI become valid on a running gateway within about 2 seconds.

If you want a per-key limit, set it now, for example `nigate -c nigate.toml key limit nirina-dev --rpm 120`. Think about NIRINA's behaviour: one user question can trigger several LLM calls (agent steps, tool loops), so leave generous headroom.

### 3.4 Give the key to NIRINA

In the shell that will start NIRINA, set the variable **before** `python app.py`. The variable name is up to you; this guide uses `NIGATE_KEY`.

```
:: Windows cmd, reading the key from a file
set /p NIGATE_KEY=<path\to\key-file.txt
python app.py
```

```bash
# POSIX shell
export NIGATE_KEY="ngk_..."
python app.py
```

Verify it before starting NIRINA. Print only the first characters, never the whole key:

```
echo %NIGATE_KEY:~0,8%          :: Windows cmd, expect something like ngk_4f83
echo "${NIGATE_KEY:0:8}"        # POSIX
```

---

## 4. Step 2: configure NIRINA (Kelola LLM)

Open *Kelola LLM* from the Streamlit sidebar. The page has the tabs *Slot agen*, *Rantai failover*, *Profil model*, *Provider* and *Riwayat*. Do the steps in this order, because each layer refers to the one before it. (Button and field labels may differ slightly between NIRINA versions; the field meanings are the same.)

### 4.1 Create the profiles (tab *Profil model*)

In the *Profil baru* box choose the provider **"OpenAI / kompatibel OpenAI (OpenRouter, vLLM, LiteLLM)"**, type a profile name and press *Buat profil*. Then fill the new card in:

| Field | Value | Why |
|---|---|---|
| Nama model | the **exact alias** from the gateway, e.g. `gpt-oss-120b` | determines "verified" status and the format handler |
| Base URL | `http://127.0.0.1:4000/v1` (see [section 2](#2-before-you-start)) | points NIRINA at the gateway |
| Env API key | `NIGATE_KEY` | the **name** of the variable, not the key |
| Temperature | `0.3` | same as the profile you replace (blank means the default, 0.3) |
| Timeout (detik) | the matching value from [section 7](#7-timeouts-and-retries) | must exceed the gateway's upstream timeout |
| Retry | **`0`** (type the number; do not leave it blank) | a blank field means the OpenAI client's default of 2 automatic retries |
| Maks token jawaban | same as the profile you replace | NIRINA's `main`/`fallback`/`fallback2` use `8192`; `chat` uses `300` |
| Reasoning | leave unchecked | not used for the `openai` provider |

Create one profile per model. For the `analitik` slot used in this guide:

| Profile name | Model name (exact) | Timeout | Max tokens |
|---|---|---|---|
| `gw_main` | `qwen/qwen3.8-27b:free` | 95 | 8192 |
| `gw_fallback` | `qwen-3.8-27b` | 35 | 8192 |
| `gw_fallback2` | `gpt-oss-120b` | 65 | 8192 |

All three use provider `openai`, Base URL `http://127.0.0.1:4000/v1`, Env API key `NIGATE_KEY`, Temperature `0.3`, Retry `0`.

After saving, each card should show a green **"terverifikasi"** badge. A red badge means the model name was not recognised: re-check the alias spelling on both sides.

You can press **Tes koneksi** (connection test) on a profile. It sends one small request through the gateway. A success line such as *"Tersambung · 0,59 detik · model ...· jawaban: ok"* proves the whole path (NIRINA, gateway, provider). It also appears in the gateway's statistics.

### 4.2 Create the chain (tab *Rantai failover*)

Create a new chain, for example `analitik_nigate`, and add the profiles in this order: `gw_main`, then `gw_fallback`, then `gw_fallback2`. Leave *Maks token* and *Min token* (the infinity symbols) empty, and save.

This mirrors the order of the existing `utama` chain exactly, so behaviour stays the same; only the route changes.

Until a chain is assigned to a slot, its profiles show **"belum dipakai rantai mana pun"** (not used by any chain) and the chain shows **"tidak dipakai slot mana pun"** (not used by any slot). That is expected at this point.

### 4.3 Assign the chain to a slot (tab *Slot agen*)

Change the slot (here `analitik`) from its current chain (`utama`) to `analitik_nigate` and save. **Move one slot only.** Leave the others on their current chains. The new version (for example `v0008`) becomes active immediately.

You can confirm on disk (read-only): `APPDB/llm/aktif.json` names the active version, and `APPDB/llm/versi/vNNNN.json` contains the profiles, chains and slot map.

---

## 5. Step 3: verify

1. **Which message uses which slot.** The `chat` slot is only called when NIRINA's conversation gate routes a message to the small-talk node *and* the message is not answered by a canned template. Greetings ("halo", "apa kabar?") and off-topic questions are often answered by built-in canned replies **without any LLM call**, so they never reach the gateway. Questions that need data (for example *"cek data pengunjung untuk brand X"*) run through the tool slots (`analitik`, `data`, ...) and generate real traffic. To verify a tool slot, ask a data question.
2. **Gateway statistics:**
   ```bash
   nigate -c nigate.toml stats --jam 1 --per alias
   ```
   The alias row (for example `qwen-3.8-27b`) should count the request and show token totals. The dashboard's *Ringkasan* tab shows the same.
3. **NIRINA's log** (`logs/nirina.log`) shows the profile being built, and the router trying each entry:
   ```
   [BUAT_LLM] Peran 'gw_main' -> provider='openai' model='qwen/qwen3.8-27b:free' timeout=95s retry=0 ...
   [ROUTER] Coba 'openai-qwen/qwen3.8-27b:free' ...
   ```
4. **Gateway log** shows upstream problems and failover, for example `upstream gagal ... sebab=HTTP 429 cooldown_dtk=30`.

A normal result when the first profile points at a free OpenRouter model: the first call is answered `429` by OpenRouter, NIRINA moves to the next profile (Cerebras) and still answers, and the dashboard's *Upstream* tab shows OpenRouter cooling down for 30 seconds. This is the same behaviour NIRINA had before, now visible in one place.

---

## 6. Recommended rollout

1. **Prove the path on a low-risk slot.** Press *Tes koneksi* on a profile (it needs no slot at all). Optionally move the `chat` slot, which has no tools and little traffic.
2. **Move one busy slot**, for example `analitik`, and watch it for a few days: error rate, latency and the *temuan* (guardrail findings) column in the dashboard, plus whether answers feel the same.
3. **Move the others** (`data`, `umum`, `rcs`, `penilai_voyager`) one at a time. They can reuse the same `gw_*` profiles: just create a chain that lists them and point the slot at it. No new profiles are needed.
4. Keep the previous versions in *Riwayat* until you are confident.

Do **not** switch everything in one version. A single bad setting would then affect every slot at once.

---

## 7. Timeouts and retries

Two timers are in play: NIRINA's **profile timeout** (how long NIRINA waits for the gateway) and the gateway's **upstream timeout** and **total budget** (how long the gateway waits for the provider).

**Rule: the profile timeout must be larger than the gateway's worst case for that alias.** Otherwise NIRINA gives up first, treats the call as failed, moves to the next profile, and meanwhile the gateway is still working on the first request: the prompt is processed twice and may be billed twice.

With `max_retries = 0` on the gateway, the worst case for an alias with a single upstream is that upstream's `timeout_secs`. The numbers in this guide:

| Alias | Gateway upstream `timeout_secs` | NIRINA profile timeout (`> upstream`) |
|---|---|---|
| `qwen/qwen3.8-27b:free` | 90 | 95 |
| `qwen-3.8-27b` | 30 | 35 |
| `gpt-oss-120b` | 60 | 65 |

`resilience.total_timeout_secs` must be at least the largest upstream timeout (here 95 covers 90). If you give an alias several upstreams, or enable gateway retries, the worst case grows: the gateway stops at `total_timeout_secs`, so keep the profile timeout above that value.

**Retries.** Set `Retry = 0` on every gateway profile. The OpenAI client NIRINA uses retries automatically on `408`, `409`, `429` and `5xx`, and honours `Retry-After`. A `429` from the gateway's own rate limiter would then be waited out and repeated by the client instead of being handled by NIRINA's chain. Retries and failover are done once, in the gateway (and by NIRINA's chain between profiles).

---

## 8. Failure behaviour, and why there is no direct fallback

NIRINA's router (`DynamicTokenRouterLLM`) tries the chain entries in order. For each failure it classifies the error by category, may open a short **circuit breaker** on that entry, and **continues to the next entry**. Observed classification of what the gateway can return:

| Gateway response | What it really means | NIRINA classifies it as | Effect |
|---|---|---|---|
| no response (connection refused, timeout) | the gateway is down | connection / timeout | next entry; breaker opens for 30 s |
| `429 rate_limit_exceeded` (the key's limit) | policy: the key is over its limit | rate limit | next entry; breaker opens 30 s |
| `403 guardrail_blocked` (secret in the prompt, `block` mode) | policy: do not send this | auth (401/403) | next entry; breaker opens **300 s** |
| `502 guardrail_blocked` / `502 upstream_*` (for example `upstream_invalid_response`, `upstream_redirect`) | the gateway blocked or exhausted its providers | server (5xx) | next entry |
| `502 upstream_auth_failed` | the gateway's own provider key was rejected (operator problem) | server (5xx) | next entry |
| `429` forwarded from a provider (for example OpenRouter free) | the provider is rate limiting | rate limit | next entry (this is the normal, desired failover) |

The breaker defaults come from NIRINA's environment: `NIRINA_LLM_JEDA_PUTUS` (30 s), `NIRINA_LLM_JEDA_PUTUS_AUTH` (300 s), `NIRINA_LLM_GAGAL_BERUNTUN` (2 consecutive server or connection failures before opening; timeouts, rate limits and auth errors open it immediately).

### What this means for you

- **Chains with only gateway profiles are fail-closed**: if the gateway is down, the slot fails until the gateway is back. That is the safe configuration and the one this guide uses.
- **Do not add a direct-to-provider profile behind a gateway profile.** Because the router continues on *any* error, a `403 guardrail_blocked` (the gateway refusing a prompt that contains a secret) or a `429` (the key being over its limit) would be retried **directly at the provider**: the secret is sent anyway and the limit is bypassed, and one blocked prompt would also open the gateway's breaker for 300 s so that all traffic goes direct and unfiltered for five minutes.
- **Fail-open, if you want it, needs a small change in NIRINA's router.** Proposed design (not implemented):
  1. Mark gateway profiles with a flag, for example `via_gateway: true`.
  2. In the router's error handler: if an entry is `via_gateway` and the error carries an HTTP status, **stop** and surface the gateway's decision (do not record a breaker failure, do not continue to the next entry). Fall through to the next entry only when there was **no HTTP response at all** (connection error or timeout).
  3. When a non-gateway entry then serves the request, log a clear warning such as *"gateway unavailable: request served DIRECTLY by '<name>' WITHOUT the secret filter and the gateway's key limits"*, throttled to once per minute, and mark the router trace.
  4. Tests: connection error falls through; `403`/`429`/`502` from the gateway raise without calling the direct entry and leave the breaker closed; the flag survives `bind_tools`.
  While this is unavailable, a fail-open chain would silently bypass the protections the gateway exists to provide.

---

## 9. The guardrail and NIRINA's data

By default the gateway runs in `redact` mode in both directions. It scans message `content` of **every role, including tool results**, and tool-call arguments. NIRINA's data tools return database rows, so tool output flows through the filter before the model sees it.

- Anything in a tool result that matches a rule (for example `password=...`, `ghp_...`, a JWT) becomes `[REDACTED:<rule>]`.
- High-entropy tokens of at least 32 characters mixing at least two of lower case, upper case and digits (for example a base64 identifier) are also replaced. Pure hexadecimal hashes and commit ids are not flagged at the default threshold.
- If a legitimate long identifier is redacted, the model reads `[REDACTED:high_entropy]` instead of the value and the answer can degrade.

Practical advice:

1. After moving a slot, watch the **temuan** (findings) column in the dashboard and the *Guardrail* tab (rule names and counts only; never the matched text). Zero findings on normal data questions is the expected, healthy result. In the verified setup the data queries produced no findings.
2. Test realistic tool output offline: `echo "<sample tool output>" | nigate -c nigate.toml guardrail cek` prints the findings and the text after redaction.
3. Tune if needed: raise `guardrail.entropy_threshold`, raise `entropy_min_length`, set `entropy = false`, or give a noisy rule `log_only` through `[guardrail.aksi]`. Rule changes apply through `POST /admin/reload`.
4. `block` mode is stricter: the gateway refuses the whole request (`403`). Combined with NIRINA's router behaviour above, that is only safe on chains without a direct fallback.

---

## 10. Observability

| Where | What you see |
|---|---|
| Dashboard *Ringkasan* | request totals, outcome mix, latency, tokens, per key / alias / upstream |
| Dashboard *Upstream* | each upstream: healthy, cooling down (seconds left) or not configured |
| Dashboard *Guardrail* | active settings and recent findings |
| `nigate stats --jam N --per alias` | the same numbers in a terminal |
| Gateway log | failover (`upstream gagal ... sebab=...`), guardrail findings (rule names and counts), rate limits |
| NIRINA `logs/nirina.log` | `[BUAT_LLM]` when a profile is built, `[ROUTER] Coba '<entry>'` per attempt, `[LLM] '<entry>' gagal (<category>)` on failures |
| NIRINA `logs/agent_events.jsonl` | one router trace per turn: which entry was used and which failed |

Gateway response headers (visible to any HTTP client; NIRINA's SDK does not surface them) are `x-nigate-upstream` and `x-nigate-attempts`.

---

## 11. Rollback

- **Per slot, the safe way:** on *Kelola LLM* open *Riwayat* and activate the previous version (for example `v0001`). It applies from the next message. Do this **before** stopping the gateway: a slot pointed at a gateway-only chain fails while the gateway is down.
- **Per chain:** point the slot back at its original chain (`utama`, `obrolan`, ...) in *Slot agen*. The old chains and profiles are untouched; the `gw_*` ones simply stay unused.
- **Emergency:** if the gateway is unavailable and a slot must work now, activate the old version in *Riwayat*.

---

## 12. Operations

- **Start order:** gateway first, then NIRINA. If NIRINA starts without `NIGATE_KEY` set, NIRINA logs that the profile's environment variable is empty and skips that profile.
- **Rotate the NIRINA key:** create a new key (`key create`), set the new value in NIRINA's environment, restart NIRINA (the variable is read when profiles are built), then revoke the old key (`key revoke`). Revocation on a running gateway takes effect within about 2 seconds.
- **Add or change a model:** add or edit the `[[model]]` block with the real model name, reload the gateway, then create or edit the NIRINA profile and chain.
- **Change a provider key:** change the variable in the gateway's environment and restart the gateway (a reload does not re-read the environment).
- **Per-key limits:** `key limit nirina-dev --rpm N --tpm N`. Limits and gateway health are in memory and reset when the gateway restarts.
- **Streaming:** not supported by the gateway. NIRINA calls the LLM without streaming, so this does not affect it today.
- **Plain HTTP:** the data port has no TLS. Keep NIRINA and the gateway on the same host or private network, or terminate TLS in front of the gateway.

---

## 13. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Profile card shows a **red** "belum terverifikasi" dot, or a tool slot refuses the profile | The model name is not recognised. Use the real model name as the gateway alias and as *Nama model* (section 1) |
| Slot refuses to save a profile for a tool slot | Same: unverified profiles are rejected on tool slots. Fix the model name |
| *Tes koneksi* fails with `401` | `NIGATE_KEY` is not set in NIRINA's environment, was set after NIRINA started, or the key is wrong or revoked. Check `echo %NIGATE_KEY:~0,8%`, set it in the same shell, restart NIRINA |
| NIRINA logs that the profile's environment variable is empty | The variable named in *Env API key* is unset in NIRINA's process. Set it and restart NIRINA |
| `404 model_not_found` | *Nama model* does not exactly match a gateway alias (check spelling, `/`, `:`). `GET /v1/models` lists the aliases |
| `503 upstream_not_configured` | The provider key variable for every upstream of that alias is empty in the gateway's environment. Check `GET /admin/upstreams` and restart the gateway with the variable set |
| *Tes koneksi* works but nothing appears in the statistics for chat | The `chat` slot is rarely called (canned replies, section 5). Test with a data question on a tool slot |
| Answers arrive but the gateway shows no traffic | The slot still points at the old chain, or NIRINA was started in a shell without the variable. Check the slot assignment and the active version |
| Frequent `429` on the first profile | Free OpenRouter tier limit, not the gateway. NIRINA moves to the next profile. Expected |
| A slot fails when the gateway is stopped | By design (fail-closed). Roll back first, or start the gateway ([section 11](#11-rollback)) |
| Requests seem to run twice | NIRINA's profile timeout is shorter than the gateway's worst case, or *Retry* is blank (default 2 retries). Section 7 |
| Empty or odd answers from `gpt-oss-120b` | Check that the profile uses the exact model name so the right format handler applies, and try *Tes koneksi* |
| One blocked prompt and everything slows or goes direct | A direct profile sits behind the gateway profile and the breaker opened. Remove the direct entry (section 8) |
| Answers lose identifiers or codes | The guardrail redacted legitimate high-entropy values. Inspect the *Guardrail* tab and tune it (section 9) |
| NIRINA reports it cannot reach the gateway | Wrong Base URL for where NIRINA runs (section 2), the gateway is not running, or the port is not published |

---

## 14. Checklist

Gateway
- [ ] Aliases use the real model names; `total_timeout_secs` covers the largest upstream timeout
- [ ] Provider key variables are set in the gateway's environment; `GET /admin/upstreams` shows every upstream `terkonfigurasi: true`
- [ ] A key exists for NIRINA (`key create`)

NIRINA
- [ ] `NIGATE_KEY` set in the shell that starts NIRINA (verified with the first 8 characters)
- [ ] Profiles: provider `openai`, exact model names, Base URL ending in `/v1`, Env API key `NIGATE_KEY`, Retry `0`, timeouts larger than the gateway's upstream timeouts; all show the green verified badge
- [ ] A chain with only gateway profiles, assigned to **one** slot; version saved and active
- [ ] *Tes koneksi* succeeded and the request appears in the gateway's statistics

After go-live
- [ ] A real data question produced traffic for the right alias
- [ ] The *temuan* column stays near zero on normal data
- [ ] You know how to roll back (*Riwayat*) and you did it once on purpose
