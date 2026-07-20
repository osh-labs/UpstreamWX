# Changelog — 2026-07-20: staging 500 root cause + deploy hardening (issues #146/#147/#148)

Follow-up to the 2026-07-20 staging outage (every `POST /v1/briefing` with a current window
returned 500 with `PermissionError: /var/lib/upstreamwx/gefs`). Engine output is unchanged
(NFR-4) — every change here is deploy-layer or failure-path behavior.

## Root cause (the full chain)

1. **Stale env file (original defect, pre-dates the incident).** The runtime env example
   shipped `UPSTREAMWX_DATA_DIR=/var/lib/upstreamwx` **active** at line 12 from the deploy
   layer's first commit; bootstrap installed it verbatim for staging (Jul 18) and never
   touches an existing env file afterward, so the prod path stayed live in
   `/etc/upstreamwx-staging/upstreamwx.env` across every redeploy and the teardown/rebuild.
2. **Ineffective fix (`87e2004`, Jul 18).** The unit gained
   `Environment=UPSTREAMWX_DATA_DIR=__DATA_DIR__` rendered *after* `EnvironmentFile=`, on the
   assumption later lines win. **systemd does not work that way**: `EnvironmentFile=` always
   overrides `Environment=` regardless of order in the unit (man systemd.exec — "Settings from
   these files override settings made with Environment="). The pin (and the incident-day
   drop-in, also `Environment=`) silently lost to the env file. This also explains every
   "anomaly" in the incident: correct `systemctl cat` output, correct drop-in, wrong process env.
3. **Silent prod default (the accident enabler).** `deploy.sh`/`bootstrap.sh` with no
   `DEPLOY_CONFIG` fell back to `deploy/config.env` = prod defaults, creating the stray
   `upstreamwx-api` service and the orphaned root-`/var/lib/upstreamwx` (0750, deleted uid)
   that the staging service — misdirected by (1)+(2) — then could not read.
