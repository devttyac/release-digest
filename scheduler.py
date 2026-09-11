#!/usr/bin/env python3
"""Long-running scheduler: fire the digest once a month, then keep waiting.

The one-shot container (`entrypoint.sh once`) suits a systemd timer or cron.
This mode suits a compose-stack manager like Dockge, which expects services
that stay up — a container that exits immediately reads as a dead stack.

Behaviour worth knowing:

* **It records the month it last sent**, in STATE_FILE on a mounted volume.
  Without that, a container restart shortly after a send would send again.

* **It catches up.** If the host was down on the 1st and comes back on the 5th,
  the run for that month still happens, because the check is "has this month
  been sent?" rather than "is it exactly the 1st right now?".

* **It does not send on first boot.** With no state file it records the current
  month and waits for the next one, so deploying the stack never fires an
  unexpected email. Set RUN_ON_START=1 to override that for a test.

Config (all optional): RUN_DAY=1 RUN_HOUR=8 RUN_MINUTE=0
                       STATE_FILE=/state/last-run  RUN_ON_START=
"""

import calendar
import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests

RUN_DAY = int(os.environ.get("RUN_DAY", "1"))
RUN_HOUR = int(os.environ.get("RUN_HOUR", "8"))
RUN_MINUTE = int(os.environ.get("RUN_MINUTE", "0"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/state/last-run"))
RUN_ON_START = bool(os.environ.get("RUN_ON_START", "").strip())

# Resolved next to this file rather than hardcoded to /app, so the scheduler is
# runnable outside the container too.
ENTRYPOINT = os.environ.get("ENTRYPOINT_PATH") or str(Path(__file__).resolve().parent / "entrypoint.sh")

# Re-check at least hourly rather than sleeping straight to the target, so a
# suspended host, a clock jump or a DST shift can't overshoot the window.
MAX_SLEEP = 3600

# Hard ceiling on a single digest run. Every network call inside it already has
# its own timeout, but those only bound individual requests — a wedged child
# would otherwise block this loop forever, leaving a container that looks alive
# and never sends again. A normal run takes well under a minute.
DIGEST_TIMEOUT = 1800

# Optional Uptime Kuma push monitor. Without it a failed run is only visible in
# the container log, which nobody reads until they notice the email is missing.
PUSH_URL = os.environ.get("UPTIME_KUMA_PUSH_URL", "").strip()

# Sticky failure flag. Idle pings report the last known send outcome rather than
# an unconditional "up" — otherwise the hourly heartbeat would clear a genuine
# failure alert an hour after it fired, and the monitor would flap.
_last_send_failed = False
_last_failure_msg = ""


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def push(status: str, msg: str) -> None:
    """Ping the push monitor. Never raises, never blocks the digest."""
    if not PUSH_URL:
        return
    parts = urlsplit(PUSH_URL)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        log("WARNING: UPTIME_KUMA_PUSH_URL is not a valid http(s) URL — skipping monitor ping")
        return
    # Rebuild the query rather than appending: Uptime Kuma shows the push URL
    # with example params attached, and people paste it whole.
    url = urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urlencode({"status": status, "msg": msg[:180]}), "",
    ))
    try:
        requests.get(url, timeout=10)
    except requests.RequestException as exc:
        # Monitoring being down must never take the job down with it.
        log(f"WARNING: monitor ping failed: {exc}")


def read_state() -> str | None:
    try:
        return STATE_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None
    except OSError as exc:
        log(f"WARNING: cannot read {STATE_FILE}: {exc}")
        return None


def write_state(month: str) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(month + "\n")
    except OSError as exc:
        log(f"ERROR: cannot persist state to {STATE_FILE}: {exc}")


