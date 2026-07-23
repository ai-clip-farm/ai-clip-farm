# Production Deployment Guide

This covers taking AI Clip Farm from "runs on my laptop" to "runs every day,
unattended, on a server." Read [SECURITY_CHECKLIST.md](SECURITY_CHECKLIST.md)
alongside this before exposing the stack to the public internet.

---

## 0. Before any production deploy

1. **Generate real secrets** — never reuse `.env.example` defaults:
   ```bash
   python -c "from app.core.config import generate_api_key; print(generate_api_key())"   # API_KEY
   openssl rand -hex 24   # POSTGRES_PASSWORD, FLOWER_PASSWORD, N8N_ENCRYPTION_KEY
   ```
2. **Set `ENVIRONMENT=production`** in `.env`. The app refuses to start in
   this mode without `ANTHROPIC_API_KEY`, `API_KEY`, and non-wildcard
   `CORS_ORIGINS` (see `app/core/config.py` → `_validate_production_invariants`)
   — a fast, loud failure instead of an insecure silent default.
3. **Set `BIND_ADDR=127.0.0.1`** so the API/worker-metrics/Flower ports are
   only reachable from the host itself; nginx (below) is the only public
   entry point.
4. **DNS**: point your domain's A record at the server before requesting a
   TLS certificate (certbot needs it resolvable).

---

## 1. Local machine (single box, any OS with Docker)

```bash
git clone <repo> && cd ai-clip-farm
cp .env.example .env    # fill in secrets; ENVIRONMENT can stay "development"
docker compose up -d --build
curl http://localhost:8000/health
```

This is the dev/staging path — see [INSTALL.md](INSTALL.md) for the full
walkthrough (GPU setup, model sizing, troubleshooting). Use section 2 below
once you're ready to run continuously on a server.

---

## 2. Ubuntu Server (bare metal / any VPS)

Works identically on **DigitalOcean Droplets**, **Hetzner Cloud/dedicated**,
and **AWS EC2** — the only difference between those three and a bare Ubuntu
box is *how you provision the VM*, covered in sections 3-5. This section is
the common Docker/TLS/systemd setup all of them share.

### 2.1 Provision

- Ubuntu 22.04 or 24.04 LTS.
- Minimum: 4 vCPU / 8 GB RAM / 60 GB disk for `WHISPER_MODEL=small` at low
  volume. For sustained "hundreds of videos/day" at `large-v3`: 8+ vCPU /
  32 GB RAM / 200+ GB disk (NVMe), or a GPU instance — see
  [PERFORMANCE.md](PERFORMANCE.md) for sizing guidance.
- Open only ports 22 (SSH), 80, 443 in the cloud firewall / security group.
  Everything else (Postgres, Redis, worker metrics, Flower) stays behind
  `BIND_ADDR=127.0.0.1` and is never reachable from outside the host.

### 2.2 Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker
docker compose version   # confirm Compose V2 (required — see note below)
```

> **Compose V2 required.** `deploy.resources.limits` (CPU/memory caps in
> `docker-compose.yml`) only take effect under the modern `docker compose`
> plugin, not the legacy standalone `docker-compose` v1 binary. `get.docker.com`
> installs the plugin by default on current Ubuntu releases.

### 2.3 Clone and configure

```bash
sudo mkdir -p /opt/clipfarm && sudo chown "$USER" /opt/clipfarm
git clone <repo> /opt/clipfarm && cd /opt/clipfarm
cp .env.example .env
nano .env   # set ENVIRONMENT=production, real secrets, BIND_ADDR=127.0.0.1
```

**Fix data-volume ownership.** The app runs as non-root `appuser` (uid 1000)
inside the container (see `Dockerfile`). If `./data` is bind-mounted from
the host, the host directory needs matching ownership or the container gets
permission-denied errors writing to it:

```bash
mkdir -p data/{input,work,output}
sudo chown -R 1000:1000 data
```

### 2.4 Configure nginx + TLS

```bash
mkdir -p nginx/conf.d
cp nginx/conf.d/clipfarm.conf.example nginx/conf.d/clipfarm.conf
sed -i 's/CHANGE_ME_DOMAIN/clips.yourdomain.com/g' nginx/conf.d/clipfarm.conf
```

Issue the initial certificate (one-time — after this, the `certbot` service
in `docker-compose.prod.yml` auto-renews twice daily):

```bash
# Start nginx first so it can serve the ACME HTTP-01 challenge
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d nginx

docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm certbot \
  certonly --webroot -w /var/www/certbot \
  -d clips.yourdomain.com --email you@example.com --agree-tos --no-eff-email
