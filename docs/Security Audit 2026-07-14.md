# Security Audit — 2026-07-14

- **Application:** UpstreamWX
- **Audit date:** 2026-07-14
- **Repository state reviewed:** commit 27793e5218bf73b92179d5b61f3a1ae032ef5998 on branch Security-audit-7.14.26
- **Declared package version:** 0.5.0
- **Review type:** pre-release source, configuration, dependency, and deployment security review
- **Overall risk:** **High**
**Release recommendation:** **Hold production promotion of the tag until the six High findings are fixed or explicitly risk-accepted.**

## Executive summary

UpstreamWX has a number of good defensive controls, including an unprivileged and substantially hardened systemd service, loopback binding in the production unit, bounded worker concurrency, request timeouts, output escaping, PDF network isolation, and graceful degradation when data providers fail. The major PDF injection and scheduler event-loop issues described in the 2026-07-02 code review have been materially improved.

The current release nevertheless has six High-severity findings. The most urgent are:

1. The repository's private-beta deployment has no access gate, while it exposes CPU-, network-, browser-, and potentially billable model operations.
2. Unbounded mission fields combined with count-bounded caches permit a small number of large requests to retain multiple gigabytes in one process.
3. Any accepted live briefing is registered for automatic refresh, allowing an unauthenticated client to create recurring background work long after the request ends.
4. The shared briefing cache omits mission identity fields, so one requester can receive another requester's mission name and presentation.
5. Mutable third-party JavaScript executes in the application origin without Subresource Integrity or a Content Security Policy.
6. The release is not reproducible, and the deployment process executes a service-user-controlled virtual-environment executable as root.

These are practical release risks for a public Internet host. An external access layer for the private beta would immediately reduce exposure, but it does not replace fixes to cache isolation, input bounds, or the deployment trust boundary.

### Finding count

| Severity | Count |
|---|---:|
| Critical | 0 |
| High | 6 |
| Medium | 5 |
| Low | 2 |

No Critical issue was confirmed in this static review. That is not a guarantee that none exists.

## Scope and limitations

### In scope

- FastAPI request handling, models, caching, scheduled refresh, and resource controls
- Weather, watershed, GRIB, and model-provider network clients
- Browser-based PDF generation
- PWA storage, rendering, service worker, and third-party browser dependencies
- nginx, systemd, bootstrap, deploy, and rollback workflow
- GitHub Actions and release provenance
- Dependency declarations and a point-in-time vulnerability scan
- Tracked-tree and Git-history secret-pattern review
- Follow-up against docs/code-review-2026-07-02.md

### Not in scope

- The live EC2 host, DNS, certificates, security groups, firewall, CDN, WAF, Tailscale, or any external identity-aware proxy
- Runtime environment variables or secret-manager configuration
- GitHub branch-protection, environment-protection, or organization settings
- Authenticated cloud/provider consoles and logs
- Dynamic penetration testing against a deployed service
- A complete browser or native-library exploit assessment
- Formal verification of meteorological correctness or clinical decision safety

If the beta is already protected by an external access-control layer, SA-01's likelihood is lower. That control should be documented and tested before release because it is not represented in this repository.

## Threat model

The principal assets are service availability during field planning, the integrity of a displayed risk assessment, precise mission locations and times, the Anthropic API key and billable usage, the deployment host, and the integrity of tagged releases.

The main adversaries considered are:

- An unauthenticated Internet client sending crafted or high-volume requests
- A user attempting to poison another user's shared cached response
- A compromised CDN, Python package, GitHub Action, or upstream data host
- An attacker who obtains limited execution as the application service account
- Accidental misconfiguration during bootstrap, TLS setup, or rollback

The application is especially sensitive to integrity failures: even when it is labelled “reference only,” altered or cross-contaminated hazard information can influence expedition decisions.

## Release-blocking findings

### SA-01 — Private beta access is not technically restricted

- **Severity:** High
- **Category:** Missing access control / abuse prevention
- **Affected components:** src/upstreamwx/api/app.py, deploy/nginx/upstreamwx.conf

#### Evidence

