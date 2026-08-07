"""Build the ether.fi weETH allocation dashboard.

    python weeth_refresh.py              # incremental refresh from public RPC
    python weeth_refresh.py --offline    # rebuild the page from the committed cache

Answers one question: how much wrapped Ethereum (weETH) have the DAOs under kpk
management allocated to ether.fi, and how did that position build up over time.

NO API KEY IS REQUIRED, ANYWHERE.
Everything comes from endpoints that are free and public:

  * positions and history  ->  Ethereum public RPC nodes
                               (`balanceOf` and `Transfer` event logs)
  * pricing                ->  CoinGecko public API

That is a deliberate design choice, not a limitation. vaults.fyi is a vault-yield
API and does not index plain ERC-20 holdings — it reports the ENS ether.fi
position as $0.00 — and Dune would need a paid key. Reading the chain directly is
free, needs no secret, and means anyone can verify every number on the page
against the same public endpoints.

How the history is maintained
-----------------------------
`data/weeth_events.json` is a committed, append-only log of allocation events.
Each run scans only the blocks mined since the last run (`last_block`), so a
daily refresh reads a few thousand blocks rather than re-deriving all of history.

Every run then checks the replayed history against the live `balanceOf` of each
wallet. If they ever disagree — a missed log, an RPC gap, an exotic transfer —
the run emits a reconciling event so the headline figure always equals onchain
truth, and says so loudly in the log. The dashboard cannot silently drift.

Outputs:
    data/weeth_events.json          allocation events + scan cursor
    data/weeth_latest.json          the snapshot — source of truth
    weeth-data.js                   same object as window.__WEETH_DATA__
    weeth-etherfi-dashboard.html    single-file offline copy

Cumulative weETH is a POSITION, not a flow: gross allocations minus withdrawals,
which by construction equals the current onchain balance. Gross inflow is
reported separately and never conflated with it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
LATEST = ROOT / "data" / "weeth_latest.json"
DATA_JS = ROOT / "weeth-data.js"
PAGE = ROOT / "index.html"
STANDALONE = ROOT / "weeth-etherfi-dashboard.html"

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DUST = 1e-9

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("weeth")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def wallet_registry(config: dict) -> list[tuple[str, str]]:
    """[(wallet, dao)] for every managed address. Addresses are chain-agnostic:
    a wallet listed for Gnosis only can still hold mainnet weETH, so no chain
    filter is applied here — the token contract pins the chain instead."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for client in config.get("clients", []):
        name = str(client.get("name", "")).strip()
        for entry in client.get("addresses", []):
            addr = str(entry.get("address", "")).strip().lower()
            if not ADDRESS_RE.match(addr) or (addr, name) in seen:
                continue
            seen.add((addr, name))
            out.append((addr, name))
    return out


# ---------------------------------------------------------------------------
# public RPC
# ---------------------------------------------------------------------------

class Chain:
    """Thin JSON-RPC client that rotates over public endpoints. Every call falls
    through the endpoint list, so one node being down or rate-limiting never
    fails the refresh."""

    def __init__(self, rpcs: list[str], timeout: int = 25):
        self.rpcs = list(rpcs)
        self.timeout = timeout
        self.session = requests.Session()
        self._block_time: dict[int, int] = {}

    def call(self, method: str, params: list) -> object | None:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for i, rpc in enumerate(self.rpcs):
            try:
                r = self.session.post(rpc, json=payload, timeout=self.timeout)
                body = r.json()
                if "result" in body and body["result"] is not None:
                    if i:            # promote the endpoint that answered
                        self.rpcs.insert(0, self.rpcs.pop(i))
                    return body["result"]
            except Exception:  # noqa: BLE001 — try the next endpoint
                continue
        return None

    def head(self) -> int | None:
        res = self.call("eth_blockNumber", [])
        return int(res, 16) if res else None

    def balance_of(self, token: str, wallet: str, decimals: int = 18) -> float | None:
        data = "0x70a08231" + wallet.lower().replace("0x", "").rjust(64, "0")
        res = self.call("eth_call", [{"to": token, "data": data}, "latest"])
        return int(res, 16) / (10 ** decimals) if res and res != "0x" else None

    def block_time(self, number: int) -> int | None:
        if number in self._block_time:
            return self._block_time[number]
        res = self.call("eth_getBlockByNumber", [hex(number), False])
        if not res:
            return None
        ts = int(res["timestamp"], 16)
        self._block_time[number] = ts
        return ts

    def transfer_logs(self, token: str, wallets: list[str], lo: int, hi: int,
                      chunk: int = 9000) -> list[dict] | None:
        """Every Transfer log of `token` where a managed wallet is sender or
        recipient, across [lo, hi]. Public nodes cap the block span per request,
        so the range is walked in chunks. Returns None if any chunk fails, so a
        partial scan is never mistaken for 'no activity'."""
        padded = ["0x" + w.lower().replace("0x", "").rjust(64, "0") for w in wallets]
        found: dict[tuple[str, str], dict] = {}
        start = lo
        while start <= hi:
            end = min(start + chunk - 1, hi)
            for topics in ([TRANSFER_TOPIC, None, padded],      # inbound
                           [TRANSFER_TOPIC, padded, None]):     # outbound
                res = self.call("eth_getLogs", [{
                    "fromBlock": hex(start), "toBlock": hex(end),
                    "address": token, "topics": topics}])
                if res is None:
                    log.error("eth_getLogs failed for blocks %d-%d on every endpoint.", start, end)
                    return None
                for entry in res:
                    # dedupe: a managed-to-managed transfer matches both filters
                    found[(entry["blockNumber"], entry["logIndex"])] = entry
            start = end + 1
        return list(found.values())


