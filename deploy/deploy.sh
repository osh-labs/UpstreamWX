#!/usr/bin/env bash
# UpstreamWX — update + restart the backend on the host (run after bootstrap.sh).
#
#   sudo deploy/deploy.sh [git-ref]        # ref defaults to DEPLOY_BRANCH
#
# Idempotent and safe to re-run. It fetches the requested ref as the service user,
# refreshes the venv (uv), reinstalls the package, restarts the systemd service, and
# blocks on a /v1/health check so a bad deploy fails loudly instead of silently.
#
# Run on the server (SSH in, then invoke it); the server pulls its own code from git.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"
load_config
require_root

REF="${1:-$DEPLOY_BRANCH}"
# -H sets HOME to the service user's home ($DEPLOY_APP_DIR); without it sudo keeps the
# invoking user's HOME (/home/ubuntu), which the service user can't read — uv then fails
# to open its config/cache there (Permission denied).
RUN_USER="sudo -u $DEPLOY_USER -H"

[ -d "$DEPLOY_APP_DIR/.git" ] || die "no checkout at $DEPLOY_APP_DIR — run bootstrap.sh first"
command -v uv >/dev/null 2>&1 || die "uv not found on PATH"

# Run from inside the app dir. uv discovers config by walking UP from the CWD; if invoked
# from the sudoer's home (/home/ubuntu, mode 0750) the service user can't read it and uv
# dies with "failed to open uv.toml: Permission denied". The app dir is service-readable.
cd "$DEPLOY_APP_DIR"

# --- 1. Sync source to the requested ref (branch, tag, or SHA) ------------------------
log "fetching $REF into $DEPLOY_APP_DIR"
$RUN_USER git -C "$DEPLOY_APP_DIR" fetch origin --prune --tags
if $RUN_USER git -C "$DEPLOY_APP_DIR" show-ref --verify --quiet "refs/remotes/origin/$REF"; then
    $RUN_USER git -C "$DEPLOY_APP_DIR" checkout -B "$REF" "origin/$REF"
else
    $RUN_USER git -C "$DEPLOY_APP_DIR" checkout --force "$REF"
fi
DEPLOYED_SHA="$($RUN_USER git -C "$DEPLOY_APP_DIR" rev-parse --short HEAD)"
ok "checked out $REF @ $DEPLOYED_SHA"

# --- 1b. Stamp the release into frontend/version.json --------------------------------
# A single source of truth for "what's deployed": prefer the nearest tag (production
# deploys a tag), else the short SHA. /v1/health echoes it for ops, and the PWA polls
# version.json to nudge stale clients to reload after a release (docs/deployment-workflow.md).
# It's git-ignored and untracked, so the checkout above never clobbers or conflicts with it.
RELEASE="$($RUN_USER git -C "$DEPLOY_APP_DIR" describe --tags --always 2>/dev/null || echo "$DEPLOYED_SHA")"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
$RUN_USER tee "$DEPLOY_APP_DIR/frontend/version.json" >/dev/null <<EOF
{"version": "$RELEASE", "sha": "$DEPLOYED_SHA", "built_at": "$BUILT_AT"}
EOF
ok "stamped release $RELEASE"

# --- 2. Refresh the virtualenv from the committed lockfile (exact, frozen) ------------
# SA-06: install the EXACT resolved set from the committed uv.lock rather than re-resolving
# unbounded deps at deploy time, so two deploys of the same ref get the same packages and a
# rollback restores the same environment. `--frozen` fails loudly if the lock is out of date
# with pyproject (no silent re-resolve); `--no-dev` omits the pytest/ruff dev group. Runs as
# the service user (never root) into the service-owned .venv.
log "syncing virtualenv from uv.lock (uv sync --frozen --no-dev)"
$RUN_USER uv sync --frozen --no-dev --python 3.11
ok "dependencies installed (exact, from uv.lock)"

# Fail fast if the GRIB stack can't import (the most likely host-specific breakage).
if ! $RUN_USER "$DEPLOY_APP_DIR/.venv/bin/python" -c "import cfgrib" 2>/dev/null; then
    warn "cfgrib failed to import — check ecCodes (see deploy/README.md troubleshooting)"
fi