4. **Hard 500 instead of degradation (#147).** `gefs.cached_cycles()` probes the cache root
   with `Path.is_dir()`, which **raises** `PermissionError` rather than returning False; the
   exception escaped `BriefingService._cycle_token` → unhandled 500 on every briefing.
5. **Half-completed re-bootstrap (#148).** Re-running bootstrap with
   `DEPLOY_CONFIG=/etc/upstreamwx-staging/deploy.conf` aborted at the ctl-config `install`
   ("are the same file" under `set -e`), leaving `/usr/local/bin/uwx-ctl` as the unrendered
   template — introduced by the uwx-ctl PR (`dd9c3e1`, #142).

No recently-merged PR *introduced* the wrong data-dir value; the recent regressions are the
ineffective pin (2, shipped in the issue-#132 branch) and the bootstrap abort (5, PR #142).

## Fixes

### systemd unit (`deploy/systemd/upstreamwx-api.service`)
- The data-dir pin moved **inside `ExecStart`** via `/usr/bin/env UPSTREAMWX_DATA_DIR=…` (and
  `PLAYWRIGHT_BROWSERS_PATH` with it). env(1) applies after systemd assembles the environment,
  so no env-file line or drop-in can divert the runtime data dir away from `ReadWritePaths=`
  again. The misleading "comes AFTER EnvironmentFile= so it overrides" comment is corrected.

### Issue #146 — hard blocks (`deploy/_lib.sh`, `bootstrap.sh`, `deploy.sh`)
- **`DEPLOY_CONFIG` is now required.** `load_config` dies with an actionable message when it
  is unset — no silent prod default. `uwx-ctl` already bakes the config in, so routine ops
  are unchanged; bootstrap passes it through to its delegated `deploy.sh` run explicitly.
- **`check_install_conflicts`** (run by both scripts before any write): any `upstreamwx*`
  systemd unit / `/opt/upstreamwx*` / `/var/lib/upstreamwx*` / `/etc/upstreamwx*` entry that
  doesn't match the loaded config is a hard stop listing the conflicts and the cleanup
  commands. `DEPLOY_ALLOW_COEXIST=1` is the documented escape hatch for a deliberate
  two-environment box; the running checkout (`$REPO_DIR`) is exempt so a rebuild clone under
  `/opt` isn't a false positive.
- **`check_data_dir_owner`**: an existing `DEPLOY_DATA_DIR` owned by anything other than
  `DEPLOY_USER` (or root, which bootstrap re-owns) is a hard stop — the direct cause of the
  briefing 500s.
- **`sanitize_env_file_data_dir`**: bootstrap's one in-place migration of an existing env
  file — an active `UPSTREAMWX_DATA_DIR=` line is commented out (marked `MIGRATED by
  bootstrap`) with a loud warning, closing the (1)+(2) combination permanently.
- `bootstrap.sh` refuses to overwrite a `/usr/local/bin/<ctl>` baked for a **different**
  config (set a distinct `DEPLOY_CTL_NAME` per env; the staging example now ships
  `DEPLOY_CTL_NAME="uwx-staging"`).

### Issue #148 — bootstrap same-file abort (`deploy/bootstrap.sh`)
- The ctl-config install compares `readlink -f` of source and dest and skips the copy when
  they are the same file, so `uwx-ctl bootstrap` (which sets
  `DEPLOY_CONFIG=$DEPLOY_CTL_CONFIG`) completes end-to-end and always renders the wrapper.

### Issue #147 — graceful cache-root degradation (backend)
- `gefs/cache.py::cached_cycles`, `ingest/refs_selection.py::cached_cycles`, and
  `sref/cache.py::cached_cycles` now treat an unreadable cache root (`OSError` from the
  `is_dir` probe or the listing) as **empty**: one WARNING log with the path, then the normal
  cold-cache path (live probe → wall-clock token → cold ingest). Per-entry `OSError`s while
  scanning are skipped. A data-dir misconfig now yields a degraded briefing, never a 500.
- `/v1/health` gains **`data_dir_ok`** (boolean only — SA-12): whether the runtime data dir
  exists/can be created and is writable by the process, so monitoring catches the misconfig
  users no longer will.
- New hermetic suite `tests/test_cache_root_degradation.py` (8 tests): permission-denied
  root and unreadable listing for all three readers, the `_cycle_token` wall-clock fallback,
  and the `data_dir_ok` signal.

### `uwx-ctl` — rendered correctly, plus a scripted uninstaller (follow-up, same day)
- **`render_template` had no `__SERVICE__` substitution rule**, so even a *successful*
  bootstrap always installed the wrapper with `SERVICE="__SERVICE__"` unrendered — the
  `Unit __SERVICE__.service could not be found` symptom on the staging box, previously
  attributed solely to the #148 abort. Introduced by the uwx-ctl PR (`dd9c3e1`, #142), which
  was the first template to use the token. The rule is added (and `__ENV_DIR__` with it).
- **`uwx-ctl uninstall`**: the scripted form of the incident teardown, baked into the
  wrapper (works with no active release tree — exactly the half-broken-box case). Removes
  only this wrapper's environment — unit + drop-ins, nginx sites (`<svc>.conf`,
  `<svc>-default.conf`, `<svc>-landing.conf`), app/data/env dirs, service account, and the
  wrapper itself — after a typed service-name confirmation (`--yes` for automation;
  `--keep-data` preserves the cache dir). Baked paths are sanity-checked against a path
  allowlist before any `rm -rf`; every step tolerates a half-broken host; a coexisting
  second environment is untouched and enumerated afterwards. Works identically for staging
  and prod since it is rendered from each env's own config.

### Docs
- `deploy/README.md`: explicit-`DEPLOY_CONFIG` everywhere, new "One install per host" section.
- `deploy/config.env.example` / `config.staging.env.example`: `DEPLOY_ALLOW_COEXIST`,
  staging `DEPLOY_CTL_NAME`, explicit-config notes.
- `docs/staging-rebuild-runbook-2026-07-20.md`: the box cleanup + rebuild procedure.