def decode_transfer(entry: dict) -> tuple[str, str, float]:
    topics = entry["topics"]
    src = "0x" + topics[1][-40:]
    dst = "0x" + topics[2][-40:]
    raw = int(entry["data"], 16) if entry.get("data") not in (None, "0x") else 0
    return src.lower(), dst.lower(), raw


def prices_from_coingecko(ids: list[str]) -> dict[str, float]:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": ",".join(ids), "vs_currencies": "usd"}, timeout=20)
        r.raise_for_status()
        return {k: v["usd"] for k, v in r.json().items() if v.get("usd")}
    except Exception as exc:  # noqa: BLE001 — pricing is best-effort, cache covers it
        log.warning("CoinGecko pricing failed (%s); falling back to cached prices.", type(exc).__name__)
        return {}


# ---------------------------------------------------------------------------
# event log maintenance
# ---------------------------------------------------------------------------

def merge_events(events: list[dict], new: list[dict]) -> list[dict]:
    """Fold new (day, dao) deltas into the committed log, summing collisions."""
    index = {(e["day"], e["dao"]): e for e in events}
    for entry in new:
        key = (entry["day"], entry["dao"])
        if key in index:
            index[key]["net_flow"] += entry["net_flow"]
            index[key]["transfers"] += entry["transfers"]
        else:
            index[key] = dict(entry)
    merged = [e for e in index.values() if abs(e["net_flow"]) > DUST or e["transfers"]]
    return sorted(merged, key=lambda e: (e["day"], e["dao"]))


def scan(chain: Chain, token: str, registry: list[tuple[str, str]],
         lo: int, hi: int, decimals: int) -> list[dict] | None:
    """Turn the Transfer logs in (lo, hi] into per-(day, dao) net flows."""
    dao_of = dict(registry)
    entries = chain.transfer_logs(token, [w for w, _ in registry], lo, hi)
    if entries is None:
        return None
    buckets: dict[tuple[str, str], dict] = {}
    for entry in entries:
        src, dst, raw = decode_transfer(entry)
        block = int(entry["blockNumber"], 16)
        ts = chain.block_time(block)
        if ts is None:
            log.error("Could not read timestamp for block %d.", block)
            return None
        day = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
        amount = raw / (10 ** decimals)
        for wallet, sign in ((dst, 1.0), (src, -1.0)):
            dao = dao_of.get(wallet)
            if not dao:
                continue
            b = buckets.setdefault((day, dao), {"day": day, "dao": dao, "net_flow": 0.0, "transfers": 0})
            b["net_flow"] += sign * amount
            b["transfers"] += 1
    return list(buckets.values())


