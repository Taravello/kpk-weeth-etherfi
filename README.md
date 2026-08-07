# weETH allocated to ether.fi — kpk-managed DAOs

A single self-refreshing page showing the cumulative amount of wrapped Ethereum
(**weETH**) that the DAOs under [kpk](https://kpk.io) management have allocated
to **ether.fi**, and how that position was built up over time.

**Live:** https://taravellokpk.github.io/kpk-weeth-etherfi/

## What it measures

Cumulative weETH is a **position, not a flow**: gross allocations minus
withdrawals, which by construction equals the current onchain balance. Gross
inflow is reported separately and never conflated with it.

weETH is the non-rebasing wrapper of eETH — the token balance stays constant and
ether.fi staking yield accrues in the redemption rate. The page therefore shows
both the weETH count and its ETH equivalent, which rises even when the count
does not.

| | |
|---|---|
| Token | weETH [`0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee`](https://etherscan.io/token/0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee) (Ethereum) |
| Wallets | The managed DAO treasuries listed in `config.yaml → clients[]` |
| History | Dune — every `erc20_ethereum.evt_Transfer` touching a managed wallet |
| Pricing | CoinGecko spot (`wrapped-eeth`, `weth`) |

All wallet addresses are public onchain data. Figures reconcile against the
main kpk AUM dashboard, which values the same positions from an independent
source (vaults.fyi + direct RPC reads).

## Run it

```bash
pip install -r requirements.txt
python weeth_refresh.py              # live: Dune + CoinGecko
python weeth_refresh.py --offline    # rebuild from the committed event cache
```

Open `index.html` directly (it loads `weeth-data.js` over `file://`), or share
`weeth-etherfi-dashboard.html` — one self-contained file, no server, no secrets.

## Configuration

Everything lives in `config.yaml`:

- **`clients[]`** — the managed wallet registry. The Dune SQL is regenerated
  from this list and PATCHed onto the query on every run, so the query can never
  drift from config. Add a DAO or an address here and it appears on the page.
- **`etherfi.token`** — the weETH contract, chain and CoinGecko id.
- **`etherfi.query_id`** — the Dune query the script rewrites and executes.

## Secrets

`DUNE_API_KEY` is the only one. It resolves from a gitignored
`configurator.json`, then `.env`, then real environment variables / CI secrets,
which always win — the same precedence as the kpk AUM dashboard, so one local
configurator can serve both.

Set it in **Settings → Secrets and variables → Actions**. Without it the daily
run still publishes: it re-prices from CoinGecko and rebuilds positions from the
committed event cache, labelling the page `CACHED` rather than presenting a
stale number as current.

## Daily refresh

`.github/workflows/refresh.yml` runs at 07:20 UTC (plus a manual **Run
workflow** button), commits the refreshed snapshot, and GitHub Pages redeploys.

## Files

| Path | Role |
|---|---|
| `index.html` | The page (brand palette, inline SVG chart, no dependencies) |
| `weeth-data.js` | Generated snapshot the page reads |
| `weeth_refresh.py` | The builder |
| `config.yaml` | Wallet registry + token + Dune settings |
| `data/weeth_events.json` | Raw allocation events, committed so the page rebuilds keyless |
| `data/weeth_latest.json` | The snapshot — source of truth |
| `weeth-etherfi-dashboard.html` | Single-file offline copy |
