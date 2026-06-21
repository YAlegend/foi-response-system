# Deploy to Google Cloud (L4 GPU) — full on-prem stack

Stands up the whole system on one GCP VM with an **NVIDIA L4** GPU: the FastAPI
app, a **local LLM** (Ollama + `qwen2.5:7b-instruct`) for drafting, semantic
retrieval, and Caddy for automatic HTTPS. **No case data leaves the box** — the
model runs locally; that is the data-sovereignty story for a government client.

Everything is in `docker-compose.yml`. You run ~6 commands.

> **Cost note:** an L4 VM (`g2-standard-4`) is roughly **$0.7–1.0/hour** on demand.
> For a demo, **stop the VM when idle** (you pay for the disk only) and start it
> when needed. Set a budget alert.

---

## 1. Create the GPU VM

Console → Compute Engine → **Create instance** (or the `gcloud` below):

- **Machine:** `g2-standard-4` (4 vCPU, 16 GB) with **1 × NVIDIA L4**
- **Boot disk:** Ubuntu 22.04 LTS, **60 GB** (model weights + image need room)
- **Firewall:** allow **HTTP** and **HTTPS**
- **Region:** a UK/EU region (e.g. `europe-west2` London) for data residency

```bash
gcloud compute instances create foi \
  --zone=europe-west2-a \
  --machine-type=g2-standard-4 \
  --accelerator=type=nvidia-l4,count=1 \
  --maintenance-policy=TERMINATE \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --boot-disk-size=60GB \
  --tags=http-server,https-server
```

SSH in: `gcloud compute ssh foi --zone=europe-west2-a`

---

## 2. Install GPU driver, Docker, NVIDIA Container Toolkit

```bash
# NVIDIA driver
sudo apt-get update && sudo apt-get install -y ubuntu-drivers-common
sudo ubuntu-drivers install
# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER       # then log out/in (or `newgrp docker`)
# NVIDIA Container Toolkit (lets containers use the GPU)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```

Verify the GPU is visible to Docker:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

---

## 3. Get the code and configure

```bash
git clone https://github.com/YAlegend/foi-response-system.git
cd foi-response-system
cp .env.production.example .env.production
nano .env.production
```

Set, at minimum:
- `SITE_ADDRESS` — your domain (e.g. `foi.example.org`) for HTTPS, or leave `:80`
  for an IP-only HTTP demo. If you use a domain, point its DNS **A record** at the
  VM's external IP first.
- `FOI_SESSION_COOKIE_SECURE=true` once you're on HTTPS.
- Leave the model as `qwen2.5:7b-instruct` (fits the L4's 24 GB).

The compose file force-wires the LLM (`ollama`), the SQLite data volume, and
offline embeddings — you don't set those by hand.

---

## 4. Launch

```bash
docker compose up -d --build
docker compose ps                 # api, ollama, caddy = running; ollama-pull exits 0
docker compose logs -f ollama-pull   # watch the model download (~4.7 GB, a few min)
```

The app is up immediately; until the model finishes pulling, drafting falls back
to template assembly, then automatically uses the local LLM once it's ready.

Load the demo data (Oxfordshire corpus, worked cases, scheme SLA spread):

```bash
docker compose run --rm api python -m app.seed
```

Open `https://<your-domain>` (or `http://<VM-IP>`), sign in as **admin / admin**.

---

## 5. Verify the local LLM is actually drafting

```bash
# model present?
docker compose exec ollama ollama list          # shows qwen2.5:7b-instruct
# a draft now goes through the local model (FOI_LLM_PROVIDER=ollama)
```
In the UI: open a case → **Retrieve & auto-draft**. The first GPU draft warms the
model (a few seconds); subsequent drafts are fast. If Ollama is ever unreachable,
drafting degrades to the template — it never hard-fails.

---

## 6. Optional: live ingestion & scheduled emails

- **Crawl Oxfordshire / WhatDoTheyKnow:** set `FOI_INGEST_ENABLED=true` and the
  relevant `FOI_INGEST_*` flags in `.env.production`, `docker compose up -d`, then
  use the admin **Knowledge base** panel. (The JS disclosure-log scraper needs
  Playwright — add `pip install playwright && playwright install chromium` to the
  Dockerfile if you want browser mode; the feed/WhatDoTheyKnow paths don't.)
- **Breach-trend alerts (hourly) + department digests (weekly):** set
  `FOI_NOTIFY_ENABLED` / `FOI_DIGEST_ENABLED`, `FOI_NOTIFY_PROVIDER=smtp`,
  recipients and SMTP creds, then add host cron:
  ```
  0 * * * *  cd /home/$USER/foi-response-system && docker compose exec -T api python -m app.notify
  0 8 * * 1  cd /home/$USER/foi-response-system && docker compose exec -T api python -m app.digest
  ```

---

## Operate

```bash
docker compose logs -f api                 # app logs
docker compose restart                     # bounce everything
git pull && docker compose up -d --build   # deploy an update
# back up the SQLite DB (holds case data) off the box:
docker compose exec -T api sh -c 'sqlite3 /app/data/foi.db ".backup /app/data/backup.db"' \
  && docker compose cp api:/app/data/backup.db ./foi-backup-$(date +%F).db
```

**Stop billing when idle:** `gcloud compute instances stop foi --zone=europe-west2-a`
(disk persists; `start` to resume). Destroy entirely with `instances delete`.

---

## Production hardening (before real case data)

- **Turn off starter accounts:** `FOI_SEED_DEFAULT_USERS=false` and create real
  accounts via the admin UI; change every default password.
- **HTTPS on:** real domain + `FOI_SESSION_COOKIE_SECURE=true`.
- **Restrict the firewall** to the council's IP ranges / VPN; consider GCP IAP.
- **Encrypt the disk** (GCP CMEK) and back the DB up off-box on a schedule.
- **Scale:** for many concurrent users, move `FOI_DATABASE_URL` to Cloud SQL
  (Postgres) and raise uvicorn workers; swap Ollama for vLLM for higher LLM
  throughput. The app code is unchanged — only env + the compose service differ.
