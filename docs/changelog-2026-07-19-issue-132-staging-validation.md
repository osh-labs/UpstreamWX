# Changelog — 2026-07-19 — Issue #132 staging-validation fixes

Follow-up to the issue #132 host-hardening work (PR #137), fixing four issues that only
surfaced when the atomic-release deploy was first run on a **real Ubuntu 24.04 (noble) host**.
The clean-box renders and offline suite couldn't catch these — they're all "the real host
differs from the assumption" gaps, which is exactly what the staging pass is for. Engine
output unchanged (NFR-4); every change is in the deploy layer.

## Fixes

1. **Ubuntu 24.04 `t64` Chromium libs (`bootstrap.sh`).** noble's 64-bit `time_t` transition
   renamed several Chromium host libraries with a `t64` suffix (`libasound2` →
   `libasound2t64`, plus `libatk1.0-0`, `libatk-bridge2.0-0`, `libcups2`, `libatspi2.0-0`),
   so the hardcoded apt list had "no installation candidate" and aborted on the first step.
   The Chromium libs are now resolved per-package to whichever variant the distro ships
   (plain or `t64`) and installed best-effort (a missing one only degrades PDF to 503, NFR-6);
   essentials (nginx, eccodes) stay a strict install.

2. **uv-managed interpreter unreachable by the service user (`_lib.sh`).** With `uv sync`
   running as root (SA-06) and no system Python 3.11 on noble, uv downloaded a managed CPython
   into root's `~/.local` (0700). The release venv's `python` symlinks into that path, so the
   non-root service got `EACCES` executing uvicorn — the deploy failed the health check with
   "Permission denied". uv now installs the managed interpreter into a shared, world-readable
   dir (`DEPLOY_UV_PYTHON_DIR`, default `<app_dir>/uv-python`), stable across releases.

3. **Data-dir mismatch (`systemd` unit, `deploy.sh`, env example).** The shared
   `upstreamwx.env.example` hardcoded `UPSTREAMWX_DATA_DIR=/var/lib/upstreamwx` (the prod
   path), which bootstrap installs verbatim for staging — so the staging service resolved its
   data dir to a path it doesn't own and that systemd's `ReadWritePaths` makes read-only. The
   systemd unit now pins `Environment=UPSTREAMWX_DATA_DIR=__DATA_DIR__` (after the
   `EnvironmentFile` line, so it always overrides the file and matches `ReadWritePaths`); the
   deploy warm appends the same value last; the env example comments the hardcoded path out.

4. **`nginx -t` failure surfaced (`bootstrap.sh`).** bootstrap swallowed `nginx -t` output on
   failure; it now prints it. (The actual failure on the staging box was a stale, pre-atomic
   `upstreamwx-api.conf` site left over from an earlier run, colliding on the shared
   `upstream upstreamwx_api` name — a leftover-cleanup, not a template bug. Separate boxes for
   staging/prod means the hardcoded upstream name is fine; a stale duplicate site just needs
   removing.)

## Staging validation results (this host)

Ran end-to-end on the tailnet staging box (Ubuntu 24.04, kernel 7.0.0-aws), all green:

- Atomic release build → root-owned `releases/<sha>` → `current` symlink flip → healthy.
- uv-managed interpreter reachable; REFS warm 64/64, GEFS warm partial (expected — the fresh
  18Z cycle hadn't published the longer lead hours yet; scheduler backfills).
- Data dir resolves to `/var/lib/upstreamwx-staging`; no permission denials post-restart.
- `nginx -t` clean after removing the stale site.
- **SA-08 PDF sandbox confirmed:** `POST /v1/briefing/pdf` returned HTTP 200 and a valid
  3-page PDF, i.e. headless Chromium launched with its **native sandbox** (no `--no-sandbox`)
  under the systemd `RestrictNamespaces=user mnt pid net` policy as the non-root service user.
  noble allows unprivileged user namespaces by default, so no `UPSTREAMWX_PDF_NO_SANDBOX`
  fallback was needed.
- Rollback: validated by inspection (the atomic `current` flip it relies on is exercised on
  every deploy).

## Notes for the production deploy

- These fixes make a first-try clean deploy on a matching noble host likely; the PDF sandbox
  is the one thing still worth confirming live on prod (kernel/AppArmor-dependent), though the
  same AMI family should behave identically.
- Running `git` directly in the root-owned mirror (`<app_dir>/repo`) as a login user trips
  git's "dubious ownership" guard — prefix `sudo`, or just don't (it's the deploy's mirror).
