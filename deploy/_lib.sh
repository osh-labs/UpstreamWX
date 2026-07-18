#!/usr/bin/env bash
# Shared helpers for the UpstreamWX deploy scripts. Sourced, not executed.
#
# Provides: coloured logging, config loading (deploy/config.env with env overrides),
# and __TOKEN__ rendering for the systemd/nginx templates. Keeping this in one place
# means bootstrap.sh and deploy.sh agree on paths by construction.

set -euo pipefail

# Directory containing the deploy scripts, regardless of the caller's CWD.
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"

_have_tty() { [ -t 1 ]; }
log()  { if _have_tty; then printf '\033[1;34m==>\033[0m %s\n' "$*"; else printf '==> %s\n' "$*"; fi; }
ok()   { if _have_tty; then printf '\033[1;32m  ✓\033[0m %s\n' "$*"; else printf '  OK %s\n' "$*"; fi; }
warn() { if _have_tty; then printf '\033[1;33m  ! \033[0m%s\n' "$*" >&2; else printf '  ! %s\n' "$*" >&2; fi; }
die()  { if _have_tty; then printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; else printf 'error: %s\n' "$*" >&2; fi; exit 1; }

# Load the deploy config if present, then let real environment variables win (so
# `DEPLOY_BRANCH=foo ./deploy.sh` overrides the file). Defaults below cover a fresh
# checkout where the config file hasn't been created yet.
#
# Which file? `config.env` by default; set DEPLOY_CONFIG to point at another (e.g.
# `sudo DEPLOY_CONFIG=deploy/config.staging.env deploy/deploy.sh v0.5.0`) so ONE set of
# scripts drives both staging and production, each with its own service name / port /
# paths (docs/deployment-workflow.md).
load_config() {
    local cfg="${DEPLOY_CONFIG:-$DEPLOY_DIR/config.env}"
    if [ -n "${DEPLOY_CONFIG:-}" ] && [ ! -f "$cfg" ]; then
        die "DEPLOY_CONFIG=$cfg not found"
    fi
    if [ -f "$cfg" ]; then
        # shellcheck disable=SC1090
        set -a; source "$cfg"; set +a
    fi
    : "${DEPLOY_REPO_URL:=https://github.com/osh-labs/upstreamwx.git}"
    : "${DEPLOY_BRANCH:=main}"
    : "${DEPLOY_APP_DIR:=/opt/upstreamwx}"
    : "${DEPLOY_USER:=upstreamwx}"
    : "${DEPLOY_GROUP:=upstreamwx}"
    : "${DEPLOY_DATA_DIR:=/var/lib/upstreamwx}"
    : "${DEPLOY_ENV_DIR:=/etc/upstreamwx}"
    : "${DEPLOY_ENV_FILE:=/etc/upstreamwx/upstreamwx.env}"
    : "${DEPLOY_BIND_HOST:=127.0.0.1}"
    : "${DEPLOY_BIND_PORT:=8000}"
    # --- SA-06 atomic releases -------------------------------------------------------
    # The service runs out of $DEPLOY_CURRENT_LINK (a symlink deploy.sh flips atomically)
    # into a fresh root-owned release under $DEPLOY_RELEASES_DIR. $DEPLOY_REPO_MIRROR is a
    # root-owned git mirror the host fetches into; each release is a clean `git archive`
    # export with its own root-owned .venv + browser (read-only to the service account), so
    # the runtime user can no longer influence what a later root deploy runs, and a rollback
    # restores source+deps+browser together. All default under $DEPLOY_APP_DIR.
    : "${DEPLOY_REPO_MIRROR:=${DEPLOY_APP_DIR}/repo}"
    : "${DEPLOY_RELEASES_DIR:=${DEPLOY_APP_DIR}/releases}"
    : "${DEPLOY_CURRENT_LINK:=${DEPLOY_APP_DIR}/current}"
    # How many past releases to keep for rollback (the active one is never pruned).
    : "${DEPLOY_KEEP_RELEASES:=5}"
    # SA-07: when "1", deploy.sh verifies the GPG signature of an annotated tag before
    # building it (git verify-tag). The signer's public key must be in root's keyring on the
    # host. Off by default so branch/SHA deploys and unsigned dev tags are unaffected; the
    # PUBLIC prod config turns it on so a tampered/unsigned tag cannot reach production.
    : "${DEPLOY_VERIFY_TAG_SIGNATURE:=0}"
    # The app (PWA + /v1/*) lives on its own subdomain; the apex serves a static landing
    # page (deploy/nginx/landing.conf). DEPLOY_APP_SERVER_NAME falls back to the legacy
    # single DEPLOY_SERVER_NAME so an existing staging config keeps working unchanged.
    : "${DEPLOY_APP_SERVER_NAME:=${DEPLOY_SERVER_NAME:-app.upstreamwx.com}}"
    # Landing is OPT-IN: empty by default so no environment (staging, a fresh box, an old
    # config.env) accidentally stands up the apex site. The names come from the env file —
    # config.env.example sets prod's; config.staging.env.example sets "" (app-only).
    : "${DEPLOY_LANDING_SERVER_NAME:=}"
    # Served from the active release so a deploy updates the landing for free (atomic model).
    : "${DEPLOY_LANDING_ROOT:=${DEPLOY_CURRENT_LINK}/landing}"
    : "${DEPLOY_SERVICE:=upstreamwx-api}"
    # --- SA-09 edge TLS (version-controlled :443 + certbot webroot) -------------------
    # The :443 server block and HTTP->HTTPS redirect are templated (deploy/nginx/*.conf),
    # NOT rewritten out-of-band by `certbot --nginx`. certbot runs in --webroot mode purely
    # to ISSUE/RENEW certs into the standard live path; nginx config stays under version
    # control. DEPLOY_ACME_WEBROOT is the ACME http-01 challenge root (served at
    # /.well-known/acme-challenge/ by the :80 block). DEPLOY_TLS_ENABLE gates whether the
    # rendered site includes the :443 block (bootstrap sets it once a cert exists).
    : "${DEPLOY_ACME_WEBROOT:=/var/www/acme}"
    : "${DEPLOY_CERTBOT_EMAIL:=}"
    # Live cert/key paths (certbot's default layout, keyed by the FIRST app server name).
    : "${DEPLOY_TLS_CERT:=/etc/letsencrypt/live/${DEPLOY_APP_SERVER_NAME%% *}/fullchain.pem}"
    : "${DEPLOY_TLS_KEY:=/etc/letsencrypt/live/${DEPLOY_APP_SERVER_NAME%% *}/privkey.pem}"
    # Rendered into the nginx template: "1" emits the :443 listener + redirect; "0" stays
    # HTTP-only (bootstrap/first run, before a cert exists — the ACME challenge needs :80).
    : "${DEPLOY_TLS_ENABLE:=0}"
    # SA-09: when "1", deploy.sh verifies the PUBLIC endpoint serves HTTPS (valid cert) and that
    # plain HTTP redirects to it, failing the deploy otherwise. Off by default so bootstrap /
    # first deploy / tailnet staging (no DNS or cert yet) are unaffected; the public prod config
    # turns it on after certbot has issued the cert. The SA-01 Secure session cookie is inert
    # without live HTTPS, so a public release must not ship without it.
    : "${DEPLOY_REQUIRE_HTTPS:=0}"
}

