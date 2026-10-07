#!/usr/bin/env python3
"""Paper-trading memecoin crawler for Solana.

Scans new tokens on DexScreener, throws out the obvious scams, and paper-trades
the survivors with a $10 bankroll. No wallet, no keys, no real money: it only
records what it *would* have done, with realistic fees, so you can see whether
the strategy actually makes money before risking anything.

    python3 crawler.py --once          # one scan + position check
    python3 crawler.py --interval 60   # keep running, scan every 60s
    python3 crawler.py --report        # print the scoreboard
"""
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.dexscreener.com"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
JOURNAL_FILE = HERE / "journal.md"

START_CASH = 10.0

# Hard filters: fail any one and the token is out.
HARD = {
    "min_age_minutes": 15,
    "max_age_hours": 72,
    "min_liquidity_usd": 12_000,
    "min_volume_h24": 45_000,
    "min_mcap_usd": 60_000,
    "max_mcap_usd": 5_000_000,
    "min_trades_h24": 150,
    "max_h1_change_pct": 300,   # momentum already spent
    "min_buy_sell_ratio": 0.8,  # sellers swamping buyers
}

# Sizing and exits (plain python, no vibes).
RISK = {
    "position_frac": 0.25,     # of equity per trade
    "max_open": 3,
    "min_score": 0.6,
    "take_profit_pct": 50,
    "stop_loss_pct": -25,
    "time_stop_hours": 6,
    "rug_liquidity_drop": 0.5,  # exit if liquidity falls by half
}

# Cost model for tiny Solana swaps: DEX fee, slippage, network + priority fee.
SWAP_FEE = 0.01
BASE_SLIPPAGE = 0.01
NETWORK_FEE_USD = 0.02


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "paper-crawler/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def now():
    return time.time()


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"cash": START_CASH, "positions": {}, "closed": [], "seen_rejects": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def journal(line):
    if not JOURNAL_FILE.exists():
        JOURNAL_FILE.write_text("# Trade journal (paper)\n\n")
    with JOURNAL_FILE.open("a") as f:
        f.write(f"- `{stamp()}` {line}\n")
    print(line)


def candidate_addresses():
    addrs = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
        try:
            for item in get(path):
                if item.get("chainId") == "solana" and item["tokenAddress"] not in addrs:
                    addrs.append(item["tokenAddress"])
        except Exception as e:
            print(f"warn: {path}: {e}", file=sys.stderr)
    return addrs


def best_pairs(addresses):
    """Map token address -> its most liquid pair."""
    out = {}
    for i in range(0, len(addresses), 30):
        chunk = ",".join(addresses[i:i + 30])
        try:
            pairs = get(f"/tokens/v1/solana/{chunk}")
        except Exception as e:
            print(f"warn: pairs: {e}", file=sys.stderr)
            continue
        for p in pairs:
            addr = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            cur = out.get(addr)
            if cur is None or liq > ((cur.get("liquidity") or {}).get("usd") or 0):
                out[addr] = p
    return out


def check(pair):
    """Return (score, reasons_rejected). Empty reasons means it passed."""
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    vol = (pair.get("volume") or {}).get("h24") or 0
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    tx = (pair.get("txns") or {}).get("h24") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    h1 = (pair.get("priceChange") or {}).get("h1") or 0
    age_min = (now() * 1000 - (pair.get("pairCreatedAt") or now() * 1000)) / 60_000
    ratio = buys / max(sells, 1)

    fails = []
    if liq == 0:
        fails.append("no pool liquidity (bonding curve, can't exit)")
    if age_min < HARD["min_age_minutes"]:
        fails.append(f"too new ({age_min:.0f}m)")
    if age_min > HARD["max_age_hours"] * 60:
        fails.append("too old")
    if liq < HARD["min_liquidity_usd"]:
        fails.append(f"liquidity ${liq:,.0f}")
    if vol < HARD["min_volume_h24"]:
        fails.append(f"volume ${vol:,.0f}")
    if not HARD["min_mcap_usd"] <= mcap <= HARD["max_mcap_usd"]:
        fails.append(f"mcap ${mcap:,.0f}")
    if buys + sells < HARD["min_trades_h24"]:
        fails.append(f"{buys + sells} trades")
    if h1 > HARD["max_h1_change_pct"]:
        fails.append(f"already pumped {h1:.0f}% 1h")
    if ratio < HARD["min_buy_sell_ratio"]:
        fails.append(f"buy/sell {ratio:.2f}")

    # Soft score in [0, 1].
    info = pair.get("info") or {}
    socials = len(info.get("socials") or []) + len(info.get("websites") or [])
    s_liq = min(liq / max(mcap, 1) / 0.3, 1)          # deep pool relative to mcap
    s_flow = min(max((ratio - 0.8) / 1.2, 0), 1)       # buyers outnumber sellers
    s_turn = min(vol / max(liq, 1) / 10, 1)             # real trading activity
    s_mom = 1 - min(max(h1, 0) / HARD["max_h1_change_pct"], 1)
    s_soc = min(socials / 2, 1)
    score = 0.25 * s_liq + 0.25 * s_flow + 0.2 * s_turn + 0.15 * s_mom + 0.15 * s_soc
    return score, fails


