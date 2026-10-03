# Deployment: Linux VM, Docker, Nginx, Let's Encrypt

Target: one Ubuntu 24.04 LTS VM (2 vCPU / 4 GB RAM is plenty for one store). Everything runs in Docker Compose (`docker-compose.prod.yml`): PostgreSQL, Redis, a one-shot migration, the API (gunicorn + uvicorn workers), the Celery worker and Celery beat. Nginx on the host terminates TLS.

## 1. Server preparation

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install nginx certbot python3-certbot-nginx ufw git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER        # log out and back in

sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

Point a DNS `A` record (e.g. `logistics.yourdomain.com`) at the VM.

## 2. Application

```bash
sudo mkdir -p /opt/trekiva-logistics && sudo chown $USER /opt/trekiva-logistics
git clone <repo> /opt/trekiva-logistics && cd /opt/trekiva-logistics
cp .env.example .env && chmod 600 .env
```

Edit `.env` for production:

| Variable | Value |
|---|---|
| `APP_ENV` | `production` (disables API docs and mock carriers) |
| `APP_URL` | `https://logistics.yourdomain.com` |
| `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` | from the Dev Dashboard |
| `SHOPIFY_SHOP_ALLOWLIST` | `your-store.myshopify.com` |
| `ENCRYPTION_KEYS` | a freshly generated Fernet key. **Back it up separately**: losing it makes stored tokens and credentials unreadable. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | a strong password |
| `LOG_JSON` | `true` |
| `ALERT_SLACK_WEBHOOK_URL` and/or `ALERT_EMAIL_TO` + `SMTP_*` | at least one alert channel ([operations.md](operations.md#2-alerts-setup)) |
| `SENTRY_DSN`, `METRICS_TOKEN` | optional ([operations.md](operations.md#4-sentry-optional)) |

Start the stack:

```bash
docker compose -f docker-compose.prod.yml --env-file .env up -d --build
docker compose -f docker-compose.prod.yml ps
curl -s localhost:8000/readyz
```

Only the API is published, and only on `127.0.0.1:8000`. PostgreSQL and Redis are not exposed.

Start on boot:

```bash
sudo cp deploy/systemd/trekiva-logistics.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable trekiva-logistics
```

## 3. Nginx and HTTPS

```bash
sudo mkdir -p /var/www/certbot
sudo cp deploy/nginx/trekiva-logistics.conf /etc/nginx/sites-available/
sudo sed -i 's/logistics.example.com/logistics.yourdomain.com/g' /etc/nginx/sites-available/trekiva-logistics.conf
sudo ln -s /etc/nginx/sites-available/trekiva-logistics.conf /etc/nginx/sites-enabled/
```

The config references the certificate files, so get the certificate first. Temporarily comment out the `443` server block, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d logistics.yourdomain.com
# then uncomment the 443 block
sudo nginx -t && sudo systemctl reload nginx
```

Certbot installs a renewal timer. Check it with `sudo certbot renew --dry-run`, and add a deploy hook so Nginx reloads after renewal:

```bash
echo -e '#!/bin/sh\nsystemctl reload nginx' | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```

Do **not** add `X-Frame-Options`, because the app is embedded in Shopify admin. The backend sends a `frame-ancestors` CSP limited to your shop and `admin.shopify.com`.

Then finish [shopify-setup.md](shopify-setup.md) (deploy the toml, install the app).

## 4. Backups

```bash
sudo mkdir -p /var/backups/trekiva && sudo chown $USER /var/backups/trekiva
crontab -e
# 15 2 * * * /opt/trekiva-logistics/deploy/backup.sh >> /var/log/trekiva-backup.log 2>&1
```

Copy `/var/backups/trekiva` off the VM (object storage, rsync). To restore:

```bash
cat trekiva-<stamp>.dump | docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists
```

Test a restore before going live.

## 5. Upgrades

```bash
cd /opt/trekiva-logistics
git pull
docker compose -f docker-compose.prod.yml --env-file .env up -d --build   # migrate runs first
docker compose -f docker-compose.prod.yml logs -f --tail=100 api worker
```

Celery tasks are safe to run twice and webhooks are stored before processing, so a restart loses no work. Unprocessed events are re-enqueued by the sweeper.

## 6. Operations

- Logs: `docker compose -f docker-compose.prod.yml logs -f api worker beat` (JSON lines).
- Health: `/healthz`, `/readyz` and `/healthz/worker`. Point an external uptime monitor at both `https://<domain>/readyz` and `https://<domain>/healthz/worker`.
- Alerts: after configuring a channel, run `docker compose -f docker-compose.prod.yml run --rm api python -m app.alerts test`.
- Webhook backlog (SQL): `SELECT processing_status, count(*) FROM webhook_events GROUP BY 1;`
- Key rotation: put the new key first in `ENCRYPTION_KEYS`, keep the old key after it, and restart. Old ciphertexts still decrypt.
- Alerts, monitoring, procedures and routine checks: [operations.md](operations.md).