# render_template SRC DEST — substitute the __TOKEN__ placeholders into DEST.
render_template() {
    local src="$1" dest="$2"
    sed -e "s|__USER__|${DEPLOY_USER}|g" \
        -e "s|__GROUP__|${DEPLOY_GROUP}|g" \
        -e "s|__APP_DIR__|${DEPLOY_APP_DIR}|g" \
        -e "s|__DATA_DIR__|${DEPLOY_DATA_DIR}|g" \
        -e "s|__ENV_FILE__|${DEPLOY_ENV_FILE}|g" \
        -e "s|__BIND_HOST__|${DEPLOY_BIND_HOST}|g" \
        -e "s|__BIND_PORT__|${DEPLOY_BIND_PORT}|g" \
        -e "s|__APP_SERVER_NAME__|${DEPLOY_APP_SERVER_NAME}|g" \
        -e "s|__SERVER_NAME__|${DEPLOY_APP_SERVER_NAME}|g" \
        -e "s|__LANDING_SERVER_NAME__|${DEPLOY_LANDING_SERVER_NAME}|g" \
        -e "s|__LANDING_ROOT__|${DEPLOY_LANDING_ROOT}|g" \
        -e "s|__ACME_WEBROOT__|${DEPLOY_ACME_WEBROOT}|g" \
        -e "s|__TLS_CERT__|${DEPLOY_TLS_CERT}|g" \
        -e "s|__TLS_KEY__|${DEPLOY_TLS_KEY}|g" \
        "$src" > "$dest"
}

