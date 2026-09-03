# Deploying

One box, one developer, no CI. Source is rsynced over SSH and built on the
server; a container registry would buy nothing here and cost a pipeline to
maintain.

```
Internet ──▶ Caddy :443  (TLS, the only exposed port)
                ├─ /api/*, /healthz, /readyz ──▶ api:8000
                └─ everything else            ──▶ web:3000
                                                   api ──▶ db:5432, redis:6379
```

Path routing rather than chaining through Next: the browser sees one origin, so
the session cookie stays first-party, while API and media responses skip the
Node hop entirely.

## 0. Before you start

| Requirement | Value |
|---|---|
| Box | 4 vCPU / 8 GB RAM / 50 GB+ disk, Ubuntu 24.04 |
| DNS | `A` record for `SITE_ADDRESS` pointing at the box, **already propagated** |
| Access | SSH as root or a sudo user |

The DNS record must resolve *before* the first deploy. Caddy requests a
certificate on startup; if the record is missing it backs off, and Let's
Encrypt rate-limits repeated failures.

The render worker is CPU-bound, so vCPU count matters more than RAM. 8 GB is
sized for ffmpeg working alongside Postgres, with swap as an OOM safety net —
not as working memory.

## 1. Prepare the box (once)

```bash
make deploy-bootstrap DEPLOY_HOST=root@your-host
```

Installs Docker and the Compose plugin, restricts the firewall to SSH/HTTP/HTTPS,
adds 4 GB of swap, enables unattended security upgrades, creates
`/opt/hbday-zee`.

## 2. Put secrets on the server (once)

Secrets live only on the box; they are never committed and never rsynced —
`deploy.sh` explicitly excludes `.env.prod`.

```bash
scp .env.prod.example root@your-host:/opt/hbday-zee/.env.prod
ssh root@your-host 'nano /opt/hbday-zee/.env.prod'
```

Fill in every value. Generate the secrets rather than inventing them:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # SESSION_SECRET
python3 -c "import secrets; print(secrets.token_urlsafe(12))"   # APP_PASSPHRASE
```

Use a **different** passphrase from your local `.env`. The API refuses to start
in `prod` with default secrets or with `SESSION_COOKIE_SECURE=false`
(`Settings.assert_production_safe`).

## 3. Deploy

```bash
make deploy DEPLOY_HOST=root@your-host
```

Rsyncs the tree, builds the images, runs `alembic upgrade head` before the
server accepts traffic, then polls `/readyz` until the database and ffmpeg both
report healthy. It fails loudly rather than leaving a half-started stack.

Redeploying is the same command.

## 4. Turn on backups (once)

```bash
ssh root@your-host \
  'crontab -l 2>/dev/null; echo "17 3 * * * /opt/hbday-zee/deploy/backup.sh"' \
  | ssh root@your-host crontab -
```

Nightly `pg_dump` to `/opt/hbday-zee/backups`, 14-day retention, and it deletes
its own output if the dump came back empty — a zero-byte file that looks like a
backup is worse than no backup.

By the end this database holds hours of curation: approved stills, chosen clips,
recorded narration. Money does not re-create those identically.

**Copy dumps off the box periodically.** A backup that only exists on the
machine it protects is not a backup:

```bash
rsync -az root@your-host:/opt/hbday-zee/backups/ ./backups/
```

## Operating it

```bash
make deploy-ps      DEPLOY_HOST=root@your-host
make deploy-logs    DEPLOY_HOST=root@your-host s=api
make deploy-backup  DEPLOY_HOST=root@your-host
```

Restore a dump:

```bash
ssh root@your-host
cd /opt/hbday-zee
docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml \
  exec -T db pg_restore -U hbz -d hbday_zee --clean --if-exists < backups/FILE.dump
```

## Rehearse locally first

The compose stack runs identically on your laptop, and catches image, ffmpeg
and migration problems before a server is involved:

```bash
make stack-up
curl localhost:8000/readyz
make stack-down
```

## Known gaps

- **Blob storage is a Docker volume**, not R2. Fine to start; the storage port
  makes switching a config change. Media is included in no backup — only the
  database is dumped.
- **No zero-downtime deploy.** `up -d --build` restarts containers; a render in
  flight is lost. Acceptable for one user, and it is why renders are resumable
  from cached intermediates.
- **Workers are declared but idle** until M3 brings arq. They will start,
  find no queue implementation, and exit — expected before that milestone.
