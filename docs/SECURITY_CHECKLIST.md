# Security Checklist

Findings from the production audit, what was fixed, and what remains an
operational responsibility (things code can't enforce for you — a strong
password, a firewall rule, a rotation schedule).

## Fixed in this pass

| # | Issue | Where | Fix |
|---|---|---|---|
| 1 | **Path traversal in file uploads** — `dest = settings.input_dir / file.filename` used the client-supplied filename unsanitized; a filename like `../../etc/cron.d/x` could write outside `INPUT_DIR`. | `app/api/routes.py` | `sanitize_filename()` (`app/core/validation.py`) strips all directory components and slugifies the stem before any filesystem write. |
| 2 | **Path traversal in local-source resolution** — `source_ref` for `local`/`upload` videos accepted arbitrary paths, including absolute ones, letting a caller point ingest at any file the worker process could read. | `app/pipeline/ingest.py` | `resolve_local_source()` resolves strictly inside `INPUT_DIR`, rejecting anything that escapes it. |
| 3 | **SSRF via YouTube URL** — `source_ref` for YouTube videos was passed to yt-dlp unvalidated; yt-dlp supports generic HTTP/HLS extraction, so a crafted URL could be used to probe internal services or cloud metadata endpoints (`http://169.254.169.254/...`). | `app/pipeline/ingest.py`, `app/core/validation.py` | `validate_youtube_url()` enforces an http(s) scheme and an allow-listed host (`ALLOWED_SOURCE_HOSTS`). |
| 4 | **No authentication on any API route** — anyone who could reach port 8000 could submit jobs (burning Claude budget and disk) or download any clip. | `app/api/routes.py`, `app/core/security.py` | `require_api_key` dependency on the whole router; disabled only when `API_KEY` is empty (dev), and the app **refuses to start in production** without one set. |
| 5 | **No rate limiting** — a single client could submit unbounded videos/uploads. | `app/core/security.py`, `app/api/routes.py` | slowapi + Redis-backed limiter; `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_UPLOAD_PER_HOUR`. |
| 6 | **Flower (Celery monitor) exposed with zero authentication** on a public host port — anyone could view queue contents or revoke/trigger tasks. | `docker-compose.yml` | BasicAuth required via `FLOWER_USER`/`FLOWER_PASSWORD` (compose fails to start without `FLOWER_PASSWORD` set — `${FLOWER_PASSWORD:?...}`). |
| 7 | **CORS wildcard (`allow_origins=["*"]`)** in a codebase that would eventually carry auth headers. | `app/main.py`, `app/core/config.py` | `CORS_ORIGINS` is explicit and configurable; production config validation rejects `*`. |
| 8 | **No file-size limits** on uploads or YouTube downloads — a large/malicious upload could exhaust disk. | `app/api/routes.py`, `app/pipeline/ingest.py` | `MAX_UPLOAD_SIZE_MB` enforced during streamed upload (not just after — the write loop aborts mid-stream once the cap is exceeded); yt-dlp's `max_filesize` option caps downloads. |
| 9 | **No corrupted/malicious file detection** — a truncated download or non-video upload would fail deep inside a 20-minute transcription run instead of immediately. | `app/core/validation.py` | `validate_media_file()` (ffprobe-based) runs right after every ingest. |
| 10 | **Docker container ran as root** — any future RCE in ffmpeg/OpenCV's media parsing would be a full container-root compromise. | `Dockerfile` | Non-root `appuser` (uid 1000); multi-stage build also drops compilers/build tools from the final image entirely. |
| 11 | **No secrets validation** — the app would start and run for a while with an empty `ANTHROPIC_API_KEY` before failing deep in a task. | `app/core/config.py` | Production mode fails fast at startup; development mode logs a loud warning instead. |
| 12 | **Stack traces / raw exceptions could leak to API responses** — no global exception handler. | `app/main.py` | Every exception path returns a clean, generic JSON body; full details go to server-side logs only. |
| 13 | **Output/thumbnail file paths served without re-validating they're inside `OUTPUT_DIR`** — defense-in-depth gap (the values come from our own DB, not directly from user input, but a future bug in path construction would have had no second line of defense). | `app/api/routes.py` | `_output_path_is_safe()` checks the resolved path is under `OUTPUT_DIR` before every `FileResponse`. |
| 14 | **ASS subtitle injection** — a transcribed word containing `{`, `}`, or `\` (ASS override-tag syntax) could corrupt caption styling or be interpreted as a format directive. | `app/pipeline/subtitles.py` | `_escape_ass_text()` escapes all three characters before they reach the `.ass` file. |

## Operational responsibilities (not enforceable by code)

- [ ] **Rotate `API_KEY`, `POSTGRES_PASSWORD`, `FLOWER_PASSWORD`, `N8N_ENCRYPTION_KEY`**
      on a schedule (quarterly minimum) and immediately if you suspect exposure
      (e.g. accidentally committed `.env`, screen-shared a terminal with it visible).
- [ ] **Never commit `.env`** — confirm `.gitignore` still excludes it before every
      commit that touches config; `git status` before `git add .` is the habit that
      catches this.
- [ ] **Firewall**: only 22 (SSH, ideally key-only + fail2ban), 80, 443 open to the
      internet. Everything else (`5432`, `6379`, `9100`, direct `8000`/`5555`) must
      stay bound to `127.0.0.1` in production (`BIND_ADDR=127.0.0.1` — see
      DEPLOYMENT.md).
- [ ] **TLS certificate renewal** — confirm the `certbot` container's auto-renewal is
      actually running (`docker compose logs certbot`), don't assume it silently works
      forever. Set a calendar reminder to check ~1 month before your first cert's
      90-day expiry as a manual backstop.
- [ ] **Dependency updates** — Dependabot is configured (`.github/dependabot.yml`) for
      pip/Docker/GitHub Actions; actually merge those PRs on a cadence rather than
      letting them accumulate. `pip-audit` or `safety check` in CI is a reasonable
      addition once the dependency set stabilizes.
- [ ] **Trivy image scan** runs on every `docker-publish.yml` build (advisory,
      non-blocking by default) — review its output periodically, and switch
      `exit-code: "0"` to `"1"` once you've triaged the baseline findings and want new
      CRITICAL/HIGH CVEs to actually fail the build.
- [ ] **Backup restoration drills** — a backup you've never restored from is a backup
      you don't actually have; test the `pg_dump`/restore cycle from DEPLOYMENT.md
      §2.7 at least once before you need it for real.
- [ ] **`ALLOWED_SOURCE_HOSTS`** — only extend this list with hosts you've deliberately
      decided to trust yt-dlp's extractor for; every addition is a new potential SSRF
      surface.
- [ ] **Least-privilege API keys**: the `ANTHROPIC_API_KEY` and any future third-party
      credentials should be scoped/organization keys with spend limits set on the
      provider side, not a personal key with unlimited billing exposure.
- [ ] **Audit `docker compose logs` for secrets** occasionally — `LOG_JSON=true`
      structured logs make it easy to grep, and it's worth confirming no request
      body/error path is accidentally logging an API key or upload content verbatim.

## Known limitations / accepted risk

- **Rate limiting fails open** (`swallow_errors=True` in `app/core/security.py`): if
  Redis is briefly unreachable, requests are allowed through unthrottled rather than
  blocked. This favors availability over strict enforcement — acceptable for a
  single-tenant, API-key-gated system; reconsider if you ever expose this to
  untrusted multi-tenant traffic.
- **Single shared API key**, not per-user credentials. Fine for "one team, one
  deployment"; if you need per-caller attribution or revocation without rotating
  everyone's access, that's a real auth system (OAuth/JWT + a users table) this
  codebase does not implement.
- **No WAF / DDoS protection** at the application layer — nginx + rate limiting
  handles application-level abuse, not a volumetric attack. Put Cloudflare or your
  cloud provider's equivalent in front if that's a real threat model for your deployment.
