"""Workload 6: release trains. A lane bakes four candidate builds one after the
other against live traffic; every build ends with a decision by an operator."""

import argparse
import random

from wl_common import *  # noqa: F401,F403  (inlined by the builder)

RATE = 40                                               # traffic lines per second
ROUTES = ("cart", "login", "search", "quote", "ship", "media")


def checksum(rid, ms):
    return (rid * 31 + ms * 17) % 4093


def request(rng, b, t, group, blip=False, down=False, counted=True):
    """One traffic line of a build at bake second t."""
    canary = group == "canary"
    kind = "probe" if rng.random() < 0.12 else "user"
    retry = rng.randrange(1, 4) if kind == "user" and rng.random() < 0.10 else 0
    special = kind == "probe" or retry or not counted
    p_err = b["noise_err"] if special and canary else b["err_c"] if canary else b["err_s"]
    ms = rng.lognormvariate(3.9, 0.35)
    if canary:
        ms *= b["noise_lat"] if special else b["lat"]
    fail = rng.random() < p_err or (canary and not special and (blip or down))
    status = rng.choice((500, 502, 503)) if fail else rng.choice((200, 200, 200, 204, 404))
    rid, ms = rng.randrange(10**7), max(1, int(ms))
    ck = checksum(rid, ms)
    if rng.random() < (b["ck_c"] if canary and not special else
                       0.02 if canary else 0.01):
        ck = (ck + rng.randrange(1, 4000)) % 4093
    return (f"{int(t * 1000):06d} REQ group={group} kind={kind} retry={retry} "
            f"route={rng.choice(ROUTES)} status={status} ms={ms} id={rid:07d} ck={ck:04d} "
            f"span={rng.getrandbits(64):016x}")


def bake(b):
    """Deterministic [(second, line)] of one build's bake. For an outage the list
    ends where the canary group goes down."""
    rng = random.Random(f"bake:{b['seed']}")
    n = int(b["bake_s"] * RATE)
    stop = int(n * b["down_at"]) if b["plan"] == "OUTAGE" else n
    warm = int(n * 0.12)
    out = [(0.0, f"000000 BAKE begin build={b['id']} seq={b['seq']}"),
           (0.0, f"000000 MANIFEST build={b['id']} " + " ".join(b["parts"]))]
    if b["policy_at"] == 0:
        out.append((0.0, f"000000 POLICY {b['policy']}"))
    blips = {int(n * x) for x in b["blips"]}
    blip, rss = 0, {"stable": 400.0, "canary": 400.0}
    for i in range(stop):
        t = i / RATE
        ms = int(t * 1000)
        if i == warm:
            out.append((t, f"{ms:06d} BAKE warm build={b['id']}"))
        if b["policy_at"] and i == int(n * b["policy_at"]):
            out.append((t, f"{ms:06d} POLICY {b['policy']}"))
        if i in blips:
            blip = 14                                   # canary lines that fail in a row
        group = "canary" if rng.random() < 0.5 else "stable"
        out.append((t, request(rng, b, t, group, blip=blip > 0, counted=i >= warm)))
        if group == "canary" and " kind=user retry=0 " in out[-1][1]:
            blip -= 1
        if i % (RATE * 2) == RATE:
            for g in rss:
                if i < warm:
                    rss[g] += rng.uniform(40, 120)      # caches fill
                elif g == "canary":
                    rss[g] += b["grow"] * 2 * RATE / (n - warm)
                out.append((t, f"{ms:06d} MEM group={g} rss_mb={rss[g] + rng.uniform(-6, 6):.1f}"))
        if rng.random() < 0.004:
            out.append((t, f"{ms:06d} " + rng.choice((
                "WARN pool near capacity, scaled", "ERROR upstream reset, request was replayed",
                "INFO config reloaded", "ERROR metrics exporter timed out, buffered"))))
    return out


def main():
    ap = argparse.ArgumentParser(description="Run one release lane.")
    ap.add_argument("--lane", type=int, required=True)
    a = ap.parse_args()
    p = load_image(f"lane{a.lane}.img")
    led = Ledger(f"lane{a.lane}", lane=a.lane, dec={})
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "control", f"lane_{a.lane}.txt")

    def decision(build):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                words = f.read().split()
        except OSError:
            return None
        for x, y in zip(words, words[1:]):
            if x == build and y in ("promote", "rollback"):
                return y
        return None

    def await_decision(b, ttl, t_out, emit, accept):
        """Print emit(second) lines until an accepted decision or ttl; its age or None."""
        t0 = now = time.time()
        while now - t0 <= ttl:
            d = decision(b["id"])
            if d in accept:
                led.rec["dec"][b["id"]] = [d, round(now - t0, 1)]
                led.mark()
                return d
            for line in emit(t_out + now - t0):
                sys.stdout.write(line + "\n")
            sys.stdout.flush()
            time.sleep(0.5)
            led.beat()
            now = time.time()
        led.rec["dec"][b["id"]] = ["none", round(now - t0, 1)]
        led.mark()
        return None

    say(f"# lane={a.lane} builds={len(p['builds'])}")
    for b in p["builds"]:
        items, rng = bake(b), random.Random(f"live:{b['seed']}")
        run_paced(((t / b["bake_s"], x) for t, x in items), b["bake_s"] * p["speed"], led)
        t_end = items[-1][0]
        if b["plan"] == "OUTAGE":
            def down(t):
                return [request(rng, b, t, rng.choice(("canary", "stable")), down=True)
                        for _ in range(RATE // 2)]
            d = await_decision(b, p["down_ttl"], t_end, down, ("rollback",))
        else:
            say(f"{int(t_end * 1000):06d} GATE open build={b['id']} ttl={p['ttl']:g}s")

            def idle(t):
                return [f"{int(t * 1000):06d} IDLE build={b['id']} drained={rng.randrange(100)}"]
            d = await_decision(b, p["ttl"], t_end, idle, ("promote", "rollback"))
        say(f"{int(t_end * 1000):06d} GATE closed build={b['id']} result={d or 'expired'}")
    say("# train finished")
    led.beat(done=True)


if __name__ == "__main__":
    main()
