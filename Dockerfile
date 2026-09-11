# One-shot job image: fetch the month's releases, email the digest, exit.
# There is no server and no inbound port — scheduling lives outside the
# container (systemd timer, cron, or a compose-side scheduler).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Singapore

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY fetch_releases.py render_digest.py send_digest.py entrypoint.sh ./
RUN chmod +x entrypoint.sh \
 && useradd --create-home --uid 10001 digest

# Nothing here needs root, and the job reaches the public internet.
USER digest

ENTRYPOINT ["/app/entrypoint.sh"]
