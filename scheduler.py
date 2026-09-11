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


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


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
        # Not fatal, but it means a restart could re-send — say so loudly.
        log(f"WARNING: cannot persist state to {STATE_FILE}: {exc} — a restart may resend")


def run_target(now: dt.datetime) -> dt.datetime:
    """The moment this month's run is due."""
    day = min(RUN_DAY, calendar.monthrange(now.year, now.month)[1])
    return now.replace(day=day, hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)


def send_digest() -> bool:
    log("running digest")
    try:
        result = subprocess.run([ENTRYPOINT, "once"], check=False)
    except OSError as exc:
        # A missing or non-executable entrypoint must not take the scheduler
        # down with it — the stack would stop instead of retrying next check.
        log(f"digest FAILED to launch {ENTRYPOINT}: {exc}")
        return False
    if result.returncode == 0:
        log("digest sent")
        return True
    log(f"digest FAILED (exit {result.returncode}) — will retry at the next check")
    return False


def main() -> int:
    log(
        f"scheduler up — day={RUN_DAY} time={RUN_HOUR:02d}:{RUN_MINUTE:02d} "
        f"tz={os.environ.get('TZ', 'system')} state={STATE_FILE}"
    )

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

        sleep_for = max(1, min(MAX_SLEEP, (target - now).total_seconds()))
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("stopping")
        sys.exit(0)
