# The hosted gateway — `cafecito gateway`

`cafecito ingest` asks GitHub every sixty seconds whether anything changed. The gateway is
told. Nothing about landing changes: a delivery arrives, a worker re-reads the PR, and the
head is submitted through the same engine, the same landing gate, the same landed log, and
reported back with the same comment and `cafecito:<verdict>` label. Only the trigger is
different.

This is increment one. Read [What this does not do](#what-this-does-not-do) before you
deploy it — the list is short and load-bearing.

```
GitHub ──delivery──▶ receiver ──202 in ~2ms──▶ queue ──▶ worker ──▶ Engine.submit
                        │                                  │
                   HMAC, project,                    re-read the PR,
                   drop, dedupe                      claim (PR, head), land, report
```

---

## 1. Create the App

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**.

| Field | Value |
|---|---|
| Name | anything; the bot account becomes `<name>[bot]` |
| Homepage URL | anything |
| Webhook | **Active** |
| Webhook URL | your public URL + the path, e.g. `https://plane.example.com/webhook` |
| Webhook secret | **required** — `openssl rand -hex 32`. Without one GitHub sends no signature and the gateway refuses every delivery. |
| Where can this be installed | *Only on this account* is the right answer for a single-repo plane |

### Repository permissions

Three, and each one is load-bearing:

| Permission | Access | Why |
|---|---|---|
| **Contents** | Read | fetching `refs/pull/N/head`, and reading the tree the gate runs |
| **Pull requests** | Read & write | reading PRs, posting the verdict comment, applying the label |
| **Issues** | Read & write | `gh label create` — labels live under Issues, not Pull requests |

That last row is the one people miss. It is a separate grant, and without it the three
`cafecito:*` labels are never created; the landing still happens and the comment still
posts, so the symptom is subtle. The gateway prints an explicit line on stderr when a
report call fails rather than swallowing the return code.

### Event subscriptions

Subscribe to exactly one:

- **Pull request**

Nothing else. The receiver drops every other event type before it parses the body, so
subscribing to more only costs you deliveries. In particular do *not* subscribe to Issue
comment or Push: the gateway's own comment and its own advance of `cafecito/main` would
come straight back as deliveries.

### Then

1. **Generate a private key** (bottom of the App settings page). GitHub downloads a
   PKCS#1 `.pem` once and never shows it again.
2. **Install the App** on the one repository this plane serves. The install URL ends in
   the **installation id** — `.../installations/12345678`. You need that number.
3. Note the **Client ID** (`Iv23li…`) from *About*. The numeric App ID also works.

---

## 2. Put the key and the secret somewhere sane

```sh
mkdir -p ~/.cafecito-gateway
mv ~/Downloads/your-app.*.private-key.pem ~/.cafecito-gateway/app.pem
chmod 600 ~/.cafecito-gateway/app.pem
openssl rand -hex 32 > ~/.cafecito-gateway/webhook-secret   # same value as the App's
chmod 600 ~/.cafecito-gateway/webhook-secret
```

The gateway refuses to start if either file is group- or world-readable, and it refuses a
private key that resolves **inside the repository working tree**. That is not superstition:
the landing gate materializes candidate trees and runs your `setup_cmd` and generator
commands with cwd inside them.

It also refuses a passphrase-protected key. `openssl` prompts for the passphrase on
`/dev/tty`, not stdin, so an encrypted key does not fail — it hangs.

---

## 3. Configuration

Secrets come from files or the environment. Behaviour comes from config or flags.

### Environment

| Variable | Meaning |
|---|---|
| `CAFECITO_WEBHOOK_SECRET` | the webhook secret, if you would rather not use `--secret-file` |

There is deliberately **no `--secret` flag**. `argv` is world-readable in `ps`.

### Flags

```
--github OWNER/REPO       default: derived from the origin remote
--app-client-id Iv23li…   App settings → About → Client ID (the App ID also works)
--installation-id N       the trailing number in the App's Configure URL
--private-key PATH        the .pem, mode 600, outside the worktree
--secret-file PATH        the webhook secret file, mode 600
--bind ADDR               default 127.0.0.1
--port N                  default 8787
--path /webhook           default /webhook
--workers N               concurrent landings (default 1)
--sweep N / --no-sweep    catch-up sweep interval, default 900s
--land-forks              land fork PRs too — read §6 first
--allow-public-bind       permit a non-loopback bind (there is no TLS here)
--no-report               land, but post no comments or labels
--check                   verify everything and exit without binding
```

### `.cafecito/config.json`

Anything but the secrets can live in an optional `gateway` block. It is merged over the
defaults, so a partial block is fine, and none of these keys appear in a plane that never
runs a gateway.

```json
{
  "gateway": {
    "bind": "127.0.0.1",
    "port": 8787,
    "path": "/webhook",
    "app_client_id": "Iv23liAbCdEf",
    "app_slug": "my-cafecito-plane",
    "installation_id": 12345678,
    "private_key_path": "/home/plane/.cafecito-gateway/app.pem",
    "openssl": "openssl",
    "workers": 1,
    "queue_max": 256,
    "sweep_s": 900,
    "max_body_bytes": 1048576,
    "claim_stale_s": 0,
    "fork_policy": "skip",
    "skip_drafts": true
  }
}
```

That is the whole surface — fifteen keys, and the sample shows all of them. The five that
have no flag: `openssl` names the binary that signs the App JWT; `queue_max` is how many
landings may wait before the receiver answers 503; `max_body_bytes` is the declared-size
limit behind the 413 in §5; `claim_stale_s` is the horizon in §7 (`0` means
`gate_timeout_s * 2`, the number the engine already uses for a dead in-flight submission);
`bind` is the config spelling of `--bind`.

`app_slug` is the App's URL slug. Setting it lets the receiver drop deliveries the App
itself caused; it is a third layer of defence behind the event and action allowlists, so
leaving it unset is not a correctness hole.

---

## 4. Run it

Check first. `--check` verifies the secret, the key's mode, format and location, the
binaries, the bind address, an actual openssl signature, the port, and a live token mint —
then exits without listening.

```sh
cafecito gateway --check \
  --github acme/widget \
  --app-client-id Iv23liAbCdEf \
  --installation-id 12345678 \
  --private-key ~/.cafecito-gateway/app.pem \
  --secret-file ~/.cafecito-gateway/webhook-secret
```

Then serve, with a tunnel in front:

```sh
cloudflared tunnel --url http://127.0.0.1:8787     # or ngrok, or nginx, or Caddy
cafecito gateway --secret-file ~/.cafecito-gateway/webhook-secret
```

```
gateway: acme/widget → cafecito/main
  listening   127.0.0.1:8787/webhook
  identity    app Iv23liA… installation 12345678
  workers     1   sweep 900s   forks: skip   isolation: none
```

The last two fields belong together: `forks: gate` with `isolation: none` is the one
combination that runs outsiders' code on your machine, and the gateway refuses to start in
it (§6).

Point the App's Webhook URL at the tunnel's public URL plus `/webhook`, then push to a PR.
`GET /healthz` returns `200 ok` for your proxy's health check.

---

## 5. What the receiver does with a delivery

Every step is a rejection point, and steps 1–8 run on unauthenticated bytes.

| Check | Failure |
|---|---|
| `POST` to the configured path | 404 |
| `Content-Type: application/json` | 415, after draining so you see the status |
| `Content-Length` present and an ASCII integer | 411 |
| declared size within `max_body_bytes` (default 1 MiB) | 413, after draining so you see the status |
| body read in full, within 5s of the headers | 400 truncated / 408 stalled |
| **`X-Hub-Signature-256` verified over the raw bytes** | **403** |
| `X-GitHub-Event` is `pull_request` (or `ping`) | 200, dropped before parsing |
| body parses to a JSON object | 400 |
| seven scalars, each type- and range-checked | 400 |
| repository and installation match this gateway | 200, ignored |
| sender is not our own App | 200, ignored |
| action in `opened`, `reopened`, `synchronize`, `ready_for_review` | 200, ignored |
| fork policy | 200, one comment |
| `X-GitHub-Delivery` not already seen | 200, duplicate |
| queued | 503 + `Retry-After` if the queue is full |
| — | **202 Accepted** |

Two properties worth stating outright.

**Signature before parse, always.** Nothing decodes or parses the body until the HMAC
matches, and the comparison is `hmac.compare_digest`. A missing signature header is a hard
403, never a skip — GitHub omits the header when no secret is configured, and treating
absent-as-fine accepts every forgery. The legacy SHA-1 `X-Hub-Signature` is never read.

**Only seven scalars cross the boundary**: delivery id, action, PR number, repo slug,
installation id, a fork flag, and where it came from. No title, no branch name, no URL, and
deliberately **not the head sha**. A delivery says *which* PR changed; GitHub is asked what
it is *now*. That is what makes a queued out-of-order delivery or force-push collapse into
a no-op instead of landing work the author already replaced (§7 has the limit).

If your proxy rewrites, pretty-prints or re-encodes the body, every delivery 403s. The
uniformity is the diagnosis.

---

## 6. Fork pull requests

**Fork PRs are skipped by default.** A fork's head is code written by someone outside the
repo, and landing it runs the gate over that code — including your `setup_cmd`, which
inherits this process's environment. `npm ci` on a fork's `package.json` runs that fork's
lifecycle scripts.

The gateway posts one comment saying so and stops. A maintainer who has read the diff can
land it deliberately with `cafecito submit <sha>`.

`--land-forks` restores `cafecito ingest`'s behaviour, and the gateway **refuses to start**
unless both of these hold — the flag is a policy, not a mitigation, so it does not create
the boundary it needs:

| Requirement | Why |
|---|---|
| `isolation` is `sandbox` or `container` | with `"none"` the gate runs the fork's tests as you, on the machine holding the App key |
| `setup_cmd` is empty | isolation wraps the **test command only**. `setup_cmd` runs on the host with the real environment and network, so `npm ci` on a fork's `package.json` executes that fork's lifecycle scripts outside every boundary |

The second row is the one that surprises people, and it is why the refusal exists rather
than a warning. If you need both a setup step and fork PRs, land forks by hand after
reading the diff.

The installation token is never pointed at the fork (the App is not installed there, and it
would 404): the head is fetched as `refs/pull/N/head` from the base repo.

---

## 7. Duplicates, ordering, and things that go wrong after the 202

GitHub gives a delivery ten seconds and **does not retry it**. A landing takes minutes. So
the handler answers 202 immediately and a worker does the work, which means there is
nothing left to return — the PR is the channel.

- **Redelivery** is free: the delivery id short-circuits it, and behind that
  `(PR, head)` is claimed atomically under the same file lock the engine uses, before the
  submission rather than after it.
- **Out-of-order deliveries and force-pushes** both name the same current head, and the
  loser collapses into a no-op — *while it is still queued*. A force-push that lands mid
  landing does not cancel the landing already in flight: the replaced head finishes, and
  the new one lands behind it. Nothing here is a substitute for the gate.
- **A landing that crashes** releases its claim, so the next delivery or sweep retries it
  immediately. A process that is `SIGKILL`ed leaves a claim that ages out after
  `claim_stale_s` (default `gate_timeout_s * 2`) — the same horizon the engine already
  uses for a dead submission.
- **A transient failure is not a verdict.** An unfetchable head (`unfetched`) and an
  admission timeout behind an overlapping changeset (`contended`) are recorded as
  retryable and age out on that same horizon, so the sweep works them again. Only a real
  verdict — landed, escalated, rejected — is terminal, because a terminal record can never
  be retried and GitHub does not redeliver.
- **A delivery lost while the gateway was down is gone**, permanently, as far as the wire
  is concerned. That is what the catch-up sweep is for: every `sweep_s` it re-offers every
  open PR, and the claim absorbs everything already landed. Do not turn it off unless you
  are also driving `cafecito ingest`.
- **A credential does not go stale mid-landing.** The worker binds a token *provider*, not
  a token, so the comment posted thirty minutes after the fetch uses a live token; the
  cache re-mints five minutes before expiry.
- **A failed report is said out loud.** If `gh pr comment` or a label write returns
  nonzero — the usual cause being a missing App permission — the gateway prints a redacted
  line naming the call instead of dropping it silently.

Tokens are redacted from every log line and every exception on the credential path. The
receiver logs one line per delivery containing only fields it constructed itself: never a
body, never a header, never the signature.

---

## What this does not do

Increment one, stated plainly so nobody assumes otherwise.

- **No multi-tenancy.** One process serves one repository's plane and one App installation.
  This is enforced, not merely intended: the receiver drops deliveries for any other repo
  or installation, and the token it mints is scoped to that one repository.
- **No hosted state.** State stays in `.cafecito/` exactly as `cafecito ingest` leaves it.
  There is no database and nothing to migrate.
- **No web console or UI.** `GET /healthz`, and that is the whole surface besides the
  webhook path.
- **No deployment, TLS termination, or process supervision.** It binds loopback, speaks
  plain HTTP, does no rate limiting, and has no connection cap. Put a tunnel or a reverse
  proxy in front for TLS, body limits, and real access logs, and run it under a supervisor;
  one crash takes the receiver with it. `--allow-public-bind` exists for people who have
  read this paragraph.
- **No failed-delivery replay.** GitHub's `/app/hook/deliveries` API can re-drive deliveries
  that failed; the slow sweep covers the same ground without a second credential path.
- **No source-IP allowlist.** GitHub publishes its hook ranges at `GET /meta`. That is
  defence in depth on top of the signature, never a substitute, and it needs a network call
  plus periodic refresh.
- **No `cafecito doctor` checks for any of this**, and no checks API integration — the
  verdict is a comment and a label, as with `ingest`.

---

## Vocabulary

Changesets **land**; collisions **commute**, **regenerate**, or **escalate**. The gateway
merges nothing and resolves nothing: it materializes a landed-log position onto
`cafecito/main` after the gate is green (SPEC §6), and it is the surface humans reach
through ordinary pull requests (SPEC §7).
