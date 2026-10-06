# Running nigate under a setup platform

nigate can run as a **team** of a setup platform: a control plane that keeps settings for each team in a web form,
writes them to a JSON file that the team's containers read, starts and stops the team's services, and serves the team's
pages behind its own login through an nginx reverse proxy. Under such a platform:

- provider keys, the admin token and the log level come from the platform's settings form instead of `.env`;
- the dashboard is opened through the platform login, with no separate password and no token field.

Without a platform nothing changes: nigate reads environment variables exactly as before.

## The platform config file

The platform writes a JSON file for the team. nigate reads it from `NIGATE_PLATFORM_CONFIG` (default
`/platform/config.json`):

```json
{
  "schema_version": 1,
  "settings": {
    "NIGATE_ADMIN_TOKEN": "<at least 24 characters>",
    "CEREBRAS_API_KEY": "<key>",
    "RUST_LOG": "info"
  },
  "platform": {"proxy_token": "<random value the proxy adds to each request>"}
}
```

Mount the **folder** that holds the file, read-only, rather than the file itself: platforms usually replace the file
atomically, and a single-file bind mount would keep showing the old content.

| Setting | Used by | When it takes effect |
|---|---|---|
| `NIGATE_ADMIN_TOKEN` (at least 24 characters) | gateway admin API, dashboard | Gateway: after a restart. Dashboard: next page load |
| Provider keys (`CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`, ...) | upstreams whose `api_key_env` has that name | Within about 5 seconds, no restart |
| `RUST_LOG` | gateway log level | After a restart |

Rules:

- A non-empty setting wins over an environment variable of the same name. An empty or null setting never erases an
  environment value.
- Any `api_key_env` name in `nigate.toml` can be a setting; the names above are only the usual ones.
- The gateway checks the file every 5 seconds and, when it changed, reloads through the same path as
  `POST /admin/reload` (`nigate.toml` is re-read at the same time).
- No file means "no platform": environment variables only. A file that exists but is unreadable, not JSON, or has a
  `schema_version` other than 1 is logged as an error; the gateway keeps the last valid settings (or, at startup, falls
  back to environment variables).

## Dashboard behind the platform login

The platform's proxy must check its own login, remove the `Authorization` header, and add
`X-Platform-Proxy-Token: <platform.proxy_token>`. It must forward the URI unchanged and pass WebSocket upgrades
(Streamlit needs them). When the config file exists, the dashboard:

- serves only requests whose `X-Platform-Proxy-Token` equals `platform.proxy_token` (constant-time comparison). Direct
  calls that bypass the proxy get an "access denied" page and no data;
- takes the admin token from the `NIGATE_ADMIN_TOKEN` setting, so there is no token field in the sidebar;
- talks to the admin API at `http://nigate-gateway:4001` (override with `NIGATE_ADMIN_URL`).

Serve the dashboard under the same path prefix that the proxy uses, for example
`STREAMLIT_SERVER_BASE_URL_PATH=nigate` for a route at `/nigate/`.

Do not put a second password prompt (for example HTTP Basic Auth on an outer proxy) in front of the platform route:
browsers send one `Authorization` header, so two Basic Auth layers cannot both succeed, and repeated wrong credentials
can trip the platform's failed-login limit.

## Containers

Run the gateway and the dashboard on the platform's Docker network, with no host ports:

- gateway: `NIGATE_CONFIG`, `NIGATE_PLATFORM_CONFIG`, the config folder mounted read-only, a volume for `/data`, and the
  network alias `nigate-gateway`. The admin API must listen on `0.0.0.0:4001` inside the container so that the
  dashboard can reach it; it still requires the admin token.
- dashboard: `NIGATE_ADMIN_URL`, `NIGATE_PLATFORM_CONFIG`, `STREAMLIT_SERVER_BASE_URL_PATH`, and the same config folder.

Keep the compose project name and the data volume of an earlier standalone install to keep its virtual keys and
statistics. Clients on the same network use `http://nigate-gateway:4000/v1` (or the compose service name).

## Local test without a platform

Create a folder with a `config.json` like the one above, mount it at `/platform`, and start the containers. Without a
proxy in front, the dashboard rejects the browser (no proxy header): send the header yourself, for example with a
browser extension, or remove `config.json` to fall back to the normal token field.
