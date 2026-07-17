# PR B Workplan — Reproducible/root-safe deploy (SA-06), TLS & host validation (SA-09), log redaction (SA-13)

- **Findings (Security Audit 2026-07-14):**
  - **SA-06** — *Deployment is not reproducible and crosses the root trust boundary* (High)
  - **SA-09** — *Repository deployment can succeed without enforced TLS or host validation* (Medium; treated as a hard prerequisite here because it gates the SA-01 access gate)
  - **SA-13** — *Healthcheck failure logging can disclose the secret ping URL* (Low)
- **Why grouped:** SA-06 and SA-09 both edit the deploy layer (`deploy/deploy.sh`, `deploy/bootstrap.sh`,
  `deploy/_lib.sh`, nginx templates, config examples) and would conflict as parallel PRs; SA-13 is a
  two-line redaction folded in to clear it. (PR A — SA-05 CDN/CSP — is a separate, parallel PR that also
  touches nginx `add_header`; coordinate the nginx overlap.)
- **Status:** 🟡 **Partially implemented** on branch `claude/security-audit-sa-03-04-v4sc2s`. The
  in-repo, offline-verifiable parts are done and tested (486 offline tests pass, ruff clean, shell
  scripts `bash -n` clean). The parts that require the live EC2 host (root-owned atomic release
  directories, certbot/TLS issuance, an SBOM pipeline) are **specified here and deferred to a host
  validation pass** (§5) — they cannot be exercised in the ephemeral container.

---

## 1. What each finding is, against the current code

### SA-06 — non-reproducible + root executes service-user code
- No committed lockfile; `pyproject.toml` deps largely unbounded → two deploys of one ref can resolve
  different packages; a rollback isn't a rollback of the environment.
- `deploy/deploy.sh` did `uv pip install -e` (re-resolve at deploy time), and ran
  `.venv/bin/playwright install-deps chromium` **as root** — root executing a **service-user-writable**
  venv binary (a compromised venv → root code execution).
- `deploy/bootstrap.sh` piped the **always-latest** `astral.sh/uv/install.sh` into a root shell.

### SA-09 — TLS not proven; no host validation
- The nginx template listens `:80` only and expects certbot to add `:443` "later"; a deploy can complete
  with no TLS. The **SA-01 session cookie is `Secure`**, so the access gate is **inert without live
  HTTPS** — this makes SA-09 a release prerequisite, not a nicety.
- FastAPI installed no `TrustedHostMiddleware`; a direct hit on the loopback uvicorn answered for any
  `Host`.

### SA-13 — secret in the log
- `scheduler.py` logged the full healthcheck ping `target` (a Healthchecks.io URL carries a bearer
  secret in its path) at DEBUG **with `exc_info`** — and a `requests` exception stringifies with the
  full URL too, so both the message and the traceback leaked the token.

---

## 2. What this PR changes (done, offline-verified)

### SA-13 — redact the ping URL (`api/scheduler.py`)
- New `_redact_ping_url()` returns `scheme://host/<redacted>` (path elided). The failure log now emits the
  redacted URL and the **exception type name only** — no `exc_info`, no `%s % exc` — so neither the
  message nor a traceback can carry the secret path. Tested in `tests/test_api_hardening_sa09_sa13.py`.

### SA-09 (application half) — host validation (`config.py`, `api/app.py`)
- New setting `api_trusted_hosts: list[str] | None` (default **None** → off, so dev/CLI/tailnet/TestClient
  are unchanged). When a public host sets it, `TrustedHostMiddleware` is installed **outermost** (a bad
  `Host` → 400 before the session/body middleware). `_trusted_host_allowlist()` always appends the
  loopback names (`127.0.0.1`/`localhost`/`::1`) so the direct `/v1/health` probe and a local uvicorn keep
  working (Starlette strips the port before matching). `/v1/health.limits.trusted_hosts` echoes whether
  it's active. Unit + wired-middleware tests in `tests/test_api_hardening_sa09_sa13.py`.

### SA-09 (deploy half) — a TLS gate (`deploy/deploy.sh`, `deploy/_lib.sh`, `config.env.example`)
- New `DEPLOY_REQUIRE_HTTPS` (default **0**). When `1` (public prod, set **after** certbot issues the
  cert) `deploy.sh` fails the deploy unless the public endpoint serves HTTPS and plain HTTP redirects
  (301/308) to it. Off by default so bootstrap / first deploy / tailnet (no cert yet) are unaffected.

### SA-06 — reproducible install + remove the root trust-boundary crossing
- **Committed `uv.lock`** (116 packages, resolved from `pyproject.toml`; `uv lock --check` clean, not
  git-ignored). This is the reproducibility keystone.
- `deploy/deploy.sh` now installs with **`uv sync --frozen --no-dev`** (exact from the lock, prod-only,
  fails loudly if the lock is stale) instead of `uv pip install -e` — as the **service user**, never root.
