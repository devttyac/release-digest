# Default mode is a long-running scheduler, which suits a compose-stack manager
# like Dockge — a container that exits immediately reads there as a dead stack.
# Override with `once` for a single run under a systemd timer or cron:
#     docker run --rm --env-file .env release-digest:latest once
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Etc/UTC

# tzdata so TZ actually resolves — the scheduler fires on local wall-clock time,
# and python:slim does not ship the zoneinfo database.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY fetch_releases.py render_digest.py send_digest.py scheduler.py entrypoint.sh ./
RUN chmod +x entrypoint.sh \
 && useradd --create-home --uid 10001 digest \
 && mkdir -p /state /archive \
 && chown digest:digest /state /archive

# Nothing here needs root, and the job reaches the public internet.
USER digest

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["scheduler"]
