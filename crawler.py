#!/usr/bin/env python3
"""Paper-trading memecoin desk for Solana, v2.

Four bots trade side by side, each with its own paper $10:

  steady  strict filters, RugCheck holder checks, small bets, losing-streak brake
  degen   young coins, big bets, big targets, a trailing stop to ride pumps
  base    the steady rules on Base instead of Solana, with GoPlus rug checks
  jev     the steady rules, plus the Jev AI model must rate the coin a likely
          winner and unlikely rug. Needs AI_GATEWAY_API_KEY (Vercel), TYPESAFE_API_KEY,
          or CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN; skipped without them

No wallet, no keys, no real money: it only records what each bot *would* have
done, with realistic fees, so you can see which (if either) actually makes money.

    python3 crawler.py --once          # one scan + position check
    python3 crawler.py --report        # print both scoreboards
    python3 crawler.py --serve --sync  # server mode: exits checked every 3s,
                                       # trades pushed to GitHub for the dashboard
"""
import argparse
import csv
import gzip
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={}"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
CF_URL = "https://api.cloudflare.com/client/v4/accounts/{}/ai/run"
VERCEL_URL = "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
GECKO = "https://api.geckoterminal.com/api/v2/networks/{}/{}?page=1"
HERE = Path(__file__).resolve().parent
START_CASH = 10.0

# Cost model for tiny Solana swaps: DEX fee, slippage, network + priority fee.
SWAP_FEE = 0.01
BASE_SLIPPAGE = 0.01
NETWORK_FEE_USD = 0.02

STRATEGIES = {
    "steady": {
        "label": "Steady",
        "chain": "solana",
        "state": "steady-state.json",
        "journal": "steady-journal.md",
        "hard": {
            "min_age_minutes": 15,
            "max_age_hours": 72,
            "min_liquidity_usd": 12_000,
            "min_volume_h24": 45_000,
            "min_mcap_usd": 60_000,
            "max_mcap_usd": 5_000_000,
            "min_trades_h24": 150,
            "max_h1_change_pct": 300,   # momentum already spent
            "min_buy_sell_ratio": 0.8,  # sellers swamping buyers
        },
        "rug": {
            "max_top10_pct": 35,        # top 10 wallets (pool excluded) own too much
            "max_insider_pct": 20,      # linked insider wallets own too much
            "min_lp_locked_pct": 80,    # liquidity can be pulled
            "allow_warn_only": True,    # "danger" risks always reject
        },
        "risk": {
            "position_frac": 0.15,      # of equity per trade
            "max_open": 3,
            "min_score": 0.6,
            "take_profit_pct": 50,
            "stop_loss_pct": -25,
            "trail_after_pct": None,
            "trail_drop_pct": None,
            "time_stop_hours": 6,
            "rug_liquidity_drop": 0.5,  # exit if liquidity falls by half
            "cooldown_hours": 24,       # don't rebuy a coin we just sold
            "brake_losses": 3,          # this many losses in a row...
            "brake_hours": 2,           # ...pauses buying this long
        },
    },
    "degen": {
        "label": "Degen",
        "chain": "solana",
        "state": "degen-state.json",
        "journal": "degen-journal.md",
        "hard": {
            "min_age_minutes": 3,
            "max_age_hours": 24,
            "min_liquidity_usd": 5_000,
            "min_volume_h24": 10_000,
            "min_mcap_usd": 15_000,
            "max_mcap_usd": 1_500_000,
            "min_trades_h24": 80,
            "max_h1_change_pct": 1_000,
            "min_buy_sell_ratio": 1.0,
        },
        "rug": {
            # Only the outright traps: mint/freeze authority and "danger" flags.
            "max_top10_pct": None,
            "max_insider_pct": None,
            "min_lp_locked_pct": None,
            "allow_warn_only": True,
        },
        "risk": {
            "position_frac": 0.5,
            "max_open": 2,
            "min_score": 0.45,
            "take_profit_pct": 100,
            "stop_loss_pct": -35,
            "trail_after_pct": 60,      # once up 60%...
            "trail_drop_pct": 25,       # ...sell if it falls 25% from its peak
            "time_stop_hours": 3,
            "rug_liquidity_drop": 0.5,
            "cooldown_hours": 24,
            "brake_losses": None,
            "brake_hours": None,
        },
    },
    "jev": {
        "label": "Jev",
        "chain": "solana",
        "state": "jev-state.json",
        "journal": "jev-journal.md",
        "jev": {
            "min_pump": 0.6,            # Jev's chance it hits +50% before -25%
            "max_rug": 0.3,             # Jev's chance it's a rug or insider dump
        },
    },
    "base": {
        "label": "Base",
        "chain": "base",
        "state": "base-state.json",
        "journal": "base-journal.md",
        "hard": {
            "min_age_minutes": 15,
            "max_age_hours": 72,
            "min_liquidity_usd": 10_000,
            "min_volume_h24": 25_000,
            "min_mcap_usd": 50_000,
            "max_mcap_usd": 5_000_000,
            "min_trades_h24": 100,
            "max_h1_change_pct": 300,
            "min_buy_sell_ratio": 0.8,
        },
        "rug": {
            "max_top10_pct": 35,        # top 10 wallets (contracts/pools excluded)
            "max_creator_pct": 10,      # creator still holds a big bag
            "max_tax_pct": 10,          # buy or sell tax
        },
        "risk": {
            "position_frac": 0.15,
            "max_open": 3,
            "min_score": 0.5,           # Base coins rarely list socials on DexScreener
            "take_profit_pct": 50,
            "stop_loss_pct": -25,
            "trail_after_pct": None,
            "trail_drop_pct": None,
            "time_stop_hours": 6,
            "rug_liquidity_drop": 0.5,
            "cooldown_hours": 24,
            "brake_losses": 3,
            "brake_hours": 2,
        },
    },
}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-crawler/2.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def now():
    return time.time()


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# The Jev bot trades the steady rules with an extra Jev gate.
STRATEGIES["jev"] = {**STRATEGIES["steady"], **STRATEGIES["jev"]}


