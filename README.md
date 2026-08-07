# f126-race-engineer

A personal race engineer for F1 25 (console or PC). The game broadcasts UDP telemetry on the LAN at
60 Hz; this captures every packet to disk losslessly, parses it (both the 2025 and the 2026
Season Pack wire formats, auto-detected), maintains a live session model — tyre wear rates,
fuel delta, ERS, sector deltas against a reference lap, gaps to the cars ahead and behind — and
pushes it to a browser pit wall over a WebSocket. Raw captures are kept forever, so any session
can be replayed through the exact same pipeline later, which is also how the thing is developed
without needing a console running.

```
   console — F1 25 (2026 Season Pack)
        │  UDP :20777, ~60 Hz, addressed to <NODE_IP>
        ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │ f126 — one pod on the cluster node, hostPort 20777/udp            │
 │                                                                   │
 │   udp/       asyncio datagram endpoint, 4 MiB rcvbuf              │
 │     │                                                             │
 │     ├──► capture/   ──►  /data/raw/<session>.f1raw   zstd, lossless
 │     │                    (every byte, before any parsing)         │
 │     │                                                             │
 │     └──► parser/    ──►  typed frames (types.py)                  │
 │                │         2025 + 2026 layouts, size-pinned         │
 │                ▼                                                  │
 │              state/      live model: deltas, wear rate, fuel,     │
 │                │         ERS, tower, sectors, events              │
 │                │                                                  │
 │                ├──► store/  ──► Postgres (batched every 250 ms)   │
 │                │                                                  │
 │                └──► web/    FastAPI: /ws, /healthz, /metrics      │
 │                       │      + the built Svelte app as static     │
 └───────────────────────┼───────────────────────────────────────────┘
                         │  WebSocket: 10 Hz "fast", 1 Hz "slow", events immediate
                         ▼
              Traefik ingress ──► https://<DASHBOARD_HOST>
```

The WebSocket message contract is frozen in [`docs/ws-protocol.md`](docs/ws-protocol.md).
Autonomous implementation choices are logged in [`decisions.md`](decisions.md).

---

## Local configuration

This repo is environment-agnostic: nothing tracked in git names a real host, node IP,
kube context or network range. Every such value lives in **`deploy/.env`**, which is
gitignored.

```bash
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env      # KUBE_CONTEXT, DASHBOARD_HOST, NODE_IP, UDP_PORT, IMAGE
make config              # print the resolved values, and what to type into the game
```

[`deploy/.env.example`](deploy/.env.example) documents every variable. The Makefile
does `-include deploy/.env`, so cluster and registry targets (`deploy`, `logs`,
`image`, `push`, `db-init`) pick the values up automatically and fail with an
explicit message if they are unset. Local-only targets (`dev`, `replay`, `test`,
`lint`) never need the file.

The Kubernetes manifests follow the same split: [`deploy/k8s/`](deploy/k8s/) is a
committed base with placeholder values, and your real hostname goes in a gitignored
`deploy/local/` kustomize overlay. See [`deploy/k8s/README.md`](deploy/k8s/README.md).

Shell snippets in this README use `$KUBE_CONTEXT`, `$NODE_IP` and `$DASHBOARD_HOST`.
To make them runnable as written, load the file into your shell first:

```bash
set -a; . ./deploy/.env; set +a
```

---

## Console setup

In the game: **Settings → Telemetry Settings**

| Setting | Value |
| --- | --- |
| UDP Telemetry | **On** |
| UDP Broadcast Mode | Off |
| UDP IP Address | `<NODE_IP>` — the machine or cluster node running f126 |
| UDP Port | `20777` |
| UDP Send Rate | `60 Hz` |
| UDP Format | `2026 Season Pack` |
| Your Telemetry | **Public** (needed for full car data) |

`make config` prints this table with your own values filled in.

Notes:

- **UDP Format** is not load-bearing — the parser detects 2025 vs 2026 from the packet header
  and handles both. Set it to 2026 Season Pack for the richer data (`CarTelemetry2`, aero
  modes, the 2026 energy model); 2025 sessions replay fine either way.
- **UDP Broadcast Mode** stays off. Broadcast sprays the whole subnet; a directed send to
  `<NODE_IP>` is what the `hostPort` on the node is listening for.
- **Your Telemetry: Public** matters. On "Restricted" the game withholds other cars' detailed
  telemetry and the tower loses tyre/pace data for rivals.
