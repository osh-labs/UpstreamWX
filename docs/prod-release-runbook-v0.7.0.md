# Production release runbook — v0.7.0

Promote the staging-validated changes to the **public production** box as the immutable tag
**`v0.7.0`**. This is the deliberate staging → prod step of the release ladder
(`docs/deployment-workflow.md`): nobody edits code on prod; the box only ever runs an exact,
named tag.

**What v0.7.0 contains** (everything ahead of the last prod tag `v0.6.2`):

- the US/metric unit localization;
- the hourly hazard series + time-aware phases;
- the 2026-07-20 staging-outage hardening (issues #146/#147/#148 + `uwx-ctl uninstall`, PR #149);
- a **watershed delineation fix** under the read-only release tree — the HyRiver cache was
  being written to CWD (`566f216`, PR #150);
- a **deploy fix**: `uwx-ctl deploy`/`bootstrap` run the release-tree exec check as root
  (`008efce`, PR #152);
- a **PWA UX pass**: confidence rendered as inline equal-height bars and centred under the
  posture pill, hazard-card confidence above the fold, Risk Discussion collapsed by default,
  the hero posture-chip pulse kept across tab switches, and an iOS Safari fix for the summary
  card vanishing (PRs #151/#153/#154/#155/#156).

**The commit being shipped:** `5254db4` — the tip of `main` (the PR #157 merge), already
tagged and validated on the staging box.

> The first three bullets are what this runbook originally scoped (commit `3d8401c`). The rest
> landed on `main` afterwards and are included in the tag; staging validated the tag, so the
> validated artifact and the shipped artifact still match.

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

## Phase 1 — cut the release — **already done**

`main` is CI-gated and `v0.7.0` is already pushed and staging-validated. Just confirm it still
points where you expect:

```sh
git fetch origin main --tags --prune
git rev-parse v0.7.0^{commit}        # expect 5254db42560d2fb735eb6f2ec59b139e55723999
git rev-parse origin/main            # same commit — the tag is main's tip
```

### Note: the SA-07 signature gate does **not** apply to this release

`v0.7.0` is a **lightweight** tag (a ref pointing straight at the commit), not a signed
annotated tag object — as is every tag in this repo's history, `v0.4.0` onward. Two
consequences you need to know before Phase 2:

1. **There is no release signing key.** The `DEPLOY_VERIFY_TAG_SIGNATURE` gate landed in
   `417ce08` (issue #132, 2026-07-18) as the verifier half of audit finding SA-07, but the
   signing half — generate a key, publish the public half to prod's root GPG keyring, adopt
   `git tag -s` — was explicitly deferred (`docs/sa-06-09-13-hardening-workplan.md` §7, still
   unchecked). Do not go looking for a key that was never created.
2. **The gate fails open, silently.** `deploy/_lib.sh` only verifies when the ref resolves to a
   tag *object*:

   ```sh
   if [ "${DEPLOY_VERIFY_TAG_SIGNATURE:-0}" = "1" ] \
           && [ "$(git -C "$DEPLOY_REPO_MIRROR" cat-file -t "$ref" 2>/dev/null)" = "tag" ]; then
   ```

   A lightweight tag makes the second test false, so verification is skipped with no error and
   no log line. This is how `v0.6.x` deployed too — the gate has never actually run.

So Phase 2 sets `DEPLOY_VERIFY_TAG_SIGNATURE=0`, to make the config state the truth rather than
imply a check that silently no-ops. Both the fail-open behaviour and the key setup are tracked
as follow-ups; neither blocks this deploy (prod pins an exact immutable tag off a CI-gated
`main`, and tag push is restricted).

Optional cosmetic: `pyproject.toml version` is stale (`0.5.0`) and does **not** drive the
deployed version string (that comes from `git describe --tags` → `v0.7.0`). Bump it to `0.7.0`
if you want the source tree to match; it changes nothing at runtime.

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

# Public gates that MUST be on for a public deploy (deploy.sh enforces REQUIRE_HTTPS):
grep -E 'DEPLOY_(REQUIRE_HTTPS|VERIFY_TAG_SIGNATURE|BRANCH)' /etc/upstreamwx/deploy.conf
grep -E 'UPSTREAMWX_(SESSION_SECRET|API_AUTH_REQUIRED|API_TRUSTED_HOSTS)' /etc/upstreamwx/upstreamwx.env
#   expect DEPLOY_REQUIRE_HTTPS=1,
#          SESSION_SECRET set, API_AUTH_REQUIRED=1, API_TRUSTED_HOSTS=["app.upstreamwx.com"]

# Signature gate: set it to 0 (see Phase 1 — no release key exists, and left at 1 it silently
# no-ops on a lightweight tag, which reads as "verified" when nothing was checked):
sudo sed -i 's/^DEPLOY_VERIFY_TAG_SIGNATURE=.*/DEPLOY_VERIFY_TAG_SIGNATURE="0"/' /etc/upstreamwx/deploy.conf
# No GPG keyring step this release — there is no signer key to import.

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
file, `daemon-reload`s, then hands off to `deploy.sh` which builds a fresh root-owned release,
warms the caches, **atomically flips `current`**, restarts, and **blocks on `/v1/health` —
auto-rolling-back** if the new release is unhealthy.

```sh
# 1. Pin the deploy config to the tag (bootstrap has no ref argument; it deploys DEPLOY_BRANCH,
#    and deploying the *tag ref* is what triggers SA-07 signature verification — a branch skips
#    it). Pinning prod to an immutable tag is also the correct end state (better than tracking a
#    moving `main`). Set it in BOTH the persisted config and the clone's config:
sudo sed -i 's/^DEPLOY_BRANCH=.*/DEPLOY_BRANCH="v0.7.0"/' /etc/upstreamwx/deploy.conf
sed        -i 's/^DEPLOY_BRANCH=.*/DEPLOY_BRANCH="v0.7.0"/' deploy/config.env

# 2. Run the promotion. #146 conflict checks pass (prod matches its own config). Watch the tail
#    for: the health JSON, and "deployed v0.7.0".
#    Do NOT expect a "tag signature verified" line — see Phase 1; nothing is verified this
#    release, and its absence is not a failure.
sudo DEPLOY_CONFIG=deploy/config.env deploy/bootstrap.sh
```

A public `bootstrap`/`deploy` will **refuse** (non-zero, no activation) if HTTPS isn't live —
that is the gate doing its job, not a regression. Fix the flagged cause and re-run.

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

# c) A real current-window briefing over HTTPS returns 200 (health doesn't write; this does).
#    The window MUST be in the future or MissionSpec currency validation rejects it — this
#    builds one two days out rather than hard-coding a date that goes stale:
D=$(date -u -d '+2 days' +%Y-%m-%d)
curl -s -o /tmp/brief.json -w '%{http_code}\n' -X POST https://app.upstreamwx.com/v1/briefing \
  -H 'content-type: application/json' \
  -d "{\"lat\":37.0192,\"lon\":-111.9889,\"activity\":\"canyon\",\"start\":\"${D}T14:00\",\"end\":\"${D}T22:00\"}"
#   expect 200

# c2) THIS RELEASE ships the watershed fix (566f216 — HyRiver cache was written to CWD, which
#     the read-only release tree forbids). A 200 alone does not prove it worked: the basin can
#     come back empty while the briefing still renders. Confirm a real upstream basin:
python3 -c "import json;b=json.load(open('/tmp/brief.json'));w=b.get('watershed') or {};print('area_sq_mi:',w.get('area_sq_mi'),'geometry:',bool(w.get('geometry')))"
#   expect a non-zero area and geometry True. Also confirm nothing is trying to write to the
#   release tree:
uwx-ctl logs -n 200 --no-pager | grep -iE 'read-only|permission denied|hyriver|pygeohydro' || echo "clean"

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
uwx-ctl deploy v0.7.1     # health-gated, auto-rollback
```

If you left `DEPLOY_BRANCH` pinned to a tag in `deploy.conf`, a bare `uwx-ctl deploy` re-deploys
that pinned tag; always pass the new tag explicitly to move forward. Keep pinning prod to tags —
never let the prod box track a moving branch.

---

## Follow-ups carried out of this release

Neither blocks v0.7.0; both should land before the signature gate is trusted.

1. **`DEPLOY_VERIFY_TAG_SIGNATURE` fails open** (`deploy/_lib.sh`). When the gate is on but the
   ref is not a signed tag object, the deploy proceeds silently instead of refusing. It should
   fail closed, so turning the flag on cannot produce a false sense of verification.
2. **No release signing key exists** (SA-07, deferred from issue #132). Closing it means
   generating a release key, deciding where the private half lives, importing the public half
   into **root's** GPG keyring on prod, and switching releases to `git tag -s`. Until then
   `DEPLOY_VERIFY_TAG_SIGNATURE` stays `0` on every environment.