- **Removed the root `playwright install-deps`.** Chromium's OS libraries already come from
  `bootstrap.sh`'s reviewed **root-owned static apt manifest**; a new `_usable_chromium_present()` helper
  (`deploy/_lib.sh`) checks for a working browser **without executing any venv binary**, and only then
  does the Google-Chrome-from-signed-apt fallback (root running apt is legitimate; it never executes venv
  code). No root process now executes a service-user-writable file in the deploy path.
- `deploy/bootstrap.sh` pins the uv installer to an **exact version** (`astral.sh/uv/${UV_VERSION}/…`)
  instead of the always-latest installer.

---

## 3. Configuration surface added

| Setting | Where | Default | Purpose |
|---|---|---|---|
| `api_trusted_hosts` | app (`UPSTREAMWX_API_TRUSTED_HOSTS`) | `None` (off) | SA-09 host validation; public host sets `["app.upstreamwx.com"]` |
| `DEPLOY_REQUIRE_HTTPS` | deploy (`config.env`) | `0` | SA-09 TLS gate; public prod sets `1` after certbot |
| `UV_VERSION` | deploy (`bootstrap.sh` env) | pinned in-script | SA-06 pin the uv installer version |

Documented in `deploy/upstreamwx.env.example` (app var) and `deploy/config.env.example` (deploy vars).

## 4. Public-beta activation (runbook additions)

On the public host, in addition to the SA-01/SA-02 env already documented:
1. Run certbot to issue the multi-SAN cert (app + landing names).
2. Set `UPSTREAMWX_API_TRUSTED_HOSTS=["app.upstreamwx.com"]` in the runtime env file; restart the service.
3. Set `DEPLOY_REQUIRE_HTTPS=1` in `config.env` so subsequent deploys fail closed without live TLS.
4. Confirm `/v1/health.limits.trusted_hosts=true` and (with the gate on) `auth_active=true`.

---

## 5. Deferred — needs the live host (specified, not done here)

These SA-06/SA-09 items are real and remain open; they cannot be built or validated in the ephemeral
container and are the residual for a host validation pass (or a follow-up PR):

- **Root-owned atomic release directories + symlink switch (SA-06, the largest remaining piece).** The
  checkout and `.venv` are still service-user-owned and updated in place. Target: deploy each ref into a
  fresh **root-owned** `releases/<ref>` dir (non-writable by the runtime account), `uv sync --frozen` into
  it, then flip a `current` symlink atomically; rollback flips back — restoring source **and** deps
  **and** the browser inventory together. This removes the last "runtime account can influence what root
  later runs" surface and makes rollback a true environment rollback.
- **Pin & pre-stage the Chromium revision in the release inventory (SA-06).** Today `playwright install
  chromium` fetches "current". Pin the revision and record it with the release.
- **Verify a published checksum/signature of the uv installer (SA-06).** Version is pinned now; add
  checksum verification.
- **Version-controlled nginx `:443` block + HTTP→HTTPS redirect + a default-server that rejects unknown
  Hosts (SA-09).** Today certbot rewrites `:80`→`:443` out of band. Bring the TLS server block and the
  `return 444` default server under version control so the edge config is reviewable and reproducible.
  (Deploy-time enforcement already exists via `DEPLOY_REQUIRE_HTTPS`; the app-side `TrustedHostMiddleware`
  already rejects unknown Hosts.)
- **SBOM + signed release tags + SHA-pinned GitHub Actions (SA-07, adjacent).** Out of scope for this PR;
  tracked under SA-07.

---

## 6. Test / acceptance mapping

- SA-13: `_redact_ping_url` strips the secret path; garbage → `<redacted>`
  (`tests/test_api_hardening_sa09_sa13.py`).
- SA-09 app: allowlist off by default; loopback always appended, de-duped; `TrustedHostMiddleware` wired
  as app.py wires it returns 400 for an unknown Host, 200 for allowed + loopback (same file).
- SA-09 deploy / SA-06: `deploy/*.sh` `bash -n` clean; `_usable_chromium_present` sources and runs;
  `uv lock --check` clean and `uv sync --frozen --no-dev` dry-run installs the exact prod set. The full
  end-to-end deploy is validated on the host (§5).
- Regression: full offline suite green (486), ruff clean, engine output unchanged (NFR-4 — no engine,
  threshold, or render change).

## 7. Definition of done (this PR)

- [x] SA-13 ping-URL redaction (+ tests).
- [x] SA-09 app-side `TrustedHostMiddleware` + setting + health echo (+ tests).
- [x] SA-09 deploy TLS gate (`DEPLOY_REQUIRE_HTTPS`).
- [x] SA-06 committed `uv.lock`; deploy uses `uv sync --frozen --no-dev`; root exec of `.venv/bin/playwright`
      removed; uv installer version pinned.
- [x] Env examples + this workplan + changelog + CLAUDE.md milestone paragraph.
- [ ] (Deferred, host pass) root-owned atomic release dirs, pinned Chromium revision, uv checksum,
      version-controlled TLS/default-server nginx, SBOM/SA-07.