- Send rate 60 Hz is the input rate. The dashboard is downsampled server-side (10 Hz fast
  frames, 1 Hz slow) — raising the console rate does not make the UI smoother, it just gives
  the capture file finer resolution.
- The console must be on the same LAN as the node. There is no NAT traversal and no
  relay; it is a directed UDP datagram to a node IP. Across subnets or VLANs you need
  routing between them and no firewall in the way.

---

## Local development

Prerequisites: [uv](https://docs.astral.sh/uv/), Node 24, Docker (only for image builds).

```bash
uv sync                 # backend deps + the f126 CLI into .venv
cd frontend && npm ci   # frontend deps
```

Run the whole thing locally — UDP listener on `:20777`, HTTP + WebSocket on `:8000`:

```bash
make dev                # == uv run f126 serve
```

If you point the console at your laptop's LAN IP instead of the cluster node, this is a
complete local pit wall — no Kubernetes involved. Otherwise, use replay (below).

### Frontend dev loop

Vite dev server with HMR, proxying `/ws` to a running backend:

```bash
cd frontend
npm run dev             # expects `make dev` (or a replay) running on :8000
npm run dev:mock        # synthetic WS feed — no backend, no console, no capture needed
```

`dev:mock` is the fast path for pure UI work: it generates protocol-shaped frames, so layout,
tweening and edge cases (nulls, STALLED, flashbacks) can be built without a car on track.
No console, no backend, no capture file needed.

### Replay-driven development

This is the main development loop, and the reason capture is lossless. A `.f1raw` file replays
through the identical parse → state → broadcast path, so a real session becomes a repeatable
fixture:

```bash
make replay FILE=data/raw/2026-08-06T19-12-33_suzuka_race.f1raw
make replay FILE=... SPEED=max          # as fast as the pipeline will go
make replay FILE=... SPEED=4 LOOP=1     # 4x, looping — good for leaving the UI running
```

`SPEED=1` reproduces original packet timing. `SPEED=max` is what you want when iterating on
state-layer logic; `LOOP=1` keeps the dashboard fed indefinitely.

To pull a capture off the cluster to replay locally:

```bash
kubectl --context "$KUBE_CONTEXT" -n f126 exec deployment/f126 -- ls -lh /data/raw

POD=$(kubectl --context "$KUBE_CONTEXT" -n f126 get pod \
        -l app.kubernetes.io/name=f126 -o jsonpath='{.items[0].metadata.name}')
kubectl --context "$KUBE_CONTEXT" -n f126 cp \
  "$POD:/data/raw/<file>.f1raw" ./data/raw/<file>.f1raw
```

---

## Testing

```bash
make test        # backend (pytest) + frontend (vitest)
make lint        # ruff check + svelte-check
```

Backend tests that need a database read `F126_TEST_DATABASE_URL` and skip when it is unset, so
a bare `uv run pytest` works offline. To run the full suite locally, point it at any throwaway
Postgres:

```bash
docker run --rm -d --name f126-pg -p 5432:5432 \
  -e POSTGRES_USER=f126 -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=f126_test postgres:18
export F126_TEST_DATABASE_URL=postgresql://f126:dev@localhost:5432/f126_test
uv run pytest -q
```

CI does exactly this with a `postgres:18` service container. The parser suite additionally pins
every packet's exact wire size, so a mis-transcribed struct fails immediately rather than
producing plausible-looking garbage.

---

## Deploying to a cluster

**Manual by design.** CI builds and publishes images on every merge to `main`; nothing deploys
itself. The node is also the box you race against, and a surprise rollout mid-session drops the
UDP listener. You choose the moment.

### Get an image

Either let CD build it — merge to `main`, and `.github/workflows/cd.yml` pushes
`ghcr.io/<owner>/<repo>:latest` plus an immutable `:sha-<shortsha>` — or build and
push by hand:

```bash
make push                      # builds linux/amd64 and pushes :latest
make push IMAGE_TAG=sha-abc1234
```

Both read `IMAGE` from `deploy/.env`. `make image` builds locally without pushing.
`PLATFORM` defaults to `linux/amd64` to match an amd64 node — an arm64 image built on a
Mac and run on an amd64 node will `CrashLoopBackOff` with an exec format error. Override
with `make push PLATFORM=linux/arm64` on an arm64 cluster.

### Deploy

```bash
make deploy                          # :latest — forces a restart so the new image is pulled
make deploy IMAGE_TAG=sha-abc1234    # pinned, reproducible, and what you want for a rollback
```

`make deploy` runs `kubectl apply -k deploy/local` and waits on the rollout. `deploy/local/`
is your gitignored kustomize overlay — it supplies the real ingress host on top of the
committed base in `deploy/k8s/`, which ships a placeholder. If the overlay does not exist,
`make deploy` falls back to `deploy/k8s` and warns loudly that it is about to deploy
`race.example.com`. Create the overlay first: [`deploy/k8s/README.md`](deploy/k8s/README.md).

Preview exactly what an apply would change, without changing it:

```bash
kubectl kustomize deploy/local                                  # render only
kubectl --context "$KUBE_CONTEXT" diff -k deploy/local          # diff against the cluster
```

Note the asymmetry: re-applying an unchanged manifest does **not** restart a pod, so
deploying `:latest` needs an explicit `rollout restart` (the Makefile does this for you).
Pinning a `sha-` tag changes the pod spec and rolls out on its own — prefer it when you
care which build is running.

Expect a few seconds of downtime on every deploy: the Deployment uses `Recreate`, not
`RollingUpdate`, because `hostPort 20777/udp` can only be held by one pod on the node and the
`local-path` PVC is `ReadWriteOnce`. A rolling update would deadlock on both.

### First-time deploy

Three one-time steps. **1) Create the namespace** (the manifests include it, so this is only
needed if you want it to exist before anything else):

