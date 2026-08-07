"""Build the ether.fi weETH allocation dashboard.

    python weeth_refresh.py              # live: Dune + CoinGecko
    python weeth_refresh.py --offline    # rebuild from the committed event cache

Answers one question: how much wrapped Ethereum (weETH) have the DAOs under kpk
management allocated to ether.fi, and how did that position build up over time.

Self-contained by design — this repo publishes one page and depends on nothing
but `requests` and `PyYAML`.

Configuration lives in config.yaml:
  * `clients[]`     the managed wallet registry (public onchain addresses)
  * `etherfi.token` the weETH contract, chain and CoinGecko id
  * `etherfi.query_id` the Dune query this script rewrites and executes

Secrets resolve exactly as in the kpk AUM dashboard: `configurator.json` (local,
gitignored), then `.env` (gitignored), then real environment variables / CI
secrets, which always win. `DUNE_API_KEY` is the only one needed.

The Dune SQL is regenerated from the registry on every run and PATCHed onto the
query before execution, so the query can never drift from config.

Outputs:
    data/weeth_events.json          raw allocation events (committed cache)
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
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
CONFIGURATOR = ROOT / "configurator.json"
LATEST = ROOT / "data" / "weeth_latest.json"
DATA_JS = ROOT / "weeth-data.js"
PAGE = ROOT / "index.html"
STANDALONE = ROOT / "weeth-etherfi-dashboard.html"

DUNE_BASE = "https://api.dune.com/api/v1"
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("weeth")


# ---------------------------------------------------------------------------
# config + secrets
# ---------------------------------------------------------------------------

def load_env() -> None:
    """Load .env into os.environ without overriding real env vars. Never logs
    values."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = val.strip().strip('"').strip("'")


