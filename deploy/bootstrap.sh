#!/usr/bin/env bash
# UpstreamWX — one-time host provisioning for the always-on backend (roadmap §M0.1.1).
#
# Run ONCE as root on a fresh EC2 host. Idempotent: safe to re-run. It installs system
# packages, the service account, the directory layout, the systemd unit and nginx site,
# then hands off to deploy.sh to build the venv, install the app, and start the service.
#
#   sudo deploy/bootstrap.sh
#
# Configure the target first by copying deploy/config.env.example -> deploy/config.env.
# Tested on Ubuntu/Debian (apt); Amazon Linux notes are in deploy/README.md.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib.sh"
load_config
require_root

# --- 1. System packages --------------------------------------------------------------
# shapely/pyproj/geopandas ship manylinux wheels that bundle GEOS/GDAL/PROJ, so the only
# GRIB/geo system library cfgrib genuinely benefits from is ecCodes (FR: SREF/HREF GRIB2).
install_packages_apt() {
    log "installing system packages (apt)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # Essentials — fatal if these can't install.
    apt-get install -y -qq \
        git curl ca-certificates build-essential \
        nginx libeccodes0 \
        fonts-liberation fonts-noto-color-emoji
    # Chromium/Playwright host libraries for server-side PDF export (FR-27, sitrep/pdf.py).
    # Playwright manages its own Chromium binary (deploy.sh does `playwright install chromium`);
    # these are the host libs it links against. On Ubuntu 24.04 (noble) the 64-bit time_t
    # transition renamed several of them with a `t64` suffix (e.g. libasound2 -> libasound2t64),
    # so a hardcoded name has "no installation candidate". Resolve each to whichever variant the
    # distro actually ships, then install best-effort (a missing Chromium lib only degrades PDF
    # export to a 503 — NFR-6 — so it must never abort bootstrap).
    local want=(
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2
        libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0
    )
    local resolved=() p
    for p in "${want[@]}"; do
        if [ "$(apt-cache policy "$p" 2>/dev/null | awk '/Candidate:/{print $2}')" != "(none)" ] \
                && apt-cache show "$p" >/dev/null 2>&1; then
            resolved+=("$p")
        elif apt-cache show "${p}t64" >/dev/null 2>&1; then
            resolved+=("${p}t64")          # Ubuntu 24.04+ time_t-renamed variant
        else
            warn "no apt candidate for $p (or ${p}t64) — skipping; PDF export may 503 if it's needed"
        fi
    done
    if [ "${#resolved[@]}" -gt 0 ]; then
        apt-get install -y -qq "${resolved[@]}" \
            || warn "some Chromium libs failed to install — PDF export may 503 (NFR-6)"
    fi
    ok "apt packages installed"
}

install_packages_dnf() {
    log "installing system packages (dnf/yum)"
    "$PKG" install -y git curl ca-certificates gcc gcc-c++ make nginx >/dev/null
    # ecCodes is not in the default Amazon Linux repos; the `eccodes` PyPI wheel bundles
    # the binary as a fallback. See deploy/README.md if cfgrib fails to import.
    "$PKG" install -y eccodes >/dev/null 2>&1 || warn "system eccodes unavailable — relying on the pip wheel"
    # Chromium system libraries for server-side PDF export (FR-27).
    "$PKG" install -y nss nspr atk at-spi2-atk cups-libs libdrm libXcomposite \
        libXdamage libXrandr mesa-libgbm alsa-lib pango cairo >/dev/null 2>&1 \
        || warn "some Chromium system libs unavailable — PDF export may fail on this host"
    ok "dnf packages installed"
}

if command -v apt-get >/dev/null 2>&1; then
    install_packages_apt
elif command -v dnf >/dev/null 2>&1; then
    PKG=dnf install_packages_dnf
elif command -v yum >/dev/null 2>&1; then
    PKG=yum install_packages_dnf
else
    die "no supported package manager (apt/dnf/yum) found"
fi

