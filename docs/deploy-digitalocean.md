# Deploying on DigitalOcean

Practical notes for running the FOI system on a DigitalOcean droplet — primarily
to do the **heavy public-information crawl + reindex on a server** instead of a
laptop, and optionally to host the application itself.

> ## ⚠️ Information-governance note — read first
>
> There are **two very different workloads** here, with different risk:
>
> 1. **Building the knowledge base** (website crawl + reindex + the embedding
>    model). This touches **only public data** and produces a public-data index.
>    Running it on a DigitalOcean droplet is low-risk.
>
> 2. **Running the live application** (intake, casework, drafting). This holds
>    **FOI case data and requesters' personal data**. Hosting that on a
>    third-party cloud (DigitalOcean) makes DO a **data processor** — the same
>    UK GDPR / DPA / data-residency / procurement scrutiny that applies to a
>    cloud LLM applies here. Per the project's offline-first principle, get
>    **Information Governance sign-off** before putting live casework on DO, use
>    a **UK region (`LON1`)**, encrypted volumes, TLS, and backups.
>
> A clean split that avoids most of the risk: use a DO droplet as a **build box**
> for the public knowledge base, then copy the resulting `foi.db` (or just the
> `knowledge_docs` / `knowledge_chunks`) and the vendored model onto the
> council-controlled host that runs live casework.

---

## 1. Provision the droplet

- **Region:** `LON1` (London) for UK data residency.
- **Size:** the embedding model + crawl are CPU/RAM bound. A 2 vCPU / 4 GB
  droplet is a sensible minimum; 4 vCPU / 8 GB makes reindexing comfortable.
- **OS:** Ubuntu 24.04 LTS.

Create a non-root user and a basic firewall:

```bash
adduser foi && usermod -aG sudo foi
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
```

## 2. Install and set up the app

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv git nginx
sudo mkdir -p /opt/foi && sudo chown foi:foi /opt/foi
cd /opt/foi
git clone <your-repo-url> .            # or rsync the project up
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## 3. Vendor the embedding model (once, while online)

```bash
. .venv/bin/activate
FOI_RETRIEVAL_PROVIDER=semantic python -m app.fetch_model
# caches BAAI/bge-small-en-v1.5 into ./models/fastembed (~64 MB)
```

After this the model is local; set `FOI_EMBEDDING_OFFLINE=true` so the app never
attempts a download at runtime.

## 4. Configure `.env`

```ini
# /opt/foi/.env  (gitignored)
FOI_ENVIRONMENT=production
FOI_RETRIEVAL_PROVIDER=semantic
FOI_EMBEDDING_OFFLINE=true
FOI_SESSION_COOKIE_SECURE=true          # served over HTTPS
FOI_SEED_DEFAULT_USERS=false            # AFTER you create real accounts (below)
FOI_INGEST_CRAWL_MAX_PAGES=4200         # full sitemap

# NB: do NOT put FOI_INGEST_ENABLED here — the cron job sets it inline (below),
# keeping the running app offline-first.
```

First boot seeds the starter accounts (with `FOI_SEED_DEFAULT_USERS=true`); sign
in as `admin`, create the real accounts + department logins in the **Accounts**
panel, change every starter password, then set `FOI_SEED_DEFAULT_USERS=false` and
restart.

## 5. Run under systemd

`/etc/systemd/system/foi.service`:

```ini
[Unit]
Description=FOI Response System
After=network.target

[Service]
User=foi
WorkingDirectory=/opt/foi
EnvironmentFile=/opt/foi/.env
ExecStart=/opt/foi/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now foi
```

> Single instance is fine for a council deployment, and matches the design
> (the auto-refresh weekly trigger is an external cron, not in-process, so it is
> safe regardless of worker count). For a SQLite database keep it to **one
> Uvicorn worker**; move to Postgres (`FOI_DATABASE_URL=postgresql+psycopg://…`)
> before scaling out — that needs Alembic migrations rather than the dev-only
> column backfill in `app/database.py`.

## 6. TLS + reverse proxy (nginx + Let's Encrypt)

```bash
sudo apt install -y certbot python3-certbot-nginx
# minimal nginx server block proxying / -> http://127.0.0.1:8000, then:
sudo certbot --nginx -d foi.example.gov.uk
```

## 7. Weekly public-information crawl (the heavy job, off-hours)

This is the workload that's too slow for a laptop (~19s/page → 4,200 pages takes
several hours). Run it from cron at a quiet time. The command enables ingestion
**inline** so the running app keeps its offline-first default:

```cron
# crontab -e  (as the foi user) — 02:30 every Sunday
30 2 * * 0  cd /opt/foi && . .venv/bin/activate && \
  FOI_INGEST_ENABLED=true FOI_INGEST_WEBSITE=true \
  FOI_RETRIEVAL_PROVIDER=semantic FOI_EMBEDDING_OFFLINE=true \
  python -m app.refresh >> /opt/foi/logs/refresh.log 2>&1
```

`python -m app.refresh` crawls, re-ingests and rebuilds the semantic index in one
pass, records a `KnowledgeRefresh` row, and exits non-zero on error so a failed
run is visible in the log / any cron-mail.

## 8. Backups

- `foi.db` (or the Postgres database) — the case record and knowledge base.
- `models/fastembed/` only needs backing up if the box is air-gapped from
  HuggingFace; otherwise `python -m app.fetch_model` re-fetches it.
- Snapshot the droplet, or `restic`/`rsync` the `/opt/foi` data to council
  storage on a schedule.

## Build-box-only shortcut

If DO is only a crawl/build box (recommended until IG signs off on hosting live
casework): provision + steps 2–3, run the step-7 crawl command once by hand, then
copy `foi.db` and `models/fastembed/` to the council host. No nginx/TLS/systemd
needed, and no personal data ever lands on DigitalOcean.
