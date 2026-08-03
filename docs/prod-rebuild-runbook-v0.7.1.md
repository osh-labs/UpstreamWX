# Runbook — production rebuild on a clean EC2 instance (v0.7.1)

Retire the incrementally-grown production box and stand production up **from bare metal** on a
fresh EC2 instance, with `v0.7.1` as its first and only release, before the public beta.

This is not a deploy. It is a provisioning run that happens to end with a deploy — the
difference matters, because `bootstrap.sh` has never actually been proven on production. The
current box predates the SA-06 atomic-release work (#132) and was migrated into that layout, so
the from-scratch path this runbook exercises is, today, an assumption.

## Why rebuild rather than promote

Found on the live box during the v0.7.0 pre-flight (2026-08-03), in about twenty minutes:

| Finding | Consequence |
|---|---|
| **No `deploy/config.env` anywhere on the host** | v0.6.2's deploy is not reproducible — nobody can say what config produced it |
| **`upstreamwx-api` was `disabled`** (running 3 days, `preset: enabled`) | one reboot from a silent outage; nothing would page, since the FR-12 dead-man's-switch is pinged *by* the process that would not be running |
| **Two duplicate Let's Encrypt lineages** (`app.upstreamwx.com` + `upstreamwx.com`, identical SANs) | 2 of 5 weekly duplicate-cert slots spent; nginx served the lineage the deploy layer does *not* derive |
| **Provisioned pre-#132, migrated in place** | the hardened bootstrap path is unexercised on prod |

Rebuilding converts the deploy layer from *assumed working* to *demonstrated working*, which is
the property you actually want going into a public beta.

## Why it is low-risk here

**There is no durable state to migrate.** Verified in the source, not assumed:

- No database. The only SQLite is `hyriver/aiohttp_cache.sqlite` — an HTTP response cache.
- No accounts and no personal data. SA-01 sessions are *stateless* HMAC tokens; there is no
  session table.
- `/var/lib/upstreamwx` is entirely regenerable cache (GEFS/REFS grids, watershed basins).
  Losing it costs one slow first briefing.

The migration payload is **three secrets and a DNS record**.

## Preconditions

- `v0.7.1` tagged on `main` and pushed. It must contain the boot-enable fix — a fresh bootstrap
  from `v0.7.0` would leave the service `disabled`, reintroducing the bug above on the clean box.
- The old box left **running and untouched** throughout. It is the rollback.
- Accepted: **downtime is fine.** No users, beta unpublished. This runbook is written for the
  simple ordering (cut over, *then* issue certs) that a live audience would not allow.

---

## Phase 0 — before you touch anything

```sh
# On the OLD box: capture the secrets to carry across (do NOT paste these anywhere).
sudo grep -E '^(ANTHROPIC_API_KEY|UPSTREAMWX_(HEALTHCHECK_URL|NWS_USER_AGENT))=' \
    /etc/upstreamwx/upstreamwx.env
```

Carry across: `ANTHROPIC_API_KEY`, `UPSTREAMWX_HEALTHCHECK_URL`, `UPSTREAMWX_NWS_USER_AGENT`.

**Generate `UPSTREAMWX_SESSION_SECRET` fresh on the new box — do not copy it.** A rebuild is the
natural rotation point, and the old value has been exposed in an operator transcript.

Certificates are **not** copied. The new box issues its own in Phase 4 — no inherited account
key, no lineage cruft, which is the entire point of a clean build.

**Determine how the public names resolve** — this decides your cutover mechanics:

```sh
dig +short app.upstreamwx.com upstreamwx.com www.upstreamwx.com
dig +short app.upstreamwx.com | xargs -I{} echo "current A -> {}"
```

- **Elastic IP** → cutover is an EIP reassociation: instant, no DNS edit, instantly reversible.
  This is strongly preferred.
- **Namecheap A records pointing at an instance IP** → you will edit three records and wait out
  the TTL. **Drop the TTL to 60 s now**, well before the cutover, so you are not waiting on a
  stale record mid-migration.

---

## Phase 1 — launch and prepare the instance

Same region/AZ, current Ubuntu LTS, sized like the existing box. Note the ≤2 GB RAM constraint
already documented in CLAUDE.md: `api_enable_decode_pool` stays **off** (each spawn worker
re-imports the scientific stack at ~300–500 MB RSS and OOMs a small host).

```sh
sudo apt-get update && sudo apt-get -y upgrade
sudo timedatectl set-timezone UTC
```

**Verify the SA-08 host prerequisite now** — it is the one requirement that cannot be fixed in
software, and PDF export (FR-27) fails without it:

```sh
sysctl user.max_user_namespaces                        # must be > 0
sysctl kernel.apparmor_restrict_unprivileged_userns    # want 0 on Ubuntu 24.04+
```

If the second is `1`, either set it to `0` persistently or plan to set
`UPSTREAMWX_PDF_NO_SANDBOX=1` in the env file (the pre-SA-08 fallback). Decide here, not after
a failing renderer.

```sh
sudo git clone https://github.com/osh-labs/upstreamwx.git /root/upstreamwx-src
cd /root/upstreamwx-src
sudo git checkout v0.7.1
sudo git rev-parse HEAD
```

> Clone to `/root`, **not** under `/opt/upstreamwx`. `${DEPLOY_APP_DIR}/repo` is the deploy's own
> git mirror (`_lib.sh:65`) — running the scripts from inside the tree they fetch into is asking
> for confusion.
>
> `/root` is mode `0700`, so a non-root login (e.g. `ssm-user`) cannot even `cd` into the clone.
> **Take a root shell for the rest of this runbook** — `sudo -i` — rather than prefixing every
> command; `bootstrap.sh` must run as root anyway. Commands below assume you are root.
> `git checkout v0.7.1` leaves you in detached HEAD; that is expected and correct for a tag.

---

## Phase 2 — write the deploy config

Every value below was read off the **running** old box (its rendered systemd unit and nginx
sites), not invented. This file is the artifact whose absence caused the archaeology in the
first place — keep it, back it up, and treat it as the environment's definition.

**TLS gates are deliberately OFF for the first pass.** No cert exists yet on this box; with
`DEPLOY_REQUIRE_HTTPS="1"` the deploy would refuse, and with `DEPLOY_TLS_ENABLE="1"` nginx would
reference a cert path that does not exist and fail to start. They are turned on in Phase 4.

```sh
sudo tee /root/upstreamwx-src/deploy/config.env >/dev/null <<'EOF'
DEPLOY_REPO_URL="https://github.com/osh-labs/upstreamwx.git"
DEPLOY_BRANCH="v0.7.1"

DEPLOY_APP_DIR="/opt/upstreamwx"
DEPLOY_USER="upstreamwx"
DEPLOY_GROUP="upstreamwx"
DEPLOY_DATA_DIR="/var/lib/upstreamwx"
DEPLOY_ENV_DIR="/etc/upstreamwx"
DEPLOY_ENV_FILE="/etc/upstreamwx/upstreamwx.env"

DEPLOY_BIND_HOST="127.0.0.1"
DEPLOY_BIND_PORT="8000"
DEPLOY_SERVICE="upstreamwx-api"

DEPLOY_APP_SERVER_NAME="app.upstreamwx.com"
DEPLOY_LANDING_SERVER_NAME="upstreamwx.com www.upstreamwx.com"
DEPLOY_LANDING_ROOT="/opt/upstreamwx/current/landing"

DEPLOY_KEEP_RELEASES="5"
UV_VERSION="0.8.17"

DEPLOY_CERTBOT_EMAIL="info@upstreamwx.com"
DEPLOY_ACME_WEBROOT="/var/www/acme"

# --- OFF for pass 1 (no cert yet). Phase 4 turns both on. ---
DEPLOY_TLS_ENABLE="0"
DEPLOY_REQUIRE_HTTPS="0"

# No release signing key exists (see docs/prod-release-runbook-v0.7.0.md Phase 1, and #159).
# Left at 1 it would silently no-op on a lightweight tag, which reads as "verified".
DEPLOY_VERIFY_TAG_SIGNATURE="0"
EOF
sudo chmod 0600 /root/upstreamwx-src/deploy/config.env
```

> `DEPLOY_ACME_WEBROOT` must stay `/var/www/acme` — the nginx `:80` block serves
> `/.well-known/acme-challenge/` from it, and certbot renewal configs record it absolutely.

---

## Phase 3 — first bootstrap (HTTP only, temp IP)

```sh
cd /root/upstreamwx-src
sudo DEPLOY_CONFIG=deploy/config.env deploy/bootstrap.sh
```

This creates the service account, installs the hardened unit + nginx sites (HTTP-only),
scaffolds `repo/` + `releases/` + the ACME webroot, persists the config to
`/etc/upstreamwx/deploy.conf`, installs `uwx-ctl`, **enables the service at boot**, then builds
`releases/<sha>`, flips `current`, and blocks on `/v1/health`.

Then fill in the secrets and restart:

```sh
sudo tee -a /etc/upstreamwx/upstreamwx.env >/dev/null <<EOF
UPSTREAMWX_SESSION_SECRET=$(openssl rand -hex 32)
UPSTREAMWX_API_AUTH_REQUIRED=1
UPSTREAMWX_API_TRUSTED_HOSTS=["app.upstreamwx.com"]
ANTHROPIC_API_KEY=<from the old box>
UPSTREAMWX_HEALTHCHECK_URL=<from the old box>
UPSTREAMWX_NWS_USER_AGENT=<from the old box>
EOF
sudo chmod 0640 /etc/upstreamwx/upstreamwx.env
sudo chown root:upstreamwx /etc/upstreamwx/upstreamwx.env
sudo grep -c '^UPSTREAMWX_SESSION_SECRET=' /etc/upstreamwx/upstreamwx.env   # expect exactly 1
sudo systemctl restart upstreamwx-api
```

Validate over plain HTTP against the temp IP:

```sh
uwx-ctl version
uwx-ctl health | python3 -m json.tool | grep -E 'release|data_dir_ok|auth_active|trusted_hosts'
#   release v0.7.1, data_dir_ok true, auth_active true
systemctl is-enabled upstreamwx-api        # enabled  <- the #132-era gap, fixed in v0.7.1
curl -sI http://<temp-ip>/ | head -1       # 200
```

> **Use `curl`, not a browser.** HSTS (`max-age=31536000; includeSubDomains`) is pinned in any
> browser that has visited these names, and it cannot be clicked through. A browser will refuse
> the HTTP-only phase and you will waste time debugging a working server.

---

## Phase 4 — cut over, then issue certificates

**4a. Repoint the names.**

- Elastic IP: disassociate from the old instance, associate with the new one.
- Namecheap A records: point `app`, `@`, and `www` at the new IP; wait for the TTL.

```sh
dig +short app.upstreamwx.com upstreamwx.com www.upstreamwx.com   # all -> new IP
```

Do not proceed until all three resolve to the new box — HTTP-01 validation fetches over the
public name and will otherwise validate against the **old** box.

**4b. Rehearse issuance.** Staging issuance does not count against the rate limit:

```sh
sudo certbot certonly --webroot -w /var/www/acme --dry-run \
  --cert-name app.upstreamwx.com \
  -d app.upstreamwx.com -d upstreamwx.com -d www.upstreamwx.com
```

Fix any DNS/port-80/webroot problem here, where retries are free. You have **3 of 5** weekly
duplicate-certificate slots left for this exact SAN set.

**4c. Issue for real and enable TLS.** Turn the gates on, then re-run bootstrap — with the cert
absent, `setup_tls_webroot` issues it (`--cert-name` pinned to the app's primary name so the
lineage path matches `DEPLOY_TLS_CERT`'s default), re-renders the sites with the `:443` block,
and reloads:

```sh
sudo sed -i 's/^DEPLOY_TLS_ENABLE=.*/DEPLOY_TLS_ENABLE="1"/;s/^DEPLOY_REQUIRE_HTTPS=.*/DEPLOY_REQUIRE_HTTPS="1"/' \
    /root/upstreamwx-src/deploy/config.env
cd /root/upstreamwx-src
sudo DEPLOY_CONFIG=deploy/config.env deploy/bootstrap.sh
```

Bootstrap persists the updated config to `/etc/upstreamwx/deploy.conf`, so every subsequent
`uwx-ctl deploy` enforces HTTPS. Confirm:

```sh
sudo grep -E '^DEPLOY_(TLS_ENABLE|REQUIRE_HTTPS)=' /etc/upstreamwx/deploy.conf
sudo certbot certificates | grep -E 'Certificate Name|Domains|Expiry'
#   ONE lineage, app.upstreamwx.com, all three SANs — no duplicates on the clean box
```

---

## Phase 5 — validation

```sh
# a) Release + gates over the public name
curl -s https://app.upstreamwx.com/v1/health | python3 -m json.tool \
  | grep -E 'release|data_dir_ok|auth_active|trusted_hosts'
#   release v0.7.1, data_dir_ok true, auth_active true, trusted_hosts true

# b) The data-dir pin is live in the PROCESS env (the ExecStart hardening, #146/#147/#148)
PID=$(systemctl show -p MainPID --value upstreamwx-api)
sudo cat /proc/$PID/environ | tr '\0' '\n' | grep UPSTREAMWX_DATA_DIR
#   UPSTREAMWX_DATA_DIR=/var/lib/upstreamwx
sudo grep -n '^UPSTREAMWX_DATA_DIR' /etc/upstreamwx/upstreamwx.env || echo "no active data-dir line (correct)"

# c) Mint an anonymous session FIRST. With the SA-01 gate active every /v1/* path except
#    health and session fails closed, so an unauthenticated briefing POST returns 401 — the
#    gate working, not a broken deploy. This is what the PWA's ensureSession() does on boot.
curl -s -c /tmp/uwx.jar -X POST https://app.upstreamwx.com/v1/session \
  -o /dev/null -w 'session mint: %{http_code}\n'
#   200

# d) A real current-window briefing, carrying the cookie (health does not write; this does).
#    Cold caches (GEFS/REFS + a cold pour-point delineation) — EXPECT ~25-60 s on first call.
D=$(date -u -d '+2 days' +%Y-%m-%d)
time curl -s -b /tmp/uwx.jar -c /tmp/uwx.jar \
  -o /tmp/brief.json -w '%{http_code}\n' \
  -X POST https://app.upstreamwx.com/v1/briefing \
  -H 'content-type: application/json' \
  -d "{\"lat\":37.0192,\"lon\":-111.9889,\"activity\":\"canyon\",\"start\":\"${D}T14:00\",\"end\":\"${D}T22:00\"}"
#   200

# d2) Keep the un-cookied call as a STANDING smoke test: if /v1/briefing ever returns 200
#     without a session, the access gate has silently opened.
curl -s -o /dev/null -w 'unauthenticated: %{http_code}\n' -X POST https://app.upstreamwx.com/v1/briefing \
  -H 'content-type: application/json' \
  -d "{\"lat\":37.0192,\"lon\":-111.9889,\"activity\":\"canyon\",\"start\":\"${D}T14:00\",\"end\":\"${D}T22:00\"}"
#   401

# e) The watershed fix (566f216 — HyRiver cache was written to CWD, which the read-only
#    release tree forbids). A 200 alone does not prove it: the basin can come back empty
#    while the briefing still renders.
python3 -c "import json;b=json.load(open('/tmp/brief.json'));w=b.get('watershed') or {};print('area_sq_mi:',w.get('area_sq_mi'),'geometry:',bool(w.get('geometry')))"
#   non-zero area, geometry True (Buckskin Gulch ~488 sq mi on the 2026-08-03 rebuild)
uwx-ctl logs -n 200 --no-pager | grep -iE 'read-only|permission denied|hyriver' || echo "clean"

# f) PDF export — exercises the SA-08 Chromium sandbox against this host's userns policy
curl -s -b /tmp/uwx.jar -o /tmp/b.pdf -w '%{http_code}\n' \
  -X POST https://app.upstreamwx.com/v1/briefing/pdf \
  -H 'content-type: application/json' --data-binary @/tmp/brief.json
file /tmp/b.pdf        # PDF document, N page(s)

# g) Public surfaces + redirect. NOTE: use GET, not `curl -I` (HEAD), against /v1/health —
#    FastAPI's @app.get registers GET only (APIRoute does not auto-add HEAD, unlike Starlette's
#    plain Route), so a HEAD falls through to the catch-all PWA StaticFiles mount and 404s on a
#    perfectly healthy service. Point external uptime monitors at GET for the same reason.
curl -sI https://app.upstreamwx.com/ | head -1                     # 200
curl -sI https://upstreamwx.com/      | head -1                    # 200 (static landing)
curl -sI http://app.upstreamwx.com/   | grep -i location           # 301/308 -> https
curl -sI https://app.upstreamwx.com/ | grep -i strict-transport    # HSTS present
curl -s -o /dev/null -w '%{http_code}\n' https://app.upstreamwx.com/v1/health   # 200 (GET)

# h) Unknown-Host handling (SA-09 default server). nginx `return 444` closes the connection
#    with no response, so curl reports 000 — that is a PASS, not a failure.
curl -sk https://<this-box-public-ip>/ -H 'Host: evil.example' -o /dev/null -w '%{http_code}\n'
#   000 (444, connection closed) — the app was never reached

# i) Renewal works from THIS box (only meaningful now that DNS points here)
sudo certbot renew --dry-run 2>&1 | tail -3

# j) Boot survival — the whole reason this rebuild exists
systemctl is-enabled upstreamwx-api    # enabled

# k) Scheduler + dead-man's-switch
uwx-ctl logs -n 30 --no-pager | grep -iE 'scheduler|cycle|healthcheck' || true
sudo grep -E 'UPSTREAMWX_HEALTHCHECK_URL' /etc/upstreamwx/upstreamwx.env
```

**Reboot test.** The box exists to survive one; prove it while nobody is watching:

```sh
sudo reboot
# then, once back:
systemctl is-active upstreamwx-api     # active
curl -s https://app.upstreamwx.com/v1/health | python3 -m json.tool | grep release
```

---

## Phase 6 — decommission and baseline

Only after Phase 5 passes clean, and not the same day.

```sh
# Snapshot the clean box as your future baseline (do this BEFORE any drift).
#   EC2 -> Actions -> Image and templates -> Create image
```

- **Stop** the old instance — do not terminate. Keep it a week; it is a one-click rollback while
  it exists (start it, move the EIP or the A records back).
- Back up `/root/upstreamwx-src/deploy/config.env` somewhere durable and private. Its absence is
  the root cause of this entire exercise.
- After the retention week: terminate the old instance, release its EIP if separate, and clean
  up the now-orphaned duplicate cert lineages if any were left behind.

---

## Rollback

| Stage | Action |
|---|---|
| Before cutover (Phases 1–3) | Nothing to roll back — the old box is still serving. Terminate the new instance and retry. |
| After cutover, before decommission | Start the old instance if stopped; move the EIP back (or revert the Namecheap A records). Old box still holds its own valid certs. |
| New box up but misbehaving | `uwx-ctl rollback` — but note a freshly built box has **only one** release, so there is no earlier release to fall back to. The rollback at this stage is the old *box*, not an old release. |

---

## Execution record — 2026-08-03

Executed on a clean EC2 instance (4 GB RAM, 30 GB root, Elastic IP re-associated so the public
names cut over ahead of provisioning). **First end-to-end proof of the from-scratch
`bootstrap.sh` path on production.** Both bootstrap passes completed with no errors.

Deviations from the runbook as written, all folded back into the text above:

- **`DEPLOY_CERTBOT_EMAIL` left empty for pass 1.** Bootstrap creates the ACME webroot and the
  nginx `:80` block, so issuance cannot be rehearsed until after it runs. Empty email ⇒
  `setup_tls_webroot` skips certbot ⇒ `--dry-run` rehearsal against a real webroot ⇒ set the
  email and re-run. A misconfiguration then costs a staging attempt, not a rate-limit slot.
- **EIP moved before provisioning**, so the temp-IP HTTP validation step was moot and DNS was
  already correct when certbot ran.
- Host baseline additions: a 4 GB swapfile with `vm.swappiness=10` (EBS-backed swap — at the
  default 60 the kernel pages out warm memory and briefings get mysteriously slow), and
  `kernel.apparmor_restrict_unprivileged_userns=0` for the SA-08 Chromium sandbox.
- `UPSTREAMWX_NWS_USER_AGENT` did **not** need carrying from the old box — bootstrap renders a
  valid self-identifying UA from the env example.

Validation results:

| Check | Result |
|---|---|
| `release` / `data_dir_ok` / `auth_active` / `trusted_hosts` | `v0.7.1` / true / true / true |
| Data-dir pin in process env; no active line in env file | pinned; absent (the 2026-07-20 outage condition, verified absent) |
| Session mint → briefing | 200, **25.8 s** cold |
| Un-cookied briefing | 401 (gate enforcing) |
| **Watershed (566f216)** | `area_sq_mi: 487.6`, `geometry: True`, logs clean |
| PDF export (SA-08 sandbox, no `--no-sandbox`) | 200, `PDF document, 2 page(s)` |
| Unknown Host | `000` (nginx 444) |
| HSTS / `certbot renew --dry-run` | present / success |
| `systemctl is-enabled` | `enabled` |

Certificate: one lineage `app.upstreamwx.com`, 3 SANs, 89 days. No duplicates on the clean box.

## Follow-ups this rebuild does not close

- **#158** — `DEPLOY_VERIFY_TAG_SIGNATURE` fails open on a lightweight tag.
- **#159** — no release signing key exists (SA-07). Until both land, the gate stays `0`.
- The duplicate cert lineages on the **old** box are retired with the box itself; nothing to fix
  on the clean one.
