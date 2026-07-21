# Production release runbook — v0.7.0

Promote the staging-validated changes to the **public production** box as the immutable tag
**`v0.7.0`**. This is the deliberate staging → prod step of the release ladder
(`docs/deployment-workflow.md`): nobody edits code on prod; the box only ever runs an exact,
named tag.

**What v0.7.0 contains** (everything on `ux-7-19-26` that is ahead of the last prod tag
`v0.6.2`): the US/metric unit localization, the hourly hazard series + time-aware phases, and
the 2026-07-20 staging-outage hardening (issues #146/#147/#148 + `uwx-ctl uninstall`, PR #149).

**The commit being shipped:** `3d8401c` — the tip of `ux-7-19-26`, the exact commit validated
on the staging box. Tag that commit, deploy that tag.

> **One release-specific wrinkle:** the systemd unit **template changed** this release
> (the data-dir pin moved into `ExecStart`). `uwx-ctl deploy` / `deploy.sh` build and activate
> a release but do **not** re-render the systemd unit — only `bootstrap` does. So this
> promotion re-renders the unit via a bootstrap run (Phase 3), not a bare `deploy`. Future
> releases that don't touch the unit template are a plain `uwx-ctl deploy <tag>`.

Prod facts assumed below (confirm in Phase 2): service `upstreamwx-api` on `127.0.0.1:8000`,
app `/opt/upstreamwx`, data `/var/lib/upstreamwx`, persisted deploy config
`/etc/upstreamwx/deploy.conf`, runtime env `/etc/upstreamwx/upstreamwx.env`, wrapper `uwx-ctl`,
public names `app.upstreamwx.com` (app) + `upstreamwx.com`/`www` (landing).

---

## Phase 1 — cut the release (from your laptop, not the box)

The fixes live on `ux-7-19-26`; prod deploys a tag off `main`. Get the code into `main`, then
tag it.

```sh
# 1. Open a PR ux-7-19-26 -> main and let CI (ruff + hermetic pytest) go green, then merge it.
#    (Do NOT commit to main directly — main must stay a deployable, CI-gated branch.)
#    Merge so that main's tip is the SAME tree validated on staging (no extra commits slipped
#    in between). If anything else lands on main first, re-validate on staging before tagging.

# 2. Tag the release commit. SIGN it — prod's DEPLOY_VERIFY_TAG_SIGNATURE gate (SA-07) refuses
#    an unsigned/invalid tag at build time. (An unsigned `-a` tag only works if that gate is 0.)
git fetch origin main
git tag -s v0.7.0 origin/main -m "v0.7.0 — units localization, hourly hazard series, staging-deploy hardening (#146/#147/#148)"
git push origin v0.7.0

# 3. Confirm the tag points at the staging-validated commit:
git rev-parse v0.7.0^{commit}        # expect the commit staging ran (3d8401c's mainline equivalent)
```

Optional cosmetic: `pyproject.toml version` is stale (`0.5.0`) and does **not** drive the
deployed version string (that comes from `git describe --tags` → `v0.7.0`). Bump it to `0.7.0`
in the same release PR if you want the source tree to match; it changes nothing at runtime.

---

## Phase 2 — pre-flight on the prod box (no changes yet)

SSH in, then confirm the box is healthy on the OLD release and that the public gates are set
before you touch anything.

```sh
# Current state — record the release you're rolling FROM (for a manual rollback target).
uwx-ctl version
uwx-ctl health | python3 -m json.tool | grep -E 'release|data_dir_ok|auth_active|trusted_hosts'
readlink -f /opt/upstreamwx/current
uwx-ctl releases                     # note the previous release dir; rollback uses it

# Public gates that MUST be on for a public deploy (deploy.sh enforces the first two):
grep -E 'DEPLOY_(REQUIRE_HTTPS|VERIFY_TAG_SIGNATURE|BRANCH)' /etc/upstreamwx/deploy.conf
grep -E 'UPSTREAMWX_(SESSION_SECRET|API_AUTH_REQUIRED|API_TRUSTED_HOSTS)' /etc/upstreamwx/upstreamwx.env
#   expect DEPLOY_REQUIRE_HTTPS=1, DEPLOY_VERIFY_TAG_SIGNATURE=1,
#          SESSION_SECRET set, API_AUTH_REQUIRED=1, API_TRUSTED_HOSTS=["app.upstreamwx.com"]

# If DEPLOY_VERIFY_TAG_SIGNATURE=1, the tag signer's PUBLIC key must be in ROOT's GPG keyring:
sudo gpg --list-keys                 # the key that signed v0.7.0 must be here, else import it:
# sudo gpg --import /path/to/signer-pubkey.asc

# Back up the runtime env file (it holds secrets; bootstrap won't clobber it, but be safe):
sudo cp -a /etc/upstreamwx/upstreamwx.env /root/upstreamwx.env.$(date -u +%Y%m%dT%H%M%SZ).bak
```

Refresh the deploy-scripts clone the bootstrap will run FROM to the v0.7.0 templates (bootstrap
reads templates from the running scripts' own repo, not from the release tree):

```sh
cd ~/upstreamwx-src 2>/dev/null || git clone https://github.com/osh-labs/upstreamwx.git ~/upstreamwx-src && cd ~/upstreamwx-src
git fetch origin --tags --prune
git checkout v0.7.0                  # run the deploy from the tagged tree itself
cp deploy/config.env.example deploy/config.env   # only if you don't already keep deploy/config.env; then re-apply prod names/gates
```

> If you keep a filled-in `deploy/config.env` on the box, use it as-is — don't overwrite it.
> The persisted `/etc/upstreamwx/deploy.conf` is what `uwx-ctl` uses; `deploy/config.env` is
> only what a from-clone `bootstrap` reads.

---

## Phase 3 — promote to v0.7.0 (re-render the unit + deploy the tag)

Pin prod to the immutable tag, then run bootstrap from the clone. `bootstrap` re-renders the
hardened systemd unit + nginx sites, comments out any stale `UPSTREAMWX_DATA_DIR` in the env
file, `daemon-reload`s, then hands off to `deploy.sh` which **verifies the tag signature
(SA-07)**, builds a fresh root-owned release, warms the caches, **atomically flips `current`**,
restarts, and **blocks on `/v1/health` — auto-rolling-back** if the new release is unhealthy.

```sh
# 1. Pin the deploy config to the tag (bootstrap has no ref argument; it deploys DEPLOY_BRANCH,
#    and deploying the *tag ref* is what triggers SA-07 signature verification — a branch skips
#    it). Pinning prod to an immutable tag is also the correct end state (better than tracking a
#    moving `main`). Set it in BOTH the persisted config and the clone's config:
sudo sed -i 's/^DEPLOY_BRANCH=.*/DEPLOY_BRANCH="v0.7.0"/' /etc/upstreamwx/deploy.conf
sed        -i 's/^DEPLOY_BRANCH=.*/DEPLOY_BRANCH="v0.7.0"/' deploy/config.env

# 2. Run the promotion. #146 conflict checks pass (prod matches its own config). Watch the tail
#    for: "tag signature verified", the health JSON, and "deployed v0.7.0".
sudo DEPLOY_CONFIG=deploy/config.env deploy/bootstrap.sh
```

A public `bootstrap`/`deploy` will **refuse** (non-zero, no activation) if HTTPS isn't live or
the tag signature fails — that is the gate doing its job, not a regression. Fix the flagged
cause and re-run.

> **nginx note:** bootstrap `restart`s nginx (to pick up group membership); expect a sub-second
> blip on the public site. The app (uvicorn) is only restarted by the atomic flip after the new
> release is built, so the API is not down during the build.

---

## Phase 4 — verify (the checks that actually close it)

```sh
# a) Running version is v0.7.0
uwx-ctl version
curl -s https://app.upstreamwx.com/v1/health | python3 -m json.tool \
  | grep -E 'release|data_dir_ok|auth_active|trusted_hosts'
#   release "v0.7.0", data_dir_ok true, auth_active true, trusted_hosts true

# b) The data-dir pin is live in the process env (the ExecStart hardening actually landed):
PID=$(systemctl show -p MainPID --value upstreamwx-api)
sudo cat /proc/$PID/environ | tr '\0' '\n' | grep UPSTREAMWX_DATA_DIR
#   UPSTREAMWX_DATA_DIR=/var/lib/upstreamwx

# c) A real current-window briefing over HTTPS returns 200 (health doesn't write; this does):
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://app.upstreamwx.com/v1/briefing \
  -H 'content-type: application/json' \
  -d '{"lat":37.0192,"lon":-111.9889,"activity":"canyon","start":"2026-07-22T14:00","end":"2026-07-22T22:00"}'
#   (use a start/end a day or two ahead so the window is current)

# d) Public surfaces: app shell, landing page, and HTTP->HTTPS redirect
curl -sI https://app.upstreamwx.com/ | head -1          # 200
curl -sI https://upstreamwx.com/      | head -1          # 200 (static landing)
curl -sI http://app.upstreamwx.com/v1/health | grep -i location   # 301/308 -> https

# e) TLS renewal timer still healthy (unchanged by this deploy, but confirm):
sudo certbot renew --dry-run 2>&1 | tail -3

# f) Scheduler is running and the dead-man's-switch is wired (stale briefings are the worst
#    failure mode for this app):
uwx-ctl logs -n 30 --no-pager | grep -iE 'scheduler|cycle|healthcheck' || true
grep -E 'UPSTREAMWX_HEALTHCHECK_URL' /etc/upstreamwx/upstreamwx.env
```

**PWA clients:** the release is tied to the service-worker URL (`sw.js?v=v0.7.0`) and the app
polls `version.json`, so installed clients get a non-dismissible "Update available — reload"
nudge automatically. No manual cache step. Reload once yourself and confirm the app comes up on
v0.7.0.

---

## Phase 5 — rollback

Releases are immutable and kept on disk (`DEPLOY_KEEP_RELEASES`, default 5), so rollback is
cheap.

- **Automatic:** if the new release fails the post-deploy `/v1/health` check, `deploy.sh`
  flips `current` back to the previous release and restarts — the run ends with
  "ROLLED BACK to …". Nothing to do but investigate.
- **Manual** (the new release is up but misbehaving): re-point to the prior tag. It reuses the
  already-built release dir (fast) and re-flips the symlink:

  ```sh
  uwx-ctl rollback          # -> the previous release on disk (fastest)
  # or an explicit prior tag:
  uwx-ctl deploy v0.6.2
  ```

  Confirm: `uwx-ctl version` and a current-window briefing return the expected release + 200.

---

## After this release — routine promotions

The unit template only changed *this* release. From v0.7.1 onward, a normal prod promotion is a
single wrapper command (no bootstrap needed unless a future release again edits
`deploy/systemd/*` or `deploy/nginx/*`):

```sh
# On the box, after the tag is pushed:
uwx-ctl deploy v0.7.1     # signature-verified (it's a tag), health-gated, auto-rollback
```

If you left `DEPLOY_BRANCH` pinned to a tag in `deploy.conf`, a bare `uwx-ctl deploy` re-deploys
that pinned tag; always pass the new tag explicitly to move forward. Keep pinning prod to tags —
never let the prod box track a moving branch.
