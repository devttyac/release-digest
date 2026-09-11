# Deploying to a server

This is a **one-shot job**, not a service — it wakes, fetches, emails, exits. There is no
inbound port, so it never needs to sit behind a reverse proxy.

Paths below use `/home/YOUR_USER/docker/release-digest`; adjust to taste.

## 1. Clone and configure

```bash
git clone https://github.com/devttyac/release-digest.git ~/docker/release-digest
cd ~/docker/release-digest
mkdir -p archive

cp .env.example .env
nano .env          # fill in the four values
chmod 600 .env
```

Create the `.env` on the server directly rather than copying one over — it keeps the
credentials out of shell history and out of any batch file transfer.

## 2. Build and smoke-test

```bash
docker build -t release-digest:latest .

# Builds the message and reports its size without sending anything.
docker run --rm --env-file .env -e DRY_RUN=1 release-digest:latest
```

A healthy dry run prints something like `Embedded 29 images (583683 bytes)` followed by
`DRY RUN — would send to ...`.

Then send one for real before scheduling it:

```bash
docker run --rm --env-file .env \
  -e ARCHIVE_DIR=/archive -v ~/docker/release-digest/archive:/archive \
  release-digest:latest
```

## 3. Schedule it

Pick one — don't install both.

### Option A — systemd timer (recommended)

Survives reboots, logs to journald, and `Persistent=true` catches up a run missed while
the machine was down.

```bash
sudo cp deploy/release-digest.service /etc/systemd/system/
sudo cp deploy/release-digest.timer   /etc/systemd/system/
sudo nano /etc/systemd/system/release-digest.service   # set YOUR_USER and TZ
sudo systemctl daemon-reload
sudo systemctl enable --now release-digest.timer
```

```bash
systemctl list-timers release-digest    # confirm next fire time
journalctl -u release-digest -n 50      # read the last run
sudo systemctl start release-digest     # fire one now, out of band
```

If your user isn't in the `docker` group, either add it
(`sudo usermod -aG docker YOUR_USER`, then re-login) or leave the units running as root,
which is how they're written.

### Option B — crontab

Simpler, but no catch-up for a missed run, and logging is whatever cron captures.

```cron
0 8 1 * * cd /home/YOUR_USER/docker/release-digest && /usr/bin/docker run --rm --env-file .env -e ARCHIVE_DIR=/archive -v /home/YOUR_USER/docker/release-digest/archive:/archive release-digest:latest >> cron.log 2>&1
```

## Knobs

Set as `-e` flags on `docker run`, or under `environment:` in `docker-compose.yml`:

| Variable | Effect |
|---|---|
| `DIGEST_MONTH` | Target a specific month (`2026-11`) instead of the current one |
| `ARCHIVE_DIR` | Write the rendered HTML + raw JSON there (mount a volume to keep them) |
| `DRY_RUN` | Build the message and report its size without sending |
| `TZ` | Container timezone; defaults to `Asia/Singapore` in the Dockerfile |

## Notes

- The image never contains `.env` — `.dockerignore` excludes it, and it is mounted at runtime.
- The container runs as a non-root user (`digest`, uid 10001).
- Archived HTML uses remote image URLs rather than the email's inline `cid:` references, so
  it renders correctly when opened in a browser.
