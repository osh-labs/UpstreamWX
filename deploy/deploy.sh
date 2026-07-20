#!/usr/bin/env bash
# UpstreamWX — build + activate a release on the host (run after bootstrap.sh).
#
#   sudo deploy/deploy.sh [git-ref]        # ref defaults to DEPLOY_BRANCH
#
# Idempotent and safe to re-run. SA-06 atomic-release model: it fetches the ref into a
# root-owned git mirror, verifies a signed tag if required (SA-07), builds a fresh
# root-owned release (clean export + `uv sync --frozen` .venv + browser) under
# releases/<sha>, warms the ensemble caches, then atomically flips the `current` symlink
# and restarts. It blocks on /v1/health and ROLLS BACK the symlink to the previous release
# if the new one is unhealthy — so a bad deploy fails loudly and self-heals instead of
# leaving the service down. The release tree is read-only to the runtime account, closing
# the last surface where that account could influence what a later root deploy runs.
#
# Run on the server (SSH in, then invoke it); the server pulls its own code from git.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"
load_config
require_root

REF="${1:-$DEPLOY_BRANCH}"
# -H sets HOME to the service user's home ($DEPLOY_APP_DIR); without it sudo keeps the
# invoking user's HOME (/home/ubuntu), which the service user can't read.
RUN_USER="sudo -u $DEPLOY_USER -H"

command -v uv >/dev/null 2>&1 || die "uv not found on PATH — run bootstrap.sh first"
[ -d "$DEPLOY_REPO_MIRROR/.git" ] || die "no git mirror at $DEPLOY_REPO_MIRROR — run bootstrap.sh first"

# --- 1. Build the release into a fresh root-owned dir (SA-06 atomic releases) ---------
# build_release fetches the mirror, verifies a signed tag if required (SA-07), exports a
# clean tree to releases/<sha>, builds a root-owned .venv (uv sync --frozen) + browser, and
# stamps frontend/version.json. Nothing is activated yet — the running service is untouched
# until the symlink flip below, so a failed build cannot take the service down.
PREV_TARGET="$(current_release_target)"      # for rollback (empty on the very first deploy)
build_release "$REF"
DEPLOYED_SHA="$RELEASE_SHA"
RELEASE="$RELEASE_NAME"

# The GEFS/REFS warm + REFS-cutover-gate steps below run the NEW release's interpreter.
RELEASE_PY="$RELEASE_DIR/.venv/bin/python"

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
# Force the data dir to the deploy layout, appended LAST so it OVERRIDES any UPSTREAMWX_DATA_DIR
# in the env file (env applies last-wins). This keeps the warm writing to the exact dir the
# service uses (systemd pins the same value) — a wrong path in the env file otherwise sends the
# warm to a dir the service account can't write (PermissionError).
_uwx_env+=("UPSTREAMWX_DATA_DIR=$DEPLOY_DATA_DIR")

# --- REFS production-feed cutover gate ------------------------------------------------
# REFS production (NOMADS com/refs/prod, ensprod NEP) goes live 2026-08-31 12Z and the AWS
# *prototype* bucket UpstreamWX defaults to is non-operational past the SCN 26-47 EOL. There
# is no automatic switch: warn loudly here if the deploy is at/after the cutover but the env
# file still selects the prototype feed, so the operator flips UPSTREAMWX_REFS_SOURCE in the
# env file rather than silently running the public beta on a prototype bucket. Non-fatal
# (a warning, not a block) so an early/dev deploy is unaffected.
_refs_gate="$($RUN_USER env "${_uwx_env[@]}" "$RELEASE_PY" - <<'PYEOF' || true
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
        "$RELEASE_PY" - <<'PYEOF'
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
        "$RELEASE_PY" - <<'PYEOF'
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

# --- 3. Activate the new release + restart (atomic flip, SA-06) -----------------------
# Flip `current` to the freshly-built release and restart. The service resolves the symlink
# on start, so it moves cleanly to the new release. If health fails, roll the symlink back to
# the previous release and restart — a true source+deps+browser rollback.
_restart_and_check() {
    # _restart_and_check LABEL -> 0 healthy, 1 not
    # Idempotently ensure the unit is boot-enabled on every deploy, so a host provisioned before
    # this fix (e.g. staging, which stayed DOWN after a reboot) self-heals without a re-bootstrap.
    # `enable` only writes the WantedBy symlink; it does not start the service — restart does that.
    systemctl enable "$DEPLOY_SERVICE" >/dev/null 2>&1 || true
    systemctl restart "$DEPLOY_SERVICE"
    local url="http://${DEPLOY_BIND_HOST}:${DEPLOY_BIND_PORT}/v1/health" i
    for i in $(seq 1 20); do
        if curl -fsS "$url" >/dev/null 2>&1; then
            ok "healthy ($1): $(curl -fsS "$url")"
            return 0
        fi
        sleep 1
    done
    return 1
}

log "activating release $DEPLOYED_SHA and restarting $DEPLOY_SERVICE"
activate_release "$RELEASE_DIR"
if ! _restart_and_check "new release"; then
    warn "service did not become healthy on $DEPLOYED_SHA — recent logs:"
    # The API logs to a private journald namespace (LogNamespace=upstreamwx); read from it.
    journalctl --namespace=upstreamwx -u "$DEPLOY_SERVICE" -n 40 --no-pager || true
    if [ -n "$PREV_TARGET" ] && [ -f "$PREV_TARGET/.release-ok" ]; then
        warn "rolling back to the previous release: $PREV_TARGET"
        activate_release "$PREV_TARGET"
        if _restart_and_check "rollback"; then
            die "deploy failed health check — ROLLED BACK to $(basename "$PREV_TARGET"). Investigate $DEPLOYED_SHA before retrying."
        fi
        die "deploy failed health check AND rollback did not recover — service is DOWN. Check journalctl."
    fi
    die "deploy failed health check (no previous release to roll back to)"
fi

# --- 3b. Prune old releases (keep DEPLOY_KEEP_RELEASES; never the active one) ----------
prune_releases

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
        *) warn "HTTP did not redirect to HTTPS (got $http_code) — check the :80 redirect block (SA-09)" ;;
    esac

    # Activation checklist (issue #132): on a PUBLIC deploy the access gate + host allowlist
    # must actually be active. Read them off /v1/health (echoed by the app) and warn loudly if
    # either is off — a public host with auth_active=false or trusted_hosts=false is a misconfig.
    _health_json="$(curl -fsS --max-time 15 "$https_url" 2>/dev/null || echo '{}')"
    case "$_health_json" in
        *'"auth_active": true'*|*'"auth_active":true'*) ok "access gate active (auth_active=true)" ;;
        *) warn "auth_active is NOT true on the public endpoint — set UPSTREAMWX_SESSION_SECRET + UPSTREAMWX_API_AUTH_REQUIRED=1 in $DEPLOY_ENV_FILE (SA-01)" ;;
    esac
    case "$_health_json" in
        *'"trusted_hosts": true'*|*'"trusted_hosts":true'*) ok "Host allowlist active (trusted_hosts=true)" ;;
        *) warn "trusted_hosts is NOT true — set UPSTREAMWX_API_TRUSTED_HOSTS=[\"$DEPLOY_APP_SERVER_NAME\"] in $DEPLOY_ENV_FILE (SA-09)" ;;
    esac
fi

log "deployed $RELEASE ($DEPLOYED_SHA) — current -> $(basename "$(current_release_target)")"
exit 0