```bash
kubectl --context "$KUBE_CONTEXT" apply -f deploy/k8s/namespace.yaml
```

**2) Create the `f126` role and database** on the shared Postgres (namespace `postgres`).
Pick a real password and substitute it for `<PASSWORD>` in both commands below:

```bash
PGPOD=$(kubectl --context "$KUBE_CONTEXT" -n postgres get pod -l app=postgres -o name | head -1)

kubectl --context "$KUBE_CONTEXT" -n postgres exec -it "$PGPOD" -- \
  psql -U postgres \
    -c "CREATE ROLE f126 LOGIN PASSWORD '<PASSWORD>';" \
    -c "CREATE DATABASE f126 OWNER f126;"
```

The role owns its own database and nothing else — it gets no rights on the other databases
sharing that instance. Verify:

```bash
kubectl --context "$KUBE_CONTEXT" -n postgres exec -it "$PGPOD" -- psql -U postgres -c '\l f126'
```

**3) Create the `f126-db` secret** with the DSN. No Secret manifest is committed to this repo —
the password lives only in the cluster:

```bash
kubectl --context "$KUBE_CONTEXT" -n f126 create secret generic f126-db \
  --from-literal=url='postgresql://f126:<PASSWORD>@postgres.postgres.svc.cluster.local:5432/f126'
```

To rotate it later, `ALTER ROLE f126 PASSWORD '<NEW>'` in psql, then:

```bash
kubectl --context "$KUBE_CONTEXT" -n f126 create secret generic f126-db \
  --from-literal=url='postgresql://f126:<NEW>@postgres.postgres.svc.cluster.local:5432/f126' \
  --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f -
kubectl --context "$KUBE_CONTEXT" -n f126 rollout restart deployment/f126
```

The deployment references this secret with `optional: true`, so the pod starts even without
it — raw capture keeps working and database writes are simply disabled. That is deliberate:
losing Postgres should never cost you a session's telemetry. `make db-init` prints these
commands without running them.

Then `make deploy`. Watch it with `make logs`.

### What gets deployed

`deploy/k8s/` contains plain manifests plus a `kustomization.yaml` listing them:

| File | Notes |
| --- | --- |
| `namespace.yaml` | namespace `f126` |
| `pvc.yaml` | `f126-data`, 50Gi, `local-path`, RWO — the raw captures live here |
| `deployment.yaml` | 1 replica, `Recreate`, hostPort 20777/udp, read-only rootfs, non-root uid 10001 |
| `service.yaml` | ClusterIP :8000 (HTTP/WS only — UDP does not go through a Service) |
| `ingress.yaml` | placeholder host `race.example.com`, Traefik, cert-manager `letsencrypt-cloudflare` |
| `middleware-ipallowlist.yaml` | **not applied** — see below |

Everything there is a **base**, and it is deliberately not deployable as-is: the ingress host
and the IP allowlist ranges are placeholders. Your values go in a gitignored `deploy/local/`
overlay — [`deploy/k8s/README.md`](deploy/k8s/README.md) has a copy-paste one.