def fill_cost(size_usd, liq_usd):
    """Fraction lost to fees + slippage on one side of a trade."""
    impact = size_usd / max(liq_usd, 1)
    return SWAP_FEE + BASE_SLIPPAGE + impact


def equity(state, prices):
    eq = state["cash"]
    for addr, pos in state["positions"].items():
        px = prices.get(addr, pos["entry_px"])
        eq += pos["qty"] * px
    return eq


def manage_positions(state, pairs):
    for addr, pos in list(state["positions"].items()):
        pair = pairs.get(addr)
        if pair is None:
            try:
                pair = best_pairs([addr]).get(addr)
            except Exception:
                pair = None
        if pair is None:
            continue
        px = float(pair.get("priceUsd") or 0)
        liq = (pair.get("liquidity") or {}).get("usd") or 0
        pnl_pct = (px / pos["entry_px"] - 1) * 100 if pos["entry_px"] else -100
        held_h = (now() - pos["opened"]) / 3600

        reason = None
        if liq < pos["entry_liq"] * RISK["rug_liquidity_drop"]:
            reason = "liquidity pulled (rug?)"
        elif pnl_pct >= RISK["take_profit_pct"]:
            reason = "take profit"
        elif pnl_pct <= RISK["stop_loss_pct"]:
            reason = "stop loss"
        elif held_h >= RISK["time_stop_hours"]:
            reason = "time stop"
        if not reason:
            continue

        gross = pos["qty"] * px
        proceeds = max(gross * (1 - fill_cost(gross, liq)) - NETWORK_FEE_USD, 0)
        state["cash"] += proceeds
        pnl = proceeds - pos["cost"]
        state["closed"].append({"symbol": pos["symbol"], "pnl": pnl, "reason": reason})
        del state["positions"][addr]
        journal(f"SELL **{pos['symbol']}** ({reason}) price {pnl_pct:+.1f}%, "
                f"net ${pnl:+.2f} after fees. Cash ${state['cash']:.2f}")


def open_positions(state, pairs):
    ranked = []
    for addr, pair in pairs.items():
        if addr in state["positions"]:
            continue
        score, fails = check(pair)
        if fails:
            state["seen_rejects"] += 1
            continue
        if score >= RISK["min_score"]:
            ranked.append((score, addr, pair))
    ranked.sort(reverse=True)

    for score, addr, pair in ranked:
        if len(state["positions"]) >= RISK["max_open"]:
            break
        prices = {a: float(p.get("priceUsd") or 0) for a, p in pairs.items()}
        size = min(equity(state, prices) * RISK["position_frac"], state["cash"])
        if size < 1:
            break
        liq = pair["liquidity"]["usd"]
        px = float(pair["priceUsd"])
        spend = size - NETWORK_FEE_USD
        qty = spend * (1 - fill_cost(spend, liq)) / px
        state["cash"] -= size
        state["positions"][addr] = {
            "symbol": pair["baseToken"]["symbol"], "qty": qty, "entry_px": px,
            "cost": size, "entry_liq": liq, "opened": now(), "url": pair.get("url"),
        }
        journal(f"BUY **{pair['baseToken']['symbol']}** ${size:.2f} @ {px:.8g} "
                f"(score {score:.2f}, liq ${liq:,.0f}, mcap ${pair.get('marketCap') or 0:,.0f}) "
                f"{pair.get('url')}")


def report(state, pairs=None):
    prices = {a: float(p.get("priceUsd") or 0) for a, p in (pairs or {}).items()}
    eq = equity(state, prices)
    wins = sum(1 for c in state["closed"] if c["pnl"] > 0)
    n = len(state["closed"])
    print(f"\nEquity ${eq:.2f} (started ${START_CASH:.2f}, {(eq / START_CASH - 1) * 100:+.1f}%)")
    print(f"Cash ${state['cash']:.2f} | open {len(state['positions'])} | "
          f"closed {n} ({wins} wins) | tokens rejected so far {state['seen_rejects']}")
    for pos in state["positions"].values():
        print(f"  holding {pos['symbol']}: cost ${pos['cost']:.2f}  {pos['url']}")


def tick(state):
    addrs = candidate_addresses() + list(state["positions"])
    pairs = best_pairs(list(dict.fromkeys(addrs)))
    manage_positions(state, pairs)
    open_positions(state, pairs)
    save_state(state)
    report(state, pairs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run a single scan")
    ap.add_argument("--interval", type=int, default=60, help="seconds between scans")
    ap.add_argument("--report", action="store_true", help="print the scoreboard and exit")
    args = ap.parse_args()

    state = load_state()
    if args.report:
        report(state)
        return
    while True:
        tick(state)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