# render_nginx_site TEMPLATE DEST — render an nginx site AND resolve the TLS conditional
# regions (SA-09). The templates carry three marker-delimited regions so ONE version-
# controlled file covers both the pre-cert (HTTP-only) and post-cert (HTTPS + redirect)
# states without certbot rewriting anything out of band:
#   __HTTP_ONLY_BEGIN__ … __HTTP_ONLY_END__  the :80 serving locations (no cert yet)
#   __REDIRECT_BEGIN__  … __REDIRECT_END__   the :80 -> :443 redirect (cert present)
#   __TLS_BEGIN__       … __TLS_END__        the whole :443 server block (cert present)
# DEPLOY_TLS_ENABLE=1 keeps redirect+TLS and drops the HTTP-only serving; =0 keeps HTTP-only
# serving and drops redirect+TLS. The ACME challenge location outside all markers is always
# kept so certbot --webroot can renew over plain :80.
render_nginx_site() {
    local src="$1" dest="$2"
    render_template "$src" "$dest"
    if [ "${DEPLOY_TLS_ENABLE:-0}" = "1" ]; then
        sed -i '/# __HTTP_ONLY_BEGIN__/,/# __HTTP_ONLY_END__/d' "$dest"
        sed -i '/# __REDIRECT_BEGIN__/d; /# __REDIRECT_END__/d' "$dest"
        sed -i '/# __TLS_BEGIN__/d; /# __TLS_END__/d' "$dest"
    else
        sed -i '/# __HTTP_ONLY_BEGIN__/d; /# __HTTP_ONLY_END__/d' "$dest"
        sed -i '/# __REDIRECT_BEGIN__/,/# __REDIRECT_END__/d' "$dest"
        sed -i '/# __TLS_BEGIN__/,/# __TLS_END__/d' "$dest"
    fi
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root (use sudo)"
}

# Whether a usable Chromium/Chrome for headless PDF export (FR-27) is present, checked WITHOUT
# executing any service-user-owned venv binary as root (SA-06). Mirrors the locations
# sitrep/pdf.py::_chromium_path searches: a Google Chrome / system Chromium on PATH, or a
# Playwright-managed Chromium under either browser dir. Relies on PLAYWRIGHT_BROWSERS_DIR and
# DEPLOY_APP_DIR being set by the caller (deploy.sh sets both before calling this).
_usable_chromium_present() {
    command -v google-chrome-stable >/dev/null 2>&1 && return 0
    command -v google-chrome        >/dev/null 2>&1 && return 0
    command -v chromium             >/dev/null 2>&1 && return 0
    command -v chromium-browser     >/dev/null 2>&1 && return 0
    local dir
    for dir in "${PLAYWRIGHT_BROWSERS_DIR:-}" "${DEPLOY_APP_DIR:-}/.cache/ms-playwright"; do
        [ -n "$dir" ] || continue
        compgen -G "$dir/chromium*/chrome-linux/chrome" >/dev/null 2>&1 && return 0
        compgen -G "$dir/chromium_headless_shell*/chrome-headless-shell-linux64/chrome-headless-shell" \
            >/dev/null 2>&1 && return 0
    done
    return 1
}