def verify(chain: Chain, token: str, registry: list[tuple[str, str]], events: list[dict],
           decimals: int, today: str) -> tuple[list[dict], dict]:
    """Compare the replayed history against live onchain balances. On drift,
    emit a reconciling event so the published figure always equals the chain."""
    replayed: dict[str, float] = {}
    for e in events:
        replayed[e["dao"]] = replayed.get(e["dao"], 0.0) + e["net_flow"]

    onchain: dict[str, float] = {}
    checked = 0
    for wallet, dao in registry:
        bal = chain.balance_of(token, wallet, decimals)
        if bal is None:
            log.warning("balanceOf failed for %s (%s); skipping it in the check.", dao, wallet[:10])
            continue
        onchain[dao] = onchain.get(dao, 0.0) + bal
        checked += 1

    fixes, max_drift = [], 0.0
    for dao, actual in onchain.items():
        drift = actual - replayed.get(dao, 0.0)
        max_drift = max(max_drift, abs(drift))
        if abs(drift) > DUST:
            log.warning("DRIFT %s: replayed %.6f vs onchain %.6f -> reconciling %+.6f",
                        dao, replayed.get(dao, 0.0), actual, drift)
            fixes.append({"day": today, "dao": dao, "net_flow": drift, "transfers": 0})

    report = {"method": "erc20 balanceOf via public RPC", "wallets_checked": checked,
              "matched": not fixes, "max_drift_weeth": round(max_drift, 9)}
    if not fixes and checked:
        log.info("Verified: replayed history matches onchain balances for %d wallets.", checked)
    return fixes, report


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def build_snapshot(events: list[dict], prices: dict, token: dict, registry: list[tuple[str, str]],
                   mode: str, now: dt.datetime, price_note: str, verification: dict) -> dict:
    events = sorted(events, key=lambda e: (e["day"], e["dao"]))
    weeth_usd = float(prices["weeth_usd"])
    eth_usd = float(prices["eth_usd"])
    ratio = weeth_usd / eth_usd if eth_usd else 0.0     # ETH per weETH (accrued yield)
    wallet_of = {dao: addr for addr, dao in reversed(registry)}

    names = sorted({e["dao"] for e in events})
    running = {n: 0.0 for n in names}
    per_dao = {n: {"name": n, "weeth": 0.0, "gross_in": 0.0, "gross_out": 0.0,
                   "events": 0, "transfers": 0, "first": None, "last": None,
                   "wallet": wallet_of.get(n, "")} for n in names}

    series: list[dict] = []
    rows: list[dict] = []
    for e in events:
        dao, delta = e["dao"], float(e["net_flow"])
        running[dao] += delta
        d = per_dao[dao]
        d["weeth"] = running[dao]
        d["events"] += 1
        d["transfers"] += int(e.get("transfers", 1))
        d["gross_in" if delta >= 0 else "gross_out"] += abs(delta)
        d["first"] = d["first"] or e["day"]
        d["last"] = e["day"]
        rows.append({"day": e["day"], "dao": dao, "delta": delta,
                     "transfers": int(e.get("transfers", 1)),
                     "dao_total": running[dao], "total": sum(running.values())})
        series.append({"day": e["day"], "by_dao": dict(running), "total": sum(running.values())})

    collapsed: list[dict] = []
    for point in series:
        if collapsed and collapsed[-1]["day"] == point["day"]:
            collapsed[-1] = point
        else:
            collapsed.append(point)
    series = collapsed

    today = now.date().isoformat()
    if series:
        zero = {"day": (dt.date.fromisoformat(series[0]["day"]) - dt.timedelta(days=21)).isoformat(),
                "by_dao": {n: 0.0 for n in names}, "total": 0.0}
        series = [zero] + series
        if series[-1]["day"] != today:
            series.append({"day": today, "by_dao": dict(running), "total": sum(running.values())})

    total = sum(running.values())
    daos = sorted(per_dao.values(), key=lambda d: -d["weeth"])
    for d in daos:
        d["usd"] = round(d["weeth"] * weeth_usd, 2)
        d["eth_equivalent"] = round(d["weeth"] * ratio, 4)
        d["share_pct"] = round(100 * d["weeth"] / total, 2) if total else 0.0
        for k in ("weeth", "gross_in", "gross_out"):
            d[k] = round(d[k], 6)

    return {
        "date": today,
        "updated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "mode": mode,
        "token": token,
        "prices": {"weeth_usd": weeth_usd, "eth_usd": eth_usd,
                   "weeth_eth_ratio": round(ratio, 6), "as_of": prices.get("as_of", ""),
                   "source": price_note},
        "verification": verification,
        "totals": {
            "weeth": round(total, 6),
            "usd": round(total * weeth_usd, 2),
            "eth_equivalent": round(total * ratio, 4),
            "gross_in": round(sum(d["gross_in"] for d in daos), 6),
            "gross_out": round(sum(d["gross_out"] for d in daos), 6),
            "daos_allocated": sum(1 for d in daos if d["weeth"] > 0),
            "daos_managed": len({dao for _, dao in registry}),
            "wallets": len(registry),
            "events": len(rows),
            "transfers": sum(d["transfers"] for d in daos),
            "first_allocation": rows[0]["day"] if rows else None,
            "last_allocation": rows[-1]["day"] if rows else None,
        },
        "daos": daos,
        "series": [{"day": p["day"], "total": round(p["total"], 6),
                    "by_dao": {k: round(v, 6) for k, v in p["by_dao"].items()}} for p in series],
        "events": list(reversed(rows)),
    }