class Bot:
    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.chain = cfg["chain"]
        self.hard, self.rug, self.risk = cfg["hard"], cfg["rug"], cfg["risk"]
        self.state_file = HERE / cfg["state"]
        self.journal_file = HERE / cfg["journal"]
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text())
        else:
            self.state = {"cash": START_CASH, "positions": {}, "closed": [], "seen_rejects": 0}
        self.state.setdefault("paused_until", 0)

    def save(self):
        self.state_file.write_text(json.dumps(self.state, indent=2))

    def journal(self, line):
        if not self.journal_file.exists():
            self.journal_file.write_text(f"# {self.cfg['label']} bot trade journal (paper)\n\n")
        with self.journal_file.open("a") as f:
            f.write(f"- `{stamp()}` {line}\n")
        print(f"[{self.name}] {line}")

    def equity(self, prices):
        return self.state["cash"] + sum(
            p["qty"] * prices.get(a, p["entry_px"]) for a, p in self.state["positions"].items())

    def check(self, pair):
        """Return (score, reasons_rejected). Empty reasons means it passed."""
        h = self.hard
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
        if age_min < h["min_age_minutes"]:
            fails.append(f"too new ({age_min:.0f}m)")
        if age_min > h["max_age_hours"] * 60:
            fails.append("too old")
        if liq < h["min_liquidity_usd"]:
            fails.append(f"liquidity ${liq:,.0f}")
        if vol < h["min_volume_h24"]:
            fails.append(f"volume ${vol:,.0f}")
        if not h["min_mcap_usd"] <= mcap <= h["max_mcap_usd"]:
            fails.append(f"mcap ${mcap:,.0f}")
        if buys + sells < h["min_trades_h24"]:
            fails.append(f"{buys + sells} trades")
        if h1 > h["max_h1_change_pct"]:
            fails.append(f"already pumped {h1:.0f}% 1h")
        if ratio < h["min_buy_sell_ratio"]:
            fails.append(f"buy/sell {ratio:.2f}")

        info = pair.get("info") or {}
        socials = len(info.get("socials") or []) + len(info.get("websites") or [])
        s_liq = min(liq / max(mcap, 1) / 0.3, 1)            # deep pool relative to mcap
        s_flow = min(max((ratio - 0.8) / 1.2, 0), 1)         # buyers outnumber sellers
        s_turn = min(vol / max(liq, 1) / 10, 1)              # real trading activity
        s_mom = 1 - min(max(h1, 0) / h["max_h1_change_pct"], 1)
        s_soc = min(socials / 2, 1)
        score = 0.25 * s_liq + 0.25 * s_flow + 0.2 * s_turn + 0.15 * s_mom + 0.15 * s_soc
        return score, fails

    def rug_fails(self, report):
        """Reasons the rug check rules this coin out."""
        if self.chain == "base":
            return self.goplus_fails(report)
        if report is None:
            return ["rugcheck unavailable"]
        r, fails = self.rug, []
        if report.get("mintAuthority"):
            fails.append("creator can mint more")
        if report.get("freezeAuthority"):
            fails.append("creator can freeze wallets")
        if report.get("rugged"):
            fails.append("already rugged")
        for risk in report.get("risks") or []:
            if risk.get("level") == "danger":
                fails.append(risk.get("name", "danger"))
        pools = {m.get("pubkey") for m in report.get("markets") or []}
        holders = [x for x in report.get("topHolders") or [] if x.get("owner") not in pools]
        if r["max_top10_pct"] is not None:
            top10 = sum(x.get("pct", 0) for x in holders[:10])
            if top10 > r["max_top10_pct"]:
                fails.append(f"top 10 wallets own {top10:.0f}%")
        if r["max_insider_pct"] is not None:
            ins = sum(x.get("pct", 0) for x in holders if x.get("insider"))
            if ins > r["max_insider_pct"]:
                fails.append(f"insiders own {ins:.0f}%")
        if r["min_lp_locked_pct"] is not None:
            markets = sorted(report.get("markets") or [],
                             key=lambda m: (m.get("lp") or {}).get("quoteUSD") or 0, reverse=True)
            locked = ((markets[0].get("lp") or {}).get("lpLockedPct") or 0) if markets else 0
            if locked < r["min_lp_locked_pct"]:
                fails.append(f"only {locked:.0f}% of liquidity locked")
        return fails

    def goplus_fails(self, r):
        if r is None:
            return ["goplus unavailable"]
        flag = lambda k: str(r.get(k)) == "1"
        fails = [msg for k, msg in (
            ("is_honeypot", "honeypot: can't sell"), ("cannot_sell_all", "can't sell all"),
            ("is_mintable", "creator can mint more"), ("hidden_owner", "hidden owner"),
            ("can_take_back_ownership", "ownership can be reclaimed"),
            ("owner_change_balance", "owner can change balances"),
            ("transfer_pausable", "trading can be paused"), ("is_blacklisted", "has a blacklist"),
            ("slippage_modifiable", "tax can be changed"),
        ) if flag(k)]
        if str(r.get("is_open_source")) == "0":
            fails.append("contract not verified")
        for k in ("buy_tax", "sell_tax"):
            tax = float(r.get(k) or 0) * 100
            if tax > self.rug["max_tax_pct"]:
                fails.append(f"{k.replace('_', ' ')} {tax:.0f}%")
        holders = [h for h in r.get("holders") or [] if str(h.get("is_contract")) != "1"]
        top10 = sum(float(h.get("percent") or 0) for h in holders[:10]) * 100
        if top10 > self.rug["max_top10_pct"]:
            fails.append(f"top 10 wallets own {top10:.0f}%")
        creator = float(r.get("creator_percent") or 0) * 100
        if creator > self.rug["max_creator_pct"]:
            fails.append(f"creator holds {creator:.0f}%")
        return fails

    def manage(self, pairs):
        r = self.risk
        for addr, pos in list(self.state["positions"].items()):
            pair = pairs.get(addr)
            if pair is None:
                continue
            px = float(pair.get("priceUsd") or 0)
            liq = (pair.get("liquidity") or {}).get("usd") or 0
            pnl_pct = (px / pos["entry_px"] - 1) * 100 if pos["entry_px"] else -100
            pos["peak_pct"] = max(pos.get("peak_pct", 0), pnl_pct)
            held_h = (now() - pos["opened"]) / 3600

            reason = None
            if liq < pos["entry_liq"] * r["rug_liquidity_drop"]:
                reason = "liquidity pulled (rug?)"
            elif pnl_pct >= r["take_profit_pct"]:
                reason = "take profit"
            elif (r["trail_after_pct"] is not None and pos["peak_pct"] >= r["trail_after_pct"]
                  and (1 + pnl_pct / 100) <= (1 + pos["peak_pct"] / 100) * (1 - r["trail_drop_pct"] / 100)):
                reason = f"trailing stop (peak +{pos['peak_pct']:.0f}%)"
            elif pnl_pct <= r["stop_loss_pct"]:
                reason = "stop loss"
            elif held_h >= r["time_stop_hours"]:
                reason = "time stop"
            if not reason:
                continue

            gross = pos["qty"] * px
            proceeds = max(gross * (1 - fill_cost(gross, liq)) - NETWORK_FEE_USD, 0)
            self.state["cash"] += proceeds
            pnl = proceeds - pos["cost"]
            self.state["closed"].append({"symbol": pos["symbol"], "pnl": pnl, "reason": reason,
                                         "addr": addr, "at": now()})
            del self.state["positions"][addr]
            self.journal(f"SELL **{pos['symbol']}** ({reason}) price {pnl_pct:+.1f}%, "
                         f"net ${pnl:+.2f} after fees. Cash ${self.state['cash']:.2f}")
            self.maybe_brake()

    def maybe_brake(self):
        n = self.risk["brake_losses"]
        if not n:
            return
        last = self.state["closed"][-n:]
        if len(last) == n and all(c["pnl"] < 0 for c in last) and \
                all(c.get("at", 0) > self.state["paused_until"] for c in last):
            self.state["paused_until"] = now() + self.risk["brake_hours"] * 3600
            self.journal(f"PAUSE {n} losses in a row, no new buys for {self.risk['brake_hours']}h")

    def open(self, pairs, rugcheck):
        r = self.risk
        if now() < self.state["paused_until"] or len(self.state["positions"]) >= r["max_open"]:
            return
        cutoff = now() - r["cooldown_hours"] * 3600
        recent = {c.get("addr") for c in self.state["closed"] if c.get("at", 0) > cutoff}
        ranked = []
        for addr, pair in pairs.items():
            if addr in self.state["positions"] or addr in recent:
                continue
            score, fails = self.check(pair)
            if fails or score < r["min_score"]:
                self.state["seen_rejects"] += 1
                continue
            ranked.append((score, addr, pair))
        ranked.sort(key=lambda x: x[0], reverse=True)

        prices = {a: float(p.get("priceUsd") or 0) for a, p in pairs.items()}
        for score, addr, pair in ranked:
            if len(self.state["positions"]) >= r["max_open"]:
                break
            report = rugcheck(addr)
            fails = self.rug_fails(report)
            note = ""
            if not fails and self.cfg.get("jev"):
                verdict = jev_verdict(addr, pair, report)
                if verdict is None:
                    fails = ["jev unavailable"]
                else:
                    pump, rug = verdict
                    note = f", jev pump {pump:.2f} rug {rug:.2f}"
                    if pump < self.cfg["jev"]["min_pump"]:
                        fails.append(f"jev pump {pump:.2f}")
                    if rug > self.cfg["jev"]["max_rug"]:
                        fails.append(f"jev rug {rug:.2f}")
            if fails:
                self.state["seen_rejects"] += 1
                print(f"[{self.name}] skip {pair['baseToken']['symbol']}: {', '.join(fails)}")
                continue
            size = min(self.equity(prices) * r["position_frac"], self.state["cash"])
            if size < 1:
                break
            liq = pair["liquidity"]["usd"]
            px = float(pair["priceUsd"])
            spend = size - NETWORK_FEE_USD
            qty = spend * (1 - fill_cost(spend, liq)) / px
            self.state["cash"] -= size
            self.state["positions"][addr] = {
                "symbol": pair["baseToken"]["symbol"], "qty": qty, "entry_px": px,
                "cost": size, "entry_liq": liq, "opened": now(), "url": pair.get("url"),
            }
            self.journal(f"BUY **{pair['baseToken']['symbol']}** ${size:.2f} @ {px:.8g} "
                         f"(score {score:.2f}{note}, liq ${liq:,.0f}, mcap ${pair.get('marketCap') or 0:,.0f}) "
                         f"{pair.get('url')}")

    def report(self, pairs=None):
        prices = {a: float(p.get("priceUsd") or 0) for a, p in (pairs or {}).items()}
        eq = self.equity(prices)
        closed = self.state["closed"]
        wins = sum(1 for c in closed if c["pnl"] > 0)
        paused = " | PAUSED" if now() < self.state["paused_until"] else ""
        print(f"[{self.name}] equity ${eq:.2f} ({(eq / START_CASH - 1) * 100:+.1f}%) | "
              f"cash ${self.state['cash']:.2f} | open {len(self.state['positions'])} | "
              f"closed {len(closed)} ({wins} wins){paused}")


