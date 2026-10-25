# sijba-out-pushover

Outbound remote handler for **SIJBA Core**: pushes a reply out via
[Pushover](https://pushover.net) instead of (or alongside) a normal channel
reply.

## How it works

1. Starts a tiny HTTP server (`/health`, `/invoke`).
2. `POST`s to Core `/api/register` with `direction: "outbound"` and
   `match: {"source": [...]}` — the same self-registration dance as
   [sijba-kubectl](../sijba-kubectl), just answering *outbound* instead of
   *inbound* traffic.
3. Heartbeats so the registration does not expire.
4. When Core has a reply for one of the matched sources, it `POST`s the
   envelope to `/invoke`; this sends it to Pushover and returns
   `{"continue": true}` — a delivery failure never breaks the pipeline.

Which sources trigger it is entirely `PUSHOVER_MATCH_SOURCES` — no handler
file or core change needed to route a new named webhook here. See
[sijba core's routing docs](../sijba/docs/routing.md#one-router-per-source)
for how named webhooks and per-source routing fit together.

## Requirements

- Python 3.11+ (stdlib only — no dependencies to install)
- A Pushover application token + user key (https://pushover.net/apps/build)
- Network path: this service → Core (register/heartbeat), and
  **Core → this service** (advertise URL, for `/invoke`)

## Configure

| Env | Default | Description |
|-----|---------|-------------|
| `SIJBA_URL` | `https://sijba.edv-bartl.at` | Core base URL |
| `SIJBA_TOKEN` | *(required)* | Bearer token for Core's API |
| `PUSHOVER_APP_TOKEN` | *(required)* | Pushover application token |
| `PUSHOVER_USER_KEY` | *(required)* | Pushover user/group key — the recipient. One key per deployed instance; run a second instance under a different `HANDLER_ID` if a different source needs a different recipient. |
| `PUSHOVER_MATCH_SOURCES` | *(required)* | Comma-separated envelope sources this instance answers for, e.g. `webhook:voice,webhook:iot`. Refuses to start if empty — an unset match would otherwise silently do nothing, or (if core's default ever changed) catch every reply in the system. |
| `PUSHOVER_TITLE` | `Sijba` | Notification title prefix; the envelope's source is always appended (`Sijba — webhook:voice`) so multiple sources sharing one instance stay distinguishable. |
| `PUSHOVER_PRIORITY` | `0` | Pushover priority (-2..2) |
| `LISTEN_HOST` / `LISTEN_PORT` | `0.0.0.0` / `9101` | Bind address |
| `ADVERTISE_URL` | auto-detected outbound IP | URL Core uses to call `/invoke`. In k8s, set this explicitly to the Service DNS name (`http://sijba-out-pushover.sijba-out-pushover.svc.cluster.local:9101`) rather than relying on auto-detection. |
| `HANDLER_ID` | `pushover` | Registered handler id — must be unique if running more than one instance |
| `TTL_SECONDS` | `90` | Registration TTL (heartbeat at ttl/3) |
| `REGISTER_PRIORITY` | `50` | Outbound handler priority among other outbound handlers for the same source |

## Run locally

```bash
export SIJBA_URL=http://localhost:8080
export SIJBA_TOKEN='…'
export PUSHOVER_APP_TOKEN='…'
export PUSHOVER_USER_KEY='…'
export PUSHOVER_MATCH_SOURCES='webhook:voice'

python3 main.py
```

Wire a webhook to it in Core's `core.yaml`:

```yaml
webhooks:
  voice: ${VOICE_WEBHOOK_TOKEN}
```

Then any reply to a `POST /webhook/<voice token>` message shows up as a
Pushover notification.

## Tests

```bash
pip install pytest pyflakes
python3 -m pyflakes main.py tests/
python3 -m pytest -q
```

`docker build -f Dockerfile.test .` runs the same gate in a clean container —
this is what CI runs before `Dockerfile` gets built and pushed.

## Not yet deployed

This repo has no Gitea remote or k8s manifests yet. To ship it:

1. Create an empty repo on Gitea (`sijba-out-pushover`), add it as `origin`, push.
2. Add `kustomize-k8s/bases/sijba-out-pushover/` (namespace, deployment, service —
   no ingress needed, Core reaches it in-cluster) to `edvbartl-ansible`,
   an overlay entry, and an ArgoCD app definition, following the existing
   `sijba` base as a template.
3. Point `Jenkinsfile`'s ansible-bump stage at the new deployment's image line
   (already assumes `kustomize-k8s/bases/sijba-out-pushover/deployment.yaml`).

