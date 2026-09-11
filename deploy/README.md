# Deploying

No inbound port, so this never needs to sit behind a reverse proxy.

Pick one of two shapes:

- **Scheduler service** (below) — the container stays up and fires monthly. Simplest, and
  what Docker Compose / Dockge / Portainer expect.
- **One-shot under a host timer** — the container runs and exits, with systemd or cron
  deciding when. See [Option B](#option-b--systemd-timer-one-shot).

A prebuilt image is published to `ghcr.io/devttyac/release-digest:latest` and is publicly
pullable, so no registry login is needed.

---

## Option A — scheduler service (Compose / Dockge)

### 1. Create the state and archive directories

The container runs as **uid 10001**, so bind-mounted directories must be writable by that
uid. Docker creates missing bind-mount paths as `root`, which the container then can't
write — do this **before** first start:

```bash
sudo mkdir -p /srv/release-digest/{state,archive}
sudo chown -R 10001:10001 /srv/release-digest/{state,archive}
```

Skip it and the container refuses to start, saying so in the log. That refusal is
deliberate: the state file is what prevents a resend on every restart, so running without
it would be worse than not running at all.

### 2. Stack definition

```yaml
services:
  release-digest:
    image: ghcr.io/devttyac/release-digest:latest
    container_name: release-digest
    restart: unless-stopped
    env_file:
      - .env
    environment:
      TZ: Etc/UTC
      RUN_DAY: "1"
      RUN_HOUR: "8"
      RUN_MINUTE: "0"
      ARCHIVE_DIR: /archive
      # RUN_ON_START: "1"   # send once on startup to verify; remove afterwards
    volumes:
      - /srv/release-digest/state:/state
      - /srv/release-digest/archive:/archive
```

`state` must persist. Lose it and the scheduler forgets what it already sent.

### 3. Fill in `.env`

Copy [.env.example](../.env.example). **Docker's `env_file` is not dotenv** — it does not
strip quotes, so `KEY="value"` passes the quote marks through as part of the value. Write
values bare:

```
TMDB_API_KEY=0123456789abcdef0123456789abcdef
RAWG_API_KEY=fedcba9876543210fedcba9876543210
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=abcdefghijklmnop
DIGEST_RECIPIENT=you@example.com
```

No quotes, no spaces around `=`, no inline comments, no trailing whitespace. The TMDB
value is the **API Key (v3 auth)**, 32 hex characters — not the longer "API Read Access
Token", which is a different auth scheme and fails with `Invalid API key`.

### 4. Start and verify

```bash
docker compose up -d && docker logs -f release-digest
```

A healthy first start logs `scheduler up …` and then seeds its state **without sending**.
To prove the whole path immediately, set `RUN_ON_START: "1"`, recreate, and look for:

```
Wrote /tmp/release-digest-data.json — games=10 movies=10 tv=10
Sent to you@example.com — ~850000 bytes
```

Then remove `RUN_ON_START` and recreate again, or it sends on every start.

---

## Editing `.env` later — the one that bites

**A container reads `env_file` when it is created, not when it restarts.** Editing `.env`
and pressing Restart (Dockge, Portainer, `docker restart`) changes nothing — the old
values stay baked into the running container. It has to be recreated:

```bash
docker compose down && docker compose up -d
```

In Dockge that's the **Update** action, not **Restart**.

To check what the container actually holds, without printing the secret:

```bash
docker exec release-digest python -c "import os;k=os.environ.get('TMDB_API_KEY','');print('len',len(k))"
```

`len 32` is correct for a TMDB v3 key. Anything else means the container has a stale or
malformed value — a key short by even one character surfaces as
`Invalid API key: You must be granted a valid key`.

Also worth knowing: a stack manager's directory is often **not** where your volumes point.
Dockge keeps stacks in `/opt/stacks/<name>/`, and that is the `.env` it reads — editing a
copy elsewhere has no effect. To find the directory a running stack was created from:

```bash
docker inspect release-digest --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

---

## Option B — systemd timer (one-shot)

For running `once` mode on a host schedule instead. `Persistent=true` catches up a run
missed while the machine was down.

```bash
sudo cp deploy/release-digest.service /etc/systemd/system/
sudo cp deploy/release-digest.timer   /etc/systemd/system/
sudo nano /etc/systemd/system/release-digest.service   # adjust paths and TZ
sudo systemctl daemon-reload
sudo systemctl enable --now release-digest.timer
```

```bash
systemctl list-timers release-digest    # next fire time
journalctl -u release-digest -n 50      # last run
sudo systemctl start release-digest     # fire one now
```

A crontab alternative, with no catch-up for missed runs:

```cron
0 8 1 * * /usr/bin/docker run --rm --env-file /srv/release-digest/.env -e ARCHIVE_DIR=/archive -v /srv/release-digest/archive:/archive ghcr.io/devttyac/release-digest:latest once >> /srv/release-digest/cron.log 2>&1
```

Note the trailing `once`. Without it you get the scheduler, which never exits.

---

## Knobs

| Variable | Effect |
|---|---|
| `DIGEST_MONTH` | Target a specific month (`2026-11`) instead of the current one |
| `ARCHIVE_DIR` | Write the rendered HTML + raw JSON there (mount a volume to keep them) |
| `DRY_RUN` | Build the message and report its size without sending |
| `RUN_DAY` / `RUN_HOUR` / `RUN_MINUTE` | When the scheduler fires (default: 1st, 08:00) |
| `RUN_ON_START` | Send once at startup, for testing |
| `STATE_FILE` | Where the last-sent month is recorded (default `/state/last-run`) |
| `TZ` | Container timezone; the scheduler fires on local wall-clock time |

## Notes

- The image never contains `.env` — `.dockerignore` excludes it; it is mounted at runtime.
- The container runs as a non-root user (`digest`, uid 10001).
- Archived HTML uses remote image URLs rather than the email's inline `cid:` references, so
  it renders correctly in a browser.
- A failed archive write warns and still sends — the email is the product, the archive is not.
- Failure is only visible in the container log. If that matters, point a push-style monitor
  at a successful run.