`local-path` does not support volume expansion, so the 50Gi PVC cannot be grown in place; it is
sized generously up front for that reason.

---

## The dashboard

**`https://$DASHBOARD_HOST`** — whatever you set in `deploy/.env`.

This deployment leaves it internet-open on purpose, and it is **strictly read-only** — every
endpoint is a GET or a WebSocket subscription, there is no mutating route, no auth, and nothing
to configure through the UI. What it exposes is telemetry the game already broadcast in clear
text on the LAN. That invariant is the whole reason it can be left open, so treat it as
load-bearing: if a write path ever appears (editing setups, deleting sessions, an admin view),
enable the IP allowlist in the same change.

`deploy/k8s/middleware-ipallowlist.yaml` is committed but deliberately disabled — not listed in
`kustomization.yaml` and not referenced by the ingress. The committed copy allows placeholder
ranges only; put your real CIDRs in `deploy/local/middleware-ipallowlist.yaml` (gitignored) and
apply that instead. To turn it on:

```bash
kubectl --context "$KUBE_CONTEXT" apply -f deploy/local/middleware-ipallowlist.yaml
# then add to ingress.yaml metadata.annotations:
#   traefik.ingress.kubernetes.io/router.middlewares: f126-ipallowlist@kubernetescrd
make deploy
```

---

## Post-session debrief

After a session finishes, the app can write you a ~250-word note in a race engineer's voice:
what the result was, how repeatable your pace was, what the tyres did, and the one corner
costing you the most against your own qualifying benchmark.

It is **grounded**, which is the only reason it is worth reading. A deterministic builder
computes every number out of the recorded telemetry — lap median and IQR, degradation slopes
with their r², fuel burn with garage refuels excluded, the top three time-loss corners with
brake-point deltas — and the model is handed that fact sheet and told it may not calculate,
convert or estimate anything that is not already in it. Both the prose and the fact sheet are
stored, so any sentence can be checked against the numbers behind it.

**No model is ever in the live loop.** Nothing about capture, the pit wall or the API waits on
it. If the endpoint is slow, down, or not configured, the only thing that does not happen is a
paragraph of prose.

Point it at any OpenAI-compatible endpoint:

```bash
F126_LLM_BASE_URL=http://your-proxy.example/v1   # empty = feature off (the default)
F126_LLM_MODEL=your-model
F126_LLM_API_KEY=                                # optional bearer token
```

Mind the path — some proxies mount the OpenAI surface under a prefix such as `/api/v1` and
answer 404 on the bare `/v1`. Use whatever your endpoint answers `GET …/models` on. For the
cluster, see the debrief section of `deploy/k8s/README.md`.

Sessions that close cleanly get one automatically. Anything else — a backfilled capture, a
session that timed out, or a debrief you want rewritten — is a command away:

```bash
f126 debrief 102                # print it, or write one if there is none
f126 debrief 102 --regenerate   # write a new one; the previous is kept
```

It shows up as a collapsible **Debrief** card on the session page, captioned with the model
and the time it was written. A session without one says so plainly rather than looking broken.

There is **no HTTP route that generates a debrief** — `GET /api/sessions/{id}/debrief` reads
one and that is all. A POST would have been convenient and would have cost the read-only
invariant that lets this dashboard sit on the open internet.

---

## Raw captures and backfill

Every packet is written to `/data/raw/<session>.f1raw` (zstd-compressed) *before* parsing. The
capture path does not depend on the parser, the state layer or the database — if any of those
break mid-race, the bytes still land on disk and the session is recoverable.

A session in progress is `<name>.f1raw.open`; it is renamed on clean session end. An `.open`
file left behind by a crash is still fully replayable.

Re-parse captures into the database — after a parser fix, a schema change, or to import
sessions recorded while Postgres was down:

```bash
make replay FILE=data/raw/<file>.f1raw SPEED=max   # local, into your dev database

# in-cluster, everything not yet imported:
kubectl --context "$KUBE_CONTEXT" -n f126 exec -it deployment/f126 -- f126 backfill
kubectl --context "$KUBE_CONTEXT" -n f126 exec -it deployment/f126 -- f126 backfill /data/raw/<file>.f1raw
```

`backfill` with no arguments processes `$F126_DATA_DIR/raw`. Captures are the source of truth;
the database is a derived index that can always be rebuilt from them.

Disk usage runs roughly 40–80 MB per race hour compressed. Check headroom with:

```bash
kubectl --context "$KUBE_CONTEXT" -n f126 exec deployment/f126 -- du -sh /data/raw
```