# ============================================================================
# SA-06 — atomic release engine (root-owned releases/ + `current` symlink flip)
# ============================================================================
# Build/activate/prune helpers shared by bootstrap.sh (initial build) and deploy.sh
# (updates). Root owns everything under $DEPLOY_RELEASES_DIR; each release is a clean
# `git archive` export with its own root-owned .venv + browser, read-only to the runtime
# account. This removes the last surface where the service user could influence what a
# later root deploy runs, and makes rollback a true source+deps+browser rollback (SA-06).

# install_uv_pinned — install the EXACT pinned uv (SA-06). Downloads Astral's versioned
# installer, optionally verifies a pinned SHA256 (UV_INSTALLER_SHA256), runs it, then
# asserts the resulting `uv --version` matches UV_VERSION so a tampered/substituted
# installer that yields a different toolchain is caught even without a pinned hash.
install_uv_pinned() {
    local ver="${UV_VERSION:-0.8.17}"
    local want_sha="${UV_INSTALLER_SHA256:-}"
    command -v uv >/dev/null 2>&1 && { ok "uv already present: $(uv --version)"; return 0; }
    local tmp
    tmp="$(mktemp)"
    log "downloading uv $ver installer"
    curl -LsSf "https://astral.sh/uv/${ver}/install.sh" -o "$tmp" || die "uv installer download failed"
    if [ -n "$want_sha" ]; then
        local got_sha
        got_sha="$(sha256sum "$tmp" | awk '{print $1}')"
        [ "$got_sha" = "$want_sha" ] \
            || { rm -f "$tmp"; die "uv installer SHA256 mismatch (got $got_sha, want $want_sha) — refusing to run it (SA-06)"; }
        ok "uv installer checksum verified"
    else
        warn "UV_INSTALLER_SHA256 not set — cannot verify the installer script; pin it in config.env (SA-06). Proceeding with a post-install version assertion."
    fi
    env UV_INSTALL_DIR=/usr/local/bin sh "$tmp"
    rm -f "$tmp"
    command -v uv >/dev/null 2>&1 || die "uv install failed; install it manually and re-run"
    case "$(uv --version 2>/dev/null)" in
        *"$ver"*) ok "uv $ver installed and verified" ;;
        *) die "installed uv version does not match the pinned $ver (got: $(uv --version)) — possible tampered installer (SA-06)" ;;
    esac
}

# install_chromium_into RELEASE_DIR — install a usable headless Chromium INTO the release
# (RELEASE_DIR/.playwright-browsers) so a rollback restores the browser matching that
# release's Playwright (SA-06). Runs Playwright's own managed install (version-pinned by the
# release's Playwright package); falls back to Google Chrome from its signed apt repo if that
# won't run on the distro (root apt only — never executes venv code, SA-06). Best-effort
# (NFR-6): a missing browser only makes the PDF endpoint return 503. Sets CHROMIUM_REVISION.
install_chromium_into() {
    local dir="$1"
    local browsers="$dir/.playwright-browsers"
    local pw="$dir/.venv/bin/playwright"
    CHROMIUM_REVISION=""
    log "installing Chromium into $browsers"
    if [ -x "$pw" ]; then
        env PLAYWRIGHT_BROWSERS_PATH="$browsers" "$pw" install chromium >/dev/null 2>&1 \
            && ok "Playwright Chromium ready" \
            || warn "playwright install chromium failed on this distro — will try Google Chrome"
    else
        warn "no playwright binary in the release venv — trying Google Chrome"
    fi
    if PLAYWRIGHT_BROWSERS_DIR="$browsers" _usable_chromium_present; then
        # Record the pinned Chromium revision (dir name, e.g. chromium-1169) for the manifest.
        local rev
        rev="$(compgen -G "$browsers/chromium-*" 2>/dev/null | head -n1 || true)"
        [ -n "$rev" ] && CHROMIUM_REVISION="$(basename "$rev")"
        ok "Chromium available for PDF export${CHROMIUM_REVISION:+ ($CHROMIUM_REVISION)}"
        return 0
    fi
    if command -v apt-get >/dev/null 2>&1; then
        warn "no usable Playwright Chromium — installing Google Chrome from its signed apt repo"
        if ! command -v google-chrome-stable >/dev/null 2>&1 \
                && ! command -v google-chrome >/dev/null 2>&1; then
            curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
                | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
            echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
http://dl.google.com/linux/chrome/deb/ stable main" \
                > /etc/apt/sources.list.d/google-chrome.list
            DEBIAN_FRONTEND=noninteractive apt-get update -qq
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq google-chrome-stable \
                || warn "google-chrome-stable install failed — PDF export will return 503"
        fi
        PLAYWRIGHT_BROWSERS_DIR="$browsers" _usable_chromium_present \
            && { CHROMIUM_REVISION="google-chrome-stable"; ok "Google Chrome available for PDF export"; } \
            || warn "no usable Chromium found — PDF export will return 503"
    else
        warn "no usable Chromium and no apt — PDF export will return 503"
    fi
}