```

### 2.5 Apply database migrations, then start everything

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml build

# Migrations run once, before the API starts — in production, alembic is
# the *only* schema authority (app/main.py disables the dev-mode
# create_all() when ENVIRONMENT=production; see app/main.py's lifespan).
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm api \
  alembic upgrade head

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose ps   # confirm every service is "healthy", not just "running"
```

Verify:

```bash
curl https://clips.yourdomain.com/health/ready
```

### 2.6 Survive reboots

`restart: always` (set throughout `docker-compose.prod.yml`) plus enabling
the Docker daemon itself is normally sufficient:

```bash
sudo systemctl enable docker
```

If you'd rather manage the stack via systemd instead of relying on Docker's
own restart policy (useful for centralized log/status via `journalctl`):

```ini
# /etc/systemd/system/clipfarm.service
[Unit]
Description=AI Clip Farm
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/clipfarm
ExecStart=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.prod.yml down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now clipfarm.service
```

### 2.7 Backups

The only state that matters is Postgres (job/clip metadata) and, if you
value them, already-rendered clips in `data/output/`:

```bash
# Nightly Postgres dump (add to cron)
docker compose exec -T postgres pg_dump -U clipfarm clipfarm | gzip > \
  /opt/clipfarm-backups/db-$(date +%F).sql.gz

# Rendered clips — rsync/rclone to off-box storage on whatever schedule
# matches how disposable you consider them (they can always be re-rendered
# from the source, at the cost of a Claude + Whisper pass).
```

---

## 3. DigitalOcean

1. Create a Droplet: **Ubuntu 24.04**, at least the `4 vCPU / 8 GB` plan
   (General Purpose or CPU-Optimized depending on Whisper model size —
   see [PERFORMANCE.md](PERFORMANCE.md)). For GPU transcription, use a
   **GPU Droplet** (NVIDIA H100/L40S) — DigitalOcean's GPU images ship with
   drivers preinstalled.
2. Enable **DigitalOcean Firewalls**: allow inbound 22/80/443 only.
3. Point a **DigitalOcean-managed domain** (or your registrar) at the
   Droplet's IP.
4. Follow section 2 above verbatim from "Install Docker" onward.
5. Optional: use **DigitalOcean Spaces** (S3-compatible) for off-box backup
   of `data/output/` via `rclone`.

---

## 4. Hetzner (Cloud or dedicated)

1. Create a **CPX41** or larger Cloud server (Ubuntu 24.04), or a dedicated
   **AX** server for sustained high-volume batch processing — Hetzner's
   dedicated line is notably cost-effective per vCPU/RAM for exactly this
   "CPU-bound Whisper + ffmpeg" workload profile.
2. Hetzner Cloud Firewalls (or `ufw` on dedicated): allow 22/80/443 only.
3. GPU: Hetzner Cloud offers dedicated GPU server types (e.g. with an
   RTX 4000 Ada) in select regions — set `WHISPER_DEVICE=cuda`,
   `FFMPEG_HWACCEL=nvenc` (or leave both at their `auto` defaults, which
   detect the GPU correctly either way).
4. Follow section 2 verbatim.
5. Hetzner Storage Boxes (SFTP/CIFS) are a low-cost off-box backup target.

---

## 5. AWS

### Option A — EC2 (closest to sections 2-4)

1. Launch an EC2 instance: `m6i.xlarge`+ for CPU-only, or a `g5.xlarge`
   (NVIDIA A10G) for GPU-accelerated transcription/encoding.
2. Security Group: inbound 22/80/443 only from the internet; everything
   else stays on the instance's private interfaces.
3. Attach an **EBS volume** for `/opt/clipfarm/data` sized for your
   retention window (`WORK_DIR_RETENTION_HOURS` × daily volume +
   `data/output` growth — see PERFORMANCE.md for per-video disk estimates).
4. Follow section 2 verbatim once the instance is up.
5. Use an **S3 bucket + lifecycle policy** for `data/output/` archival
   (`aws s3 sync` on a cron, or mount via `s3fs`/`mountpoint-s3` if you
   prefer a filesystem view).

### Option B — ECS / Fargate (more "cloud-native," more setup)

Push the built image (`docker-publish.yml` already publishes to
`ghcr.io/<org>/ai-clip-farm`) to ECR or reference the GHCR image directly,
then define:

- One ECS **service** for `api` (Fargate, behind an **Application Load
  Balancer** terminating TLS via **ACM** — replaces the nginx/certbot
  setup in sections 2-4 entirely).