# Install a Chromium binary for server-side PDF export (FR-27, sitrep/pdf.py).
#
# Strategy: try Playwright's own managed Chromium first (preferred — version-pinned,
# known-good with the installed Playwright Python package).  If the distro isn't
# supported yet (e.g. Ubuntu 26.04 before Playwright catches up), fall back to the
# system Chromium from apt.  pdf.py searches both locations via _chromium_path().
PLAYWRIGHT_BROWSERS_DIR="$DEPLOY_APP_DIR/.playwright-browsers"
log "ensuring Chromium is available for PDF export"
# Install to two locations so the binary is found regardless of whether the running
# service has PLAYWRIGHT_BROWSERS_PATH set in its environment (it may not, if the unit
# file predates that env var or the __APP_DIR__ substitution failed on this host).
#
# Location 1: explicit PLAYWRIGHT_BROWSERS_PATH dir (matches the systemd unit template).
$RUN_USER env PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_DIR" \
    "$DEPLOY_APP_DIR/.venv/bin/playwright" install chromium 2>/dev/null \
    && ok "Playwright Chromium ready at $PLAYWRIGHT_BROWSERS_DIR" || true
# Location 2: default $HOME/.cache/ms-playwright — used when PLAYWRIGHT_BROWSERS_PATH is
# absent from the running process.  -H sets HOME=$DEPLOY_APP_DIR (via sudo -H in RUN_USER)
# so the install lands at $DEPLOY_APP_DIR/.cache/ms-playwright/, exactly where Playwright
# looks when the env var is unset.
$RUN_USER "$DEPLOY_APP_DIR/.venv/bin/playwright" install chromium 2>/dev/null \
    && ok "Playwright Chromium also ready at $DEPLOY_APP_DIR/.cache/ms-playwright" || true
# Chromium's OS-level shared libraries (libatk, libnss3, libx11, …) are installed ONCE, as
# root, from bootstrap.sh's reviewed static apt manifest — NOT by executing the service-user-
# owned `.venv/bin/playwright install-deps` as root, which would cross the deploy trust
# boundary (a compromised venv → root code execution, SA-06). If Playwright's own Chromium
# won't run on this distro, fall back to Google Chrome from its signed apt repo; root running
# apt is legitimate and never executes venv code. Best-effort throughout (NFR-6): a missing
# browser only makes the PDF endpoint return 503, and an unnecessary fallback install is harmless.
if _usable_chromium_present; then
    ok "Chromium available for PDF export"
elif command -v apt-get >/dev/null 2>&1; then
    # Ubuntu 22.04+ ships Chromium as a snap only; a system service account has no snap session,
    # so the snap wrapper fails headlessly. Google Chrome's apt package works without snap.
    warn "no usable Playwright Chromium — installing Google Chrome from its signed apt repo"
    if ! command -v google-chrome-stable >/dev/null 2>&1 \
            && ! command -v google-chrome >/dev/null 2>&1; then
        log "adding Google Chrome apt repository"
        curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
            | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
        echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
http://dl.google.com/linux/chrome/deb/ stable main" \
            > /etc/apt/sources.list.d/google-chrome.list
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq google-chrome-stable \
            || warn "google-chrome-stable install failed — PDF export endpoint will return 503"
    fi
    _usable_chromium_present \
        && ok "Google Chrome available for PDF export" \
        || warn "no usable Chromium found — PDF export endpoint will return 503"
else
    warn "no usable Chromium and no apt — PDF export endpoint will return 503"
fi

# --- 2b. Warm the REFS ensemble cache ------------------------------------------------
# REFS is cache-driven: the scheduler fills it on 00/06/12/18Z cycle boundaries, so a
# fresh deploy or server restart leaves the cache empty until the next tick (up to 6 h).
# Pre-fill it now — field-by-field with verbose output — so the first briefing on staging
# has a live REFS signal rather than degrading to GEFS-only.
#
# Skip: cache is current (≤ 2 cycles / 12 h old).
# Warm: cache is empty, absent, or > 2 cycles stale.
log "checking REFS ensemble cache"

