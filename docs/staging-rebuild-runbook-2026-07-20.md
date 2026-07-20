# Runbook — staging box cleanup + rebuild (2026-07-20 incident)

Target: the EC2 staging host (`ip-172-31-44-135`, via SSM as `ssm-user`). Goal: remove the
stray prod-named install and every stale artifact, then rebuild the single staging
environment with the hardened deploy layer (issues #146/#147/#148; changelog
`docs/changelog-2026-07-20-staging-deploy-hardening.md`).

**Run the scripts from a clone of the branch that contains the fixes** (until merged:
`claude/staging-deployment-debug-uh7oxo`; after merge: `ux-7-19-26`/`main`). The old
scripts on the box must not be reused — they carry the ineffective unit pin and the
bootstrap abort.

## 0. Snapshot state (optional but cheap)

```sh
systemctl list-units 'upstreamwx*' --all
ls -la /opt/ /var/lib/ /etc/ | grep -i upstreamwx
sudo cp -a /etc/upstreamwx-staging /root/upstreamwx-staging.env.bak 2>/dev/null || true
```
Nothing in the data dirs is worth preserving (ensemble caches re-warm from the feeds).

## 1. Full teardown (both installs — the stray prod one AND the broken staging one)

```sh
# Stop + disable every UpstreamWX service
sudo systemctl disable --now upstreamwx-api upstreamwx-staging 2>/dev/null || true

# Units, drop-ins, and the incident-day datadir.conf drop-in
sudo rm -rf /etc/systemd/system/upstreamwx-api.service \
            /etc/systemd/system/upstreamwx-staging.service \
            /etc/systemd/system/upstreamwx-api.service.d \
            /etc/systemd/system/upstreamwx-staging.service.d
sudo systemctl daemon-reload
sudo systemctl reset-failed 2>/dev/null || true

# App trees, data dirs, env/config dirs — prod-named AND staging-named
sudo rm -rf /opt/upstreamwx /opt/upstreamwx-staging /opt/upstreamwx-src \
            /var/lib/upstreamwx /var/lib/upstreamwx-staging \
            /etc/upstreamwx /etc/upstreamwx-staging

# nginx sites (both namings), then reload
sudo rm -f /etc/nginx/sites-enabled/upstreamwx*.conf /etc/nginx/sites-available/upstreamwx*.conf \
           /etc/nginx/conf.d/upstreamwx*.conf
sudo nginx -t && sudo systemctl reload nginx

# The ops wrapper (whatever name(s) it was installed under)
sudo rm -f /usr/local/bin/uwx-ctl /usr/local/bin/uwx-staging

# Service accounts (both)
sudo userdel upstreamwx 2>/dev/null; sudo groupdel upstreamwx 2>/dev/null
sudo userdel upstreamwx-staging 2>/dev/null; sudo groupdel upstreamwx-staging 2>/dev/null
```

Verify the host is clean — **all four must come back empty**:

```sh
systemctl list-units 'upstreamwx*' --all --no-legend
ls -d /opt/upstreamwx* /var/lib/upstreamwx* /etc/upstreamwx* 2>/dev/null
ls /etc/systemd/system/upstreamwx* 2>/dev/null
ls /usr/local/bin/uwx* 2>/dev/null
```

## 2. Clone the fixed branch (NOT under /opt)

```sh
cd ~ && rm -rf upstreamwx-src
git clone -b claude/staging-deployment-debug-uh7oxo \
    https://github.com/osh-labs/upstreamwx.git upstreamwx-src
cd upstreamwx-src
```

## 3. Create the staging config

```sh
cp deploy/config.staging.env.example deploy/config.staging.env
nano deploy/config.staging.env
#   DEPLOY_BRANCH="ux-7-19-26"          # or the fixes branch until it's merged
#   DEPLOY_APP_SERVER_NAME="<node>.<tailnet>.ts.net"
#   (leave DEPLOY_LANDING_SERVER_NAME empty; ctl name is uwx-staging)
```

## 4. Bootstrap — explicit config, guards active

```sh
sudo DEPLOY_CONFIG=deploy/config.staging.env deploy/bootstrap.sh
```

Expected: the #146 conflict scan passes on the now-clean host (any leftover from step 1
hard-stops here — fix and re-run); the env file installs fresh from the corrected example
(no active `UPSTREAMWX_DATA_DIR`); the unit renders with the data dir pinned inside
`ExecStart`; `uwx-staging` lands rendered in `/usr/local/bin`; the delegated first deploy
builds, warms, flips `current`, and health-checks.

Then set the staging NWS contact:

```sh
sudo nano /etc/upstreamwx-staging/upstreamwx.env    # UPSTREAMWX_NWS_USER_AGENT, optional keys
sudo systemctl restart upstreamwx-staging
```

## 5. Prove the fix (the checks the incident taught us)

```sh
# a) Live process environment — must NOT carry a data-dir override from the env file
PID=$(systemctl show -p MainPID --value upstreamwx-staging)
sudo cat /proc/$PID/environ | tr '\0' '\n' | grep -E 'UPSTREAMWX_DATA_DIR|PLAYWRIGHT'
# UPSTREAMWX_DATA_DIR=/var/lib/upstreamwx-staging   <- pinned by the ExecStart env wrapper

# b) The env file must carry NO active data-dir line (bootstrap migrates/never re-adds one)
sudo grep -nE '^[[:space:]]*UPSTREAMWX_DATA_DIR=' /etc/upstreamwx-staging/upstreamwx.env \
  && echo 'FAIL: active data-dir line present' || echo 'OK: no active data-dir line'

# c) Health — data_dir_ok must be true (new #147 signal)
curl -s localhost:8001/v1/health | python3 -m json.tool | grep -E 'data_dir_ok|release'

# d) A CURRENT-window briefing (past windows 422 before touching the cache) — expect 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8001/v1/briefing \
  -H 'content-type: application/json' \
  -d '{"lat":37.0192,"lon":-111.9889,"activity":"canyon","start":"2026-07-21T08:00","end":"2026-07-21T18:00"}'

# e) The guards themselves: a config-less run must refuse (exit non-zero, no changes)
sudo deploy/deploy.sh 2>&1 | head -3   # expect the "DEPLOY_CONFIG is not set" die

# f) Re-bootstrap through the wrapper completes end-to-end (#148 regression check)
uwx-staging bootstrap
uwx-staging health
```

## 6. Ongoing ops on this box

```sh
uwx-staging deploy [ref]    # build + activate (ref defaults to DEPLOY_BRANCH)
uwx-staging logs -f         # journald namespace follow
uwx-staging rollback        # previous release
```

Even with a wrong/stale env file in the future, the service's data dir can no longer be
diverted (ExecStart pin), a wrong `DEPLOY_CONFIG` can no longer create a second install
(hard blocks), and a broken cache root degrades the briefing instead of 500ing it
(`data_dir_ok` on `/v1/health` is the monitoring hook).