- One ECS **service** for `worker` (EC2 launch type if you need GPU
  instances; Fargate doesn't support GPUs as of this writing).
- **RDS Postgres** and **ElastiCache Redis** instead of the `postgres`/
  `redis` containers.
- **EFS** mounted at `/data` so `work`/`output` are shared across worker
  tasks (a bind-mounted EBS volume doesn't work across multiple Fargate
  tasks the way EFS does).
- Task-level health checks pointing at `/health` (API) and `celery inspect
  ping` (worker) — same commands as the Docker healthchecks in
  `docker-compose.yml`, just wired through ECS's health-check block instead.

This path trades the simplicity of sections 2-4 for AWS-native scaling,
managed DB/cache failover, and ALB-based TLS — worth it once you're running
enough volume to want autoscaling groups per queue (see PERFORMANCE.md
§ Scaling to hundreds of videos/day).

---

## 6. Windows

Two supported paths:

### 6.1 Docker Desktop (recommended — identical to every other platform)

1. Install **Docker Desktop for Windows** (WSL2 backend — required; the
   legacy Hyper-V backend does not support the GPU passthrough needed for
   `WHISPER_DEVICE=cuda`/`FFMPEG_HWACCEL=nvenc`).
2. Clone the repo inside the **WSL2 filesystem** (e.g. `\\wsl$\Ubuntu\home\you\clipfarm`),
   not on the Windows `C:\` drive — bind-mount I/O across the Windows/WSL2
   boundary is measurably slower for the many-small-file access pattern
   Whisper/ffmpeg produce, which matters at daily-batch volume.
3. From a WSL2 shell, follow section 1 (Local machine) verbatim —
   `docker compose up -d --build` behaves identically to Linux.
4. GPU: install the NVIDIA driver on Windows (not inside WSL2) plus the
   [NVIDIA CUDA on WSL](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)
   support — Docker Desktop then exposes `--gpus all` to containers
   automatically.

### 6.2 Native Windows (no Docker) — supported, not recommended for production

Running the API/worker directly as Windows services is possible but loses
the process isolation, resource limits, and healthchecks this guide relies
on elsewhere. If you need it (e.g. a locked-down environment that disallows
Docker):

1. Install Python 3.11, PostgreSQL, Redis for Windows (or Memurai, a
   Windows-native Redis-protocol server), and `ffmpeg` (add to `PATH`).
2. `pip install -r requirements.txt`.
3. Run the API and worker as NSSM-wrapped services (NSSM turns any console
   app into a proper Windows service with restart-on-crash):
   ```powershell
   nssm install ClipFarmAPI "C:\clipfarm\.venv\Scripts\uvicorn.exe" "app.main:app --host 0.0.0.0 --port 8000"
   nssm install ClipFarmWorker "C:\clipfarm\.venv\Scripts\celery.exe" "-A app.workers.celery_app.celery_app worker --loglevel=INFO --pool=solo"
   ```
   `--pool=solo` is required for Celery on Windows — the default `prefork`
   pool depends on `os.fork()`, which Windows doesn't have.
4. There is no native Windows nginx-equivalent covered by this guide; put
   IIS with URL Rewrite, or Caddy (`caddy reverse-proxy`), in front for TLS.

For anything beyond a single-operator local setup, prefer 6.1.

---

## 7. Post-deploy checklist

- [ ] `curl https://<domain>/health/ready` returns `{"status": "ready", ...}`
- [ ] Submit one real video end-to-end and confirm a clip renders
- [ ] `docker compose -f docker-compose.yml -f docker-compose.prod.yml ps`
      shows every service `healthy`
- [ ] Flower reachable at `https://<domain>/flower/` and prompts for the
      BasicAuth credentials you set (never unauthenticated — see
      SECURITY_CHECKLIST.md)
- [ ] `/metrics` (API) responds; worker metrics reachable at `localhost:9100/metrics`
      *from the host itself* (not from outside — confirm with `curl` from another
      machine that it times out)
- [ ] A test API request without `X-API-Key` returns 401
- [ ] Postgres backup cron entry installed and one manual dry run completed
- [ ] Alerting: `SLACK_WEBHOOK_URL` set and a forced test failure actually
      posts a message (temporarily submit a garbage YouTube URL, confirm
      the Slack alert arrives, then verify `alembic`/DB state is unaffected)

## CI/CD note (branch protection)

`docker-publish.yml` runs independently of `ci.yml` on the same push (see
that workflow's header comment). To make CI failures actually block
merges/deploys: **Settings → Branches → Branch protection rule** for `main`
→ enable "Require status checks to pass before merging" → select the `lint`
and `test` jobs from `ci.yml`. This is a one-time GitHub repo setting, not
something expressible in the workflow YAML itself.
