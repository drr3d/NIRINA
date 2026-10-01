# NIRINA add-ons (`add-on` branch)

Optional add-ons for [NIRINA](https://github.com/drr3d/NIRINA), the modular agentic-AI framework.

This branch contains **only add-ons**, laid out at the same relative paths as in the NIRINA core (the `main`
branch). There is nothing to build or install with `pip`: you copy the folders over a checkout of the core, and the
core picks them up through the extension points it already has. The core runs fine without any of this.

## Contents

1. [What is in this branch](#what-is-in-this-branch)
2. [What is not included](#what-is-not-included)
3. [How add-ons work](#how-add-ons-work)
4. [Installation](#installation)
5. [Kelola: the add-on manager](#kelola-the-add-on-manager)
6. [The Add-On tab](#the-add-on-tab)
7. [Add-on: Kamus Entitas](#add-on-kamus-entitas)
8. [Add-on: Voyager Adaptif](#add-on-voyager-adaptif)
9. [Add-on: Konsultasi Pakar (skeleton)](#add-on-konsultasi-pakar-skeleton)
10. [Security and data egress](#security-and-data-egress)
11. [Writing your own add-on](#writing-your-own-add-on)
12. [Testing without a network](#testing-without-a-network)
13. [Known limitations](#known-limitations)
14. [License](#license)

Identifiers in the code are Indonesian, like the core's. A small glossary: `nama` name, `judul` title,
`deskripsi` description, `versi` version, `aktif` enabled, `dipasang` installed, `saat_diubah` when changed,
`kelola` manage, `pengelola` manager, `sidik` fingerprint, `atur` set, `tertunda` pending.

## What is in this branch

| Path | What it is |
|---|---|
| `addons/__init__.py` | Empty. Makes `addons` a Python package. Required. |
| `addons/kelola/` | Add-on manager: lists add-ons, records on/off choices, requests a soft restart. Also called Kelola. |
| `addons/kamus_entitas/` | Entity dictionary: real names stored as salted hashes, used to scan and mask text. |
| `addons/voyager_adaptif/` | Per-task companion that tracks requirements and tool evidence and asks a separate LLM whether the final answer covers them. |
| `addons/konsultasi_pakar/` | Advisor-consultation **skeleton**: a second opinion for stuck investigations, with a pluggable provider. |
| `views/tab6_addons.py` | The Streamlit tab "Add-On" (the UI for Kelola). |
| `LICENSE`, `.gitignore`, `.gitattributes` | Belong to this branch only; do not copy them over the core. |

| Add-on | Default | Own HTTP routes | Needs wiring in your agent code |
|---|---|---|---|
| Kelola | active when installed | yes, see [Kelola](#kelola-the-add-on-manager) | no (the core calls it at startup) |
| Kamus Entitas | library, no switch of its own | no | yes, you call it from your code |
| Voyager Adaptif | off | no | yes, `pendamping_task=` |
| Konsultasi Pakar | off, and inert without a provider | no | yes, `penasihat_task=` plus a control tool |

"Voyager Adaptif" is unrelated to the "Voyager Viz" tab and the skill library of the core, apart from the name.

## What is not included

- No sign-in, login or tenant isolation add-on: nothing that separates users or clients, issues accounts or
  enforces access policies. The core has optional hooks for such things; none ships on this branch.
- No domain-specific add-ons: nothing for a particular industry, company, data source or business process. Those
  belong in your own repositories (see [Writing your own add-on](#writing-your-own-add-on)).
- No model SDK, no credentials and no network code. Konsultasi Pakar and Voyager Adaptif use whatever model
  object your code passes in.
- No test suite. See [Testing without a network](#testing-without-a-network) for how to try things quickly.

## How add-ons work

An add-on is a Python **package** (a folder with `__init__.py`) directly inside `addons/`. Kelola finds add-ons by
looking at the sub-packages of `addons`; nothing is registered anywhere else.

**Discovery.** Every sub-package of `addons` is an add-on, except `kelola` itself and names that do not match
`[a-z][a-z0-9_]{0,63}` (so folders starting with an underscore or containing capitals are ignored). The folder name
is the identity of the add-on. Packages are imported lazily and tolerantly: an add-on that fails to import is listed
as unavailable with a short reason and never breaks listing or startup. A successful import is cached for the life
of the process; a failed one is retried by a later listing after 30 seconds.

**The contract.** Every part is optional. A package with none of these is still listed and can be toggled.

| Part | Where | Meaning |
|---|---|---|
| `ADDON_INFO` | dict in the package `__init__.py` | `{"nama", "judul", "deskripsi", "versi"}`. Missing keys: the title is derived from the folder name, the rest is empty. Text is reduced to one printable line and truncated (title 80, description 300, version 40 characters). `nama` is informational only. |
| `aktif()` and `set_aktif(bool)` | module `addons/<nama>/settings.py` | The add-on owns its enabled flag; Kelola reads and writes it through these. Both must exist. |
| `dipasang()` | callable in the package | Returning `False` marks the add-on unavailable in this process. |
| `saat_diubah(aktif)` | callable in the package | Called best-effort right after Kelola applied a toggle at startup. Exceptions are logged and ignored. |

Without a `settings` module, Kelola keeps the flag itself in `state.json` (default **on**). An add-on can read
that flag with `from addons.kelola import aktif; aktif("<nama>", bawaan=True)`. Nothing forces an add-on to
honour it: it is a convention for add-ons that have no settings of their own.

**Toggles are applied at the next start, never live.** The API and the tab only record the *wanted* value in
`pending.json`. At process start, before the API server is built, `terapkan_startup()` applies and clears the
pending changes. Each list entry therefore carries `aktif_sekarang` (in effect now) and `aktif_diminta` (will be in
effect after the next start).

## Installation

Requirements: a working NIRINA core (Python 3.11 or newer, the core's `requirements.txt`). The add-ons need no
extra packages: they import only the standard library, `langchain_core`, `fastapi`, `streamlit` and the core itself.

```bash
git clone https://github.com/drr3d/NIRINA.git                                  # the core (main)
git clone --depth 1 --branch add-on https://github.com/drr3d/NIRINA.git NIRINA-addon

cp -r NIRINA-addon/addons NIRINA/                  # the add-ons (and the empty addons/__init__.py)
cp NIRINA-addon/views/tab6_addons.py NIRINA/views/ # the Add-On tab (optional, needs addons/kelola)
```

PowerShell:

```powershell
Copy-Item -Recurse NIRINA-addon\addons NIRINA\
Copy-Item NIRINA-addon\views\tab6_addons.py NIRINA\views\
```

Copy only the parts you want. `addons/__init__.py` is always needed, each add-on folder is independent of the
others, and the tab needs `addons/kelola`. If your checkout already has an `addons/` package, copy the individual
sub-folders and keep your own `addons/__init__.py`. Do not copy `LICENSE`, `.gitignore` or `.gitattributes`.

Then start NIRINA the usual way (`python app.py`). There is nothing to configure for the add-ons to be found.

**What the core already does** (so there is nothing to edit in it):

- `app.py` tries `from addons.kelola import terapkan_startup` at bootstrap, before the API server starts. If the
  package `addons` or `addons.kelola` does not exist, this is skipped silently. If Kelola exists but is broken
  (for example a missing dependency), startup fails instead of hiding the problem.
- `streamlit_runner.py` shows the "Add-On" tab when `views.tab6_addons` exists, and a "not available" notice if that
  module exists but fails to load. Without the file the tab is simply absent.
- `core_agent/tools/management.py` provides the management router, authentication, CSRF and audit that Kelola reuses.
  `core_agent/transport/lifecycle.py` provides the drain-and-restart controller.
- `AIBrainProcessor` accepts `pendamping_task=` and `penasihat_task=`, the hooks used by Voyager Adaptif and
  Konsultasi Pakar. The tool name `minta_konsultasi_pakar` is reserved in the core catalogue for trusted sources.

**What the core does not do:** it does not create the companion or the advisor for you. See the add-on sections.

### Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `NIRINA_KELOLA_KUNCI` | core, Kelola | Manager key, at least 24 characters, sent as the header `X-Nirina-Kelola`. Unset or shorter: every change is disabled (403) and the add-on list is refused. |
| `NIRINA_ADMIN_PREFIX` | core, Kelola | Prefix of the admin routes. Default `/admin`. The routes below are written with the default. |
| `NIRINA_DATA_DIR` | core, all add-ons | Runtime data folder. Default `APPDB/` in the checkout; a relative value is resolved from the checkout root. |
| `NIRINA_KAMUS_ENTITAS` | Kamus Entitas | Full path of the dictionary file (default `<data dir>/kamus_entitas.json`). |
| `NIRINA_KATALOG_TOOL_DIR` | core | Moves the folder that holds the audit log (default `<data dir>/katalog_tool`). |
| `AGENT_API_PORT`, `AGENT_API_BASE_URL` | core, Add-On tab | Where the tab finds the API (default `http://127.0.0.1:7000`). |

NIRINA does not load `.env` files by itself; export the variables before `python app.py`.

### Where state lives

All under the data folder (`NIRINA_DATA_DIR`, default `APPDB/`):

| File | Written by | Content |
|---|---|---|
| `addons/pending.json` | Kelola | `{"perubahan": {"<nama>": true}}`. Emptied (`{}`) once applied. |
| `addons/state.json` | Kelola | `{"aktif": {"<nama>": true}}`, only for add-ons without their own settings. |
| `addons/konsultasi_pakar.json` | Konsultasi Pakar | `{"aktif": false, "maks_konsultasi": 2}` |
| `addons/voyager_adaptif.json` | Voyager Adaptif | `{"aktif": false}` |
| `kamus_entitas.json` | Kamus Entitas | Salt and hashes. Treat as secret. |
| `katalog_tool/_audit.jsonl` | core | One JSON line per management action (also the add-on ones). |

## Kelola: the add-on manager

Kelola is the add-on that the core calls at startup. At startup `terapkan_startup()` (1) applies pending toggles and
(2) mounts the routes below on the core management router. Every call is idempotent, and a failure to mount is
logged, never raised. A host that builds its own FastAPI app can call `addons.kelola.pasang(app_or_router)`
itself, and a host that does not use `app.py` must call `terapkan_startup()` before the API server module is
imported.

### Routes

With the default prefix. These routes are hidden from the OpenAPI docs.

| Route | Auth | Purpose |
|---|---|---|
| `GET /admin/addons` | manager | List discovered add-ons. |
| `POST /admin/addons/atur` | manager + CSRF + audit | Record the wanted state of one add-on. Body: exactly `{"nama": str, "aktif": bool, "sidik": str}`. |
| `GET /admin/system/status` | manager | Restart state: `didukung`, `instance_id`, `restart_id`, `pekerjaan_aktif`, `fase`. |
| `POST /admin/system/restart` | manager + CSRF + audit | Ask for a soft restart. Body: exactly `{"instance_id": str}` (taken from the status). |
| `GET /admin/tools/csrf` | manager | Core route that issues the CSRF token (`{"csrf", "kedaluwarsa", "pengelola"}`). |

A list entry looks like this:

```json
{"nama": "konsultasi_pakar", "judul": "Advisor consultation (skeleton)", "deskripsi": "...", "versi": "0.1",
 "tersedia": true, "sumber": "settings", "aktif_sekarang": false, "aktif_diminta": true,
 "perlu_restart": true, "bisa_diatur": true, "alasan": ""}
```

`sumber` is `settings` (the add-on's own flag) or `kelola` (`state.json`). `tersedia` is false when the import
failed or `dipasang()` returned `False`; `alasan` then holds a short, path-free reason. `bisa_diatur` is false for
unavailable add-ons and when the flag cannot be read. The envelope is
`{"status": "ok", "sidik", "addons": [...], "tertunda": <number pending>, "mode_kelola"}`.

`sidik` is a fingerprint of the list. `atur` must send the `sidik` it last saw; if the list changed in between the
answer is 409 and the caller refreshes and retries. `mode_kelola` is `kunci_lokal` (the manager key is set),
`nonaktif` (no key: changes disabled), `kebijakan` (an installed access policy supplies the manager identity) or
`lokal` (a host hook allows local management without a key).

### Authentication, CSRF and audit

Kelola adds no security code of its own: it calls the helpers of `core_agent/tools/management.py`.

- **Manager credential.** Either the identity supplied by an installed access policy, or the local key
  `NIRINA_KELOLA_KUNCI` (24 or more characters) in the header `X-Nirina-Kelola`, compared in constant time. A wrong
  key counts against the caller's address; 5 failures within 300 seconds lock that address out with 429 and
  `Retry-After`. The correct key always passes. A missing key is 401, no key configured on the server is 403.
- **The list needs the credential too.** Unlike `GET /admin/tools`, the add-on list gets no read exemption for
  loopback callers and accepts no host proxy token. In a plain install, a key is required even from
  `127.0.0.1`.
- **CSRF** for every POST: header `X-Nirina-Csrf` with a token from `GET /admin/tools/csrf`. The token is an HMAC
  bound to the manager identity, valid 1800 seconds, and only obtainable with the manager credential.
- **Request shape.** JSON body only (`Content-Type: application/json`), at most 16 KiB, no extra keys.
- **Audit.** Every POST (including rejected ones) is logged to `katalog_tool/_audit.jsonl` as `addon:atur` or
  `system:restart`, without secrets. The action is logged as "in progress" before it runs; if the audit line cannot
  be written, the action is refused (503).

### Example (default port, key exported before NIRINA started)

```bash
H="X-Nirina-Kelola: $NIRINA_KELOLA_KUNCI"
curl -s -H "$H" http://127.0.0.1:7000/admin/addons              # note "sidik"

TOKEN=$(curl -s -H "$H" http://127.0.0.1:7000/admin/tools/csrf \
        | python -c "import sys, json; print(json.load(sys.stdin)['csrf'])")
curl -s -X POST http://127.0.0.1:7000/admin/addons/atur \
     -H "$H" -H "X-Nirina-Csrf: $TOKEN" -H "Content-Type: application/json" \
     -d '{"nama": "konsultasi_pakar", "aktif": true, "sidik": "<sidik from the list>"}'
```

The answer is the refreshed list with `perlu_restart: true` for that add-on. Nothing has changed yet in the running
process.

### Soft restart

`POST /admin/system/restart` does not run any command. It asks the lifecycle controller of the core to hold new work,
let running work drain and let the worker exit with the code that the `python app.py` supervisor treats as "start a
new generation". The new worker applies the pending add-on changes in `terapkan_startup()`. While draining, other
routes answer 503 with `Retry-After: 5`; `/health`, the CSRF route and the add-on list, status and restart routes stay available.

This works **only when NIRINA was started through `python app.py`**. Otherwise `didukung` is false and the restart
is refused with 409; restart the application yourself. An `instance_id` from an older process is also refused
(409), so read the status again first.

## The Add-On tab

`views/tab6_addons.py` adds the sixth tab, "Add-On", to the Streamlit UI (shown with the other developer tabs; absent
when the file is absent). It is a plain consumer of the routes above: it touches no add-on code and no config file.
Its texts are in Indonesian, like the core's.

- One card per add-on: title, version, description, the current state ("Aktif", "Nonaktif", "Tidak tersedia"), a
  toggle "Aktif setelah restart", and a warning while a change waits for a restart.
- An "Terapkan perubahan" panel with the number of pending changes and a **Restart NIRINA** button. It polls the
  status every 5 seconds and explains when restart is not supported (not started via `python app.py`).
- **Credentials.** With the access setup of the host, the signed-in account is used. Otherwise the tab shows a
  password field "Kunci kelola" for `NIRINA_KELOLA_KUNCI`, kept in this browser session only (the key typed in the
  Tools Management tab is reused). *Reading* the list also sends the key from the server's environment if the
  Streamlit process has it, but *changing* anything never uses the environment key: toggles stay disabled until the
  operator types it (or the server is in `lokal` mode). When the server has no key, the list is still refused
  and the tab says so.
- Add-on text is cleaned of control characters and markdown-escaped before display.

## Add-on: Kamus Entitas

**Purpose.** A dictionary of real names (customers, partners, anything you choose) stored as **salted SHA-256
hashes, never as text**. Use it to find names in text or files without writing the names down, to recognise them in
user messages, and to mask text before it is stored or shared.

**Enable.** It is a library: nothing to switch on. It has no `settings` module, so Kelola lists it with the
`kelola` flag (default on) which nothing in this branch reads. What kinds of entities exist and where the names
come from is entirely up to your code.

**Use.**

```python
from addons.kamus_entitas import KamusEntitas, pindai_berkas

kamus = KamusEntitas.dari_nama([("Acme Corp", "customer"), ("Widget Works", "partner")])
kamus.simpan()                          # <data dir>/kamus_entitas.json: salt + hashes only
kamus = KamusEntitas.muat()

kamus.samarkan("Order from AcmeCorp")   # 'Order from [customer]'  (label= for other patterns)
kamus.temukan("acme corp ships")        # [Temuan(awal, akhir, teks, jenis)], longest match first
kamus.jenis("ACME corp")                # 'customer'

# Scan files: the report has the relative path, line and kind, never the name itself.
pindai_berkas(kamus, ".", pola=("*.py", "*.md"), lewati=lambda rel: rel.startswith((".git/", ".venv/")))
```

**Matching.** Lower case, accents removed, all punctuation is a word separator, and the form without spaces is
recognised too ("WidgetWorks", "Widget-Works", "widget works"). Digits that are part of a number ("42,000") are not
the name "42". Names on the `abaikan` list (common words) are not reported by `temukan`/`pindai_berkas` but are
still masked by `samarkan`. Binary files are skipped, unreadable ones are reported as `tidak-terbaca`.

**Configuration.** Dictionary path: `NIRINA_KAMUS_ENTITAS`, else `<data dir>/kamus_entitas.json`. `simpan()` refuses a
path inside the application folder unless it is inside the data folder, so the file does not end up in version
control. Writes are atomic. `muat()` raises `OSError` if the file is missing and `ValueError` if it is corrupt or
from another format version (the current one is `VERSI = 2`); rebuild in that case.

**Routes.** None.

**Needs from the host.** `core_agent.config` (`app_dir`, `data_dir`). Nothing else.

**Limits and security.**

- A hash of a short or common name can be guessed by trying every word. The salt makes precomputed tables useless
  but is stored in the same file. **Keep the dictionary file secret** and out of version control.
- SHA-256 with one salt per dictionary is not a slow password hash.
- Whole-word matching only: typos, abbreviations, transliterations and names split by other words are not found.
- Masking reduces exposure; it is not anonymisation.

## Add-on: Voyager Adaptif

**Purpose.** A per-task companion. For each new task it asks a **separate LLM** to turn the request into up to 8
concrete requirements, records tool results as bounded evidence (status `success`, `error`, `pending` or
`unknown`), and when the model produces a final answer asks the same LLM whether each requirement is covered by
evidence. Statuses are `terpenuhi`, `terbuka` or `belum_diketahui`; a requirement only counts as met if it cites
valid, successful, untruncated evidence ids. The result is diagnostic: the assessor never calls tools. It also
injects a short "task check" note into the main model's prompt each turn, telling it to check requirements against
tool results before answering. The assessment is an LLM guess, not a proof.

**Enable.** Off by default. Switch on in the Add-On tab (applied after restart), or write
`<data dir>/addons/voyager_adaptif.json` as `{"aktif": true}` (exactly this one key; anything else raises
`ValueError`). The flag is read when a new task starts.

**Wire it.** The core does not build the companion; your agent code does:

```python
from addons.voyager_adaptif import buat
# AIBrainProcessor, LLM, tools and system_prompt come from your own agent factory

AI = AIBrainProcessor(
    LLM, tools, system_prompt,
    pendamping_task=buat(assessor_llm, None),     # assessor_llm: any chat model with .invoke(messages)
    # ... your other arguments
)
```

`buat(llm, config_path)`: `config_path` is accepted for call compatibility and ignored. The core calls the
companion with a 10-second limit per step; a late or failing call is logged and the normal flow continues.

**Configuration.** Only the on/off flag. Built-in bounds: goal 6000 characters, 24 evidence items of 1600 characters
each, at most 12 requirements, at most 2 final assessments per task (so at most 3 extra LLM calls per task).

**Routes.** None.

**Needs from the host.** An assessor LLM object; the `pendamping_task` hook; the core modules
`core_agent.llm.format`, `core_agent.runtime.state` and `core_agent.tools.unduhan`; `langchain_core`.

**Limits and security.** The assessor sees the task goal, tool results (cut to 1600 characters each, server file
paths of download tools removed), the draft answer and up to three past-experience descriptions. **No other
redaction is applied.** If the assessor is a remote model, that data leaves your system; use a local model or one
you are allowed to send the data to. Every extra step costs latency and tokens. The per-task note is kept in the agent's
task state, not in a file of its own.

## Add-on: Konsultasi Pakar (skeleton)

**Purpose.** A second opinion for investigations that are stuck. Once per agent turn the core calls the advisor. When
a trigger fires (the agent asks via the control tool `minta_konsultasi_pakar`, the same operational failure
repeats, or evidence reads loop) the advisor builds a **redacted evidence package**, asks a **provider**, validates
the answer against a fixed schema and gives the core a short advice note and tool suggestions. At most
`maks_konsultasi` consultations run per task, and every failure degrades to "continue with the evidence you have".
Advice is a recommendation, never evidence or permission.

This is a **skeleton**: the loop, the data contract, redaction, validation, a mock provider and a generic wrapper
are here; a real expert service is not.

**Files.** `advisor.py` (policy and loop), `contract.py` (package, redaction, schema, validation), `provider.py`
(`ProviderTiruan` mock, optional `ProviderLLM`), `settings.py` (flag and limit), `wiring.py` (tool provider
wrapper), `tool_permintaan.py` (the control tool).

**Enable.** Three layers, all required:

1. The flag is on. It is **off by default**. Use the Add-On tab (applied after restart), or edit
   `<data dir>/addons/konsultasi_pakar.json` (`{"aktif": true, "maks_konsultasi": 2}`; `maks_konsultasi` 0 to 5;
   takes effect for new tasks, no restart), or call `addons.konsultasi_pakar.settings.tulis(aktif=True)`.
2. Your agent code passes a provider. Without one, `buat()` returns an **inert** advisor that never consults and
   never produces advice, even when the flag is on.
3. The control tool is registered and the advisor is wired into the brain.

```python
from addons.konsultasi_pakar import buat, ProviderTiruan, ProviderLLM
from addons.konsultasi_pakar.tool_permintaan import daftarkan_tool
from addons.konsultasi_pakar.wiring import lengkapi_penyedia
# AIBrainProcessor, ToolRegistry, LLM, tools and system_prompt come from your own agent factory

daftarkan_tool()                                  # registers minta_konsultasi_pakar once, idempotent

AI = AIBrainProcessor(
    LLM, tools, system_prompt,
    penyedia_tools=lengkapi_penyedia(ToolRegistry.penyedia_agen(None, ambang=...)),   # optional
    penasihat_task=buat(provider=ProviderTiruan()),                                   # mock: no network
)
```

`lengkapi_penyedia` is only needed when your agents normally see just their own domain's tools, so the advisor's
suggestion of the control tool can actually be bound. Register the tool only if you also pass `penasihat_task`;
otherwise the agent could call a tool that never gets an answer.

**Providers.** A provider is any object with `konsultasi(paket: dict) -> dict` returning the schema below, or a plain
callable.

- `ProviderTiruan(maks_tools=3)`: deterministic mock. No model, no network, no I/O. It suggests candidate tools that
  exist and have not produced a result yet, and labels its output "Simulated". For tests, demos and wiring checks.
- `ProviderLLM(llm, izinkan_data_keluar=True, maks_bytes_kirim=200_000)`: wraps a chat model object **you** pass in
  (anything with `.invoke(messages)`). It refuses to be built without `izinkan_data_keluar=True`. One call at a time;
  packages above the byte cap fail with `konteks_melebihi_batas`. This module opens no connection and bundles no
  model SDK.

Response schema (all keys required, no others): `snapshot` (echoed package fingerprint), `penilaian` (one of `lanjut`,
`tunggu`, `perlu_bukti`, `perlu_tool`, `terhalang`, `belum_pasti`), `referensi_bukti`, `kebutuhan_terbuka`, `tools`
(lists of strings), `langkah`, `batas_kesimpulan`. Strings are at most 3000 characters, lists at most 24 items. Tool
names must exist in the catalogue and evidence ids in the history, or the answer is rejected; at most 5 tools are kept.

**Configuration.** `aktif` (default false) and `maks_konsultasi` (default 2, 0 to 5) in the settings file above.
Unknown keys or wrong types raise `ValueError`, which the advisor treats as "unavailable".

**Routes.** None.

**Needs from the host.** The `penasihat_task` hook of `AIBrainProcessor`; the `ToolRegistry` (for the control tool);
a provider. The core gives the advisor at most 65 seconds per turn.

**Limits and security.** See [Security and data egress](#security-and-data-egress). Beyond that: validation checks
shape and references, not whether the advice is right; provider error text is never stored (it might echo package
data); non-text message content (for example images) makes the package fail rather than be sent.

## Security and data egress

Read this before enabling anything that talks to a model.

- **Konsultasi Pakar sends task data to whatever provider you wire in.** The evidence package contains the task
  goal, the message history since the task started, tool results, the tool catalogue (names, descriptions,
  parameters) and the candidate list. With `ProviderLLM` and a remote model, all of it leaves your system.
  Choose the model consciously (ideally local or contractually approved) and document the decision.
- **It is inert unless you opt in:** off by default, no provider means no consultation, and `ProviderLLM` refuses to
  exist without `izinkan_data_keluar=True`. The bundled mock makes no network call.
- **Redaction is best-effort pattern matching.** It masks values under secret-looking keys (`password`, `passwd`,
  `secret`, `authorization`, `cookie`, `api_key`, `access_token`, `refresh_token`, `token`), `key=value`, header and
  cookie shapes, and credentials in URLs, replacing them with `[disamarkan]`, including inside JSON strings. It cannot
  recognise personal data, business figures, free-text secrets, encoded or split values. **It is not a guarantee.**
  Server file paths of download results are removed by the core before the package is built.
- **Voyager Adaptif** sends goals, truncated tool results and draft answers to its assessor LLM with no redaction (see
  its section).
- **Kamus Entitas** stores no names but its file is a guessing target; keep it private.
- **Add-on code is trusted code.** Listing add-ons imports every sub-package of `addons/`, and settings modules are
  imported too. Only put code you trust there. Importing runs its top-level code in the NIRINA process.
- **The management surface is guarded** by the manager key, CSRF, rate limiting and an audit log (see
  [Kelola](#kelola-the-add-on-manager)). Use a long random `NIRINA_KELOLA_KUNCI`, and put the API behind TLS if it is
  reachable from other machines; the key travels in a header.
- **The restart route runs no command.** The supervisor of `python app.py` decides what to start.
- Add-on state files hold no secrets (flags and limits), except the dictionary file described above.

## Writing your own add-on

Create a package under `addons/`. The smallest useful add-on:

```
addons/halo/__init__.py
addons/halo/settings.py        # optional
```

`addons/halo/__init__.py`:

```python
ADDON_INFO = {"nama": "halo", "judul": "Hello", "deskripsi": "Minimal example add-on.", "versi": "0.1"}


def dipasang():                 # optional: return False to show as "not installed in this process"
    return True


def saat_diubah(aktif):         # optional: called once at startup after Kelola applied a toggle
    print("halo is now", "on" if aktif else "off")
```

`addons/halo/settings.py` (optional; without it Kelola keeps the flag in `state.json`):

```python
import json

from core_agent.config import data_dir

_PATH = data_dir / "addons" / "halo.json"


def aktif():
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))["aktif"] is True
    except FileNotFoundError:
        return False


def set_aktif(nilai):
    from core_agent.storage.atomic import tulis_json_atomik     # atomic write helper of the core
    tulis_json_atomik(_PATH, {"aktif": bool(nilai)})
```

Restart NIRINA and "Hello" appears in the tab and in `GET /admin/addons`. Guidelines:

- Import cheaply. The Streamlit process imports `addons.kelola`, and listing imports every add-on. Defer heavy or
  core-only imports into functions, as `konsultasi_pakar/settings.py` does.
- Keep the flag in one place and read it at the start of a task (or process), not on every call.
- Never put secrets or real names in `ADDON_INFO`, state files or log lines.
- Anything that sends data out must be opt-in and say so in its description.
- Hooks into the agent (`pendamping_task`, `penasihat_task`, tools) are wired by your own agent code; Kelola only
  manages the on/off flag.

## Testing without a network

Nothing below needs a model, a network or a running server. Use a throw-away data folder so no real state is touched,
and run from the root of the core checkout that contains the copied add-ons.

```bash
export NIRINA_DATA_DIR="$(python -c 'import tempfile; print(tempfile.mkdtemp())')"
# PowerShell: $env:NIRINA_DATA_DIR = python -c "import tempfile; print(tempfile.mkdtemp())"
export NIRINA_KELOLA_KUNCI=0123456789abcdef0123456789abcdef
```

**Kelola routes in-process** (FastAPI's in-process test harness, no server):

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient
import addons.kelola as kelola
from core_agent.tools import management

kelola.terapkan_startup()                        # mounts the routes on management.router
app = FastAPI(); app.include_router(management.router)
c = TestClient(app)
H = {"X-Nirina-Kelola": "0123456789abcdef0123456789abcdef"}

lst = c.get("/admin/addons", headers=H).json()
tok = c.get("/admin/tools/csrf", headers=H).json()["csrf"]
r = c.post("/admin/addons/atur", headers={**H, "X-Nirina-Csrf": tok},
           json={"nama": "konsultasi_pakar", "aktif": True, "sidik": lst["sidik"]})
print(r.status_code, r.json()["tertunda"])       # 200 1
kelola.pengelola().terapkan_startup()            # what the next start does; writes konsultasi_pakar.json
```

**Konsultasi Pakar with the mock provider:**

```python
import json
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from addons.konsultasi_pakar import PenasihatPakar, ProviderTiruan
from addons.konsultasi_pakar.contract import TOOL_PERMINTAAN

msgs = [HumanMessage(content="Why is the report empty?", id="h1"),
        AIMessage(content="", tool_calls=[{"name": TOOL_PERMINTAAN, "args": {"pertanyaan": "stuck"}, "id": "c1"}]),
        ToolMessage(content=json.dumps({"status": "permintaan_pakar"}), name=TOOL_PERMINTAAN, tool_call_id="c1")]
katalog = [{"name": TOOL_PERMINTAAN, "description": "ask", "parameters": {}},
           {"name": "cek_laporan", "description": "check the report", "parameters": {}}]
out = PenasihatPakar(True, ProviderTiruan()).tinjau(
    None, task_id="h1", tujuan="Why is the report empty?", messages=msgs, konteks={}, task_baru=True,
    katalog=katalog, kandidat=[{"name": "cek_laporan", "asal": ["manual"]}], versi_katalog="v1", catatan_task=None)
print(out["catatan"]["status"], out["tools_saran"])      # ok ['minta_konsultasi_pakar', 'cek_laporan']
```

**Voyager Adaptif with a fake model:**

```python
import json
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from addons.voyager_adaptif import buat, settings

settings.set_aktif(True)

class FakeLLM:
    def invoke(self, messages):
        if "Rumuskan" in messages[0].content:                      # first call: requirements
            return AIMessage(content=json.dumps({"kebutuhan": ["Report the total"]}))
        return AIMessage(content=json.dumps({"kebutuhan": [{"id": "K1", "status": "terpenuhi",
                                                           "bukti": ["t1"], "alasan": "ok"}],
                                              "kecocokan_memori": []}))

p = buat(FakeLLM(), None)
msgs = [HumanMessage(content="Total?", id="task1"),
        ToolMessage(content='{"status":"ok","total":5}', name="sales", tool_call_id="t1")]
cat = p.siapkan(None, task_id="task1", tujuan="Total?", messages=msgs, konteks={}, task_baru=True)
cat, extra = p.konteks(cat, [])
print(p.selesai(cat, AIMessage(content="The total is 5."))["pemeriksaan"])   # status: dinilai_lengkap
```

**Kamus Entitas** needs no model at all; use the example in its section. To test your own add-on, point a
`PengelolaAddon(folder=<temp dir>, paket="<your package>")` (from `addons.kelola.store`) at it and call `daftar()`,
`atur(nama, aktif, sidik)` and `terapkan_startup()`.

## Known limitations

- **Restart needs the supervisor.** The restart button and route work only when NIRINA was started with
  `python app.py`. Otherwise restart the application by hand for toggles to apply.
- **Toggles are never live.** A choice only takes effect at the next start. Exception: the Konsultasi Pakar and
  Voyager Adaptif settings files can be edited directly and are re-read when a new task starts.
- **An add-on that fails to import cannot be toggled.** It is listed as unavailable with a reason and `atur` answers
  409. Fix it and restart (or list again after 30 seconds). A successful import is cached, so edited add-on code needs
  a restart too.
- **The list needs the manager credential**, including from loopback. With no `NIRINA_KELOLA_KUNCI` on the server
  the tab shows an explanation and no add-ons.
- **Kelola's own flag is advisory.** For add-ons without a `settings` module (Kamus Entitas here) it is recorded but only
  matters if the add-on reads it.
- **Voyager Adaptif and Konsultasi Pakar are not self-wiring.** Until your agent code passes the hook, enabling them
  does nothing. Konsultasi Pakar additionally needs a provider.
- **Konsultasi Pakar is a skeleton.** The mock gives simulated advice; real advice quality depends on the provider
  you supply. Redaction is best-effort.
- **Names are strict:** `[a-z][a-z0-9_]{0,63}`, and `kelola` is reserved.
- **The tab and the API messages are in Indonesian.** Text you put in `ADDON_INFO` is shown as written.
- **No automated tests ship on this branch.**

## License

MIT. See [LICENSE](LICENSE).