def load_config() -> dict:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))

    # Optional local configurator (gitignored): API key + client roster override,
    # same contract as the AUM dashboard so one file can serve both.
    if CONFIGURATOR.exists():
        try:
            cfg = json.loads(CONFIGURATOR.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — fall back to config.yaml cleanly
            log.warning("Could not read %s: %s", CONFIGURATOR.name, type(exc).__name__)
            return config
        key = str(cfg.get("dune_api_key", "") or "").strip()
        env_name = config.get("dune", {}).get("api_key_env", "DUNE_API_KEY")
        if key and not os.environ.get(env_name):
            os.environ[env_name] = key          # real env / CI secret always wins
        if cfg.get("clients"):
            config["clients"] = cfg["clients"]
    return config


# ---------------------------------------------------------------------------
# wallet registry -> Dune SQL
# ---------------------------------------------------------------------------

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


def build_sql(registry: list[tuple[str, str]], token: str) -> str:
    """Daily net weETH flow + running cumulative per DAO, over a dense day spine
    so the series stays continuous on days with no transfers."""
    values = ",\n        ".join(f"({addr}, '{dao}')" for addr, dao in registry)
    return f"""-- kpk: cumulative weETH (ether.fi) allocated by managed DAOs
-- GENERATED by weeth_refresh.py from clients[] in config.yaml — do not hand-edit.
WITH wallets (wallet, dao) AS (
    VALUES
        {values}
),
xfers AS (
    SELECT evt_block_time, "from" AS src, "to" AS dst, value
    FROM erc20_ethereum.evt_Transfer
    WHERE contract_address = {token}
),
flows AS (
    SELECT CAST(date_trunc('day', x.evt_block_time) AS date) AS day,
           w.dao,
           CAST(x.value AS double) / 1e18 AS amount
    FROM xfers x JOIN wallets w ON x.dst = w.wallet
    UNION ALL
    SELECT CAST(date_trunc('day', x.evt_block_time) AS date) AS day,
           w.dao,
           -CAST(x.value AS double) / 1e18 AS amount
    FROM xfers x JOIN wallets w ON x.src = w.wallet
),
daily AS (
    SELECT day, dao, SUM(amount) AS net_flow, COUNT(*) AS transfers
    FROM flows GROUP BY 1, 2
),
spine AS (
    SELECT d.day, o.dao
    FROM (SELECT s.t AS day
          FROM UNNEST(sequence((SELECT min(day) FROM daily), CURRENT_DATE, INTERVAL '1' day)) AS s(t)) d
    CROSS JOIN (SELECT DISTINCT dao FROM daily) o
)
SELECT s.day,
       s.dao,
       COALESCE(d.net_flow, 0)  AS net_flow,
       COALESCE(d.transfers, 0) AS transfers,
       SUM(COALESCE(d.net_flow, 0)) OVER (PARTITION BY s.dao ORDER BY s.day) AS cumulative_weeth
FROM spine s
LEFT JOIN daily d ON d.day = s.day AND d.dao = s.dao
ORDER BY s.day, s.dao"""


# ---------------------------------------------------------------------------
# data sources
# ---------------------------------------------------------------------------

class DuneRunner:
    def __init__(self, api_key: str, timeout: int = 60):
        self.session = requests.Session()
        self.session.headers.update({"X-Dune-API-Key": api_key})
        self.timeout = timeout
        self.execution_id = ""

    def sync_sql(self, query_id: int, sql: str) -> None:
        r = self.session.patch(f"{DUNE_BASE}/query/{query_id}", json={"query_sql": sql}, timeout=self.timeout)
        r.raise_for_status()

    def run(self, query_id: int, performance: str = "medium", poll_seconds: int = 300) -> list[dict]:
        r = self.session.post(f"{DUNE_BASE}/query/{query_id}/execute",
                              json={"performance": performance}, timeout=self.timeout)
        r.raise_for_status()
        self.execution_id = r.json()["execution_id"]
        log.info("Dune execution %s started.", self.execution_id)

        deadline = time.time() + poll_seconds
        while time.time() < deadline:
            s = self.session.get(f"{DUNE_BASE}/execution/{self.execution_id}/status", timeout=self.timeout)
            s.raise_for_status()
            state = s.json().get("state")
            if state == "QUERY_STATE_COMPLETED":
                res = self.session.get(f"{DUNE_BASE}/execution/{self.execution_id}/results",
                                       params={"limit": 32000}, timeout=self.timeout)
                res.raise_for_status()
                return res.json().get("result", {}).get("rows", [])
            if state in {"QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"}:
                raise RuntimeError(f"Dune execution {state}")
            time.sleep(5)
        raise TimeoutError("Dune execution did not complete in time")


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
# snapshot
# ---------------------------------------------------------------------------

def build_snapshot(events: list[dict], prices: dict, token: dict, registry: list[tuple[str, str]],
                   mode: str, now: dt.datetime, price_note: str) -> dict:
    """Fold the allocation events into per-DAO positions and a step series."""
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

    # Collapse same-day events to one series point (the end-of-day position).
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
                    help="rebuild from the committed event cache instead of querying Dune")
    args = ap.parse_args()

    load_env()
    config = load_config()
    ecfg = config.get("etherfi", {})
    token = {**ecfg.get("token", {}), "query_id": ecfg.get("query_id")}
    cache_path = ROOT / ecfg.get("events_cache", "data/weeth_events.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    registry = wallet_registry(config)
    log.info("Wallet registry: %d addresses across %d clients.",
             len(registry), len({d for _, d in registry}))

    api_key = os.environ.get(config.get("dune", {}).get("api_key_env", "DUNE_API_KEY"), "")
    mode, events = "cached", cache.get("events", [])

    if not args.offline and api_key:
        runner = DuneRunner(api_key)
        query_id = int(ecfg["query_id"])
        runner.sync_sql(query_id, build_sql(registry, token["address"]))
        rows = runner.run(query_id, performance=ecfg.get("performance", "medium"))
        events = [{"day": str(r["day"])[:10], "dao": r["dao"],
                   "net_flow": float(r["net_flow"]), "transfers": int(r["transfers"])}
                  for r in rows if int(r.get("transfers", 0)) > 0]
        mode = "live"
        cache = {**cache, "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                 "execution_id": runner.execution_id, "query_id": query_id,
                 "token_address": token["address"], "events": events}
        cache_path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
        log.info("Dune returned %d allocation events.", len(events))
    elif not args.offline:
        log.warning("DUNE_API_KEY not set; rebuilding positions from the committed event cache.")

    if not events:
        log.error("No allocation events available (no Dune key and no cache). Nothing written.")
        return 1

    cached_prices = cache.get("prices", {})
    live = prices_from_coingecko([token.get("coingecko", "wrapped-eeth"),
                                  ecfg.get("reference_coingecko", "weth")]) if not args.offline else {}
    if live.get(token.get("coingecko")) and live.get(ecfg.get("reference_coingecko")):
        prices = {"weeth_usd": live[token["coingecko"]], "eth_usd": live[ecfg["reference_coingecko"]],
                  "as_of": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
        price_note = "coingecko spot"
        cache["prices"] = {**prices, "source": price_note}
        cache_path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    else:
        prices = cached_prices
        price_note = cached_prices.get("source", "cached")
        if not prices:
            log.error("No prices available. Nothing written.")
            return 1

    now = dt.datetime.now(dt.timezone.utc)
    snapshot = build_snapshot(events, prices, token, registry, mode, now, price_note)

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