# --- 1b. Unattended security updates -------------------------------------------------
# Keep the host patched without manual intervention — but SECURITY origin only (no feature
# churn that could break the GRIB stack) and NO automatic reboot: on a single-instance host
# a surprise reboot is downtime, so reboot manually when /var/run/reboot-required appears
# (see deploy/README.md). Idempotent; safe to re-run.
configure_auto_updates() {
    if command -v apt-get >/dev/null 2>&1; then
        log "enabling unattended security upgrades (apt)"
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unattended-upgrades
        cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
        ok "unattended-upgrades enabled (security origin; manual reboot)"
    elif command -v dnf >/dev/null 2>&1; then
        log "enabling automatic security updates (dnf-automatic)"
        dnf install -y dnf-automatic >/dev/null
        sed -i 's/^apply_updates = .*/apply_updates = yes/' /etc/dnf/automatic.conf 2>/dev/null || true
        sed -i 's/^upgrade_type = .*/upgrade_type = security/' /etc/dnf/automatic.conf 2>/dev/null || true
        systemctl enable --now dnf-automatic.timer >/dev/null 2>&1 || true
        ok "dnf-automatic enabled (security; manual reboot)"
    else
        warn "no apt/dnf — configure OS auto-updates manually"
    fi
}
configure_auto_updates

# --- 2. uv (Python toolchain, matches the repo) --------------------------------------
# SA-06: install the EXACT pinned uv, verify a pinned installer checksum if provided, and
# assert the resulting version — see install_uv_pinned in _lib.sh. Override UV_VERSION to bump
# and set UV_INSTALLER_SHA256 in config.env to enable full installer verification.
install_uv_pinned
export UV_VERSION

# --- 3. Service account + directories (SA-06 atomic layout) ---------------------------
# The base dir ($DEPLOY_APP_DIR) is now ROOT-OWNED: it holds the root-owned git mirror,
# releases/, and the `current` symlink. The service account may traverse it but must not
# write into it — the runtime user can no longer drop files that a later root deploy runs.
if ! getent group "$DEPLOY_GROUP" >/dev/null; then
    groupadd --system "$DEPLOY_GROUP"
fi
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    log "creating service user $DEPLOY_USER"
    useradd --system --gid "$DEPLOY_GROUP" --home-dir "$DEPLOY_APP_DIR" \
            --shell /usr/sbin/nologin "$DEPLOY_USER"
fi
install -d -o root -g "$DEPLOY_GROUP" -m 0755 "$DEPLOY_APP_DIR"
install -d -o root -g "$DEPLOY_GROUP" -m 0755 "$DEPLOY_RELEASES_DIR"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" -m 0750 "$DEPLOY_DATA_DIR"
install -d -o root -g "$DEPLOY_GROUP" -m 0750 "$DEPLOY_ENV_DIR"
# ACME http-01 webroot for certbot --webroot (SA-09). World-readable (nginx serves it).
install -d -o root -g "$DEPLOY_GROUP" -m 0755 "$DEPLOY_ACME_WEBROOT"
# The apex landing is served BY NGINX (as its worker user) straight from the release tree,
# which SA-06 hardens to root:DEPLOY_GROUP with no world access. Add nginx's worker user to
# DEPLOY_GROUP so it can read current/landing/ — otherwise the apex returns 500 (Permission
# denied). Debian/Ubuntu uses www-data; RHEL/Amazon Linux uses nginx. Idempotent; harmless on
# an app-only box with no landing. nginx must be (re)started for the new group to take effect —
# section 6 does that.
for _web in www-data nginx; do
    if id "$_web" >/dev/null 2>&1 && ! id -nG "$_web" | tr ' ' '\n' | grep -qx "$DEPLOY_GROUP"; then
        usermod -aG "$DEPLOY_GROUP" "$_web" && ok "added $_web to $DEPLOY_GROUP (nginx landing access)"
    fi
done
ok "user + directories ready (base is root-owned)"

