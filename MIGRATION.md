# Migrating Workspace Chat: Heroku → DigitalOcean droplet

Target: one small droplet running the app + Postgres + Caddy (auto-HTTPS) via Docker
Compose. Your existing users/messages are preserved by dumping Heroku Postgres and
restoring into the new database.

> Why a single droplet / one worker: the app keeps presence (`online_users`,
> `user_status`) in memory and Socket.IO has no Redis message queue, so it must run
> as **one instance / one worker**. That's fine here — just don't scale out without
> first adding Redis.

---

## 0. Prerequisites (GitHub Student Pack)
- **DigitalOcean**: redeem the Student Pack credit ($200/yr) on your DO account.
- **Namecheap**: claim the free `.me` domain (also in the pack).
- Locally: the Heroku CLI (you already deploy with it) and `git`.

## 1. Create the droplet
- DigitalOcean → Create → Droplet.
- Image: **Ubuntu 24.04 LTS**. Plan: **Basic / Regular, $6/mo (1 GB)** is enough.
- Add your **SSH key**. Create it, note the **public IPv4**.

## 2. Point the domain at it
In Namecheap (or move DNS to DO) create an **A record**:
`@`  (or a subdomain like `chat`)  →  the droplet's IPv4. Lower the TTL to ~5 min so
the later cutover is quick. Wait for it to resolve (`ping yourdomain.me`).

## 3. Install Docker on the droplet
```bash
ssh root@YOUR_DROPLET_IP
curl -fsSL https://get.docker.com | sh
docker compose version    # confirm the compose plugin is present
```

## 4. Get the code onto the droplet
```bash
git clone <your-repo-url> chat && cd chat
# (or: scp the project up if the repo isn't hosted anywhere)
```

## 5. Create the .env
```bash
cp .env.example .env
nano .env
```
Fill in `DOMAIN`, a strong `POSTGRES_PASSWORD`, a random `SECRET_KEY`, and copy the
rest from Heroku. To see your current Heroku values:
```bash
heroku config -a chat-app-wuduh
```
Port over: `CLOUDINARY_URL`, `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`,
`VAPID_CLAIM_EMAIL`, `GIPHY_API_KEY`, `ADMIN_USERNAMES`, and `SENTRY_DSN` if set.
(Leave `DATABASE_URL` unset — compose injects it.)

## 6. Migrate the data (keep existing users/messages)
On your **local machine** (has the Heroku CLI), capture and download a dump:
```bash
heroku pg:backups:capture -a chat-app-wuduh
curl -o heroku.dump "$(heroku pg:backups:url -a chat-app-wuduh)"
```
`heroku.dump` is a custom-format pg_dump. Copy it to the droplet:
```bash
scp heroku.dump root@YOUR_DROPLET_IP:/root/chat/
```
On the **droplet**, start just the database and restore into it:
```bash
docker compose up -d db
# wait ~5s for it to become healthy, then:
docker compose exec -T db pg_restore --no-owner --no-acl -U chat -d chat < heroku.dump
```
A few `role "..." does not exist` notices are normal (`--no-owner` ignores them).

## 7. Launch
```bash
docker compose up -d --build
docker compose logs -f web      # watch it boot; safe_auto_migrate() runs on startup
```
Visit `https://yourdomain.me`. Caddy fetches a TLS cert automatically on first hit
(give it ~30s). Check: login works, messages send (WebSocket), and — over HTTPS —
push notifications + "install app" (PWA) work.

## 8. Cut over & decommission
Once verified, you're already live on the new domain. When you're confident:
```bash
heroku ps:scale web=0 -a chat-app-wuduh    # stop the dynos (keep the app a while as backup)
```

---

## Redeploying after code changes
```bash
git pull && docker compose up -d --build
```
or use the helper: `./deploy.sh`

## Handy operations
```bash
docker compose ps                 # status
docker compose logs -f web        # app logs
docker compose restart web        # restart app only
# database backup:
docker compose exec -T db pg_dump -U chat -Fc chat > backup_$(date +%F).dump
```

## Notes / gotchas
- **One worker only** — see the box at the top.
- **HTTPS is required** for web push and the PWA; Caddy handles it automatically once
  DNS points at the droplet.
- **Firewall**: if you enable `ufw`, allow `OpenSSH`, `80`, and `443`.
- **psql version**: the restore runs inside the `postgres:17` container, so client and
  server versions always match regardless of your laptop's setup.
- This repo still has the Heroku `Procfile`; it's harmless and lets you keep Heroku as a
  fallback until you delete the app.
