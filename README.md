# release-digest

A monthly email digest of upcoming **games, movies, TV and anime** — sourced from
[RAWG](https://rawg.io/apidocs) and [TMDB](https://www.themoviedb.org/), ranked by each
API's own popularity signal, with posters embedded so they display without a
"load remote images" prompt.

<p align="center">
  <img src="docs/screenshot.png" alt="The digest email: a Games section of two-column cards, each with a poster thumbnail, release date, platforms and a short synopsis, with the highest-popularity title badged Top Pick" width="680">
</p>

No inbound port, no database. The container runs in either of two modes:

| Mode | Behaviour | Suits |
|---|---|---|
| `scheduler` *(default)* | Stays up, fires the digest once a month | Docker Compose, Dockge, Portainer — anything expecting a service to stay running |
| `once` | Runs the digest immediately and exits | systemd timers, cron, manual runs |

## What it does

1. **Fetch** — pulls the target month's releases from RAWG (games) and TMDB (movies, TV
   and anime).
2. **Rank** — sorts each category latest-first by release date, badges each card with its
   popularity rank within that category, and flags the top one as **Top Pick**.
3. **Render** — builds an email-safe HTML digest.
4. **Send** — emails it over Gmail SMTP with every poster embedded as an inline attachment.

## Quick start

A prebuilt image is published to `ghcr.io/devttyac/release-digest:latest`, so there's
nothing to compile.

```bash
git clone https://github.com/devttyac/release-digest.git
cd release-digest
cp .env.example .env && nano .env     # five values
chmod 600 .env
```

```bash
# Single run. `once` matters — without it you get the scheduler, which idles.
docker run --rm --env-file .env ghcr.io/devttyac/release-digest:latest once

# Same, but builds the message without sending.
docker run --rm --env-file .env -e DRY_RUN=1 ghcr.io/devttyac/release-digest:latest once
```

To run it on a schedule instead, see [deploy/README.md](deploy/README.md).

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

`DIGEST_RECIPIENT` takes one address or a comma-separated list. An entry that can't be
parsed is reported and skipped rather than silently dropped, so one typo can't quietly
stop a recipient receiving the digest.

The TMDB value must be the **API Key (v3 auth)** — 32 hex characters — not the longer
"API Read Access Token" shown beneath it, which is for a different auth scheme and fails
with `Invalid API key`.

Runtime knobs (`DIGEST_MONTH`, `ARCHIVE_DIR`, `DRY_RUN`, `TZ`, `RUN_DAY`, `RUN_HOUR`,
`RUN_MINUTE`, `RUN_ON_START`, `STATE_FILE`) are documented in
[deploy/README.md](deploy/README.md).

## Scheduling

[compose.yaml](compose.yaml) runs the scheduler as a service — the simplest option, and
what a stack manager expects. [deploy/](deploy/) additionally ships a systemd service +
timer and a crontab line for driving `once` mode externally.

The scheduler keeps a small state file recording the month it last sent. That gives it
these properties:

- **No resend** after a container restart.
- **Catch-up** — if the host was down on the 1st and returns on the 5th, that month still
  goes out.
- **Catch-up sends the current month, not a missed one.** Down for all of October and back
  on 5 November, you get November's digest and October is skipped. A digest of a month
  that has already happened is less useful than the current one, so it doesn't backfill.
- **No send on first boot** — it seeds state and waits for the next month, so deploying
  never fires an unexpected email. `RUN_ON_START=1` overrides this for a test.

It refuses to start if the state directory isn't writable, rather than running without
those guarantees. It re-checks at least hourly instead of sleeping straight to the target,
so a suspended host or a clock jump can't overshoot the window, and caps each run at 30
minutes — a wedged run is killed and retried rather than blocking forever behind a
container that still looks healthy.

## Design notes

Most of these exist because something actually went wrong without them. Worth knowing
before "simplifying" any of them.

### Email rendering

- **No `<style>` block.** Every style is inlined per element. Proton Mail strips `<style>`
  entirely, which silently flattens the whole design to unstyled HTML.
- **Light theme, not dark.** Clients routinely drop `background` on `body` and containers
  but keep text colors. A dark design degrades to near-white text on white; a light one
  stays readable no matter what the sanitizer removes.
- **Tables, not CSS Grid or flexbox**, for multi-column layout — and each card *is* a table
  cell rather than a table nested inside one. Cells in a row are always equal height, so
  two cards side by side match; a nested table only takes its content height, leaving a
  short card floating above a gap.