- FastAPI exposes POST /v1/briefing, /v1/briefing/frame, /v1/briefing/pdf, and /v1/watershed/warm without authentication or authorization (app.py:264, 289, 340, 431).
- The nginx template proxies these endpoints without Basic Authentication, an allowlist, signed access token, or identity-aware proxy integration.
- /v1/briefing performs weather/watershed/model work, /v1/briefing/pdf launches Chromium, and /v1/briefing/frame can make billable Anthropic requests.
- Per-IP rate limits exist for framing, PDF, and warming. The main briefing endpoint relies on a 2 requests/second nginx limit and cold-generation concurrency rather than a per-principal budget.

#### Impact

Possession of the URL is effectively the beta invitation. Crawlers, leaked links, distributed clients, or deliberate abuse can consume host resources, fill persistent in-process registries, and create model charges. IP-only throttling is weak identity and is readily shared, rotated, or bypassed when the app is started directly.

#### Recommendation

Before the private-beta release, place the entire application behind one tested access gate, such as an identity-aware reverse proxy, a VPN/tailnet, or temporary nginx Basic Authentication over TLS. Exempt only a minimal liveness endpoint if the monitoring system requires it.

For a future public release, use an application-level anonymous session or API token for fair-use accounting, global and per-principal cost budgets, cache-miss limits, model-spend limits, and alerting. Keep the edge rate limiter as defense in depth.

#### Acceptance test

- An unauthenticated request to the PWA and each /v1 endpoint is denied.
- An authorized beta user can complete the normal PWA, PDF, and framing flow.
- The access control cannot be bypassed by reaching port 8000 directly.

### SA-02 — Unbounded mission input can exhaust memory through count-only caches

- **Severity:** High
- **Category:** Uncontrolled resource consumption
- **Affected components:** src/upstreamwx/api/models.py, api/cache.py, api/service.py, sitrep/render.py, deploy/nginx/upstreamwx.conf

#### Evidence

- MissionSpec.name has no maximum length; party_size and route_note also have no bounds (models.py:90-94).
- inputs is an untyped dictionary and is expanded into HazardInputs with HazardInputs(**data) (models.py:115-118, 188). Unknown keys or invalid values can escape as application errors rather than clean validation failures.
- nginx accepts request bodies up to 4 MiB (upstreamwx.conf:38-39). The application does not independently cap /v1/briefing request bytes.
- The rendered Markdown copies mission.name (render.py:114), and BriefingResult retains the Mission object.
- Briefing and result caches are bounded by entry count, not retained bytes. The default is 512 entries (config.py:112-118).
- Requests with inputs are treated as deterministic and use a non-expiring static token (service.py:144-146).

#### Exploit scenario

An unauthenticated client can submit a roughly 3 MiB mission name with inputs set to an empty object, then vary a cache-key field such as rounded latitude across 512 requests. Each entry retains the large mission value and a rendered Markdown copy. This can retain multiple gigabytes despite the “512 entry” cap. At the repository's nginx rate of two requests per second, filling 512 entries takes about 4.3 minutes from one address, before considering burst capacity or distributed sources.

The explicit-input path avoids live ingest and never expires, which makes this attack relatively cheap and durable until eviction or process restart.

#### Recommendation

- Apply server-side limits to every string and collection. Suggested starting points: name 80 characters, route_note 1,000 characters, and party_size 1-200.
- Replace inputs: dict with a strict Pydantic representation of HazardInputs that rejects unknown fields, coerced containers, non-finite numbers, and out-of-range values.
- Feature-flag or authorize the replay/inputs path in production if it is not required by ordinary PWA users.
- Enforce streaming request-size limits in the application as well as nginx.
- Bound caches by estimated retained bytes in addition to entry count, and add a TTL for static replay entries.
- Rate-limit cache misses and request cost, not just request count.

#### Acceptance test

- Oversized fields return 422 or 413 without invoking generation.
- Unknown inputs keys and non-finite values return a bounded 422 response.
- A load test using the maximum legal request cannot exceed the configured cache memory budget.

### SA-03 — Public requests create recurring scheduler workload

- **Severity:** High
- **Category:** Persistent workload amplification / availability
- **Affected components:** src/upstreamwx/api/service.py, api/scheduler.py, api/models.py

#### Evidence

