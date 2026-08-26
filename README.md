# weETH allocated to ether.fi — kpk-managed DAOs

A single self-refreshing page showing the cumulative amount of wrapped Ethereum
(**weETH**) that the DAOs under [kpk](https://kpk.io) management have allocated
to **ether.fi**, and how that position was built up over time.

**Live:** https://taravello.github.io/kpk-weeth-etherfi/

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
| Positions | Ethereum public RPC — `balanceOf` per wallet |
| History | Ethereum public RPC — `Transfer` logs touching a managed wallet |
| Pricing | CoinGecko public API (`wrapped-eeth`, `weth`) |

**No API key is required, anywhere.** That is deliberate. vaults.fyi is a
vault-yield API and does not index plain ERC-20 holdings — it reports the ENS
ether.fi position as `$0.00` — and Dune needs a paid key. Reading the chain
directly is free, needs no secret, and means anyone can verify every number on
the page against the same public endpoints.

All wallet addresses are public onchain data. Figures reconcile against the main
kpk AUM dashboard, which values the same positions independently.

## Run it

```bash
pip install -r requirements.txt
python weeth_refresh.py              # incremental refresh from public RPC
python weeth_refresh.py --offline    # rebuild the page from the committed cache
```

Open `index.html` directly (it loads `weeth-data.js` over `file://`), or share
`weeth-etherfi-dashboard.html` — one self-contained file, no server, no secrets.

## Configuration

Everything lives in `config.yaml`:

- **`clients[]`** — the managed wallet registry. Add an entity or an address
  here and it appears on the page on the next run. An entity may optionally
  carry `color: "#RRGGBB"` to override the palette; entities without one take
  the monochrome ramp in size order, so darkest still reads as largest.
- **`etherfi.token`** — the weETH contract, chain, decimals and CoinGecko id.
- **`rpc.endpoints`** — public Ethereum nodes, tried in order. Whichever answers
  first is promoted for the rest of the run, so one node being down or
  throttling never fails a refresh.

## How the history stays correct

`data/weeth_events.json` is a committed, append-only log of allocation events
plus a `last_block` cursor. Each run scans only the blocks mined since the last
run, so a daily refresh reads a few thousand blocks instead of re-deriving all
of history.

Every run then replays that history and compares it against the live
`balanceOf` of each wallet. If they ever disagree — a missed log, an RPC gap, an
exotic transfer — the run emits a reconciling event so the published figure
always equals onchain truth, logs a warning, and reports the check in the page
footer. The dashboard cannot silently drift.

## Secrets

**There are none.** Nothing to rotate, nothing to leak, nothing to expire.

## Daily refresh

`.github/workflows/refresh.yml` runs at 07:20 UTC (plus a manual **Run
workflow** button), commits the refreshed snapshot, and GitHub Pages redeploys.

## Files

| Path | Role |
|---|---|
| `index.html` | The page (brand palette, inline SVG chart, no dependencies) |
| `weeth-data.js` | Generated snapshot the page reads |
| `weeth_refresh.py` | The builder |
| `config.yaml` | Wallet registry, token, RPC endpoints |
| `data/weeth_events.json` | Raw allocation events, committed so the page rebuilds keyless |
| `data/weeth_latest.json` | The snapshot — source of truth |
| `weeth-etherfi-dashboard.html` | Single-file offline copy |