# update_mirror — ensure the root-owned git mirror exists and fetch the latest refs.
update_mirror() {
    if [ ! -d "$DEPLOY_REPO_MIRROR/.git" ]; then
        log "cloning mirror $DEPLOY_REPO_URL -> $DEPLOY_REPO_MIRROR (root-owned)"
        rm -rf "$DEPLOY_REPO_MIRROR"
        git clone "$DEPLOY_REPO_URL" "$DEPLOY_REPO_MIRROR"
    fi
    log "fetching origin into the mirror"
    git -C "$DEPLOY_REPO_MIRROR" fetch origin --prune --tags --force
}

# resolve_committish REF — echo a committish the mirror can archive (origin/REF for a
# branch, else the ref as a tag/SHA). Returns non-zero if the ref is unknown.
resolve_committish() {
    local ref="$1"
    if git -C "$DEPLOY_REPO_MIRROR" show-ref --verify --quiet "refs/remotes/origin/$ref"; then
        echo "origin/$ref"
    elif git -C "$DEPLOY_REPO_MIRROR" rev-parse --verify --quiet "${ref}^{commit}" >/dev/null; then
        echo "$ref"
    else
        return 1
    fi
}

# _harden_release_perms DIR — root owns; group (the service account) may read+traverse+exec
# but NOT write; others get nothing. This is the SA-06 trust boundary: the runtime account
# cannot alter the code/venv/browser it runs.
_harden_release_perms() {
    local dir="$1"
    chown -R "root:$DEPLOY_GROUP" "$dir"
    chmod -R u=rwX,g=rX,o= "$dir"
}

# current_release_target — echo the canonical path `current` points at, or "" if unset.
current_release_target() {
    [ -L "$DEPLOY_CURRENT_LINK" ] || { echo ""; return; }
    readlink -f "$DEPLOY_CURRENT_LINK" 2>/dev/null || echo ""
}