def fill_cost(size_usd, liq_usd):
    """Fraction lost to fees + slippage on one side of a trade."""
    return SWAP_FEE + BASE_SLIPPAGE + size_usd / max(liq_usd, 1)


def candidate_addresses():
    addrs = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
        try:
            for item in get(API + path):
                if item.get("chainId") == "solana" and item["tokenAddress"] not in addrs:
                    addrs.append(item["tokenAddress"])
        except Exception as e:
            print(f"warn: {path}: {e}", file=sys.stderr)
    return addrs


def gecko_candidates(network):
    """New and trending tokens on a network from GeckoTerminal."""
    addrs = []
    for i, kind in enumerate(("new_pools", "trending_pools")):
        if i:
            time.sleep(1.5)            # GeckoTerminal's free API allows only a few calls a minute
        try:
            try:
                data = get(GECKO.format(network, kind))["data"]
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise
                time.sleep(5)          # rate limited: wait once, then retry
                data = get(GECKO.format(network, kind))["data"]
            for pool in data:
                a = pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                if a not in addrs:
                    addrs.append(a)
        except Exception as e:
            print(f"warn: gecko {network} {kind}: {e}", file=sys.stderr)
    return addrs


def base_candidates():
    """Base tokens for the bots (DexScreener's feeds rarely list Base)."""
    return gecko_candidates("base")