def bundle_standalone() -> None:
    """Inline weeth-data.js into the page so one file can be emailed / Slacked."""
    html = PAGE.read_text(encoding="utf-8")
    inline = "<script>\n" + DATA_JS.read_text(encoding="utf-8") + "\n</script>"
    pattern = re.compile(r'<script src="weeth-data\.js"[^>]*></script>')
    html = pattern.sub(lambda _m: inline, html, count=1) if pattern.search(html) \
        else html.replace("</body>", inline + "\n</body>")
    STANDALONE.write_text(html, encoding="utf-8")
    log.info("Wrote %s (%d KB)", STANDALONE.name, len(html.encode("utf-8")) // 1024)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the ether.fi weETH allocation dashboard.")
    ap.add_argument("--offline", action="store_true",
                    help="rebuild the page from the committed cache without touching the network")
    args = ap.parse_args()

    config = load_config()
    ecfg = config.get("etherfi", {})
    tcfg = ecfg.get("token", {})
    token = {k: tcfg[k] for k in ("symbol", "address", "chain", "decimals", "coingecko") if k in tcfg}
    decimals = int(tcfg.get("decimals", 18))
    cache_path = ROOT / ecfg.get("events_cache", "data/weeth_events.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    events = list(cache.get("events", []))

    registry = wallet_registry(config)
    log.info("Wallet registry: %d addresses across %d clients.",
             len(registry), len({d for _, d in registry}))

    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    mode = "cached"
    verification = cache.get("verification", {"method": "not checked this run",
                                              "wallets_checked": 0, "matched": None,
                                              "max_drift_weeth": None})

    if not args.offline:
        chain = Chain(config.get("rpc", {}).get("endpoints", []))
        head = chain.head()
        if head is None:
            log.error("No public RPC endpoint responded. Nothing written.")
            return 1
        last_block = int(cache.get("last_block") or 0)

        if last_block:
            span = head - last_block
            log.info("Scanning blocks %d-%d (%d blocks) for weETH transfers.", last_block + 1, head, span)
            new = scan(chain, token["address"], registry, last_block + 1, head, decimals) if span > 0 else []
            if new is None:
                log.error("Block scan incomplete; keeping the previous cursor and cache.")
                return 1
            if new:
                log.info("Found %d new (day, DAO) flows.", len(new))
            events = merge_events(events, new)
        else:
            # First run on this cursor: the committed cache is the backfill. The
            # balanceOf check below proves whether it is complete as of `head`.
            log.info("No scan cursor yet; validating the committed backfill against the chain.")

        fixes, verification = verify(chain, token["address"], registry, events, decimals, today)
        if fixes:
            events = merge_events(events, fixes)
        mode = "live"

        cache = {**cache, "fetched_at": now.strftime("%Y-%m-%d %H:%M UTC"),
                 "source": "ethereum public RPC (balanceOf + Transfer logs)",
                 "token_address": token["address"], "last_block": head,
                 "verification": verification, "events": events}

    if not events:
        log.error("No allocation events available. Nothing written.")
        return 1

    cached_prices = cache.get("prices", {})
    live = prices_from_coingecko([tcfg.get("coingecko", "wrapped-eeth"),
                                  ecfg.get("reference_coingecko", "weth")]) if not args.offline else {}
    if live.get(tcfg.get("coingecko")) and live.get(ecfg.get("reference_coingecko")):
        prices = {"weeth_usd": live[tcfg["coingecko"]], "eth_usd": live[ecfg["reference_coingecko"]],
                  "as_of": now.strftime("%Y-%m-%d %H:%M UTC")}
        price_note = "coingecko spot"
        cache["prices"] = {**prices, "source": price_note}
    else:
        prices = cached_prices
        price_note = cached_prices.get("source", "cached")
        if not prices:
            log.error("No prices available. Nothing written.")
            return 1

    if not args.offline:
        cache_path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")

    snapshot = build_snapshot(events, prices, token, registry, mode, now, price_note, verification)

    LATEST.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    DATA_JS.write_text("window.__WEETH_DATA__ = " + json.dumps(snapshot, separators=(",", ":")) + ";\n",
                       encoding="utf-8")
    bundle_standalone()

    t = snapshot["totals"]
    log.info("%s weETH across %d DAOs  ($%s, ~%s ETH)  mode=%s",
             format(round(t["weeth"], 2), ","), t["daos_allocated"],
             format(round(t["usd"]), ","), format(round(t["eth_equivalent"], 1), ","), mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
