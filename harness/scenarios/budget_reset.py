"""Finding: budget windows are fixed clock slots set at key creation, and a
key that hit its budget stays blocked past budget_reset_at until the reset
job next runs (every 597-605s by default).

Part 1, alignment: creates keys with several budget_duration values and
records budget_reset_at against the creation time, before any spend.

Part 2, reset lag (2 replicas): a $0.004 key with budget_duration "2m" is
spent until blocked. After budget_reset_at passes, it probes every 5s and
records when a request first gets through and when /key/info shows the spend
reset.

Usage: python3 scenarios/budget_reset.py classic|split
"""

import time

from lib import Stack, log, parse_ts, save, stack_arg, utcnow

DURATIONS = ["2m", "5m", "1h", "1d", "7d", "30d", "1mo"]
MAX_BUDGET = 0.004
LAG_TIMEOUT = 16 * 60


def alignment(stack):
    rows = []
    for duration in DURATIONS:
        created = utcnow()
        resp = stack.new_key(max_budget=1.0, budget_duration=duration, models=["mock-a"])
        info = stack.key_info(resp["key"])
        reset_at = parse_ts(info.get("budget_reset_at"))
        rows.append({
            "budget_duration": duration,
            "created": created.isoformat(),
            "budget_reset_at": reset_at.isoformat() if reset_at else None,
            "first_window_seconds": round((reset_at - created).total_seconds()) if reset_at else None,
            "spend_at_creation": info.get("spend"),
        })
        log(f"{stack.name}: {duration:>4} created {created:%H:%M:%S} -> resets {reset_at}")
    return rows


def reset_lag(stack):
    while True:
        key = stack.new_key(max_budget=MAX_BUDGET, budget_duration="2m", models=["mock-a"])["key"]
        reset_at = parse_ts(stack.key_info(key).get("budget_reset_at"))
        # Need time to spend the key down before its window ends
        if (reset_at - utcnow()).total_seconds() > 40:
            break
        time.sleep(max(0, (reset_at - utcnow()).total_seconds()) + 2)
    spends = []
    for _ in range(10):
        status, body, _ = stack.chat(key)
        spends.append({"at": utcnow().isoformat(), "status": status})
        if status != 200:
            break
    blocked_at = utcnow()
    log(f"{stack.name}: key blocked at {blocked_at:%H:%M:%S} after {len(spends) - 1} calls, "
        f"budget_reset_at {reset_at:%H:%M:%S}")

    while utcnow() < reset_at:
        time.sleep(1)

    unblocked_at = db_reset_at = None
    probes = []
    while (utcnow() - reset_at).total_seconds() < LAG_TIMEOUT and (unblocked_at is None or db_reset_at is None):
        now = utcnow()
        info = stack.key_info(key)
        row = {"at": now.isoformat(), "since_reset_at": round((now - reset_at).total_seconds()),
               "db_spend": info.get("spend"), "budget_reset_at": info.get("budget_reset_at")}
        if unblocked_at is None:
            status, _, _ = stack.chat(key)
            row["request_status"] = status
            if status == 200:
                unblocked_at = now
                log(f"{stack.name}: first request through {row['since_reset_at']}s after budget_reset_at")
        if db_reset_at is None and (info.get("spend") or 0) < MAX_BUDGET / 2:
            db_reset_at = now
            log(f"{stack.name}: /key/info spend reset {row['since_reset_at']}s after budget_reset_at")
        probes.append(row)
        time.sleep(5)

    def lag(t):
        return None if t is None else round((t - reset_at).total_seconds())

    return {
        "budget_reset_at": reset_at.isoformat(),
        "blocked_at": blocked_at.isoformat(),
        "calls_before_block": len(spends) - 1,
        "seconds_blocked_after_reset_at": lag(unblocked_at),
        "seconds_until_db_spend_reset": lag(db_reset_at),
        "stack_started_at": stack.started_at.isoformat(),
        "probes": probes,
    }


def reset_job_log_lines(stack):
    services = [stack.serving] + (["backend"] if stack.name == "split" else [])
    lines = []
    for service in services:
        lines += [l for l in stack.logs(service).splitlines() if "reset" in l.lower() and "budget" in l.lower()]
    return lines[-40:]


if __name__ == "__main__":
    stack_name, _ = stack_arg()
    stack = Stack(stack_name, replicas=2)
    stack.up()
    try:
        result = {"finding": "budget-reset", "settings": stack.settings,
                  "alignment": alignment(stack), "lag": reset_lag(stack)}
        result["reset_job_log_lines"] = reset_job_log_lines(stack)
    finally:
        stack.down()
    save("budget-reset", stack_name, result)