# --- 3b. Migrate an old in-place checkout (pre-SA-06) ---------------------------------
# Earlier hosts had a service-user-owned git checkout AT $DEPLOY_APP_DIR (with .git/.venv at
# the top level). The atomic model builds into releases/<sha> and points `current` there, so
# the old top-level tree is vestigial. Move it aside ONCE so nothing service-writable remains
# in the run path (the base is now root-owned; systemd runs from current/). Data, env, and
# systemd/nginx configs live outside $DEPLOY_APP_DIR and are untouched.
if [ -d "$DEPLOY_APP_DIR/.git" ] && [ ! -e "$DEPLOY_APP_DIR/.pre-atomic" ]; then
    log "migrating old in-place checkout aside -> $DEPLOY_APP_DIR/.pre-atomic"
    install -d -o root -g root -m 0700 "$DEPLOY_APP_DIR/.pre-atomic"
    for entry in "$DEPLOY_APP_DIR"/* "$DEPLOY_APP_DIR"/.git "$DEPLOY_APP_DIR"/.venv; do
        [ -e "$entry" ] || continue
        base="$(basename "$entry")"
        case "$base" in
            repo|releases|current|.pre-atomic) continue ;;   # new-layout entries — keep
        esac
        mv "$entry" "$DEPLOY_APP_DIR/.pre-atomic/" 2>/dev/null || true
    done
    warn "old checkout moved to $DEPLOY_APP_DIR/.pre-atomic — remove it once the new deploy is verified"
fi

# --- 4. Source mirror ----------------------------------------------------------------
# The root-owned git mirror deploy.sh archives releases from. update_mirror clones it if
# absent; do it here so bootstrap's own delegated deploy.sh run has it ready.
update_mirror

# Templates are read from the running scripts' own repo ($REPO_DIR), NOT from $DEPLOY_APP_DIR
# (which is now the root-owned atomic base with no top-level checkout). Run bootstrap from a
# clone of the repo.
TPL="$REPO_DIR/deploy"

# --- 5. Runtime env file (install once; never clobber live secrets) ------------------
if [ ! -f "$DEPLOY_ENV_FILE" ]; then
    log "installing runtime env file -> $DEPLOY_ENV_FILE"
    install -o root -g "$DEPLOY_GROUP" -m 0640 \
        "$TPL/upstreamwx.env.example" "$DEPLOY_ENV_FILE"
    warn "edit $DEPLOY_ENV_FILE — set NWS contact and (optional) ANTHROPIC_API_KEY"
else
    ok "env file exists at $DEPLOY_ENV_FILE (left untouched)"
fi

# --- 6. systemd unit + nginx sites (rendered from templates) -------------------------
log "installing systemd unit and nginx sites"
render_template "$TPL/systemd/upstreamwx-api.service" \
                "/etc/systemd/system/${DEPLOY_SERVICE}.service"

# Private journald namespace for the API (LogNamespace=upstreamwx in the unit) so its logs
# prune to ~10 days independently of the system journal (deploy/systemd/journald@upstreamwx.conf).
install -o root -g root -m 0644 \
    "$TPL/systemd/journald@upstreamwx.conf" \
    /etc/systemd/journald@upstreamwx.conf
systemctl restart systemd-journald@upstreamwx 2>/dev/null || true

# Name the site after the service so a second environment (e.g. staging) installs its own
# site instead of clobbering production's. render_nginx_site (SA-09) resolves the TLS
# conditional regions from DEPLOY_TLS_ENABLE. The landing + default-server sites install only
# where they make sense.
install_nginx_site() {
    # install_nginx_site TEMPLATE SITE_NAME
    local template="$1" name="$2"
    if [ -d /etc/nginx/sites-available ]; then
        render_nginx_site "$template" "/etc/nginx/sites-available/${name}.conf"
        ln -sf "/etc/nginx/sites-available/${name}.conf" \
               "/etc/nginx/sites-enabled/${name}.conf"
    else
        # Amazon Linux / RHEL nginx uses conf.d, not sites-available.
        render_nginx_site "$template" "/etc/nginx/conf.d/${name}.conf"
    fi
}

install_nginx_site "$TPL/nginx/upstreamwx.conf" "$DEPLOY_SERVICE"
# Default server: drop unknown Hosts at the edge with 444 (SA-09). Only the PUBLIC app box
# installs it (it claims listen ...default_server); a tailnet-only staging box skips it so it
# doesn't shadow another service's default vhost.
if [ -n "${DEPLOY_LANDING_SERVER_NAME:-}" ]; then
    install_nginx_site "$TPL/nginx/default-server.conf" "${DEPLOY_SERVICE}-default"
    install_nginx_site "$TPL/nginx/landing.conf" "${DEPLOY_SERVICE}-landing"
    ok "landing + default-server sites enabled for: $DEPLOY_LANDING_SERVER_NAME"
else
    warn "DEPLOY_LANDING_SERVER_NAME empty — skipping the apex landing + default-server sites"
fi
[ -e /etc/nginx/sites-enabled/default ] && rm -f /etc/nginx/sites-enabled/default
systemctl daemon-reload
# Enable the API unit so its [Install] WantedBy=multi-user.target wiring takes effect and the
# service starts automatically on boot. Without this the unit is loaded and can be started by
# deploy.sh's `systemctl restart`, but a host reboot leaves it DOWN (the staging box did exactly
# this). deploy.sh re-asserts this idempotently, so already-provisioned hosts self-heal on deploy.
systemctl enable "$DEPLOY_SERVICE" >/dev/null 2>&1 \
    && ok "boot-enabled $DEPLOY_SERVICE (starts on reboot)" \
    || warn "could not enable $DEPLOY_SERVICE — it will NOT start on reboot"
if _nginx_out="$(nginx -t 2>&1)"; then
    systemctl enable nginx >/dev/null 2>&1 || true
    # restart (not reload) so nginx workers pick up the DEPLOY_GROUP membership added above
    # (needed to read the hardened release tree for the apex landing). Bootstrap is one-time
    # provisioning, so a restart here is fine; deploy.sh never restarts nginx.
    systemctl restart nginx
    ok "nginx configured (TLS ${DEPLOY_TLS_ENABLE:-0})"
else
    warn "nginx -t failed — review before reloading nginx. Output:"
    printf '%s\n' "$_nginx_out" >&2
fi

# --- 6b. TLS via certbot --webroot (SA-09) -------------------------------------------
# Issue/renew a multi-SAN cert WITHOUT letting certbot rewrite nginx (--webroot, not --nginx),
# then re-render the sites with the version-controlled :443 block (DEPLOY_TLS_ENABLE=1). Runs
# only when an email is configured and the cert isn't already present. Renewal is certbot's
# own systemd timer; we add a reload deploy-hook so nginx picks up renewed certs.
setup_tls_webroot() {
    [ -n "${DEPLOY_CERTBOT_EMAIL:-}" ] || { warn "DEPLOY_CERTBOT_EMAIL empty — skipping certbot (add TLS later, see deploy/README.md)"; return 0; }
    command -v certbot >/dev/null 2>&1 || {
        log "installing certbot"
        if command -v apt-get >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot || { warn "certbot install failed — skipping TLS"; return 0; }
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y certbot >/dev/null 2>&1 || { warn "certbot install failed — skipping TLS"; return 0; }
        else
            warn "no apt/dnf — install certbot manually"; return 0
        fi
    }
    # Build the -d list: app name(s) + landing name(s).
    local d_args=() name
    for name in $DEPLOY_APP_SERVER_NAME $DEPLOY_LANDING_SERVER_NAME; do
        d_args+=(-d "$name")
    done
    local primary="${DEPLOY_APP_SERVER_NAME%% *}"
    # Reuse an existing valid cert if DEPLOY_TLS_CERT already resolves to one (its default is
    # the app-name path, but an operator can point it at a cert issued under another name —
    # e.g. one whose lineage is named after the apex). Only issue when no cert is present, so a
    # good cert is never needlessly re-issued.
    if [ -f "$DEPLOY_TLS_CERT" ]; then
        ok "cert already present at $DEPLOY_TLS_CERT"
    elif [ -f "/etc/letsencrypt/live/${primary}/fullchain.pem" ]; then
        ok "cert already present for ${primary}"
    else
        log "issuing cert via certbot --webroot for: $DEPLOY_APP_SERVER_NAME $DEPLOY_LANDING_SERVER_NAME"
        # --cert-name pins the lineage (and therefore the live/<name>/ path) to the app's
        # primary name, so it always matches DEPLOY_TLS_CERT's default — certbot otherwise names
        # the dir after the first -d, and any drift makes nginx fail to load the cert.
        certbot certonly --webroot -w "$DEPLOY_ACME_WEBROOT" --cert-name "$primary" "${d_args[@]}" \
            --email "$DEPLOY_CERTBOT_EMAIL" --agree-tos --non-interactive --keep-until-expiring \
            --deploy-hook "systemctl reload nginx" \
            || { warn "certbot issuance failed — leaving the site HTTP-only; fix DNS/ports and re-run bootstrap"; return 0; }
        ok "certificate issued for ${primary}"
    fi
    # Re-render the sites WITH the :443 block now that the cert exists.
    DEPLOY_TLS_ENABLE=1
    install_nginx_site "$TPL/nginx/upstreamwx.conf" "$DEPLOY_SERVICE"
    install_nginx_site "$TPL/nginx/default-server.conf" "${DEPLOY_SERVICE}-default"
    install_nginx_site "$TPL/nginx/landing.conf" "${DEPLOY_SERVICE}-landing"
    if nginx -t >/dev/null 2>&1; then
        systemctl reload nginx
        ok "nginx now serving HTTPS (:443 under version control, SA-09)"
    else
        warn "nginx -t failed after enabling TLS — review the :443 config"
    fi
}
# Only attempt TLS on a public box (landing names set imply real DNS). Tailnet staging keeps
# TLS via `tailscale serve` and skips certbot.
if [ -n "${DEPLOY_LANDING_SERVER_NAME:-}" ]; then
    setup_tls_webroot
fi

# --- 6c. Ops wrapper (uwx-ctl) -------------------------------------------------
# Persist the loaded deploy config to a durable, root-owned path and install a wrapper that
# bakes it in — so ongoing ops are `uwx-ctl deploy` / `logs` / `rollback`, with no
# DEPLOY_CONFIG or current/ path to remember (which is exactly the footgun that mis-fires a
# staging deploy against prod defaults). The persisted file becomes THE config the wrapper
# uses: edit it in place, or re-run bootstrap from a clone to refresh it.
_loaded_cfg="${DEPLOY_CONFIG:-$DEPLOY_DIR/config.env}"
if [ -f "$_loaded_cfg" ]; then
    install -o root -g "$DEPLOY_GROUP" -m 0640 "$_loaded_cfg" "$DEPLOY_CTL_CONFIG"
else
    # Pure defaults (no config file) — write an empty one so the wrapper's DEPLOY_CONFIG
    # points at a valid (no-op) file and load_config falls through to defaults.
    printf '# UpstreamWX deploy config (defaults; generated by bootstrap)\n' > "$DEPLOY_CTL_CONFIG"
    chown "root:$DEPLOY_GROUP" "$DEPLOY_CTL_CONFIG"; chmod 0640 "$DEPLOY_CTL_CONFIG"
fi
render_template "$TPL/uwx-ctl" "/usr/local/bin/$DEPLOY_CTL_NAME"
chmod 0755 "/usr/local/bin/$DEPLOY_CTL_NAME"
ok "installed $DEPLOY_CTL_NAME -> /usr/local/bin/$DEPLOY_CTL_NAME (config: $DEPLOY_CTL_CONFIG)"

# --- 7. Build the first release + start the service (delegated to deploy.sh) ----------
log "running deploy.sh for the initial release build + service start"
"$DEPLOY_DIR/deploy.sh" "$DEPLOY_BRANCH"

cat <<EOF

$(ok "bootstrap complete")

Next steps:
  1. Edit secrets/contact:   sudo nano $DEPLOY_ENV_FILE
                             sudo systemctl restart $DEPLOY_SERVICE
  2. TLS:                    handled by certbot --webroot above when DEPLOY_CERTBOT_EMAIL is
                             set (SA-09); otherwise add it later and re-run bootstrap.
  3. Public activation:      set UPSTREAMWX_SESSION_SECRET + UPSTREAMWX_API_TRUSTED_HOSTS +
                             UPSTREAMWX_API_AUTH_REQUIRED=1 in $DEPLOY_ENV_FILE, and
                             DEPLOY_REQUIRE_HTTPS=1 + DEPLOY_VERIFY_TAG_SIGNATURE=1 in config.env.
  4. Verify:                 $DEPLOY_CTL_NAME health

From now on, drive this box with the wrapper (no DEPLOY_CONFIG / paths to remember):
  $DEPLOY_CTL_NAME deploy [ref]     # build + activate a release
  $DEPLOY_CTL_NAME rollback         # flip back to the previous release
  $DEPLOY_CTL_NAME status | logs -f | releases | version | restart
  $DEPLOY_CTL_NAME bootstrap        # re-run provisioning
Its config lives at $DEPLOY_CTL_CONFIG (edit there, or re-run bootstrap to refresh).
EOF
