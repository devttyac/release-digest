# release-digest

A monthly email digest of upcoming **games, movies, and TV** — sourced from
[RAWG](https://rawg.io/apidocs) and [TMDB](https://www.themoviedb.org/), ranked by each
API's own popularity signal, with posters embedded so they display without a
"load remote images" prompt.

Runs as a one-shot container. No server, no inbound port, no database.

## What it does

1. **Fetch** — pulls the target month's releases from RAWG (games) and TMDB (movies + TV).
2. **Rank** — sorts each category latest-first by release date, and flags the single
   highest-popularity title per category as **Top Pick**.
3. **Render** — builds an email-safe HTML digest.
4. **Send** — emails it over Gmail SMTP with every poster embedded as an inline attachment.

## Quick start

```bash
git clone https://github.com/devttyac/release-digest.git
cd release-digest
cp .env.example .env && nano .env     # four values; see below
chmod 600 .env

docker build -t release-digest:latest .
docker run --rm --env-file .env -e DRY_RUN=1 release-digest:latest   # no mail sent
docker run --rm --env-file .env release-digest:latest                # send it
```

Without Docker:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python fetch_releases.py          # -> /tmp/release-digest-data.json
./.venv/bin/python send_digest.py --dry-run
```

## Configuration

All configuration lives in `.env` (see [.env.example](.env.example)): a TMDB key, a RAWG
key, a Gmail address, a Gmail **App Password**, and a recipient. Nothing is read from
command-line arguments, so credentials never land in shell history.

Runtime knobs (`DIGEST_MONTH`, `ARCHIVE_DIR`, `DRY_RUN`, `TZ`) are documented
in [deploy/README.md](deploy/README.md).

## Scheduling

[deploy/](deploy/) ships a systemd service + timer (recommended — it catches up a run
missed while the machine was down) and a crontab one-liner. There's also a
`docker-compose.yml` for a single manual run.

## Design notes

Most of the non-obvious decisions here were forced by how email clients actually behave,
and are worth knowing before "simplifying" them:

- **No `<style>` block.** Every style is inlined per element. Proton Mail strips `<style>`
  entirely, which silently flattens the whole design to unstyled HTML.
- **Light theme, not dark.** Clients routinely drop `background` on `body` and containers
  but keep text colors. A dark design degrades to near-white text on white; a light one
  stays readable no matter what the sanitizer removes.
- **Tables, not CSS Grid or flexbox**, for multi-column layout.
- **Posters embedded as inline `cid:` attachments.** Remote images are blocked by default
  in most clients, because loading them reveals that the reader opened the message.
  Embedding removes the external fetch, so there's nothing left to block.
- **Small CDN image variants** (`w185` for TMDB, `/media/resize/420/-/` for RAWG). The
  originals pushed a single email to 6.8MB; these bring it to roughly 1MB.
- **Image fetches are host-allowlisted** to the two CDNs over HTTPS. The URLs come from
  third-party API responses, which are untrusted input — an allowlist keeps a crafted
  entry from turning the sender into an SSRF vector.
- **Image type is sniffed from magic bytes**, not the `Content-Type` header, because TMDB
  sometimes omits the header and valid posters would otherwise be dropped.

## Layout

```
fetch_releases.py   fetch + dedupe + date-clip + rank  -> JSON
render_digest.py    JSON -> email-safe HTML
send_digest.py      render, embed posters, send over SMTP
entrypoint.sh       chains fetch -> send for the container
deploy/             systemd units, cron line, deployment guide
```

## License

MIT — see [LICENSE](LICENSE).