- Every successful live briefing whose mission has not ended calls _register_active (service.py:169-175).
- The active registry permits 256 entries by default (config.py:120-125).
- A mission may start up to 10 days in the future and span seven days (models.py:30-32, 130-134, 149-153).
- At every scheduled refresh pass, refresh_active sequentially calls generate_briefing for every in-range entry (service.py:234-250).
- Scheduled generation does not acquire the request-generation semaphore and has no total work or wall-clock budget.
- The active dictionary is accessed from request worker threads and the scheduler thread without a lock.

#### Impact

An unauthenticated client can fill the registry with 256 distinct long-lived missions. The original request is rate-limited, but the server then repeats up to 256 complete ingests at each cycle until entries expire or are evicted. This is an amplification from one request into days of recurring network, CPU, disk, and memory activity. A long refresh pass can compete with real users and data warming. Concurrent registry mutation also risks exceptions or inconsistent refresh behavior.

#### Recommendation

- Register missions for refresh only for authenticated/authorized principals.
- Set per-principal and global active-mission quotas substantially below 256.
- Refresh only missions that have been viewed recently or explicitly opted into monitoring.
- Deduplicate upstream work by weather cycle and geographic domain rather than regenerating every presentation independently.
- Put each pass under a hard item, cost, and wall-clock budget; stop cleanly when the budget is exhausted.
- Coordinate scheduled work with request concurrency and add a lock around registry mutation.
- Emit metrics and alerts for registry size, pass duration, failures, and work skipped by budget.

#### Acceptance test

- Creating many missions cannot cause unbounded or multi-day work for an anonymous client.
- A scheduler pass stops within its configured budget and does not starve an interactive briefing.
- Concurrency tests cover simultaneous registration, eviction, pruning, and refresh.

### SA-04 — Cache key collision leaks or substitutes mission metadata

- **Severity:** High
- **Category:** Cross-request data isolation / cache poisoning
- **Affected components:** src/upstreamwx/api/cache.py, api/service.py, tests/test_api_cycles.py

#### Evidence

- mission_cache_key includes activity, rounded location, time window, phases, slot status, radii, and optional inputs, but omits name, party_size, and route_note (cache.py:75-98).
- The test suite explicitly asserts that two differently named missions have the same key (test_api_cycles.py:86-95).
- On a cache hit, the service returns the previously generated briefing unchanged (service.py:148-150, 161-164).
- BriefingResult contains the original Mission, and the rendered Markdown displays the original mission name.

#### Impact

Two users requesting the same place, activity, and window can receive the first user's mission name and request-specific presentation. This is a confidentiality failure and an integrity problem. An attacker can pre-seed a predictable location/window with a misleading mission label that appears in later users' briefings.

The current frontend escaping prevents this from being a straightforward script-injection issue, but it does not prevent misleading text or cross-user disclosure.

#### Recommendation

The immediate fix is to include every response-affecting mission field in the response cache key. The stronger design is:

1. Cache expensive, shareable conditions and hazard computation under a conditions key.
2. Rebuild the request-specific MissionView and Markdown for every response.
3. Never share presentation objects containing user-supplied metadata between requests.

Also decide whether party_size and route_note are genuinely required. Remove them if they are unused.

#### Acceptance test

- Requests differing only in name, party size, or route note never return each other's metadata.
- A conditions-cache hit still produces presentation from the current request.
- A regression test replaces the current “name is not part of identity” assertion.

### SA-05 — Mutable CDN JavaScript runs without integrity or CSP protection

- **Severity:** High
- **Category:** Third-party script supply chain
- **Affected components:** frontend/index.html, deploy/nginx/upstreamwx.conf

#### Evidence

- MapLibre JavaScript and CSS are loaded from jsDelivr using the floating major specifier maplibre-gl@5 (index.html:16, 231).
- maplibre-contour is loaded from jsDelivr at 0.1.0 (index.html:233).
- None of these resources has an integrity hash.
- The nginx configuration does not send a Content-Security-Policy header.
- Application-origin scripts can read persisted mission and briefing data from localStorage, request geolocation through the application's UI, call same-origin APIs, and change the displayed assessment.

#### Impact

A compromised CDN path, package publication, or mutable major-version resolution can execute attacker-controlled code in the UpstreamWX origin. This could disclose precise mission data, manipulate safety-relevant output, or invoke billable endpoints. A Git tag does not freeze a floating CDN dependency.