def best_pairs(addresses, chain="solana"):
    """Map token address -> its most liquid pair."""
    out = {}
    for i in range(0, len(addresses), 30):
        chunk = ",".join(addresses[i:i + 30])
        try:
            pairs = get(f"{API}/tokens/v1/{chain}/{chunk}")
        except Exception as e:
            print(f"warn: pairs: {e}", file=sys.stderr)
            continue
        for p in pairs:
            addr = p["baseToken"]["address"]
            if chain != "solana":
                addr = addr.lower()
            liq = (p.get("liquidity") or {}).get("usd") or 0
            cur = out.get(addr)
            if cur is None or liq > ((cur.get("liquidity") or {}).get("usd") or 0):
                out[addr] = p
    return out


_rug_cache = {}


def rugcheck(addr):
    """RugCheck report for a token, cached for 15 minutes. None if unavailable."""
    hit = _rug_cache.get(addr)
    if hit and now() - hit[0] < 900:
        return hit[1]
    try:
        report = get(RUGCHECK.format(addr))
    except Exception as e:
        print(f"warn: rugcheck {addr[:6]}: {e}", file=sys.stderr)
        report = None
    _rug_cache[addr] = (now(), report)
    return report


def goplus(addr):
    """GoPlus security report for a Base token, cached for 15 minutes. None if unavailable."""
    hit = _rug_cache.get(addr)
    if hit and now() - hit[0] < 900:
        return hit[1]
    try:
        res = get(GOPLUS.format(addr)).get("result") or {}
        report = res.get(addr.lower()) or (next(iter(res.values())) if res else None)
    except Exception as e:
        print(f"warn: goplus {addr[:8]}: {e}", file=sys.stderr)
        report = None
    _rug_cache[addr] = (now(), report)
    return report