- **Posters sit beside the text, not as banners.** A blocked or missing image then leaves a
  small gap instead of a large hole.
- **Posters embedded as inline `cid:` attachments.** Remote images are blocked by default
  in most clients, because loading them reveals that the reader opened the message.
  Embedding removes the external fetch, so there's nothing left to block.
- **Small CDN image variants** (`w185` for TMDB, `/media/resize/420/-/` for RAWG). The
  originals pushed a single email to 6.8MB; these bring it to roughly 1MB.
- **Descriptions are clamped to ~105 characters** so card heights stay in a narrow band.
  Uneven lengths were the main cause of ragged rows.
- **Empty sections are left out** rather than printed as "no releases" — a month with no
  anime is normal, and an empty block reads as a fault.

### Data correctness

- **The month comes from the clock; the state file only decides *whether* to send.** On
  1 October the fetch asks for October's window because that's today's month, while the
  stored `2026-09` is what triggers the send. The heading, subject line and archive
  filenames all derive from the fetched window, so the label can't disagree with the
  content.
- **Results are re-clipped to the month locally.** TMDB's `primary_release_date` filter
  matches a title's *global* primary date, which can differ from the regional date the
  response shows — without the local check, October films leaked into September.
- **Near-duplicates are collapsed.** RAWG sometimes lists one game twice under slightly
  different names for different platform or regional entries.
- **An API error is not an empty month.** TMDB answers a bad key with HTTP 200 and
  `success:false`. Treating that as "no results" would make an expired key look exactly
  like a quiet month.
- **Top Pick is chosen before the per-section cap**, then guaranteed a slot. Capping by date
  first could otherwise drop the one title the ranking exists to surface.
- **Descriptions fall back rather than go blank.** RAWG's `description_raw` is often empty
  when the site shows an About section — the text is in the HTML `description` field, which
  gets stripped. TMDB's English overview is often empty for foreign-language titles, so
  translations are tried next: English, then the original language, then any.
- **Truncation never splits on `. `** — abbreviations like "U.S." and "Dr." produced
  fragments such as "When a U.S". It cuts at a word boundary instead.
- **Anime is matched by genre 16 + Japanese original language**, ANDed, so live-action
  Japanese titles can't match. That trades false negatives for precision: anime that
  isn't Japanese-language (co-productions, English-original anime-style shows) is missed
  here, though it still reaches the digest via Movies or TV. Anime needs its own section
  because it loses on TMDB's global popularity score against mainstream releases, and the
  Movies query's US-theatrical filter would exclude most anime films outright.
- **Explicit titles are excluded** via `include_adult=false` plus TMDB's `hentai` keyword,
  looked up at runtime. `ecchi` is deliberately *not* blocked — it denotes fanservice
  rather than explicit content and is applied to plenty of mainstream titles, so blocking
  it would remove legitimate releases invisibly.
- **Popularity shows as a rank, not a raw score.** RAWG's `added` is a count of users who
  added the game; TMDB's `popularity` is an opaque float that changes daily. Printing both
  would invite a comparison that means nothing — a game at 4,981 against a film at 230 is
  not 20x more popular. A per-category rank is comparable and answers the question a
  reader actually has, given the cards are ordered by date rather than by popularity.

### Security

- **Image fetches are host-allowlisted** to the two CDNs over HTTPS. The URLs come from
  third-party API responses, which are untrusted input — an allowlist keeps a crafted
  entry from turning the sender into an SSRF vector.
- **Upstream ids are coerced to integers** before going into request paths, so API data
  can't reshape the request target.
- **Image type is sniffed from magic bytes**, not the `Content-Type` header. TMDB sometimes
  omits the header, which would drop valid posters, and sniffing also stops a non-image
  response being attached as though it were one.

## Layout

```
fetch_releases.py   fetch -> clip to month -> dedupe -> rank        -> JSON
render_digest.py    JSON -> email-safe HTML (inline styles, tables)
send_digest.py      render, embed posters as cid: parts, send via SMTP
scheduler.py        long-running mode: fires monthly, tracks what it sent
entrypoint.sh       dispatches `scheduler` (default) or `once`
compose.yaml        the scheduler as a service, for a stack manager
Dockerfile          the image both modes run from
deploy/             systemd units, cron line, deployment guide
```

Each stage runs independently, which is what makes it debuggable: run
`fetch_releases.py` alone to inspect the JSON, `render_digest.py` to get the HTML
without sending, or `send_digest.py --dry-run` to build the entire message and
report its size while sending nothing.

## License

MIT — see [LICENSE](LICENSE).