# build_release REF — build (or reuse) the release for REF. Sets RELEASE_DIR, RELEASE_SHA,
# RELEASE_NAME. Idempotent: a release whose `.release-ok` marker exists is reused as-is. A
# half-built dir (marker absent) is wiped and rebuilt, and is never activated.
build_release() {
    local ref="$1"
    update_mirror
    local committish
    committish="$(resolve_committish "$ref")" || die "unknown ref '$ref' (not a branch, tag, or SHA in the mirror)"

    # SA-07: verify a signed annotated tag before building it, when required.
    if [ "${DEPLOY_VERIFY_TAG_SIGNATURE:-0}" = "1" ] \
            && [ "$(git -C "$DEPLOY_REPO_MIRROR" cat-file -t "$ref" 2>/dev/null)" = "tag" ]; then
        log "verifying signature of tag $ref (SA-07)"
        git -C "$DEPLOY_REPO_MIRROR" verify-tag "$ref" \
            || die "tag '$ref' failed signature verification — refusing to deploy (SA-07)"
        ok "tag signature verified"
    fi

    RELEASE_SHA="$(git -C "$DEPLOY_REPO_MIRROR" rev-parse --short "$committish")"
    RELEASE_NAME="$(git -C "$DEPLOY_REPO_MIRROR" describe --tags --always "$committish" 2>/dev/null || echo "$RELEASE_SHA")"
    RELEASE_DIR="$DEPLOY_RELEASES_DIR/$RELEASE_SHA"

    if [ -f "$RELEASE_DIR/.release-ok" ]; then
        ok "release $RELEASE_SHA already built — reusing $RELEASE_DIR"
        return 0
    fi

    log "building release $RELEASE_NAME ($RELEASE_SHA) -> $RELEASE_DIR"
    install -d -o root -g "$DEPLOY_GROUP" -m 0755 "$DEPLOY_RELEASES_DIR"
    rm -rf "$RELEASE_DIR"
    # Clean export (no .git) — root-owned by construction.
    mkdir -p "$RELEASE_DIR"
    git -C "$DEPLOY_REPO_MIRROR" archive --format=tar "$committish" | tar -x -C "$RELEASE_DIR"

    # Build the venv IN the final release path so uv's editable install records the stable
    # path (a build-then-rename would bake a stale path into the venv). uv runs as ROOT here
    # (the whole point of SA-06): the resulting venv is root-owned and the runtime account
    # cannot tamper with it. `--frozen` installs the exact committed uv.lock set.
    ( cd "$RELEASE_DIR" && uv sync --frozen --no-dev --python 3.11 ) \
        || die "uv sync failed for $RELEASE_SHA — release not activated"

    # Fail fast if the GRIB stack can't import (the most likely host-specific breakage).
    "$RELEASE_DIR/.venv/bin/python" -c "import cfgrib" 2>/dev/null \
        || warn "cfgrib failed to import in $RELEASE_SHA — check ecCodes (deploy/README.md)"

    install_chromium_into "$RELEASE_DIR"

    # Stamp the release manifest (frontend/version.json): what's deployed, plus the pinned
    # Chromium revision (SA-06). /v1/health echoes version; the PWA polls it to nudge stale
    # clients to reload.
    local built_at
    built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    mkdir -p "$RELEASE_DIR/frontend"
    cat > "$RELEASE_DIR/frontend/version.json" <<EOF
{"version": "$RELEASE_NAME", "sha": "$RELEASE_SHA", "built_at": "$built_at", "chromium": "${CHROMIUM_REVISION:-unknown}"}
EOF

    touch "$RELEASE_DIR/.release-ok"
    _harden_release_perms "$RELEASE_DIR"
    ok "release $RELEASE_SHA built (chromium: ${CHROMIUM_REVISION:-unknown})"
}

# activate_release RELEASE_DIR — atomically flip `current` to point at RELEASE_DIR. The
# ln+mv -T pair replaces the symlink via rename(2), so the switch is atomic; systemd resolves
# it on the next restart.
activate_release() {
    local target="$1"
    [ -f "$target/.release-ok" ] || die "refusing to activate an incomplete release: $target"
    ln -sfn "$target" "$DEPLOY_CURRENT_LINK.tmp"
    mv -Tf "$DEPLOY_CURRENT_LINK.tmp" "$DEPLOY_CURRENT_LINK"
    ok "current -> $target"
}

# prune_releases — keep the newest $DEPLOY_KEEP_RELEASES release dirs; never remove the one
# `current` points at (needed for rollback / the running service).
prune_releases() {
    local keep="${DEPLOY_KEEP_RELEASES:-5}" active i=0 d
    active="$(current_release_target)"
    while IFS= read -r d; do
        d="${d%/}"
        i=$((i + 1))
        [ "$i" -le "$keep" ] && continue
        [ "$d" = "$active" ] && continue
        log "pruning old release $d"
        rm -rf "$d"
    done < <(ls -1dt "$DEPLOY_RELEASES_DIR"/*/ 2>/dev/null || true)
}
