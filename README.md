# make-me-money: paper-trading memecoin crawler

**v2 (Oct 8):** two bots, each with its own paper $10. *Steady* adds RugCheck
holder checks, 15% bets and a pause after 3 losses in a row. *Degen* buys young
coins with 50% bets, +100%/−35% exits and a trailing stop. v1's record is kept
in `state.json` / `journal.md`. The notes below describe v1.

A $10 Solana memecoin desk that **doesn't touch real money**. It:

1. **Watches** new tokens on DexScreener (free public API, no key needed).
2. **Throws out the noise** with hard filters: no liquidity pool, too new, already pumped, thin volume, sellers dumping.
3. **Scores** the survivors on liquidity depth, buy pressure, turnover, leftover momentum and socials.
4. **Sizes** each position with plain math: 25% of equity, max 3 open.
5. **Exits** at +50% take profit, −25% stop loss, a 6h time stop, or when liquidity gets pulled (a rug).
6. **Journals** every trade to `journal.md`, with fees and slippage priced in.

```bash
python3 crawler.py --once          # one scan
python3 crawler.py --interval 60   # run continuously
python3 crawler.py --report        # scoreboard
```

Python 3 standard library only, nothing to install.

## Rule before using real money
Paper-trade for at least 1–2 weeks. If the equity line isn't clearly above $10
after 30+ closed trades, the edge isn't real, and real money will lose faster
than paper (MEV bots, failed transactions, worse fills).

## Run it on a server (Oracle Cloud free tier)
On a fresh Ubuntu server:
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/smallkhk/Make-me-money/claude/paper-trading-crawler/setup-server.sh)
```
It asks for a GitHub token (fine-grained, this repo only, Contents: Read and write),
then runs `crawler.py --serve --sync` as a service: new coins scanned every 60s,
held coins checked every 3s, trades pushed to GitHub so the dashboard stays live.
Disable the GitHub Action once the server is running, so only one bot trades.