#### Recommendation

Vendor reviewed, exact JavaScript and CSS assets into the release and serve them from the same origin. If a CDN must remain, use exact immutable versions plus Subresource Integrity and crossorigin attributes.

Add a Content Security Policy. Begin with Report-Only, remove inline script/style requirements, then enforce a policy based on default-src 'self' with narrowly enumerated connect-src, img-src, worker-src, style-src, and font-src values. Avoid unsafe-inline and unsafe-eval where possible.

The [W3C Subresource Integrity recommendation](https://www.w3.org/TR/sri/) describes hash verification for externally fetched resources. The [Content Security Policy Level 3 specification](https://www.w3.org/TR/CSP3/) defines the browser policy mechanism.

#### Acceptance test

- A release works with third-party CDN access blocked.
- Every unavoidable external executable resource has an exact version and verified integrity hash.
- Enforced CSP produces no unexpected violations in the complete PWA, map, service-worker, PDF, and offline flows.

### SA-06 — Deployment is not reproducible and crosses the root trust boundary

- **Severity:** High
- **Category:** Software supply chain / privilege boundary
- **Affected components:** pyproject.toml, deploy/bootstrap.sh, deploy/deploy.sh, deploy/README.md

#### Evidence

- Nearly all production Python dependencies are unbounded in pyproject.toml, and no uv.lock or equivalent production lockfile is committed.
- deploy.sh runs uv pip install -e against the mutable checkout and existing virtual environment (deploy.sh:55-57). It does not perform an exact frozen sync or remove undeclared packages.
- Browser binaries and operating-system dependencies are selected and installed at deploy time (deploy.sh:71-92).
- bootstrap.sh downloads and executes the current uv installer as root through curl | sh (bootstrap.sh:90).
- /opt/upstreamwx, including the checkout and virtual environment, is owned by the application service user (deploy/README.md:28; bootstrap.sh:104, 111-113).
- deploy.sh invokes .venv/bin/playwright install-deps chromium as root (deploy.sh:89). That executable is created in the service-user-owned environment from dependencies selected during deployment.
- Rollback checks out an earlier source ref but does not restore the dependency or browser set used by that release.

#### Impact

Two deployments of the same tag can contain different Python packages, native wheels, browser revisions, and system dependencies. A rollback is therefore not a rollback of the executable system.

More seriously, the root deployment step trusts an executable from a service-user-owned, dependency-populated virtual environment. A compromised package, poisoned install, or modification available to that account can turn a routine deploy into root code execution. The systemd sandbox reduces what the running service can modify, but it does not make this a sound deployment trust boundary.

#### Recommendation

- Commit uv.lock and generate it for the supported Python/platform policy.
- Deploy with an exact frozen sync, such as uv sync --frozen --no-dev, into a fresh versioned release directory.
- Make the release checkout, deployment scripts, lockfile, and virtual environment root-owned and non-writable by the runtime account.
- Never execute virtual-environment console scripts as root. Install OS packages from a reviewed, root-owned static manifest or prebuilt image.
- Pin uv installation by version and verify a published checksum or signature instead of piping an unpinned installer into a root shell.
- Pin and pre-stage the Playwright/Chromium revision; include it in the release inventory.
- Use atomic release directories and a symlink switch so rollback restores source, dependencies, browser, and configuration together.
- Produce an SBOM and retain the resolved package/browser inventory with every tag.

The [uv project documentation](https://docs.astral.sh/uv/concepts/projects/sync/) describes exact synchronization and frozen lock use; its [project layout documentation](https://docs.astral.sh/uv/concepts/projects/layout/) recommends checking the lockfile into version control.

#### Acceptance test

- Two clean deployments of the same tag produce the same locked Python and browser inventory.
- Rollback restores that inventory, not only Git source.
- No root process executes a file writable by the runtime service account.

## Medium-severity findings

### SA-07 — Release provenance and CI supply-chain controls are incomplete

- **Severity:** Medium
- **Affected components:** .github/workflows, deploy/deploy.sh, Git tags

The inspected v0.4.0 through v0.6.0 tag references are lightweight tags that point directly to commits rather than signed annotated tag objects. deploy.sh accepts a branch or tag ref and checks it out without verifying a tag signature, commit signature, allowed signer, or expected immutable commit.

GitHub Actions use actions/checkout@v4 and astral-sh/setup-uv@v5 rather than full commit SHA pins. CI installs the current dependency resolution and runs Ruff and pytest, but it has no dependency-vulnerability gate, secret scan, SAST/CodeQL, SBOM, artifact attestation, or deploy-time provenance verification. The package version (0.5.0) and FastAPI metadata version (0.3) are also independent from the deployed Git description.

Pin third-party actions to reviewed full-length SHAs. GitHub states that a full-length commit SHA is the only immutable action reference in its [secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use). Create and verify signed annotated release tags, restrict acceptable signers, and require deployment by exact commit. Add lock validation, dependency audit, secret scanning, SAST, SBOM generation, and an offline test pass on the exact release commit. Consider GitHub's [immutable release controls](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases) where available.

### SA-08 — Chromium runs without its native sandbox and accepts structurally broad input

- **Severity:** Medium
- **Affected components:** src/upstreamwx/sitrep/pdf.py, api/app.py, api/models.py, deploy/systemd/upstreamwx-api.service

The PDF renderer explicitly launches Chromium with --no-sandbox because RestrictNamespaces=true blocks its renderer sandbox (pdf.py:134-141). Client-supplied BriefingResponse JSON is rendered in that browser. The implementation has good controls: a 2 MiB body threshold, a two-render semaphore, rate limiting, HTML escaping, a local template allowlist, and request interception that aborts all other browser requests.

Residual issues remain:

- A Chromium renderer vulnerability has no native browser sandbox containment.
- Several BriefingResponse fields are broad strings, lists, or dictionaries without list cardinality and nested-string limits (models.py:330, 342, 360-365, 373).
- For chunked requests, app.py calls await request.body() before enforcing the actual-byte limit (app.py:364-371). nginx limits the deployed edge, but the standalone server does not stream-reject.

Restore the browser sandbox if the systemd model can support it, or move rendering into a separately contained service/container with no network, a read-only filesystem, minimal readable paths, resource quotas, and no secrets. Consider a non-browser PDF renderer. Disable JavaScript for the template if feasible. Add strict bounds to all nested response fields and enforce a streaming body limit.

Chromium's [sandbox design documentation](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/docs/design/sandbox.md) explains why renderer isolation limits exploit severity. Playwright's [BrowserType documentation](https://playwright.dev/python/docs/api/class-browsertype) documents its sandbox and executable-path controls.

### SA-09 — Repository deployment can succeed without enforced TLS or host validation

- **Severity:** Medium
- **Affected components:** deploy/nginx/upstreamwx.conf, deploy/bootstrap.sh, src/upstreamwx/api/app.py

The versioned nginx template listens on port 80 only (upstreamwx.conf:29-32). The comments expect Certbot to rewrite the configuration later. HSTS sent over HTTP is ignored by browsers. Bootstrap/deploy can complete without proving that HTTPS exists or redirects correctly.

FastAPI does not install TrustedHostMiddleware or HTTPSRedirectMiddleware. nginx's server_name gives some edge protection, but direct use of the standalone entry point bypasses it.

Automate TLS as a required deployment stage, keep the HTTPS and redirect configuration under version control, and fail deployment if an external HTTPS health check or certificate validation fails. Reject unknown hosts with a default nginx server and configure TrustedHostMiddleware. If a CDN or load balancer is added, explicitly define trusted proxy and real-IP behavior.

### SA-10 — Privacy claims and data handling do not match

- **Severity:** Medium
- **Affected components:** frontend/js/app.js, landing/index.html, api/service.py, UpstreamWX-PRD-v0.8.md

The product says it “asks for no personal data,” and NFR-7 says missions are stored client-side by default. In practice:

- Mission name, exact point, activity, and time window are persisted in localStorage.
- The backend retains mission data in caches and the active refresh registry.
- A mission name can contain a person's name because the API does not constrain its semantics.
- Exact or near-exact location/time queries are sent to weather and watershed providers.
- Geocoder queries and map/tile access go to third parties.
- Third-party CDN scripts execute with access to application-origin localStorage.

This does not necessarily violate a legal rule, but it is a material transparency and field-safety risk. Mission names and coordinates can reveal sensitive expedition plans.

Replace the absolute “no personal data” statement with an accurate data-flow disclosure. Document each data category, recipient, purpose, and retention period. Minimize stored fields, add a visible “clear mission and offline briefing” action, apply server-side TTLs, and remove unused party/route fields. Avoid third-party executable JavaScript. Confirm with counsel which privacy notice and consent obligations apply in beta jurisdictions.

### SA-11 — Upstream responses and redirects lack explicit size/domain bounds

- **Severity:** Medium
- **Affected components:** src/upstreamwx/grib/idx.py and provider clients

Outbound requests generally use fixed HTTPS endpoints and timeouts, which limits classic user-controlled SSRF. However, JSON clients load provider responses without explicit byte limits, and GRIB range downloads stream to disk without enforcing a cumulative expected-size cap or validating Content-Range. The requests library follows redirects by default, with no destination-domain allowlist.

A compromised or malfunctioning provider can therefore return oversized JSON, ignore Range and send a full file, redirect to an unexpected host, or fill the data volume. Validate 206 and Content-Range for range fetches, cap bytes before and during download, cap JSON response size before parsing, restrict redirect destinations to approved HTTPS hosts, verify content types, and apply filesystem quotas.

## Low-severity findings

### SA-12 — Diagnostic and direct-run surfaces expose unnecessary information

- **Severity:** Low
- **Affected components:** src/upstreamwx/api/app.py

FastAPI's default /docs, /redoc, and /openapi.json endpoints are enabled. /v1/health is unthrottled and discloses cache size, active mission count, release, cycle, concurrency, decode cache size, warm settings, and rate-limit state (app.py:229-261). This helps an attacker measure resource-exhaustion attempts.

The console main function binds 0.0.0.0:8000 (app.py:494). The production systemd unit correctly binds the configured loopback address, but direct execution exposes the application without nginx TLS, body limits, and edge throttling.

Expose only minimal liveness publicly; put readiness and operational details behind admin authentication or a private network. Disable API documentation in production or protect it with the beta gate. Default the standalone command to 127.0.0.1 and require an explicit flag for all-interface binding.

### SA-13 — Healthcheck failure logging can disclose the secret ping URL

- **Severity:** Low
- **Affected components:** src/upstreamwx/api/scheduler.py

On a ping failure, scheduler.py logs the full healthcheck target at DEBUG (line 46). Healthchecks-style URLs commonly contain the bearer secret in the path. DEBUG is not the expected production log level, but enabling it can place that credential in the private journal and support bundle.

Log only the provider name or a redacted URL. Rotate the ping token if it has ever appeared in collected debug logs.

## Positive controls observed

- The production systemd service runs as an unprivileged user and uses NoNewPrivileges, ProtectSystem=strict, ProtectHome, PrivateTmp, kernel/control-group protection, and namespace restrictions.
- The production unit binds uvicorn behind nginx rather than directly to the Internet.
- Request generation, PDF rendering, and watershed warming have concurrency or queue bounds.
- PDF rendering escapes dynamic fields, validates a response model, caps declared/actual body size, blocks browser network requests, sanitizes filenames, and avoids returning Playwright internals to clients.
- Frontend rendering consistently uses escaping, and inspected Markdown link handling permits only HTTP(S) links with noopener/noreferrer. No direct frontend or PDF script-injection sink was confirmed.
- The service worker does not cache POST briefing responses, and offline briefing state is labelled with mission/age checks.
- Outbound provider endpoints are fixed rather than directly user-selected; requests generally use HTTPS and timeouts.
- YAML parsing uses safe_load. No production eval, exec, pickle deserialization, shell=True, or obvious command-injection sink was found.
- Rate-limit client-IP extraction trusts X-Forwarded-For only from the loopback proxy path.
- Environment files and service logs have reasonably restrictive deployment permissions.
- GitHub Actions currently use minimal contents: read permission for CI.
- No credential matching the audit's AWS, GitHub, Anthropic, OpenAI, Stripe, private-key, or common assignment patterns was found in the tracked tree or scanned Git history.

## Verification performed

| Check | Result | Notes |
|---|---|---|
| Manual source/config review | Completed | API, frontend, PDF, storage, providers, CI, systemd, nginx, bootstrap, deploy, and rollback paths |
| Ruff | Passed | ruff check . reported “All checks passed” |
| Bandit static scan | Completed | No High findings; one Medium all-interface bind and four Low findings, including one constant-name false positive and three assert uses |
| Secret-pattern scan | Completed | No matching secret values found in tracked files or scanned history |
| Dependency resolution | Completed | Linux/Python 3.11 production graph resolved to 101 packages on 2026-07-14 |
| Dependency vulnerability scan | Passed with limitation | pip-audit using OSV reported zero known vulnerabilities in that resolved snapshot |
| Full pytest suite | Not completed | Clean-room dependency setup was stopped after excessive duration at the user's request |
| Focused API/PDF pytest subset | Not completed | First retry was blocked by a stale uv Playwright cache lock; a subsequent slow setup was stopped at the user's request |
| Live penetration test | Not performed | No deployed target was in scope |

The dependency result is not proof of the production host's contents. Without a lockfile and production inventory, a deployment may resolve a different set at any time.

## Follow-up on the 2026-07-02 review

The following earlier risks are materially improved in the current code:

- PDF input now passes through response models, template escaping, request routing denial, a body cap, rate limiting, and a render semaphore.
- Scheduler blocking work is moved through asyncio.to_thread, and shutdown has a timeout.
- Briefing/result caches, active missions, and warm queues now have count bounds.
- Cold briefing generation has a concurrency semaphore and busy response.
- Mission windows and several geographic inputs have server-side limits.

This audit's findings are the residual risks after those changes. In particular, entry-count limits do not bound retained bytes, the active-mission cap still permits substantial recurring work, and the cache still shares request-specific metadata.

## Required actions before tagging or promotion

### Release blockers

- [ ] Put the complete private-beta application behind a tested access-control layer.
- [ ] Bound all MissionSpec and HazardInputs values; strictly type inputs; enforce application request-byte limits.
- [ ] Add byte and TTL budgets to briefing/result caches.
- [ ] Prevent anonymous requests from creating long-lived scheduled refresh work; add scheduler budgets and synchronization.
- [ ] Separate cached conditions from request-specific presentation, or include all metadata in the cache key.
- [ ] Vendor/pin browser dependencies, add integrity where needed, and enforce an appropriate CSP.
- [ ] Commit a production lockfile and deploy an exact frozen environment.
- [ ] Remove every root execution of service-user-writable code, especially .venv/bin/playwright.
- [ ] Run the full offline pytest suite successfully on the exact release commit.

### Strongly recommended before production exposure

- [ ] Make TLS and HTTPS redirect verification a mandatory, versioned deployment gate.
- [ ] Restore Chromium sandboxing or isolate PDF rendering in a separate hardened boundary.
- [ ] Add nested PDF-schema limits and streaming body enforcement.
- [ ] Pin GitHub Actions by full SHA and verify signed release provenance during deployment.
- [ ] Add automated dependency, secret, and SAST checks plus an SBOM.
- [ ] Publish an accurate privacy/data-flow and retention disclosure.
- [ ] Cap and validate all upstream response bodies, range responses, redirects, and disk usage.

## Suggested remediation order

1. **Contain exposure:** add the beta access gate and verify port 8000 is unreachable externally.
2. **Remove direct application abuse paths:** fix input bounds, cache bytes/TTL, cache isolation, and active-refresh authorization/budgets.
3. **Fix release trust:** commit the lockfile, make artifacts root-owned, eliminate privileged execution of environment code, and verify release signatures.
4. **Freeze browser supply chain:** vendor MapLibre assets and deploy CSP.
5. **Harden remaining boundaries:** TLS gate, PDF isolation, upstream size controls, privacy disclosure, and diagnostic minimization.
6. **Verify:** run CI and targeted negative/load tests on the exact signed release commit, then deploy to staging and repeat from outside the trusted network.

## Final release decision

**Do not promote the current commit as an Internet-accessible release in its present form.**

For a genuinely access-restricted private beta, a verified external access gate can temporarily reduce the immediate likelihood of SA-01 through SA-05. SA-04 cache isolation and SA-06 deployment privilege/reproducibility should still be corrected before treating the tag as a trustworthy release artifact. Any deferred High finding should have a named owner, expiry date, monitoring control, and written risk acceptance.
