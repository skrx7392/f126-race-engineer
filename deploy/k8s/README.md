# deploy/k8s — the environment-agnostic base

Everything in this directory is committed and contains **no real hostnames, node IPs,
kube contexts or network ranges**. It is a kustomize base, not a deployable
configuration: `ingress.yaml` points at `race.example.com` and
`middleware-ipallowlist.yaml` allows `192.0.2.0/24`, both placeholders.

Your environment lives in a gitignored overlay at `deploy/local/`.

```
deploy/
  .env.example      tracked    documented variables
  .env              gitignored KUBE_CONTEXT, DASHBOARD_HOST, NODE_IP, IMAGE, ...
  k8s/              tracked    this base — placeholders only
  local/            gitignored your overlay — the real host, the real CIDRs
```

## Why an overlay and not just editing the base

Editing `ingress.yaml` in place works right up until you push. The split exists so
that the thing you must not publish and the thing you want to publish are different
files, and the one you must not publish is named in `.gitignore`. There is no state
where a hostname leaks because someone forgot to revert a local edit before
committing.

`make deploy` applies `deploy/local` when that directory exists and falls back to
`deploy/k8s` with a loud warning when it does not — so a fresh clone fails visibly
on a placeholder host instead of silently deploying one.

## Creating the overlay

```bash
mkdir -p deploy/local
cp deploy/.env.example deploy/.env       # then edit it
```

`deploy/local/kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../k8s

patches:
  - target:
      kind: Ingress
      name: f126-ingress
    patch: |-
      - op: replace
        path: /spec/rules/0/host
        value: race.your-domain.example
      - op: replace
        path: /spec/tls/0/hosts/0
        value: race.your-domain.example
      - op: replace
        path: /spec/tls/0/secretName
        value: race-your-domain-example-tls
```

Three fields, and they must agree: the routing host, the certificate's SAN, and the
Secret cert-manager writes the issued certificate into. The `-tls` secret name is a
convention (host with dots replaced by dashes) — any name works as long as it is used
consistently and does not collide with another Ingress in the namespace.

JSON6902 `replace` ops rather than a strategic-merge patch: `spec.rules` and
`spec.tls` are lists without a merge key, so a merge patch replaces the entire list
instead of the one field you meant, and you lose the backend service definition.

To run a different image than the published one, add to the same file:

```yaml
images:
  - name: ghcr.io/skrx7392/f126-race-engineer   # the name as written in deployment.yaml
    newName: ghcr.io/YOUR-USER/f126-race-engineer
    newTag: latest
```

## Checking your overlay before you apply it

Render it and read the result — no cluster contact:

```bash
kubectl kustomize deploy/local
```

Then validate against the API server without changing anything:

```bash
kubectl --context "$KUBE_CONTEXT" apply -k deploy/local --dry-run=client
kubectl --context "$KUBE_CONTEXT" apply -k deploy/local --dry-run=server   # also runs admission
```

To confirm a deploy will be a no-op, diff the rendering against what is live:

```bash
kubectl --context "$KUBE_CONTEXT" diff -k deploy/local
```

Empty output means the cluster already matches.

## The IP allowlist

`middleware-ipallowlist.yaml` is committed but is **not** in `kustomization.yaml`'s
`resources` and **not** referenced by the Ingress, so `make deploy` never applies it.
The dashboard is internet-open on purpose: every route is a GET or a WebSocket
subscription and there is nothing to mutate.

Keep your real ranges in `deploy/local/middleware-ipallowlist.yaml` and apply that
file by hand if you ever want the restriction on. Do not add it to the overlay's
`resources` unless you also add the Ingress annotation in the same change — applying
the Middleware alone does nothing, and adding the annotation without the Middleware
takes the site down with a 500 from Traefik.

## Post-session debrief

Optional, and **off in the base**. When it is configured, the serve process writes a
~250-word debrief after every session that finishes, from a fact sheet it computes
itself; when it is not, nothing else changes. No model is ever in the live loop, so a
missing or broken endpoint costs a paragraph and nothing more.

It needs two values in the pod, plus an optional third:

| Variable | Meaning |
| --- | --- |
| `F126_LLM_BASE_URL` | OpenAI-compatible base URL **including the version segment**. The client appends `/chat/completions`. Empty = feature disabled. |
| `F126_LLM_MODEL` | Model id as the endpoint names it. Required — a base URL with no model counts as disabled. |
| `F126_LLM_API_KEY` | Optional bearer token. |

Mind the path. Some proxies serve `/v1`, others mount the same surface under a prefix
such as `/api/v1` and answer 404 on the bare one. Use whatever path your endpoint
answers `GET …/models` on.

The base ships none of these, because a service name is environment-specific in
exactly the way a hostname is. Put them in your gitignored overlay:

```yaml
patches:
  - target:
      kind: Deployment
      name: f126
    patch: |-
      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: F126_LLM_BASE_URL
          value: http://llm-proxy.example.svc.cluster.local/api/v1
      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: F126_LLM_MODEL
          value: example-model:latest
      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: F126_LLM_API_KEY
          valueFrom:
            secretKeyRef:
              name: f126-llm
              key: api_key
              optional: true
```

`add` at `…/env/-` appends, so these do not depend on the index of anything already in
the base's `env` and a re-apply stays a no-op diff.

The key, if you need one, is created by hand like `f126-db`:

```bash
kubectl -n f126 create secret generic f126-llm --from-literal=api_key=<key>
```

Backfilled sessions get no debrief automatically — `f126 backfill` has no session-close
callback, by design. Write one on demand instead:

```bash
kubectl -n f126 exec deploy/f126 -- f126 debrief <session_id>
```

## What is NOT in here

No Secret. `f126-db` (key: `url`) and `f126-llm` (key: `api_key`) are created by hand on
the cluster — see the first-time deploy section of the top-level README and the debrief
section above. The Deployment references both with `optional: true`, so the pod starts
without them and degrades (raw-capture-only, no debriefs) rather than crash-looping.