---

## Troubleshooting

### No packets arriving

The dashboard connects but nothing moves, and `/metrics` shows `packets_per_sec` at 0.

Work down the path:

```bash
# 1. Is the pod up and did the UDP listener bind?
kubectl --context "$KUBE_CONTEXT" -n f126 get pods
make logs

# 2. Is anything reaching the node at all? Attach an ephemeral debug container sharing the
#    pod's network namespace. This is the check to reach for on a LIVE pod — it observes the
#    traffic without competing for the port.
kubectl --context "$KUBE_CONTEXT" -n f126 debug -it deployment/f126 \
  --image=nicolaka/netshoot --target=f126 -- tcpdump -n -i any udp port 20777

# 3. tools/sniff.py dumps parsed packet headers as they land — more readable than tcpdump when
#    you want to know *which* packets are arriving, not just that bytes are.
kubectl --context "$KUBE_CONTEXT" -n f126 exec -it deployment/f126 -- python /app/tools/sniff.py
```

`sniff.py` binds `:20777` itself, so it cannot run alongside the capture loop that already
holds the port. Either run it locally against a console pointed at your laptop, or scale the
deployment to zero first and remember to scale it back:

```bash
kubectl --context "$KUBE_CONTEXT" -n f126 scale deployment/f126 --replicas=0
# ... run sniff.py via a debug pod, then:
kubectl --context "$KUBE_CONTEXT" -n f126 scale deployment/f126 --replicas=1
```

The runtime image has a read-only root filesystem and essentially no shell utilities, so
`tcpdump`/`ss`/`nc` are only available through the `kubectl debug` route above — there is
nowhere to install anything.

Then check, in order:

- **Is the console on the node's subnet?** Same physical LAN, not guest Wi-Fi, not a VLAN
  with inter-VLAN routing disabled. This is plain directed UDP — nothing traverses NAT.
- **Telemetry settings reverted?** A game update or a profile switch can reset them. Re-check
  UDP Telemetry = On, IP = `<NODE_IP>`, port = `<UDP_PORT>` (`make config` prints both).
- **Host firewall on the node.** `hostPort` publishes on the node, but the host firewall sits
  in front of it: on Ubuntu, `sudo ufw status` and, if enabled, `sudo ufw allow 20777/udp`.
- **Port already held.** hostPort is exclusive per node. If an old pod is still terminating,
  the new one cannot bind. `kubectl -n f126 get pods` — with `Recreate` you should never see
  two, but a stuck terminating pod will do it.
- **Wrong destination.** Confirm the console is aimed at the node IP, not at a router, the
  ingress hostname, or an old laptop address.

### `STALLED`

The dashboard shows a STALLED banner and a `STALLED` event arrives on the WebSocket. It means
**no UDP packet has been seen for more than 5 seconds** (`F126_STALL_AFTER_S`) while a session
is considered active. It is a capture-health signal, not a game state.

Normal causes: you paused, backed out to a menu, or the session ended. Abnormal causes: the
console slept, the network dropped, or the pod restarted. It clears automatically on the next
packet — the session is not closed, and any capture file stays open and valid. A session is
only retired after `F126_SESSION_TIMEOUT_MIN` (default 30 min) of silence.

### Metrics

`https://$DASHBOARD_HOST/metrics` — packets/sec, parse errors, kernel drop counts,
last-packet age, connected WebSocket clients, database batch latency. The same counters are
mirrored into the `health` block of every 1 Hz `slow` WebSocket frame, so the dashboard shows
them without a separate scrape.

`kernel_drops_total` climbing is the one to watch: it means the socket receive buffer overflowed
and packets were lost before userspace saw them. Raise `F126_RCVBUF` (default 4 MiB) if it does.

### Other things worth knowing

- **`/healthz`** is what the k8s probes hit. It reports process liveness only — deliberately
  *not* telemetry health, so that a pod sitting idle between race sessions is never restarted
  for the crime of having nothing to do.
- **Certificate not issuing.** cert-manager uses the `letsencrypt-cloudflare` ClusterIssuer
  (DNS-01) — rename it in your overlay if your cluster's issuer is called something else.
  Check with `kubectl --context "$KUBE_CONTEXT" -n f126 get certificate` and then
  `describe certificate <name>` on the one it lists.
- **Config** is entirely environment-driven — every knob is in `src/f126/config.py` with its
  default. Override via `env:` in `deploy/k8s/deployment.yaml`.
