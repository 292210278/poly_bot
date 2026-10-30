#!/usr/bin/env python3
"""Local paper trader for Polymarket BTC 5-minute Up/Down markets.

Only public Gamma metadata and the public CLOB market WebSocket are used.
No API key, wallet, signer, or real order endpoint is used.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
GAMMA = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ZERO = Decimal("0")
FEE_RATE = Decimal("0.07")  # Polymarket crypto taker fee rate; override with --fee-rate.


class Tee:
    """Write console output to both the terminal and the persistent run log."""

    def __init__(self, console: Any, log_file: Any):
        self.console = console
        self.log_file = log_file

    def write(self, text: str) -> int:
        self.console.write(text)
        self.log_file.write(text)
        self.log_file.flush()
        return len(text)

    def flush(self) -> None:
        self.console.flush()
        self.log_file.flush()


def D(value: Any, fallback: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(fallback)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def gamma_json(url: str) -> Any:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "btc5m-local-paper-trader/1.0"})
    try:
        with urlopen(req, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"Polymarket Gamma HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Polymarket Gamma connection failed: {exc}") from exc


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except json.JSONDecodeError:
            return []
    return []


def best(levels: dict[Decimal, Decimal], side: str) -> Decimal | None:
    if not levels:
        return None
    return max(levels) if side == "bid" else min(levels)


def parse_market(event: Any, slug: str) -> dict[str, Any] | None:
    if isinstance(event, list):
        event = event[0] if event else None
    if not isinstance(event, dict):
        return None
    markets = event.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    outcomes = [str(x) for x in json_list(market.get("outcomes"))]
    if not outcomes:
        outcomes = [str(x) for x in event.get("outcomes", [])]
    ids = json_list(market.get("clobTokenIds")) or json_list(market.get("positionIds"))
    if not ids:
        ids = json_list(market.get("clob_token_ids")) or json_list(market.get("position_ids"))
    if len(ids) != len(outcomes) or len(outcomes) < 2:
        return None
    by_label = {label.strip().lower(): str(asset_id) for label, asset_id in zip(outcomes, ids)}
    if "up" not in by_label or "down" not in by_label:
        return None
    try:
        start_ts = int(slug.rsplit("-", 1)[1])
    except (ValueError, IndexError):
        return None
    return {
        "slug": slug,
        "market_id": str(market.get("conditionId") or market.get("condition_id") or ""),
        "start_ts": start_ts,
        "end_ts": start_ts + 300,
        "active": market.get("active", event.get("active", True)) is not False,
        "closed": market.get("closed", event.get("closed", False)) is True,
        "fees_enabled": market.get("feesEnabled", market.get("fees_enabled", True)) is not False,
        "assets": {"Up": by_label["up"], "Down": by_label["down"]},
        "min_order_size": D(market.get("orderMinSize") or market.get("minimumOrderSize") or "5"),
    }


class PnLTracker:
    """Dedicated profit-and-loss ledger written to pnl.log.

    Entry and exit records are written separately from runtime diagnostics.
    A running summary is maintained in pnl_summary.json for quick inspection.
    """

    def __init__(self, log_path: Path, summary_path: Path):
        self.log_path = log_path
        self.summary_path = summary_path
        self.trades: list[dict[str, Any]] = []
        self._load_summary()

    def _load_summary(self) -> None:
        if not self.summary_path.exists():
            return
        try:
            data = json.loads(self.summary_path.read_text(encoding="utf-8"))
            self.trades = data.get("trades", [])
        except (OSError, json.JSONDecodeError):
            pass

    def record_entry(self, *, stamp: str, market_slug: str, outcome: str,
                     quantity: Decimal, avg_price: Decimal, fees: Decimal,
                     target: Decimal, stop: Decimal, cash_after: Decimal) -> None:
        line = (f"[{stamp}] BUY  {market_slug} {outcome} | "
                f"qty={quantity:.4f} @ {avg_price:.4f} | "
                f"fees=${fees:.4f} | target={target:.4f} stop={stop:.4f} | "
                f"cash=${cash_after:.2f}\n")
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line)

    def record_exit(self, *, stamp: str, market_slug: str, outcome: str,
                    reason: str, quantity: Decimal, entry_price: Decimal,
                    exit_price: Decimal, entry_fees: Decimal, exit_fees: Decimal,
                    pnl: Decimal, position_age_s: Decimal,
                    cash_after: Decimal, cumulative_pnl: Decimal) -> None:
        result = "WIN" if pnl > ZERO else "LOSS" if pnl < ZERO else "FLAT"
        total_fees = entry_fees + exit_fees
        line = (f"[{stamp}] SELL {market_slug} {outcome} | "
                f"reason={reason} | qty={quantity:.4f} | "
                f"entry={entry_price:.4f} -> exit={exit_price:.4f} | "
                f"fees=${total_fees:.4f} | P&L=${pnl:+.4f} ({result}) | "
                f"hold={position_age_s:.1f}s | "
                f"cash=${cash_after:.2f} | cumulative=${cumulative_pnl:+.4f}\n")
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line)

        self.trades.append({
            "time": stamp, "market": market_slug, "outcome": outcome,
            "reason": reason, "entry": str(entry_price), "exit": str(exit_price),
            "qty": str(quantity), "pnl": str(pnl), "fees": str(total_fees),
            "hold_s": str(position_age_s),
        })
        self._save_summary(cash_after, cumulative_pnl)

    def _save_summary(self, cash: Decimal, cumulative_pnl: Decimal) -> None:
        wins = sum(1 for t in self.trades if D(t["pnl"]) > ZERO)
        losses = sum(1 for t in self.trades if D(t["pnl"]) < ZERO)
        total = len(self.trades)
        win_rate = f"{wins / total * 100:.1f}%" if total else "N/A"
        total_fees = sum(D(t["fees"]) for t in self.trades)
        ledger_pnl = sum(D(t["pnl"]) for t in self.trades)
        avg_pnl = ledger_pnl / total if total else ZERO
        best = max((D(t["pnl"]) for t in self.trades), default=ZERO)
        worst = min((D(t["pnl"]) for t in self.trades), default=ZERO)
        avg_hold = sum(D(t["hold_s"]) for t in self.trades) / total if total else ZERO
        summary = {
            "updated_at": utc_now().isoformat(),
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "cumulative_pnl": str(cumulative_pnl),
            "ledger_pnl": str(ledger_pnl),
            "avg_pnl_per_trade": str(avg_pnl.quantize(Decimal("0.0001"))),
            "best_trade": str(best),
            "worst_trade": str(worst),
            "total_fees": str(total_fees),
            "avg_hold_seconds": str(avg_hold.quantize(Decimal("0.1"))),
            "cash": str(cash),
            "trades": self.trades[-200:],
        }
        temp = self.summary_path.with_suffix(".tmp")
        temp.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.summary_path)


class LocalPaperBot:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.state_path = ROOT / "local_state.json"
        self.csv_path = ROOT / "local_trades.csv"
        self.pnl = PnLTracker(ROOT / "pnl.log", ROOT / "pnl_summary.json")
        self.markets: dict[str, dict[str, Any]] = {}
        self.asset_meta: dict[str, dict[str, Any]] = {}
        self.books: dict[str, dict[str, Any]] = {}
        self.entry_samples: dict[str, list[tuple[float, Decimal]]] = {}
        self.stale_logged: set[str] = set()
        self.used_markets: set[str] = set()
        self.cash = args.initial_cash
        self.realized_pnl = ZERO
        self.position: dict[str, Any] | None = None
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.cash = D(saved.get("cash"), str(self.args.initial_cash))
            self.realized_pnl = D(saved.get("realized_pnl"))
            self.used_markets = set(saved.get("used_markets", []))
            self.position = saved.get("position")
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read {self.state_path}: {exc}") from exc

    def save_state(self) -> None:
        data = {
            "cash": str(self.cash),
            "realized_pnl": str(self.realized_pnl),
            "used_markets": sorted(self.used_markets)[-500:],
            "position": self.position,
            "updated_at": utc_now().isoformat(),
        }
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.state_path)

    def log_trade(self, *, market: dict[str, Any], outcome: str, action: str, reason: str,
                  quantity: Decimal, avg_price: Decimal, gross: Decimal, fees: Decimal,
                  pnl: Decimal = ZERO, entry_price: Decimal | None = None,
                  target: Decimal | None = None, stop: Decimal | None = None,
                  trigger_mid: Decimal | None = None, trigger_bid: Decimal | None = None,
                  trigger_ask: Decimal | None = None, last_trade: Decimal | None = None,
                  book_age_ms: int | None = None, position_age_seconds: Decimal | None = None,
                  fill_levels: list[tuple[Decimal, Decimal]] | None = None) -> None:
        exists = self.csv_path.exists()
        fields = ["timestamp_utc", "market_slug", "market_id", "outcome", "action", "reason",
                  "quantity", "avg_price", "gross_usd", "fees_usd", "realized_pnl_usd",
                  "cash_usd", "equity_usd", "entry_price", "target_price", "stop_price",
                  "trigger_mid", "trigger_bid", "trigger_ask", "last_trade", "book_age_ms",
                  "position_age_seconds", "fill_levels"]
        marked = D(self.position.get("quantity")) * D(self.position.get("last_bid")) if self.position else ZERO
        row = {
            "timestamp_utc": utc_now().isoformat(), "market_slug": market["slug"],
            "market_id": market.get("market_id", ""), "outcome": outcome, "action": action,
            "reason": reason, "quantity": str(quantity), "avg_price": str(avg_price),
            "gross_usd": str(gross), "fees_usd": str(fees), "realized_pnl_usd": str(pnl),
            "cash_usd": str(self.cash), "equity_usd": str(self.cash + marked),
            "entry_price": str(entry_price or ""), "target_price": str(target or ""),
            "stop_price": str(stop or ""),
            "trigger_mid": str(trigger_mid or ""), "trigger_bid": str(trigger_bid or ""),
            "trigger_ask": str(trigger_ask or ""), "last_trade": str(last_trade or ""),
            "book_age_ms": "" if book_age_ms is None else str(book_age_ms),
            "position_age_seconds": str(position_age_seconds or ""),
            "fill_levels": "" if fill_levels is None else json.dumps(
                [{"price": str(price), "shares": str(shares)} for price, shares in fill_levels],
                separators=(",", ":")),
        }
        if exists:
            with self.csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                old_fields = reader.fieldnames or []
                old_rows = list(reader)
            expanded_fields = list(dict.fromkeys([*old_fields, *fields]))
            if expanded_fields != old_fields:
                temp = self.csv_path.with_suffix(".migrating")
                with temp.open("w", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.DictWriter(handle, fieldnames=expanded_fields)
                    writer.writeheader()
                    writer.writerows(old_rows)
                temp.replace(self.csv_path)
            fields = expanded_fields
        with self.csv_path.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def discover(self) -> list[str]:
        now_ts = int(time.time())
        start = now_ts // 300 * 300
        found: dict[str, dict[str, Any]] = {}
        for market_start in (start, start + 300):
            slug = f"btc-updown-5m-{market_start}"
            event = gamma_json(f"{GAMMA}/events/slug/{slug}")
            market = parse_market(event, slug)
            if not market or not market["active"] or market["closed"]:
                continue
            found[slug] = market
        self.markets = found
        self.asset_meta = {}
        for market in found.values():
            for outcome, asset_id in market["assets"].items():
                self.asset_meta[asset_id] = {"market": market, "outcome": outcome}
                self.books.setdefault(asset_id, {"bids": {}, "asks": {}, "last_mid": None, "last_bid": None, "last_ask": None, "last_trade": None, "updated": 0.0})
        return list(self.asset_meta)

    def apply_book(self, asset_id: str, bids: list[Any], asks: list[Any], last_trade: Any = None) -> None:
        book = self.books.setdefault(asset_id, {"bids": {}, "asks": {}, "last_mid": None, "last_bid": None, "last_ask": None, "updated": 0.0})
        book["bids"] = {D(level.get("price")): D(level.get("size")) for level in bids if isinstance(level, dict) and D(level.get("size")) > ZERO}
        book["asks"] = {D(level.get("price")): D(level.get("size")) for level in asks if isinstance(level, dict) and D(level.get("size")) > ZERO}
        if last_trade is not None:
            book["last_trade"] = D(last_trade)
        book["updated"] = time.monotonic()
        self.evaluate(asset_id)

    def apply_price_level(self, change: dict[str, Any]) -> None:
        asset_id = str(change.get("asset_id") or change.get("assetId") or "")
        if asset_id not in self.asset_meta:
            return
        book = self.books[asset_id]
        side = str(change.get("side", "")).upper()
        levels = book["bids"] if side == "BUY" else book["asks"] if side == "SELL" else None
        if levels is None:
            return
        price, size = D(change.get("price")), D(change.get("size"))
        if size <= ZERO:
            levels.pop(price, None)
        else:
            levels[price] = size
        book["updated"] = time.monotonic()
        # When present, BBO values are authoritative for the crossing check.
        if change.get("best_bid") is not None:
            book["last_bid"] = D(change["best_bid"])
        if change.get("best_ask") is not None:
            book["last_ask"] = D(change["best_ask"])
        self.evaluate(asset_id)

    def handle_message(self, message: Any) -> None:
        events = message if isinstance(message, list) else [message]
        for event in events:
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
            event_type = str(event.get("event_type") or event.get("type") or "").lower()
            if event_type == "book":
                asset_id = str(payload.get("asset_id") or payload.get("assetId") or "")
                if asset_id in self.asset_meta:
                    last_trade = payload.get("last_trade_price") or payload.get("lastTradePrice")
                    self.apply_book(asset_id, payload.get("bids", []), payload.get("asks", []), last_trade)
            elif event_type == "price_change":
                changes = payload.get("price_changes") or payload.get("priceChanges") or []
                for change in changes:
                    if isinstance(change, dict):
                        self.apply_price_level(change)
            elif event_type == "best_bid_ask":
                asset_id = str(payload.get("asset_id") or payload.get("assetId") or "")
                if asset_id in self.books:
                    book = self.books[asset_id]
                    book["last_bid"] = D(payload.get("best_bid") or payload.get("bestBid"))
                    book["last_ask"] = D(payload.get("best_ask") or payload.get("bestAsk"))
                    book["updated"] = time.monotonic()
                    self.evaluate(asset_id)
            elif event_type == "last_trade_price":
                asset_id = str(payload.get("asset_id") or payload.get("assetId") or "")
                if asset_id in self.books:
                    price = payload.get("price") or payload.get("last_trade_price") or payload.get("lastTradePrice")
                    if price is not None:
                        self.books[asset_id]["last_trade"] = D(price)

    def quote(self, asset_id: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        book = self.books[asset_id]
        bid = best(book["bids"], "bid") or book.get("last_bid")
        ask = best(book["asks"], "ask") or book.get("last_ask")
        if bid is None or ask is None or bid <= ZERO or ask <= ZERO or bid > ask:
            return bid, ask, None
        return bid, ask, (bid + ask) / Decimal("2")

    def taker_fee(self, qty: Decimal, price: Decimal, market: dict[str, Any]) -> Decimal:
        if not market.get("fees_enabled", True):
            return ZERO
        fee = qty * self.args.fee_rate * price * (Decimal("1") - price)
        return fee.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)

    def simulate_buy(self, asset_id: str, budget: Decimal, max_price: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        levels = self.books[asset_id]["asks"]
        remaining = budget
        qty_total = gross = fees = ZERO
        for price in sorted(levels):
            if price > max_price or price <= ZERO:
                continue
            available = levels[price]
            per_share = price + self.args.fee_rate * price * (Decimal("1") - price)
            take = min(available, remaining / per_share)
            if take <= ZERO:
                continue
            fee = self.taker_fee(take, price, self.asset_meta[asset_id]["market"])
            cost = take * price + fee
            qty_total += take
            gross += take * price
            fees += fee
            remaining -= cost
            if remaining <= Decimal("0.000001"):
                break
        avg = gross / qty_total if qty_total else ZERO
        return qty_total, avg, gross, fees

    def simulate_sell(self, asset_id: str, quantity: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal, list[tuple[Decimal, Decimal]]]:
        levels = self.books[asset_id]["bids"]
        remaining = quantity
        sold = gross = fees = ZERO
        fills: list[tuple[Decimal, Decimal]] = []
        market = self.asset_meta[asset_id]["market"]
        for price in sorted(levels, reverse=True):
            if price <= ZERO:
                continue
            take = min(levels[price], remaining)
            if take <= ZERO:
                continue
            sold += take
            gross += take * price
            fees += self.taker_fee(take, price, market)
            fills.append((price, take))
            remaining -= take
            if remaining <= Decimal("0.000001"):
                break
        avg = gross / sold if sold else ZERO
        return sold, avg, gross, fees, fills

    def entry_signal(self, asset_id: str, mid: Decimal,
                     bid: Decimal | None, ask: Decimal | None) -> bool:
        book = self.books[asset_id]
        book["last_mid"] = mid
        meta = self.asset_meta[asset_id]
        market_slug, outcome = meta["market"]["slug"], meta["outcome"]
        stamp = utc_now().isoformat(timespec="milliseconds")
        bid_text = f"{bid:.4f}" if bid is not None else "n/a"
        ask_text = f"{ask:.4f}" if ask is not None else "n/a"
        now = time.monotonic()
        samples = self.entry_samples.setdefault(asset_id, [])
        if not samples or now - samples[-1][0] >= 0.5:
            samples.append((now, mid))
        samples[:] = [(at, price) for at, price in samples if now - at <= 8.0]

        needed = self.args.confirm_up_ticks + 1
        if mid < Decimal("0.50") or mid > Decimal("0.55") or len(samples) < needed:
            return False
        window = samples[-needed:]
        prices = [price for _, price in window]
        recent_low = min(price for _, price in samples)
        gaps = [window[i][0] - window[i - 1][0] for i in range(1, len(window))]
        if (any(gap > 1.5 for gap in gaps)
                or any(prices[i] <= prices[i - 1] for i in range(1, len(prices)))
                or recent_low > Decimal("0.48")
                or prices[-1] - recent_low < Decimal("0.03")
                or any(samples[i][1] < samples[i - 1][1] - Decimal("0.01")
                       for i in range(1, len(samples)))):
            return False
        print(f"{stamp} {market_slug} {outcome} rising-entry signal: "
              f"start={prices[0]:.4f}, mid={mid:.4f}, bid={bid_text}, ask={ask_text}; "
              f"{self.args.confirm_up_ticks} consecutive upticks, "
              f"rise_from_recent_low={mid - recent_low:.4f}")
        return True

    def evaluate(self, asset_id: str) -> None:
        meta = self.asset_meta.get(asset_id)
        if not meta:
            return
        market, outcome = meta["market"], meta["outcome"]
        book = self.books[asset_id]
        bid, ask, mid = self.quote(asset_id)
        if bid is not None:
            book["last_bid"] = bid
        if ask is not None:
            book["last_ask"] = ask
        if mid is None:
            return
        book_age_ms = max(0, int((time.monotonic() - book.get("updated", 0.0)) * 1000))
        if book_age_ms > self.args.max_book_age_seconds * 1000:
            if asset_id not in self.stale_logged:
                stamp = utc_now().isoformat(timespec="milliseconds")
                print(f"{stamp} {market['slug']} {outcome} stale book; skipping decisions: "
                      f"mid={mid:.4f}, bid={bid:.4f}, ask={ask:.4f}, age_ms={book_age_ms}, "
                      f"limit_ms={self.args.max_book_age_seconds * 1000:.0f}")
                self.stale_logged.add(asset_id)
            return
        self.stale_logged.discard(asset_id)

        if self.position and self.position.get("asset_id") == asset_id:
            self.position["last_bid"] = str(bid or ZERO)
            target, stop = D(self.position["target"]), D(self.position["stop"])
            entry_price = D(self.position.get("entry_price") or ZERO)
            now = time.time()

            highest_mid = D(self.position.get("highest_mid") or mid)
            if mid > highest_mid:
                highest_mid = mid
                self.position["highest_mid"] = str(highest_mid)

            try:
                entry_at = datetime.fromisoformat(self.position["entry_at"])
                pos_age = max(0.0, (utc_now() - entry_at).total_seconds())
            except (KeyError, TypeError, ValueError):
                pos_age = 0.0

            reason = ""
            if mid >= target and bid is not None and bid >= target:
                reason = "take_profit"
            elif mid <= stop and bid is not None and bid <= stop:
                reason = "stop_loss"
            elif market["end_ts"] - now <= self.args.force_exit_last_seconds:
                reason = "time_exit"
            elif self.args.stall_timeout_seconds > 0 and pos_age >= self.args.stall_timeout_seconds:
                if mid <= entry_price:
                    reason = "breakout_failure"

            if reason:
                self.exit(asset_id, reason, trigger={
                    "mid": mid, "bid": bid, "ask": ask,
                    "last_trade": book.get("last_trade"), "book_age_ms": book_age_ms,
                })
            return

        if self.position or market["slug"] in self.used_markets or not market["active"] or market["closed"]:
            return
        now = time.time()
        if not (market["start_ts"] <= now < market["end_ts"] - self.args.no_entry_last_seconds):
            book["last_mid"] = mid
            return
        if now < market["start_ts"] + self.args.entry_delay_seconds:
            # Ignore the opening 20 seconds and seed the signal baseline from
            # fresh quotes so a move during the excluded window is not reused.
            book["last_mid"] = mid
            return
        if self.entry_signal(asset_id, mid, bid, ask):
            self.enter(asset_id, outcome, mid, bid, ask, market, book_age_ms)

    def enter(self, asset_id: str, outcome: str, mid: Decimal, bid: Decimal | None,
              ask: Decimal | None, market: dict[str, Any], book_age_ms: int) -> None:
        stamp = utc_now().isoformat(timespec="milliseconds")
        if ask is None:
            print(f"{stamp} {market['slug']} {outcome} entry skipped: ask unavailable; mid={mid:.4f}")
            return
        if bid is None:
            print(f"{stamp} {market['slug']} {outcome} entry skipped: bid unavailable; mid={mid:.4f}")
            return
        spread = ask - bid
        if self.args.max_spread is not None and spread > self.args.max_spread:
            print(f"{stamp} {market['slug']} {outcome} entry skipped: "
                  f"spread too wide; mid={mid:.4f}, bid={bid:.4f}, ask={ask:.4f}, "
                  f"spread={spread:.4f}, max_allowed={self.args.max_spread:.4f}")
            return
        max_price = min(Decimal("0.55"), self.args.entry + self.args.entry_slippage)
        if ask < Decimal("0.50") or ask > max_price:
            print(f"{stamp} {market['slug']} {outcome} entry skipped: "
                  f"mid={mid:.4f}, ask={ask:.4f}, allowed_ask=0.5000-{max_price:.4f}")
            return
        budget = min(self.args.stake, self.cash)
        qty, avg, gross, fees = self.simulate_buy(asset_id, budget, max_price)
        min_size = D(self.books[asset_id].get("min_order_size") or market.get("min_order_size"))
        required_shares = max(min_size, self.args.min_depth_shares)
        if qty <= ZERO or qty < required_shares:
            print(f"{stamp} {market['slug']} {outcome} entry skipped: "
                  f"insufficient ask depth; mid={mid:.4f}, ask={ask:.4f}, "
                  f"available_shares={qty:.4f}, required_shares={required_shares:.4f}")
            return
        cost = gross + fees
        if cost > self.cash:
            print(f"{stamp} {market['slug']} {outcome} entry skipped: "
                  f"cost=${cost:.4f} exceeds virtual cash=${self.cash:.4f}; ask={ask:.4f}")
            return
        target = self.args.take_profit_price if self.args.take_profit_price is not None else avg * self.args.take_profit_multiple
        stop = self.args.stop_price if self.args.stop_price is not None else avg * self.args.stop_multiple
        self.cash -= cost
        self.position = {
            "asset_id": asset_id, "market_slug": market["slug"], "market_id": market["market_id"],
            "outcome": outcome, "quantity": str(qty), "entry_price": str(avg),
            "entry_cost": str(cost), "entry_fees": str(fees), "target": str(target), "stop": str(stop),
            "market_end_ts": market["end_ts"], "last_bid": str(self.books[asset_id].get("last_bid") or ZERO),
            "realized_pnl": "0", "entry_at": utc_now().isoformat(),
            "highest_mid": str(mid),
        }
        self.used_markets.add(market["slug"])
        self.save_state()
        self.log_trade(market=market, outcome=outcome, action="BUY", reason="entry",
                       quantity=qty, avg_price=avg, gross=gross, fees=fees,
                       entry_price=avg, target=target, stop=stop,
                       trigger_mid=mid, trigger_bid=bid, trigger_ask=ask,
                       last_trade=self.books[asset_id].get("last_trade"), book_age_ms=book_age_ms)
        bid_text = f"{bid:.4f}" if bid is not None else "n/a"
        ask_text = f"{ask:.4f}" if ask is not None else "n/a"
        stamp = utc_now().isoformat(timespec="milliseconds")
        self.pnl.record_entry(stamp=stamp, market_slug=market["slug"], outcome=outcome,
                              quantity=qty, avg_price=avg, fees=fees, target=target,
                              stop=stop, cash_after=self.cash)
        print(f"{stamp} {market['slug']} {outcome} paper buy {qty:.4f} shares at avg {avg:.3f}; "
              f"trigger_mid={mid:.4f}, bid={bid_text}, ask={ask_text}, book_age_ms={book_age_ms}; "
              f"take_profit={target:.3f}, stop={stop:.3f}, fees=${fees:.4f}")

    def exit(self, asset_id: str, reason: str, trigger: dict[str, Any] | None = None) -> None:
        position = self.position
        if not position:
            return
        market = self.markets.get(position["market_slug"])
        if not market:
            # Keep the persisted position if discovery has not refreshed yet.
            return
        book = self.books[asset_id]
        if trigger is None:
            bid, ask, mid = self.quote(asset_id)
            trigger = {"mid": mid, "bid": bid, "ask": ask,
                       "last_trade": book.get("last_trade"),
                       "book_age_ms": max(0, int((time.monotonic() - book.get("updated", 0.0)) * 1000))}
        stamp = utc_now().isoformat(timespec="milliseconds")
        try:
            entry_at = datetime.fromisoformat(position["entry_at"])
            position_age = max(ZERO, D(str((utc_now() - entry_at).total_seconds())))
        except (KeyError, TypeError, ValueError):
            position_age = ZERO
        def price_text(key: str) -> str:
            value = trigger.get(key)
            return f"{D(value):.4f}" if value is not None else "n/a"
        print(f"{stamp} {market['slug']} {position['outcome']} EXIT_TRIGGER reason={reason}; "
              f"entry={D(position['entry_price']):.4f}, target={D(position['target']):.4f}, "
              f"stop={D(position['stop']):.4f}, mid={price_text('mid')}, bid={price_text('bid')}, "
              f"ask={price_text('ask')}, last_trade={price_text('last_trade')}, "
              f"book_age_ms={trigger.get('book_age_ms', 'n/a')}, position_age_s={position_age:.3f}")
        qty = D(position["quantity"])
        sold, avg, gross, fees, fills = self.simulate_sell(asset_id, qty)
        if sold <= ZERO:
            print(f"{stamp} {market['slug']} {position['outcome']} EXIT_DEFERRED reason={reason}; "
                  "no executable bid levels; paper position remains open.")
            return
        fill_text = ",".join(f"{price:.4f}x{shares:.4f}" for price, shares in fills)
        cost_left = D(position["entry_cost"])
        allocated_cost = cost_left if sold >= qty else cost_left * sold / qty
        entry_fees_left = D(position.get("entry_fees"))
        allocated_entry_fees = entry_fees_left if sold >= qty else entry_fees_left * sold / qty
        pnl = gross - fees - allocated_cost
        self.cash += gross - fees
        self.realized_pnl += pnl
        position["realized_pnl"] = str(D(position.get("realized_pnl")) + pnl)
        position["quantity"] = str(max(ZERO, qty - sold))
        position["entry_cost"] = str(max(ZERO, cost_left - allocated_cost))
        position["entry_fees"] = str(max(ZERO, entry_fees_left - allocated_entry_fees))
        self.save_state()
        self.log_trade(market=market, outcome=position["outcome"], action="SELL", reason=reason,
                       quantity=sold, avg_price=avg, gross=gross, fees=fees, pnl=pnl,
                       entry_price=D(position["entry_price"]), target=D(position["target"]), stop=D(position["stop"]),
                       trigger_mid=trigger.get("mid"), trigger_bid=trigger.get("bid"),
                       trigger_ask=trigger.get("ask"), last_trade=trigger.get("last_trade"),
                       book_age_ms=trigger.get("book_age_ms"), position_age_seconds=position_age,
                       fill_levels=fills)
        self.pnl.record_exit(stamp=stamp, market_slug=market["slug"], outcome=position["outcome"],
                             reason=reason, quantity=sold, entry_price=D(position["entry_price"]),
                             exit_price=avg, entry_fees=allocated_entry_fees, exit_fees=fees,
                             pnl=pnl, position_age_s=position_age, cash_after=self.cash,
                             cumulative_pnl=self.realized_pnl)
        print(f"{stamp} {market['slug']} {position['outcome']} EXIT_FILL reason={reason}; "
              f"sold={sold:.4f}, avg={avg:.4f}, levels=[{fill_text}], fees=${fees:.4f}, "
              f"net_P&L=${pnl:.4f}, cumulative_realized_P&L=${self.realized_pnl:.4f}")
        if D(position["quantity"]) <= Decimal("0.000001"):
            self.position = None
            self.save_state()
            print(f"Paper balance ${self.cash:.2f}")

    def evaluate_time_exit(self) -> None:
        if not self.position:
            return
        asset_id = self.position.get("asset_id")
        if asset_id not in self.books:
            return
        book = self.books[asset_id]
        now = time.time()
        market_end = D(self.position.get("market_end_ts") or ZERO)

        reason = ""
        if market_end - Decimal(str(now)) <= self.args.force_exit_last_seconds:
            reason = "time_exit"
        elif self.args.stall_timeout_seconds > 0:
            try:
                entry_at = datetime.fromisoformat(self.position["entry_at"])
                pos_age = max(0.0, (utc_now() - entry_at).total_seconds())
            except (KeyError, TypeError, ValueError):
                pos_age = 0.0
            if pos_age >= self.args.stall_timeout_seconds:
                bid, ask, mid = self.quote(asset_id)
                entry_price = D(self.position.get("entry_price") or ZERO)
                if mid is not None and mid <= entry_price:
                    reason = "breakout_failure"

        if not reason:
            return

        age_ms = max(0, int((time.monotonic() - book.get("updated", 0.0)) * 1000))
        if age_ms > self.args.max_book_age_seconds * 1000:
            if asset_id not in self.stale_logged:
                position = self.position
                print(f"{utc_now().isoformat(timespec='milliseconds')} {position['market_slug']} "
                      f"{position['outcome']} EXIT_DEFERRED reason={reason}; "
                      f"stale book age_ms={age_ms}, limit_ms={self.args.max_book_age_seconds * 1000:.0f}")
                self.stale_logged.add(asset_id)
            return
        self.stale_logged.discard(asset_id)
        bid, ask, mid = self.quote(asset_id)
        self.exit(asset_id, reason, trigger={
            "mid": mid, "bid": bid, "ask": ask, "last_trade": book.get("last_trade"),
            "book_age_ms": age_ms,
        })

    def subscribe_updates(self, ws: Any, current: list[str], desired: list[str]) -> list[str]:
        remove = [asset for asset in current if asset not in desired]
        add = [asset for asset in desired if asset not in current]
        if remove:
            ws.send(json.dumps({"operation": "unsubscribe", "assets_ids": remove}))
        if add:
            ws.send(json.dumps({"operation": "subscribe", "assets_ids": add}))
        return desired

    def run(self) -> None:
        import websocket  # type: ignore[import-not-found]

        assets = self.discover()
        print(f"Polymarket public-market paper trader | starting virtual cash ${self.args.initial_cash:.2f}")
        max_entry = min(Decimal("0.55"), self.args.entry + self.args.entry_slippage)
        print(f"Strategy: low-start rising signal into mid 0.50-0.55; confirm with "
              f"{self.args.confirm_up_ticks} uptick(s); start entry checks "
              f"{self.args.entry_delay_seconds}s after each market opens; buy only when ask is "
              f"0.50-{max_entry:.2f} and spread <= {self.args.max_spread:.2f}; "
              f"stop={self.args.stop_multiple*Decimal('100'):.0f}%, "
              f"stall exit={self.args.stall_timeout_seconds:.0f}s; exits use fill-based or fixed prices.")
        print("Public market data only. No API key or wallet required; no real orders are sent. Press Ctrl+C to stop.")
        if not assets:
            print("No current BTC 5-minute market found; retrying discovery.")

        while True:
            try:
                if not assets:
                    assets = self.discover()
                    if not assets:
                        time.sleep(10)
                        continue
                # Give the TLS/WebSocket handshake time on slower networks, then
                # use short reads so refresh and heartbeat checks stay timely.
                ws = websocket.create_connection(WS_URL, timeout=15, suppress_origin=True)
                ws.settimeout(1)
                ws.send(json.dumps({"assets_ids": assets, "type": "market", "custom_feature_enabled": True}))
                print(f"Connected to Polymarket public market data; subscribed to {len(assets)} Up/Down outcomes.")
                last_refresh = time.monotonic()
                last_ping = time.monotonic()
                while True:
                    now = time.monotonic()
                    if now - last_refresh >= self.args.discovery_seconds:
                        desired = self.discover()
                        if desired != assets:
                            assets = self.subscribe_updates(ws, assets, desired)
                            print(f"Subscription updated: {len(assets)} outcomes.")
                        last_refresh = now
                    if now - last_ping >= 10:
                        ws.send("PING")
                        last_ping = now
                    self.evaluate_time_exit()
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw or raw == "PONG":
                        continue
                    try:
                        self.handle_message(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
            except KeyboardInterrupt:
                print("Stopped. Local state and trade records have been saved.")
                return
            except Exception as exc:
                print(f"Market data connection error: {exc}; reconnecting in 10 seconds.")
                time.sleep(10)
                assets = self.discover()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Local paper trading on Polymarket BTC 5-minute Up/Down")
    result.add_argument("--entry", type=Decimal, default=Decimal("0.50"), help="minimum ask price for a paper buy (default: 0.50)")
    result.add_argument("--stake", type=Decimal, default=Decimal("4.00"), help="virtual amount per trade in USD (default: 4)")
    result.add_argument("--initial-cash", type=Decimal, default=Decimal("100.00"), help="starting virtual cash in USD")
    result.add_argument("--entry-slippage", type=Decimal, default=Decimal("0.05"), help="maximum entry slippage (hard-capped at 0.55)")
    result.add_argument("--take-profit-multiple", type=Decimal, default=Decimal("1.20"), help="take-profit multiple of entry price")
    result.add_argument("--stop-multiple", type=Decimal, default=Decimal("0.85"), help="stop-loss multiple of entry price (default: 0.85)")
    result.add_argument("--take-profit-price", type=Decimal, default=None, help="fixed take-profit price, e.g. 0.40")
    result.add_argument("--stop-price", type=Decimal, default=None, help="fixed stop-loss price, e.g. 0.10")
    result.add_argument("--confirm-up-ticks", type=int, default=3, help="strict consecutive upticks required in the rising-entry window (default: 3)")
    result.add_argument("--max-spread", type=Decimal, default=Decimal("0.04"), help="maximum allowed bid-ask spread to enter (default: 0.04)")
    result.add_argument("--min-depth-shares", type=Decimal, default=Decimal("5.0"), help="minimum ask shares required in book to enter (default: 5.0)")
    result.add_argument("--stall-timeout-seconds", type=float, default=40.0, help="exit position if breakout stalls without gain after this many seconds (default: 40.0, 0 to disable)")
    result.add_argument("--entry-delay-seconds", type=int, default=20, help="wait this many seconds after each 5-minute market opens before checking entries")
    result.add_argument("--fee-rate", type=Decimal, default=FEE_RATE, help="Crypto taker fee rate used in local simulation")
    result.add_argument("--no-entry-last-seconds", type=int, default=20, help="stop entering this many seconds before market expiry")
    result.add_argument("--force-exit-last-seconds", type=int, default=15, help="exit this many seconds before market expiry")
    result.add_argument("--max-book-age-seconds", type=float, default=5.0, help="maximum order book age for expiry exits")
    result.add_argument("--discovery-seconds", type=int, default=15, help="seconds between market metadata refreshes")
    return result


def main() -> int:
    log_file = (ROOT / "bot.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = Tee(sys.stdout, log_file)
    sys.stderr = Tee(sys.stderr, log_file)
    print(f"\n--- Run started at {utc_now().isoformat()} ---")
    args = parser().parse_args()
    if not (ZERO < args.entry < Decimal("1")) or args.stake <= ZERO or args.initial_cash <= ZERO:
        print("entry must be positive and below 1; stake and initial-cash must be positive.", file=sys.stderr)
        return 2
    if (args.take_profit_price is None) != (args.stop_price is None):
        print("fixed-price mode requires both --take-profit-price and --stop-price.", file=sys.stderr)
        return 2
    if args.take_profit_price is not None:
        if not (args.entry < args.take_profit_price < Decimal("1") and ZERO < args.stop_price < args.entry):
            print("take-profit price must be above entry and stop price must be below entry.", file=sys.stderr)
            return 2
    elif not (ZERO < args.stop_multiple < Decimal("1") < args.take_profit_multiple):
        print("multiple mode requires stop-multiple between 0 and 1 and take-profit-multiple above 1.", file=sys.stderr)
        return 2
    if args.confirm_up_ticks < 1 or not (ZERO <= args.fee_rate <= Decimal("1")):
        print("confirm-up-ticks must be positive and fee-rate must be between 0 and 1.", file=sys.stderr)
        return 2
    if args.max_spread is not None and args.max_spread <= ZERO:
        print("max-spread must be positive.", file=sys.stderr)
        return 2
    if args.min_depth_shares < ZERO:
        print("min-depth-shares cannot be negative.", file=sys.stderr)
        return 2
    if args.stall_timeout_seconds < 0:
        print("stall-timeout-seconds cannot be negative.", file=sys.stderr)
        return 2
    try:
        LocalPaperBot(args).run()
        return 0
    except KeyboardInterrupt:
        print("Stopped.")
        return 130
    except Exception as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