_jev_cache = {}


def jev_configured():
    return bool(os.environ.get("AI_GATEWAY_API_KEY") or os.environ.get("TYPESAFE_API_KEY") or
                (os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_API_TOKEN")))


def jev_verdict(addr, pair, report):
    """Ask Jev for (pump, rug) probabilities, cached 30 minutes. None if unavailable."""
    if not jev_configured():
        return None
    hit = _jev_cache.get(addr)
    if hit and now() - hit[0] < 1800:
        return hit[1]
    tx = (pair.get("txns") or {})
    pools = {m.get("pubkey") for m in (report or {}).get("markets") or []}
    holders = [x for x in (report or {}).get("topHolders") or [] if x.get("owner") not in pools]
    state = {
        "chain": "solana",
        "symbol": pair["baseToken"]["symbol"],
        "name": pair["baseToken"].get("name"),
        "age_minutes": round((now() * 1000 - (pair.get("pairCreatedAt") or 0)) / 60_000),
        "price_usd": pair.get("priceUsd"),
        "market_cap_usd": pair.get("marketCap"),
        "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
        "volume_usd": pair.get("volume"),
        "price_change_pct": pair.get("priceChange"),
        "buys_sells": {k: tx.get(k) for k in ("m5", "h1", "h6", "h24")},
        "socials": [x.get("type") for x in (pair.get("info") or {}).get("socials") or []],
        "has_website": bool((pair.get("info") or {}).get("websites")),
        "top10_holders_pct": round(sum(x.get("pct", 0) for x in holders[:10]), 1),
        "insider_holders_pct": round(sum(x.get("pct", 0) for x in holders if x.get("insider")), 1),
        "liquidity_locked_pct": (report or {}).get("lpLockedPct"),
        "rugcheck_risks": [r.get("name") for r in (report or {}).get("risks") or []],
    }
    body = {
        "model": "jev-latest",
        "state": state,
        "questions": {
            "pump": {"type": "noul", "instructions":
                     "This is a Solana memecoin. Will its price rise at least 50% before it falls 25%, "
                     "within the next 6 hours?"},
            "rug": {"type": "noul", "instructions":
                    "Is this memecoin likely to be rugged or dumped by insiders within the next 6 hours?"},
        },
    }
    if os.environ.get("AI_GATEWAY_API_KEY"):   # Vercel AI Gateway, same API as TypeSafe's
        url, key = VERCEL_URL, os.environ["AI_GATEWAY_API_KEY"]
        body = {**body, "model": "typesafe-ai/jev"}
    elif os.environ.get("TYPESAFE_API_KEY"):
        url, key = JEV_URL, os.environ["TYPESAFE_API_KEY"]
    else:   # same model through Cloudflare Workers AI
        url, key = CF_URL.format(os.environ["CLOUDFLARE_ACCOUNT_ID"]), os.environ["CLOUDFLARE_API_TOKEN"]
        body = {"model": "typesafe/jev", "input": {"state": body["state"], "questions": body["questions"]}}
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "User-Agent": "paper-crawler/2.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        answers = (data.get("result") or data)["answers"]
        verdict = (float(answers["pump"]["noul"]), float(answers["rug"]["noul"]))
    except Exception as e:
        print(f"warn: jev {addr[:6]}: {e}", file=sys.stderr)
        verdict = None
    _jev_cache[addr] = (now(), verdict)
    return verdict


FEEDS = {"solana": candidate_addresses, "base": base_candidates}
CHECKERS = {"solana": rugcheck, "base": goplus}


def held(bots, chain=None):
    return list(dict.fromkeys(a for b in bots if chain in (None, b.chain) for a in b.state["positions"]))


def chains(bots):
    return list(dict.fromkeys(b.chain for b in bots))


class Recorder:
    """Logs a price snapshot of every coin the bots see, each scan, for 6 hours after
    first sighting. Hourly gzip files under research/ let us backtest rule changes on
    thousands of would-be trades instead of a few dozen real ones."""
    FIELDS = ["ts", "chain", "addr", "symbol", "price", "liq", "mcap", "vol_h1", "vol_h24",
              "buys_m5", "sells_m5", "buys_h1", "sells_h1", "chg_m5", "chg_h1", "chg_h6",
              "age_min", "socials"]
    WATCH_HOURS = 6
    MIN_LIQ = 5_000
    MAX_WATCH = 400            # per chain; newest coins win
    EVERY = 115                # seconds between snapshots

    def __init__(self):
        self.dir = HERE / "research"
        self.watch = {}            # addr -> (chain, first_seen)
        self.rows, self.hour = [], None
        self.done = []             # finished hourly files waiting to be committed
        self.last = {}             # chain -> time of last snapshot

    def due(self, chain):
        return now() - self.last.get(chain, 0) >= self.EVERY

    def add(self, chain, pairs):
        for a, p in pairs.items():
            if ((p.get("liquidity") or {}).get("usd") or 0) >= self.MIN_LIQ:
                self.watch.setdefault(a, (chain, now()))
        mine = [kv for kv in self.watch.items() if kv[1][0] == chain]
        if len(mine) > self.MAX_WATCH:     # cap per chain, so one chain can't crowd out another
            for a, _ in sorted(mine, key=lambda kv: kv[1][1])[:-self.MAX_WATCH]:
                del self.watch[a]

    def record(self, chain, pairs):
        cutoff = now() - self.WATCH_HOURS * 3600
        for a in [a for a, (c, t) in self.watch.items() if t < cutoff]:
            del self.watch[a]
        self.last[chain] = now()
        ts = int(now())
        for a, (c, _) in self.watch.items():
            p = pairs.get(a)
            if c != chain or p is None:
                continue
            tx, pc, vol = p.get("txns") or {}, p.get("priceChange") or {}, p.get("volume") or {}
            info = p.get("info") or {}
            self.rows.append([
                ts, chain, a, p["baseToken"]["symbol"], p.get("priceUsd"),
                (p.get("liquidity") or {}).get("usd"), p.get("marketCap") or p.get("fdv"),
                vol.get("h1"), vol.get("h24"),
                (tx.get("m5") or {}).get("buys"), (tx.get("m5") or {}).get("sells"),
                (tx.get("h1") or {}).get("buys"), (tx.get("h1") or {}).get("sells"),
                pc.get("m5"), pc.get("h1"), pc.get("h6"),
                round((now() * 1000 - (p.get("pairCreatedAt") or now() * 1000)) / 60_000),
                len(info.get("socials") or []) + len(info.get("websites") or []),
            ])

    def flush(self, force=False):
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%d/%H")
        if self.hour is None:
            self.hour = hour
        if (hour == self.hour and not force) or not self.rows:
            self.hour = hour
            return
        day, hh = self.hour.split("/")
        path = self.dir / day / f"{hh}.csv.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        w = csv.writer(buf)
        if not path.exists():
            w.writerow(self.FIELDS)
        w.writerows(self.rows)
        with gzip.open(path, "at") as f:   # appending adds a gzip member; readers handle it
            f.write(buf.getvalue())
        self.done.append(path)
        self.rows, self.hour = [], hour


RECORDER = Recorder()
_last_extra = [0.0]


def tick(bots):
    for chain in chains(bots):
        found = [a.lower() for a in FEEDS[chain]()] if chain != "solana" else FEEDS[chain]()
        pairs = best_pairs(list(dict.fromkeys(found + held(bots, chain))), chain)
        RECORDER.add(chain, pairs)
        if chain == "solana" and now() - _last_extra[0] >= 180:
            # Research only: GeckoTerminal's Solana lists widen the recorded sample.
            # The bots never trade these, so their results stay comparable.
            # Every 3 minutes, to stay inside GeckoTerminal's rate limit beside the Base feed.
            _last_extra[0] = now()
            more = [a for a in gecko_candidates("solana") if a not in pairs and a not in RECORDER.watch]
            if more:
                RECORDER.add(chain, best_pairs(more, chain))
        # Base's feed only lists pools minutes old, gone before they pass min_age, so the
        # Base bot also re-checks coins the recorder is following.
        if RECORDER.due(chain) or chain == "base":
            extra = [a for a, (c, _) in RECORDER.watch.items() if c == chain and a not in pairs]
            seen = {**best_pairs(extra, chain), **pairs} if extra else pairs
            if RECORDER.due(chain):
                RECORDER.record(chain, seen)
            if chain == "base":
                pairs = seen
        for b in bots:
            if b.chain != chain:
                continue
            b.manage(pairs)
            b.open(pairs, CHECKERS[chain])
            b.save()
            b.report(pairs)


def sync(reason):
    """Commit state + journals and push, so the dashboard sees them."""
    def git(*a):
        return subprocess.run(["git", "-C", str(HERE), *a], capture_output=True, text=True, timeout=60)
    files = [f for cfg in STRATEGIES.values() for f in (cfg["state"], cfg["journal"]) if (HERE / f).exists()]
    files += [str(p.relative_to(HERE)) for p in RECORDER.done if p.exists()]
    git("add", *files)
    RECORDER.done.clear()
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-qm", f"crawler: {reason} {datetime.now(timezone.utc):%H:%M}")
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    for _ in range(3):
        # On a conflict this bot's own state wins (-X theirs = the commits being replayed).
        ok = (git("fetch", "-q", "origin", branch).returncode == 0 and
              git("rebase", "-q", "-X", "theirs", "--autostash", f"origin/{branch}").returncode == 0)
        if ok and git("push", "-q", "origin", f"HEAD:{branch}").returncode == 0:
            return
        git("rebase", "--abort")
        time.sleep(5)
    print("warn: push failed, will retry on next sync", file=sys.stderr)


def serve(bots, scan_every, exit_every, do_sync):
    """Scan for new coins every scan_every s; check held coins every exit_every s."""
    last_scan = last_sync = 0.0
    while True:
        count = lambda: sum(len(b.state["closed"]) + len(b.state["positions"]) for b in bots)
        before = count()
        try:
            if now() - last_scan >= scan_every:
                tick(bots)
                last_scan = now()
            else:
                for chain in chains(bots):
                    if not held(bots, chain):
                        continue
                    pairs = best_pairs(held(bots, chain), chain)
                    for b in bots:
                        if b.chain == chain:
                            b.manage(pairs)
                            b.save()
        except Exception as e:
            print(f"warn: {e}", file=sys.stderr)
        RECORDER.flush()
        traded = count() != before
        if do_sync and (traded or now() - last_sync >= 600):
            try:
                sync("trade" if traded else "scan")
            except Exception as e:
                print(f"warn: sync: {e}", file=sys.stderr)
            last_sync = now()
        time.sleep(exit_every)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run a single scan")
    ap.add_argument("--interval", type=int, default=60, help="seconds between scans")
    ap.add_argument("--report", action="store_true", help="print the scoreboards and exit")
    ap.add_argument("--serve", action="store_true", help="run nonstop with fast exit checks")
    ap.add_argument("--exit-every", type=float, default=3, help="seconds between exit checks in --serve")
    ap.add_argument("--sync", action="store_true", help="push trades to GitHub in --serve")
    ap.add_argument("--only", choices=sorted(STRATEGIES), help="run just one bot")
    args = ap.parse_args()

    names = [n for n in STRATEGIES if not args.only or n == args.only]
    if "jev" in names and not jev_configured() and not args.report:
        print("note: no Jev key set, the Jev bot is off", file=sys.stderr)
        names.remove("jev")
    bots = [Bot(n, STRATEGIES[n]) for n in names]
    if args.report:
        for b in bots:
            b.report()
        return
    if args.serve:
        serve(bots, args.interval, args.exit_every, args.sync)
        return
    while True:
        tick(bots)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