def ensure_state_writable() -> bool:
    """Refuse to run if state can't be persisted.

    Every safety property here depends on durable state: "don't send on first
    boot" and "don't resend after a restart" are both decided by reading it
    back. If the write silently fails, the scheduler believes nothing has been
    sent and mails on every restart. Crash-looping with a clear message is far
    better than quietly spamming the recipient.
    """
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        probe = STATE_FILE.parent / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError as exc:
        log(f"FATAL: state directory {STATE_FILE.parent} is not writable: {exc}")
        log("Refusing to start: without durable state this would resend on every restart.")
        log("The mounted directory must be writable by the container user (uid 10001), e.g.:")
        log(f"    sudo chown -R 10001:10001 <host dir mounted at {STATE_FILE.parent}>")
        return False


def run_target(now: dt.datetime) -> dt.datetime:
    """The moment this month's run is due."""
    day = min(RUN_DAY, calendar.monthrange(now.year, now.month)[1])
    return now.replace(day=day, hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)


def send_digest() -> bool:
    global _last_send_failed, _last_failure_msg
    log("running digest")
    try:
        result = subprocess.run([ENTRYPOINT, "once"], check=False, timeout=DIGEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"digest TIMED OUT after {DIGEST_TIMEOUT}s — killed; will retry at the next check")
        _last_send_failed = True
        _last_failure_msg = f"digest timed out after {DIGEST_TIMEOUT}s"
        push("down", _last_failure_msg)
        return False
    except OSError as exc:
        # A missing or non-executable entrypoint must not take the scheduler
        # down with it — the stack would stop instead of retrying next check.
        log(f"digest FAILED to launch {ENTRYPOINT}: {exc}")
        _last_send_failed, _last_failure_msg = True, f"cannot launch digest: {exc}"
        push("down", _last_failure_msg)
        return False
    if result.returncode == 0:
        log("digest sent")
        _last_send_failed, _last_failure_msg = False, ""
        push("up", f"digest sent for {dt.datetime.now():%Y-%m}")
        return True
    log(f"digest FAILED (exit {result.returncode}) — will retry at the next check")
    _last_send_failed = True
    _last_failure_msg = f"digest exited {result.returncode} — see container log"
    push("down", _last_failure_msg)
    return False


def main() -> int:
    log(
        f"scheduler up — day={RUN_DAY} time={RUN_HOUR:02d}:{RUN_MINUTE:02d} "
        f"tz={os.environ.get('TZ', 'system')} state={STATE_FILE}"
    )

    if not ensure_state_writable():
        push("down", "refusing to start: state directory is not writable")
        return 1

    if PUSH_URL:
        log("monitor ping enabled")

    if read_state() is None and not RUN_ON_START:
        seeded = dt.datetime.now().strftime("%Y-%m")
        write_state(seeded)
        log(f"first boot — seeding state to {seeded} without sending; next run is next month")
        log("set RUN_ON_START=1 if you wanted an immediate send instead")

    if RUN_ON_START:
        log("RUN_ON_START set — sending now")
        if send_digest():
            write_state(dt.datetime.now().strftime("%Y-%m"))

    while True:
        now = dt.datetime.now()
        this_month = now.strftime("%Y-%m")

        if now >= run_target(now) and read_state() != this_month:
            if send_digest():
                write_state(this_month)
            else:
                # Back off so a persistent failure doesn't hot-loop the API.
                time.sleep(MAX_SLEEP)
            continue

        target = run_target(now)
        if now >= target:
            # Already handled this month — aim at next month's target.
            nxt = (target.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
            day = min(RUN_DAY, calendar.monthrange(nxt.year, nxt.month)[1])
            target = nxt.replace(day=day, hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)

        # Idle heartbeat. Reports the last known send outcome, not an
        # unconditional "up" — a failed run must stay flagged until a later run
        # actually succeeds, otherwise the next heartbeat silently clears the
        # alert and the monitor flaps green while the digest is still broken.
        if _last_send_failed:
            push("down", _last_failure_msg)
        else:
            push("up", f"idle — next run {target:%Y-%m-%d %H:%M}")

        sleep_for = max(1, min(MAX_SLEEP, (target - now).total_seconds()))
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("stopping")
        sys.exit(0)
