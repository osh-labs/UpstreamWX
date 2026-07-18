# Changelog — 2026-07-18 — Issue #132 host-only deploy hardening (SA-06/07/08/09)

Scripts the live-host residuals from the 2026-07-14 security audit into the deploy layer so
staging and production get them **identically and reproducibly** (no manual host surgery).
Tracked by issue #132. Engine output is unchanged (NFR-4) — nothing here touches the engine,
thresholds, or render; the only product-code change is the PDF renderer's sandbox flags.

Validate on **staging first** (the tailnet box): the atomic release + rollback flow and the
Chromium sandbox both need a real host to exercise. Flip the prod-only gates
(`DEPLOY_REQUIRE_HTTPS`, `DEPLOY_VERIFY_TAG_SIGNATURE`, certbot) on production only after
staging is green.

## SA-06 — root-owned atomic releases + reproducible toolchain

- **Atomic release model.** `deploy.sh` no longer updates a service-user-owned checkout in
  place. It now: fetches into a **root-owned git mirror** (`<app_dir>/repo`), exports a clean
  tree to **`<app_dir>/releases/<sha>`**, builds a **root-owned** `.venv`
  (`uv sync --frozen --no-dev`) **and** per-release Chromium there, stamps the manifest, then
  **atomically flips the `<app_dir>/current` symlink** and restarts. The release tree is
  read-only to the runtime account — closing the last surface where that account could
  influence what a later root deploy runs.
- **Health-checked rollback.** If the new release fails `/v1/health`, the symlink is flipped
  back to the previous release and the service restarted — a true source+deps+browser
  rollback. Old releases are pruned to `DEPLOY_KEEP_RELEASES` (the active one is never
  removed).
- **systemd** runs from `current/` (`WorkingDirectory`, `ExecStart`,
  `PLAYWRIGHT_BROWSERS_PATH`); the base dir is root-owned; the browser dir is no longer a
  `ReadWritePaths` (per-release, read-only).
- **Migration.** `bootstrap.sh` detects an old in-place checkout and moves it aside to
  `<app_dir>/.pre-atomic` (one-time), root-owns the base, and scaffolds `repo/`, `releases/`,
  and the ACME webroot. Templates are read from the running scripts' repo (`$REPO_DIR`), since
  the base dir is no longer a checkout.
- **uv installer.** `install_uv_pinned` downloads Astral's **versioned** installer, verifies a
  pinned `UV_INSTALLER_SHA256` when set, and **asserts the resulting `uv --version`** matches
  the pin regardless — a tampered installer that yields a different toolchain is rejected.
- **Chromium revision** is recorded in `frontend/version.json` (`"chromium"`), so a release
  pins and advertises the browser it was built with.

## SA-09 — version-controlled TLS/edge, certbot `--webroot`

- The nginx `:443` server block and the HTTP→HTTPS redirect are **under version control**
  (`deploy/nginx/upstreamwx.conf`, `landing.conf`) via marker regions that
  `render_nginx_site` toggles from `DEPLOY_TLS_ENABLE`. certbot runs **`--webroot`** (issue/
  renew only, no nginx rewrite); the `/.well-known/acme-challenge/` location stays on `:80`
  for renewals, and certbot's timer reloads nginx via a deploy-hook.
- **Default server** (`deploy/nginx/default-server.conf`) returns **`444`** for unknown
  Host/SNI at the edge — defense in depth beneath the app's `TrustedHostMiddleware`.
- `deploy.sh` (when `DEPLOY_REQUIRE_HTTPS=1`) already fails a public deploy without live HTTPS
  + redirect; it now also reads `/v1/health` and warns if `auth_active`/`trusted_hosts` are
  not active on the public endpoint (the issue's activation checklist).
- `http2` uses the portable `listen … ssl http2` form (nginx 1.18/1.24 lack server-scoped
  `http2 on`).

## SA-08 — restore the PDF renderer's native sandbox

- The systemd unit relaxes `RestrictNamespaces` from `true` to **`user mnt pid net`** — the
  namespaces the unprivileged Chromium sandbox creates.
- `sitrep/pdf.py` **drops `--no-sandbox`** for the non-root service (native sandbox on); it is
  re-added only when running as **root** (dev/CI/containers, where Chromium refuses its
  sandbox) or when `UPSTREAMWX_PDF_NO_SANDBOX=1` forces it (hosts that can't allow user
  namespaces). Host prerequisites are documented (userns sysctl; Ubuntu 24.04 AppArmor).

## SA-07 — release provenance + CI supply chain

- **Signed-tag verification at deploy** (`DEPLOY_VERIFY_TAG_SIGNATURE=1` → `git verify-tag`
  before building an annotated tag).
- **CI actions pinned to full commit SHAs** (`ci.yml`, `deploy-pages.yml`).
- New **`supply-chain` CI job**: `pip-audit` (dependency audit) + `detect-secrets` (secret
  scan) + a **CycloneDX SBOM** artifact, all against the exact `uv.lock` export. Add both
  `test` and `supply-chain` to branch protection to make them enforcing.

## New / changed config

`deploy/config.env.example` (+ staging): `DEPLOY_KEEP_RELEASES`, `UV_VERSION`,
`UV_INSTALLER_SHA256`, `DEPLOY_CERTBOT_EMAIL`, `DEPLOY_ACME_WEBROOT`, `DEPLOY_TLS_ENABLE`,
`DEPLOY_TLS_CERT/KEY`, `DEPLOY_VERIFY_TAG_SIGNATURE` (+ the optional
`DEPLOY_REPO_MIRROR/RELEASES_DIR/CURRENT_LINK`). `deploy/upstreamwx.env.example`:
`UPSTREAMWX_PDF_NO_SANDBOX`. Full offline suite green (500), ruff clean.