# Collect UPSTREAMWX_* env vars from the app env file so the Python subprocess sees the
# same data dir and feed config the running service will use.  Vars already set in the
# caller's environment take precedence (env file read is additive, not overriding).
_uwx_env=()
if [ -f "$DEPLOY_ENV_FILE" ]; then
    while IFS= read -r _line; do
        [[ "$_line" =~ ^[[:space:]]*# ]] && continue   # skip comment lines
        [[ -z "${_line//[[:space:]]/}" ]] && continue   # skip blank lines
        [[ "$_line" =~ ^UPSTREAMWX_ ]] && _uwx_env+=("$_line")
    done < "$DEPLOY_ENV_FILE"
fi
# Always ensure the data dir is set; default to the deploy layout if the env file omits it.
if ! printf '%s\n' "${_uwx_env[@]+"${_uwx_env[@]}"}" | grep -q '^UPSTREAMWX_DATA_DIR='; then
    _uwx_env+=("UPSTREAMWX_DATA_DIR=$DEPLOY_DATA_DIR")
fi

# --- REFS production-feed cutover gate ------------------------------------------------
# REFS production (NOMADS com/refs/prod, ensprod NEP) goes live 2026-08-31 12Z and the AWS
# *prototype* bucket UpstreamWX defaults to is non-operational past the SCN 26-47 EOL. There
# is no automatic switch: warn loudly here if the deploy is at/after the cutover but the env
# file still selects the prototype feed, so the operator flips UPSTREAMWX_REFS_SOURCE in the
# env file rather than silently running the public beta on a prototype bucket. Non-fatal
# (a warning, not a block) so an early/dev deploy is unaffected.
_refs_gate="$($RUN_USER env "${_uwx_env[@]}" "$DEPLOY_APP_DIR/.venv/bin/python" - <<'PYEOF' || true
import sys
from datetime import UTC, date, datetime
try:
    from upstreamwx.config import get_settings
except ImportError:
    sys.exit(0)
CUTOVER = date(2026, 8, 31)  # SCN 26-48 REFS production go-live (SREF/HREF EOL)
src = get_settings().refs_source
if datetime.now(UTC).date() >= CUTOVER and src == "aws":
    print("PROTOTYPE_AFTER_CUTOVER")
PYEOF
)"
if [[ "$_refs_gate" == *PROTOTYPE_AFTER_CUTOVER* ]]; then
    warn "REFS still on the AWS *prototype* feed (refs_source=aws) past the 2026-08-31 production cutover."
    warn "Set UPSTREAMWX_REFS_SOURCE=nomads_prod in $DEPLOY_ENV_FILE (SCN 26-48) — see deploy/README.md."
fi

# Run the warm script as the service user so all cache files are owned correctly.
# Python reads the script from stdin (python -) to avoid writing a temp file.
# Non-fatal: a warm failure degrades REFS (scheduler recovers on next tick) but must not
# block the deploy.
if ! $RUN_USER env "${_uwx_env[@]}" \
        "$DEPLOY_APP_DIR/.venv/bin/python" - <<'PYEOF'
import sys
import logging
from datetime import UTC, datetime

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

try:
    import requests
    from upstreamwx.config import get_settings
    from upstreamwx.refs.sources import REFS_FHOURS, RefsCycle, latest_available_cycle, refs_feed
    from upstreamwx.refs.cache import DEFAULT_FIELDS, accum_window, load_probability_field_cached
except ImportError as exc:
    print(f"  REFS warm skipped (import error: {exc})", file=sys.stderr)
    sys.exit(0)

settings = get_settings()
cache_root = settings.data_dir / "refs"
now = datetime.now(UTC)

# ── staleness check ──────────────────────────────────────────────────────────────────
def newest_cached() -> RefsCycle | None:
    """Newest non-empty cycle dir in the on-disk cache, or None."""
    if not cache_root.is_dir():
        return None
    best: RefsCycle | None = None
    for d in cache_root.iterdir():
        if not d.is_dir() or not any(d.iterdir()):
            continue
        try:
            date, hh = d.name.split("_")
            c = RefsCycle(date=date, hour=int(hh))
        except (ValueError, KeyError):
            continue
        if best is None or c.init_time > best.init_time:
            best = c
    return best

STALE_CYCLES = 2          # warm if the cache is older than this many REFS cycles
STALE_H = STALE_CYCLES * 6.0  # cycles are 6 h apart

existing = newest_cached()
if existing is None:
    print("  REFS cache: empty")
    needs_warm = True
else:
    age_h = (now - existing.init_time).total_seconds() / 3600.0
    print(f"  REFS cache: newest cycle {existing.date}/{existing.hh}Z,  age {age_h:.1f} h")
    if age_h > STALE_H:
        print(f"  Stale (> {STALE_H:.0f} h / {STALE_CYCLES} cycles) — warming")
        needs_warm = True
    else:
        print(f"  Current — skipping warm")
        needs_warm = False

if not needs_warm:
    sys.exit(0)

# ── probe the feed for the newest live cycle ─────────────────────────────────────────
base, subdir = refs_feed(settings)
print(f"  Feed: {settings.refs_source}  {base}/{subdir}/")
cycle = latest_available_cycle(settings=settings)
if cycle is None:
    print(
        f"  No live REFS cycle found on the {settings.refs_source!r} feed.\n"
        "  If using the AWS prototype, try UPSTREAMWX_REFS_SOURCE=nomads_para in the env file.",
        file=sys.stderr,
    )
    sys.exit(1)
print(f"  Live cycle: {cycle.date}/{cycle.hh}Z")

# ── warm field by field with progress output ─────────────────────────────────────────
FMIN, FMAX = 3, 48
fhours = [f for f in REFS_FHOURS if FMIN <= f <= FMAX]
total = len(fhours) * len(DEFAULT_FIELDS)
fetched = cached = skipped = 0

print(f"  Warming {len(fhours)} forecast hours × {len(DEFAULT_FIELDS)} fields  ({total} total)")
for fhour in fhours:
    for spec in DEFAULT_FIELDS:
        fcst = accum_window(fhour, spec.window_h) if spec.window_h else None
        label = f"f{fhour:02d}  {spec.var}{spec.prob}"
        idx = fetched + cached + skipped + 1
        try:
            field = load_probability_field_cached(
                cycle, fhour, spec.var, spec.prob, fcst=fcst, settings=settings
            )
            hit = field.extras.get("cached", False)
            if hit:
                cached += 1
                status = "cached"
            else:
                fetched += 1
                status = "fetched"
        except (LookupError, TimeoutError, OSError, requests.RequestException) as exc:
            skipped += 1
            status = f"skip ({exc})"
        print(f"    [{idx:2d}/{total}]  {label}: {status}")

print(f"  Warm complete: {fetched} fetched,  {cached} already cached,  {skipped} skipped")
if fetched + cached == 0:
    print(
        "  All fields failed — REFS will remain degraded until the scheduler recovers.",
        file=sys.stderr,
    )
    sys.exit(1)
PYEOF
then
    warn "REFS cache warm had issues (see above) — GEFS covers briefings until the scheduler fills it"
fi

# --- 2c. Warm the GEFS ensemble cache ------------------------------------------------
# GEFS is per-member (31 members × 2 fields × the f24-f120 band ≈ 1000 subsets), so a fresh
# deploy/restart otherwise pays the full cold ingest on the first briefing's critical path.
# Pre-fill it now — download-only and fanned across a thread pool inside warm_cycle, fhour by
# fhour for progress. Non-fatal: failure degrades GEFS until the scheduler recovers (NFR-6).
# Reuses the $_uwx_env collected for the REFS warm above.
log "checking GEFS ensemble cache"
if ! $RUN_USER env "${_uwx_env[@]}" \
        "$DEPLOY_APP_DIR/.venv/bin/python" - <<'PYEOF'
import sys
import logging
from datetime import UTC, datetime

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

try:
    from upstreamwx.config import get_settings
    from upstreamwx.gefs.sources import GEFS_CYCLES, GefsCycle, latest_available_cycle
    from upstreamwx.gefs.cache import warm_cycle
except ImportError as exc:
    print(f"  GEFS warm skipped (import error: {exc})", file=sys.stderr)
    sys.exit(0)

settings = get_settings()
fhours = sorted(settings.gefs_warm_fhours or [])
if not fhours:
    print("  GEFS warm disabled (gefs_warm_fhours empty) — serving on demand")
    sys.exit(0)

cache_root = settings.data_dir / "gefs"
now = datetime.now(UTC)

# ── staleness check ──────────────────────────────────────────────────────────────────
def newest_cached() -> GefsCycle | None:
    """Newest non-empty cycle dir in the on-disk cache, or None."""
    if not cache_root.is_dir():
        return None
    best: GefsCycle | None = None
    for d in cache_root.iterdir():
        if not d.is_dir() or not any(d.iterdir()):
            continue
        try:
            date, hh = d.name.split("_")
            c = GefsCycle(date=date, hour=int(hh))
        except (ValueError, KeyError):
            continue
        if c.hour in GEFS_CYCLES and (best is None or c.init_time > best.init_time):
            best = c
    return best

STALE_CYCLES = 2          # warm if the cache is older than this many GEFS cycles
STALE_H = STALE_CYCLES * 6.0  # cycles are 6 h apart

existing = newest_cached()
if existing is None:
    print("  GEFS cache: empty")
    needs_warm = True
else:
    age_h = (now - existing.init_time).total_seconds() / 3600.0
    print(f"  GEFS cache: newest cycle {existing.date}/{existing.hh}Z,  age {age_h:.1f} h")
    if age_h > STALE_H:
        print(f"  Stale (> {STALE_H:.0f} h / {STALE_CYCLES} cycles) — warming")
        needs_warm = True
    else:
        print("  Current — skipping warm")
        needs_warm = False

if not needs_warm:
    sys.exit(0)

# ── probe NOMADS for the newest live cycle ───────────────────────────────────────────
cycle = latest_available_cycle()
if cycle is None:
    print("  No live GEFS cycle found on NOMADS (retention/lag).", file=sys.stderr)
    sys.exit(1)
print(f"  Live cycle: {cycle.date}/{cycle.hh}Z")
print(f"  Warming {len(fhours)} forecast hours (31 members × 2 fields each, download-only)")

# ── warm fhour by fhour (each fhour's ~62 subsets fan out inside warm_cycle) ──────────
total = 0
for fhour in fhours:
    paths = warm_cycle(cycle, (fhour,), settings=settings)
    total += len(paths)
    print(f"    f{fhour:03d}: {len(paths)} subsets cached")

print(f"  Warm complete: {total} member subsets cached")
if total == 0:
    print(
        "  All fields failed — GEFS will remain on-demand until the scheduler recovers.",
        file=sys.stderr,
    )
    sys.exit(1)
PYEOF
then
    warn "GEFS cache warm had issues (see above) — GEFS serves on demand until the scheduler fills it"
fi

# --- 3. Restart the service ----------------------------------------------------------
log "restarting $DEPLOY_SERVICE"
systemctl restart "$DEPLOY_SERVICE"

# --- 4. Health check (loopback) ------------------------------------------------------
log "waiting for /v1/health"
url="http://${DEPLOY_BIND_HOST}:${DEPLOY_BIND_PORT}/v1/health"
healthy=0
for i in $(seq 1 20); do
    if curl -fsS "$url" >/dev/null 2>&1; then
        ok "healthy: $(curl -fsS "$url")"
        healthy=1
        break
    fi
    sleep 1
done
if [ "$healthy" -ne 1 ]; then
    warn "service did not become healthy in time — recent logs:"
    # The API logs to a private journald namespace (LogNamespace=upstreamwx); read from it.
    journalctl --namespace=upstreamwx -u "$DEPLOY_SERVICE" -n 40 --no-pager || true
    die "deploy failed health check"
fi

# --- 4b. Public TLS gate (SA-09) -----------------------------------------------------
# The SA-01 session cookie is Secure, so the access gate is inert without live HTTPS. When
# DEPLOY_REQUIRE_HTTPS=1 (set it on the PUBLIC prod config, AFTER certbot has issued the cert)
# fail the deploy unless the public endpoint actually serves HTTPS and plain HTTP redirects to
# it — so a public release can't silently ship without TLS. Off by default so bootstrap / first
# deploy / tailnet (no DNS or cert yet) are unaffected.
if [ "$DEPLOY_REQUIRE_HTTPS" = "1" ]; then
    https_url="https://${DEPLOY_APP_SERVER_NAME}/v1/health"
    log "verifying public HTTPS: $https_url"
    curl -fsS --max-time 15 "$https_url" >/dev/null 2>&1 \
        || die "HTTPS health check failed ($https_url) — is the certbot cert issued and nginx :443 live? (SA-09)"
    ok "public HTTPS healthy"
    http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
        "http://${DEPLOY_APP_SERVER_NAME}/v1/health" 2>/dev/null || echo 000)"
    case "$http_code" in
        301|308) ok "HTTP -> HTTPS redirect in place ($http_code)" ;;
        *) warn "HTTP did not redirect to HTTPS (got $http_code) — check the certbot :80 redirect block (SA-09)" ;;
    esac
fi

log "deployed $REF @ $DEPLOYED_SHA"
exit 0
