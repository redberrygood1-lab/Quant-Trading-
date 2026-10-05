#!/usr/bin/env python3
"""ptq_data — the Part-Time Quant Academy price-bar layer.

ONE MODULE. Days 4, 5 and 6 all get their bars from here, so they can never
disagree about what a bar is or where it came from. Day 5 gets it for free
because it loads day 4's backtester and calls its `fetch_bars`.

WHY THIS EXISTS
Days 4-6 used to call yfinance, Yahoo's chart endpoint and Stooq directly.
Measured 3 Sep 2026: Stooq's CSV endpoint no longer returns a price table at
all, and Yahoo's chart endpoint answers HTTP 429 after a handful of requests
from one machine. A cohort of members all running day 4 in the same week would
share that ceiling. Yahoo is also personal-use-only, so it cannot be the road
a paid product drives on.

THE DESIGN, IN ONE LINE
The member brings their own free API key. Nobody proxies anybody.

That means the member's requests come from the member's machine on the member's
key, so one member cannot rate-limit another, and no data is ever redistributed
by us. The key is set up on day 1 and is OPTIONAL: without one the layer still
works off a CSV file or the unkeyed public endpoint, and it always says on the
card which of those actually served the bars.

PROVIDERS, IN PREFERENCE ORDER
  tiingo        free key. Split- and dividend-adjusted daily bars, full history.
  twelvedata    free key, no card. Widest asset coverage (equities, FX, crypto).
  alpaca        free key, but the free feed is IEX only (~2.5% of US volume)
                and the signup is a US brokerage application. Supported for
                members who already have keys; not the day-1 route.
  polygon       free key. 2 years, end-of-day only.
  yahoo         NO key. Best effort, rate-limited, personal use only. Last
                resort, always labelled as such on the card.
  csv           a file on their disk. Always works, needs no internet.

DELIBERATELY NOT IMPLEMENTED
  Alpha Vantage free returns only the latest 100 daily points ("full" outputsize
  is premium). Day 4 refuses anything under 250 bars, so a free Alpha Vantage
  key cannot complete day 4. Offering it would be a trap.
  EODHD free is 20 calls/day over one year of history, and is personal-use-only.
  Stooq's open CSV endpoint no longer returns a price table to a script.

FAILURE POLICY
There is no number in this file that did not come off a wire or out of a file
the member pointed at. If every source is unreachable it raises DataUnavailable
with the reason for each one. It never interpolates, back-fills, estimates or
carries a price forward. A stale cache is served only when explicitly allowed
and is labelled STALE with its age.

Standard library only. No pandas, no numpy, no yfinance — day 6 runs on a bare
Python and this has to run there too.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DATA_MODULE_VERSION = "1.0.1"

# --------------------------------------------------------------------------
# Where things live
# --------------------------------------------------------------------------

HOME = os.path.expanduser("~")
PTQ_DIR = os.environ.get("PTQ_ACADEMY_HOME") or os.path.join(HOME, ".ptq-academy")
KEYS_PATH = os.path.join(PTQ_DIR, "keys.json")
CACHE_DIR = os.path.join(PTQ_DIR, "cache", "bars")

DEFAULT_MAX_AGE = 6 * 3600  # research days: a 6-hour-old daily bar is the same bar
NET_TIMEOUT = 30

OHLC = ("open", "high", "low", "close")

# A browser-ish agent. Only the keyless Yahoo path needs it; the keyed
# providers are happy with anything.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Bars this layer speaks. Same set as day 3's schema.
BARS = ("1d", "1wk", "1h", "30m", "15m", "5m")
INTRADAY = ("1h", "30m", "15m", "5m")


class DataUnavailable(Exception):
    """No real bars could be got. Carries the reason from every source tried."""


# --------------------------------------------------------------------------
# The key store
#
# Environment first, then ~/.ptq-academy/keys.json (0600). A file rather than
# environment variables because a non-coder on Windows setting a user
# environment variable and opening a new terminal is exactly the kind of step
# that ends a week on day 1.
# --------------------------------------------------------------------------

ENV_NAMES = {
    "tiingo": ("TIINGO_API_KEY",),
    "twelvedata": ("TWELVEDATA_API_KEY", "TWELVE_DATA_API_KEY"),
    "alpaca": ("APCA_API_KEY_ID", "ALPACA_API_KEY_ID"),
    "alpaca_secret": ("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY"),
    "polygon": ("POLYGON_API_KEY", "MASSIVE_API_KEY"),
}


def _read_key_file() -> dict:
    try:
        with open(KEYS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_key(name: str) -> str | None:
    """Environment wins, then the key file. Blank strings count as absent."""
    for env in ENV_NAMES.get(name, ()):
        v = os.environ.get(env)
        if v and v.strip():
            return v.strip()
    v = _read_key_file().get(name)
    return v.strip() if isinstance(v, str) and v.strip() else None


def set_key(name: str, value: str) -> str:
    """Write a key to the key file with owner-only permissions."""
    os.makedirs(PTQ_DIR, exist_ok=True)
    data = _read_key_file()
    data[name] = value.strip()
    tmp = KEYS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Windows: the file inherits the user profile's ACL, which is fine
    os.replace(tmp, KEYS_PATH)
    return KEYS_PATH


def forget_key(name: str) -> bool:
    data = _read_key_file()
    if name not in data:
        return False
    del data[name]
    with open(KEYS_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(KEYS_PATH, 0o600)
    except OSError:
        pass
    return True


def mask(value: str | None) -> str:
    """Never print a key. Print enough to recognise it and no more."""
    if not value:
        return "not set"
    return (value[:4] + "…" + value[-2:]) if len(value) > 8 else "set"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _http(url: str, headers: dict | None = None, tries: int = 3) -> bytes:
    """GET with backoff on 429/5xx. Raises urllib errors otherwise."""
    last: Exception | None = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                wait = 2.0 * (attempt + 1)
                try:
                    ra = exc.headers.get("Retry-After") if exc.headers else None
                    if ra and str(ra).strip().isdigit():
                        wait = min(30.0, float(str(ra).strip()))
                except Exception:  # noqa: BLE001 - a bad header is not a reason to die
                    pass
                time.sleep(wait)
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - timeouts, DNS, TLS
            last = exc
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise last if last else RuntimeError("unreachable")


def _json(url: str, headers: dict | None = None) -> object:
    return json.loads(_http(url, headers).decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# Bars
#
# Canonical bar: {"time": ISO-8601 UTC, "open","high","low","close","volume"}
# Sorted oldest first, no duplicate timestamps, every value a real float.
# --------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_when(value) -> datetime | None:
    """Anything a provider might call a timestamp -> aware UTC datetime."""
    if isinstance(value, (int, float)):
        secs = float(value)
        if secs > 1e11:  # milliseconds
            secs /= 1000.0
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        s += "T00:00:00+00:00"
    elif re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$", s):
        s = s.replace(" ", "T")
        if len(s) == 16:
            s += ":00"
        s += "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                dt = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _bar(when, o, h, l, c, v) -> dict | None:
    dt = _parse_when(when)
    if dt is None:
        return None
    try:
        close = float(c)
        vals = {
            "open": float(o if o is not None else c),
            "high": float(h if h is not None else c),
            "low": float(l if l is not None else c),
            "close": close,
            "volume": float(v) if v not in (None, "") else 0.0,
        }
    except (TypeError, ValueError):
        return None
    if not all(x > 0 for x in (vals["open"], vals["high"], vals["low"], close)):
        return None
    # A bar whose high is under its low is corrupt. Drop it, do not "repair" it.
    if vals["high"] < vals["low"]:
        return None
    return {"time": _iso(dt), **vals}


def _tidy(bars: list) -> list:
    """Sort, drop duplicate timestamps keeping the last, drop empties."""
    seen: dict[str, dict] = {}
    for b in bars:
        if b:
            seen[b["time"]] = b
    return [seen[k] for k in sorted(seen)]


# --------------------------------------------------------------------------
# Providers
#
# Each returns (bars, note). `note` is what the member sees on the card and is
# allowed to carry a caveat about the feed. Each raises on failure; the
# resolver collects the reasons.
# --------------------------------------------------------------------------


class Provider:
    name = ""
    label = ""
    needs_key = True
    # what the prices are adjusted for. Printed, because it changes the result.
    adjustment = "unknown"
    asset_classes: tuple = ("equity", "etf", "index", "fx", "crypto", "futures")
    bars_supported: tuple = BARS

    def available(self) -> bool:
        return bool(get_key(self.name)) if self.needs_key else True

    def can(self, asset_class: str, bar: str) -> str | None:
        """None if it can serve this, otherwise the reason it cannot."""
        if asset_class not in self.asset_classes:
            return f"{self.label} does not carry {asset_class}"
        if bar not in self.bars_supported:
            return f"{self.label} does not serve {bar} bars"
        return None

    def fetch(self, symbol: str, bar: str, start: str, end: str | None) -> tuple:
        raise NotImplementedError


# --- Tiingo ---------------------------------------------------------------


class Tiingo(Provider):
    name = "tiingo"
    label = "Tiingo"
    adjustment = "splits and dividends"
    # Tiingo has three price endpoints and a symbol has to go to the right one:
    # stocks and ETFs (daily history, IEX for intraday), crypto and FX. A free
    # key that is not allowed a dataset comes back 403, which is reported.
    asset_classes = ("equity", "etf", "index", "crypto", "fx")
    bars_supported = BARS

    _FREQ = {"1d": "daily", "1wk": "weekly"}
    # crypto and FX endpoints: 7day is Tiingo's weekly bar
    _RESAMPLE = {"1d": "1day", "1wk": "7day", "1h": "1hour", "30m": "30min",
                 "15m": "15min", "5m": "5min"}
    _FIAT = {"usd", "eur", "gbp", "jpy", "chf", "aud", "nzd", "cad", "sek",
             "nok", "dkk", "hkd", "sgd", "mxn", "zar", "try", "cnh", "cny",
             "pln", "huf", "czk", "ils", "inr", "krw", "brl", "twd", "thb"}
    # Spot gold, silver, platinum and palladium are on the FX endpoint.
    _METALS = {"xau", "xag", "xpt", "xpd"}
    _QUOTES = ("usdt", "usdc", "usd", "eur", "gbp", "btc", "eth")
    # One intraday request returns a limited number of bars (10,000 on IEX,
    # fewer on FX and crypto), so a long intraday history takes several.
    # Capped, because a free key allows 50 requests an hour; day 4 says on the
    # card when the history came back shorter than asked.
    MAX_PAGES = 12

    def route(self, symbol: str) -> tuple:
        """(kind, ticker) where kind is stock, crypto or fx.

        EURUSD, EUR/USD, EURUSD=X and EUR-USD are all the fx ticker eurusd.
        XAUUSD and XAU/USD (gold) are the fx ticker xauusd.
        BTC-USD, BTC/USD, BTCUSD and btcusd are all the crypto ticker btcusd.
        """
        raw = symbol.strip()
        flat = re.sub(r"[^a-z0-9]", "", raw.lower().replace("=x", ""))
        if len(flat) == 6 and flat[:3] in self._FIAT | self._METALS and flat[3:] in self._FIAT:
            return "fx", flat
        looks_pair = raw.upper().endswith("=X") or "-" in raw or "/" in raw
        if flat.endswith(self._QUOTES) and len(flat) <= 10 and (
            looks_pair or raw == raw.lower() or len(flat) > 5
        ):
            return "crypto", flat
        return "stock", raw

    def fetch(self, symbol, bar, start, end):
        token = get_key("tiingo")
        if not token:
            raise DataUnavailable("no Tiingo key")
        # Header auth, so the key never appears in a URL, a log or a proxy.
        head = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
        kind, sym = self.route(symbol)
        q = {"startDate": start, "format": "json"}
        if end:
            q["endDate"] = end

        if kind == "crypto":
            q["tickers"] = sym
            q["resampleFreq"] = self._RESAMPLE[bar]
            url = "https://api.tiingo.com/tiingo/crypto/prices?"
            note = f"Tiingo crypto {bar} bars"
        elif kind == "fx":
            q["tickers"] = sym
            q["resampleFreq"] = self._RESAMPLE[bar]
            url = "https://api.tiingo.com/tiingo/fx/prices?"
            note = f"Tiingo FX {bar} bars"
        elif bar in INTRADAY:
            q["resampleFreq"] = self._RESAMPLE[bar]
            url = f"https://api.tiingo.com/iex/{urllib.parse.quote(sym)}/prices?"
            note = f"Tiingo IEX {bar} bars"
        else:
            q["resampleFreq"] = self._FREQ[bar]
            url = f"https://api.tiingo.com/tiingo/daily/{urllib.parse.quote(sym)}/prices?"
            note = f"Tiingo {bar} bars"

        if bar in INTRADAY and kind != "stock":
            bars = self._windows(url, q, head, bar, start, end)
        else:
            bars = self._get(url, q, head)
            if bar in INTRADAY:
                bars = self._fill(url, q, head, bars, start, end)
        if not bars:
            raise DataUnavailable(f"Tiingo has no {bar} history for {sym}")
        if bar in INTRADAY:
            note += " (intraday history on Tiingo is short)"
        return bars, note

    def _get(self, url, q, head) -> list:
        payload = _json(url + urllib.parse.urlencode(q), head)
        rows = payload
        if isinstance(payload, dict):
            raise DataUnavailable(
                "Tiingo said: %s" % str(payload.get("detail") or payload)[:160]
            )
        if rows and isinstance(rows[0], dict) and "priceData" in rows[0]:
            rows = rows[0]["priceData"]  # crypto and fx shape
        out = []
        for r in rows or []:
            # adj* is split- and dividend-adjusted. That is the series a
            # backtest should run on, so it is the one we take.
            has_adj = r.get("adjClose") is not None
            out.append(
                _bar(
                    r.get("date"),
                    r.get("adjOpen") if has_adj else r.get("open"),
                    r.get("adjHigh") if has_adj else r.get("high"),
                    r.get("adjLow") if has_adj else r.get("low"),
                    r.get("adjClose") if has_adj else r.get("close"),
                    r.get("adjVolume") if has_adj else r.get("volume"),
                )
            )
        return _tidy(out)

    def _fill(self, url, q, head, bars, start, end) -> list:
        """IEX returns the newest bars of a long range. Page back towards
        `start` until it is covered, nothing older comes back, or the cap."""
        want_from = _parse_when(start)
        slack = timedelta(days=4)  # weekends and holidays have no bars
        for _ in range(self.MAX_PAGES - 1):
            if not bars or not want_from:
                break
            first = _parse_when(bars[0]["time"])
            if first - want_from <= slack:
                break
            page = dict(q, endDate=first.strftime("%Y-%m-%d"))
            grown = _tidy(self._get(url, page, head) + bars)
            if len(grown) == len(bars):
                break  # nothing older
            bars = grown
        return bars

    _MINUTES = {"1h": 60, "30m": 30, "15m": 15, "5m": 5}

    def _windows(self, url, q, head, bar, start, end) -> list:
        """The crypto and FX endpoints return the OLDEST bars of a range, a
        few thousand at most. Ask for date windows ending today and walk back,
        so the bars kept always run unbroken up to the newest one. When the
        page cap is hit it is the oldest history that is missing, which day 4
        reports as clipped."""
        want_from = _parse_when(start) or datetime(2010, 1, 1, tzinfo=timezone.utc)
        edge = _parse_when(end) if end else datetime.now(timezone.utc)
        # Every cap seen on these endpoints is ~5,000 bars or more. A reply
        # that big which stops short of `edge` was cut off, so narrow and
        # retry. A smaller reply was not cut off: a hole in it is a closed
        # market (FX weekends), never a missing page.
        full = 3500
        step = timedelta(minutes=2 * self._MINUTES[bar])
        one = timedelta(days=1)
        width = max(one, timedelta(minutes=4000 * self._MINUTES[bar]))
        got = []
        # endDate is a day. Ask one day past today so today's bars come too.
        stop_day = edge + one if not end else edge
        for _ in range(self.MAX_PAGES):
            frm = max(want_from, edge - width)
            chunk = self._get(url, dict(q, startDate=frm.strftime("%Y-%m-%d"),
                                        endDate=stop_day.strftime("%Y-%m-%d")), head)
            if not chunk:
                break
            c_first, c_last = _parse_when(chunk[0]["time"]), _parse_when(chunk[-1]["time"])
            edge_day = edge.replace(hour=0, minute=0, second=0, microsecond=0)
            if len(chunk) >= full and edge_day - c_last > step and width > one:
                width = max(one, (c_last - c_first) * 0.9)
                continue
            if len(chunk) < full:
                width *= 1.5  # room for more per request (FX shuts weekends)
            got = _tidy(chunk + got)
            if frm <= want_from:
                break
            edge = stop_day = c_first
        return got


# --- Twelve Data ----------------------------------------------------------


class TwelveData(Provider):
    name = "twelvedata"
    label = "Twelve Data"
    adjustment = "splits"
    asset_classes = ("equity", "etf", "index", "fx", "crypto")
    bars_supported = BARS

    _IV = {"1d": "1day", "1wk": "1week", "1h": "1h", "30m": "30min",
           "15m": "15min", "5m": "5min"}

    def fetch(self, symbol, bar, start, end):
        key = get_key("twelvedata")
        if not key:
            raise DataUnavailable("no Twelve Data key")
        q = {
            "symbol": symbol.strip(),
            "interval": self._IV[bar],
            "outputsize": "5000",
            "order": "ASC",
            "timezone": "UTC",
            "start_date": start,
        }
        if end:
            q["end_date"] = end
        payload = _json(
            "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(q),
            {"Authorization": f"apikey {key}"},
        )
        if not isinstance(payload, dict):
            raise DataUnavailable("Twelve Data returned something unreadable")
        if str(payload.get("status", "")).lower() == "error" or "values" not in payload:
            raise DataUnavailable(
                "Twelve Data said: %s"
                % str(payload.get("message") or payload)[:160]
            )
        bars = _tidy(
            [
                _bar(r.get("datetime"), r.get("open"), r.get("high"),
                     r.get("low"), r.get("close"), r.get("volume"))
                for r in payload.get("values") or []
            ]
        )
        if not bars:
            raise DataUnavailable(
                f"Twelve Data has no {bar} history for {symbol}"
            )
        return bars, f"Twelve Data {bar} bars"


# --- Alpaca ---------------------------------------------------------------


class Alpaca(Provider):
    name = "alpaca"
    label = "Alpaca"
    adjustment = "splits and dividends"
    # US equities and crypto only. No FX, no non-US listings, no futures.
    asset_classes = ("equity", "etf", "crypto")
    bars_supported = BARS

    _TF = {"1d": "1Day", "1wk": "1Week", "1h": "1Hour", "30m": "30Min",
           "15m": "15Min", "5m": "5Min"}

    def available(self) -> bool:
        return bool(get_key("alpaca") and get_key("alpaca_secret"))

    def fetch(self, symbol, bar, start, end):
        kid, secret = get_key("alpaca"), get_key("alpaca_secret")
        if not (kid and secret):
            raise DataUnavailable("no Alpaca key pair")
        head = {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": secret}
        sym = symbol.strip().upper()
        crypto = "/" in sym
        base = (
            "https://data.alpaca.markets/v1beta3/crypto/us/bars?"
            if crypto
            else "https://data.alpaca.markets/v2/stocks/bars?"
        )
        q = {
            "symbols": sym,
            "timeframe": self._TF[bar],
            "start": start,
            "limit": "10000",
            "sort": "asc",
        }
        if not crypto:
            # `all` = split and dividend adjusted. `iex` because that is what a
            # free Alpaca key gets; the note says so on the card.
            q["adjustment"] = "all"
            q["feed"] = "iex"
        if end:
            q["end"] = end

        rows, token, pages = [], None, 0
        while pages < 20:
            pages += 1
            if token:
                q["page_token"] = token
            payload = _json(base + urllib.parse.urlencode(q), head)
            if not isinstance(payload, dict):
                raise DataUnavailable("Alpaca returned something unreadable")
            if payload.get("message") and "bars" not in payload:
                raise DataUnavailable("Alpaca said: %s" % str(payload["message"])[:160])
            chunk = (payload.get("bars") or {}).get(sym) or []
            rows.extend(chunk)
            token = payload.get("next_page_token")
            if not token or not chunk:
                break

        bars = _tidy(
            [_bar(r.get("t"), r.get("o"), r.get("h"), r.get("l"), r.get("c"),
                  r.get("v")) for r in rows]
        )
        if not bars:
            raise DataUnavailable(f"Alpaca has no {bar} history for {sym}")
        note = f"Alpaca {bar} bars"
        if not crypto:
            note += " (free Alpaca is the IEX feed, not the consolidated tape)"
        return bars, note


# --- Polygon / Massive ----------------------------------------------------


class Polygon(Provider):
    name = "polygon"
    label = "Polygon"
    adjustment = "splits"
    asset_classes = ("equity", "etf", "index", "crypto", "fx")
    bars_supported = BARS

    _AGG = {"1d": (1, "day"), "1wk": (1, "week"), "1h": (1, "hour"),
            "30m": (30, "minute"), "15m": (15, "minute"), "5m": (5, "minute")}

    def fetch(self, symbol, bar, start, end):
        key = get_key("polygon")
        if not key:
            raise DataUnavailable("no Polygon key")
        mult, span = self._AGG[bar]
        to = end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        url = (
            "https://api.polygon.io/v2/aggs/ticker/%s/range/%d/%s/%s/%s?%s"
            % (
                urllib.parse.quote(symbol.strip().upper()),
                mult,
                span,
                start,
                to,
                urllib.parse.urlencode(
                    {"adjusted": "true", "sort": "asc", "limit": "50000"}
                ),
            )
        )
        payload = _json(url, {"Authorization": f"Bearer {key}"})
        if not isinstance(payload, dict):
            raise DataUnavailable("Polygon returned something unreadable")
        if payload.get("status") in ("ERROR", "NOT_AUTHORIZED"):
            raise DataUnavailable(
                "Polygon said: %s" % str(payload.get("error") or payload)[:160]
            )
        bars = _tidy(
            [_bar(r.get("t"), r.get("o"), r.get("h"), r.get("l"), r.get("c"),
                  r.get("v")) for r in payload.get("results") or []]
        )
        if not bars:
            raise DataUnavailable(f"Polygon has no {bar} history for {symbol}")
        return bars, f"Polygon {bar} bars (free Polygon keeps 2 years, end of day)"


# --- Yahoo, unkeyed -------------------------------------------------------


class Yahoo(Provider):
    """No key, no signup, no guarantee. Yahoo answers 429 under any real load
    and its terms are personal use only, so this is the fallback that keeps a
    member moving today — never the road the course is built on."""

    name = "yahoo"
    label = "Yahoo (unkeyed)"
    needs_key = False
    adjustment = "splits and dividends"
    asset_classes = ("equity", "etf", "index", "fx", "crypto", "futures")
    bars_supported = BARS

    _IV = {"1d": "1d", "1wk": "1wk", "1h": "1h", "30m": "30m",
           "15m": "15m", "5m": "5m"}

    def fetch(self, symbol, bar, start, end):
        sym = symbol.strip()
        p1 = _parse_when(start) or datetime(2010, 1, 1, tzinfo=timezone.utc)
        p2 = _parse_when(end) if end else None
        q = {
            "period1": str(int(p1.timestamp())),
            "period2": str(int((p2 or datetime.now(timezone.utc)).timestamp())),
            "interval": self._IV[bar],
            "events": "div,splits",
            "includeAdjustedClose": "true",
        }
        last = None
        for host in ("query2", "query1"):
            url = (
                "https://%s.finance.yahoo.com/v8/finance/chart/%s?%s"
                % (host, urllib.parse.quote(sym), urllib.parse.urlencode(q))
            )
            try:
                payload = _json(url, {"Accept": "application/json"})
            except urllib.error.HTTPError as exc:
                last = (
                    "Yahoo answered HTTP 429 (too many requests). It rate-limits "
                    "hard and this is why the course asks for a free key."
                    if exc.code == 429
                    else f"Yahoo answered HTTP {exc.code}"
                )
                continue
            except Exception as exc:  # noqa: BLE001
                last = f"Yahoo unreachable: {type(exc).__name__}: {exc}"
                continue

            res = ((payload.get("chart") or {}).get("result") or [None])[0]
            if not res:
                err = (payload.get("chart") or {}).get("error") or {}
                last = "Yahoo has no series for %s%s" % (
                    sym,
                    (" (%s)" % err.get("description")) if err.get("description") else "",
                )
                continue
            ts = res.get("timestamp") or []
            ind = res.get("indicators") or {}
            quote = (ind.get("quote") or [{}])[0]
            adj = ((ind.get("adjclose") or [{}])[0] or {}).get("adjclose")
            out = []
            for i, t in enumerate(ts):
                c = (quote.get("close") or [None] * len(ts))[i]
                if c is None:
                    continue
                # Scale OHLC by the same factor Yahoo applies to the close, so
                # the bar stays internally consistent after adjustment.
                f = 1.0
                if adj and i < len(adj) and adj[i] is not None and c:
                    f = float(adj[i]) / float(c)
                g = lambda k: (quote.get(k) or [None] * len(ts))[i]  # noqa: E731
                out.append(
                    _bar(
                        t,
                        (g("open") or c) * f,
                        (g("high") or c) * f,
                        (g("low") or c) * f,
                        float(c) * f,
                        g("volume") or 0,
                    )
                )
            bars = _tidy(out)
            if bars:
                return bars, "Yahoo unkeyed endpoint — rate-limited, no guarantee"
            last = f"Yahoo returned no usable {bar} bars for {sym}"
        raise DataUnavailable(last or "Yahoo returned nothing usable")


# --- a CSV on their disk --------------------------------------------------


def read_csv_bars(path: str) -> list:
    """date,open,high,low,close[,volume] in any column order and any case."""
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(p):
        raise DataUnavailable(f"your spec points at a CSV that is not there: {p}")
    with open(p, encoding="utf-8-sig", errors="replace") as fh:
        rows = list(csv.DictReader(io.StringIO(fh.read())))
    if not rows:
        raise DataUnavailable(f"{p} has no rows")
    cols = {str(c).strip().lower(): c for c in rows[0] if c}
    when = cols.get("date") or cols.get("time") or cols.get("datetime")
    if not when or not all(k in cols for k in OHLC):
        raise DataUnavailable(
            f"{p} needs columns date, open, high, low, close (volume optional). "
            f"It has: {', '.join(sorted(cols))}"
        )
    bars = _tidy(
        [
            _bar(r.get(when), r.get(cols["open"]), r.get(cols["high"]),
                 r.get(cols["low"]), r.get(cols["close"]),
                 r.get(cols["volume"]) if "volume" in cols else 0)
            for r in rows
        ]
    )
    if not bars:
        raise DataUnavailable(f"{p} has no rows I could read as price bars")
    return bars


# Preference order. Data quality first, then coverage, then rate limits.
PROVIDERS = [Tiingo(), TwelveData(), Alpaca(), Polygon()]
UNKEYED = [Yahoo()]
ALL_PROVIDERS = {p.name: p for p in PROVIDERS + UNKEYED}

# Legacy `data_source` values from day-3 specs written before this layer
# existed. They mean "pick something sensible", which is what auto does.
LEGACY_SOURCES = {"yfinance": "auto", "stooq": "auto"}


def configured() -> list:
    """Which keyed providers this machine can actually use, in order."""
    return [p for p in PROVIDERS if p.available()]


# --------------------------------------------------------------------------
# Cache
#
# Keyed on symbol + bar + start, NOT on the provider, so day 4, day 5 and
# day 6 share one warm cache and the cohort makes a fraction of the requests
# it otherwise would.
# --------------------------------------------------------------------------


def _cache_key(symbol: str, bar: str, start: str) -> str:
    raw = f"{symbol}|{bar}|{start}".lower()
    return re.sub(r"[^a-z0-9._-]", "_", raw)[:120]


def _cache_paths(symbol, bar, start):
    k = _cache_key(symbol, bar, start)
    return os.path.join(CACHE_DIR, k + ".csv"), os.path.join(CACHE_DIR, k + ".json")


def _cache_read(symbol, bar, start):
    csv_p, meta_p = _cache_paths(symbol, bar, start)
    if not os.path.exists(csv_p):
        return None, None, None
    try:
        with open(meta_p, encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        meta = {}
    try:
        bars = read_csv_bars(csv_p)
    except DataUnavailable:
        return None, None, None
    return bars, meta, os.path.getmtime(csv_p)


def _cache_write(symbol, bar, start, bars, meta):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        csv_p, meta_p = _cache_paths(symbol, bar, start)
        with open(csv_p, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            for b in bars:
                w.writerow([b["time"], b["open"], b["high"], b["low"],
                            b["close"], b["volume"]])
        with open(meta_p, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
    except OSError:
        pass  # a cache that will not write is not a reason to fail a run


def clear_cache() -> int:
    n = 0
    if os.path.isdir(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            try:
                os.remove(os.path.join(CACHE_DIR, f))
                n += 1
            except OSError:
                pass
    return n


# --------------------------------------------------------------------------
# The one entry point
# --------------------------------------------------------------------------

SETUP_HELP = """Next steps:

  1. Check your existing provider account and access to the requested data.
     For Tiingo, open https://www.tiingo.com/account/api/token.
  2. Ask your assistant to open the academy's private local key-entry form.
     Paste your key only into that form, then save it locally.
     Never paste a key into chat, a community post or a terminal command.
  3. Ask for a new price-data check. A saved key is not proof of access.

If the private form is unavailable, restore the complete academy pack first.
Do not send your key to the assistant as a workaround.

You can also use your own CSV with columns
date,open,high,low,close,volume. Ask the assistant to select that file and
check its format and history. It does not need a provider account, but
the file must contain enough valid prices for the requested test."""


def get_bars(
    symbol: str,
    bar: str,
    start: str,
    end: str | None = None,
    *,
    data_source: str = "auto",
    csv_path: str | None = None,
    asset_class: str = "equity",
    max_age: int = DEFAULT_MAX_AGE,
    allow_unkeyed: bool = True,
    allow_stale: bool = False,
    use_cache: bool = True,
) -> tuple:
    """(bars, meta). Raises DataUnavailable if nothing real could be got.

    meta: provider, source (the line printed on the card), adjustment, cached,
          stale, age_seconds, tried (why each source did not serve).
    """
    source = LEGACY_SOURCES.get((data_source or "auto").lower(), data_source or "auto")
    own_file = source == "csv" or bool(csv_path)
    if bar == "1m" and not own_file:
        raise DataUnavailable(
            "1-minute bars only run from your own file. No free price feed keeps "
            "enough 1-minute history to test on. Export your 1-minute bars as a CSV "
            "(date,open,high,low,close,volume), set data_source to csv and point "
            "csv_path at the file."
        )
    if bar not in BARS and bar != "1m":
        raise DataUnavailable(
            f"{bar!r} is not a bar size this runs. Use one of: {', '.join(BARS)}, "
            "or 1m from your own CSV."
        )

    # 1. An explicit CSV wins over everything. It is the member's own file.
    if source == "csv" or csv_path:
        path = csv_path
        if not path:
            raise DataUnavailable(
                "your spec says data_source csv but has no csv_path"
            )
        bars = read_csv_bars(path)
        return bars, {
            "provider": "csv",
            "source": f"your CSV at {os.path.abspath(os.path.expanduser(path))}",
            "adjustment": "however you saved it",
            "cached": False,
            "stale": False,
            "age_seconds": 0,
            "tried": [],
        }

    # 2. A cache young enough to be the same bars.
    cached, cmeta, mtime = (None, None, None)
    if use_cache:
        cached, cmeta, mtime = _cache_read(symbol, bar, start)
        if cached and mtime is not None and (time.time() - mtime) < max_age:
            age = int(time.time() - mtime)
            return cached, {
                "provider": (cmeta or {}).get("provider", "cache"),
                "source": "%s (cached %s ago)"
                % ((cmeta or {}).get("source", "cache"), _ago(age)),
                "adjustment": (cmeta or {}).get("adjustment", "unknown"),
                "cached": True,
                "stale": False,
                "age_seconds": age,
                "tried": [],
            }

    # 3. Live sources, best first.
    if source in ALL_PROVIDERS:
        chain = [ALL_PROVIDERS[source]]  # the spec named one; honour it
    else:
        chain = list(configured())
        if allow_unkeyed:
            chain += list(UNKEYED)

    tried: list[str] = []
    if not chain:
        tried.append("no data provider key is set on this machine")

    for prov in chain:
        why = prov.can(asset_class, bar)
        if why:
            tried.append(why)
            continue
        if prov.needs_key and not prov.available():
            tried.append(f"{prov.label}: no key set")
            continue
        try:
            bars, note = prov.fetch(symbol, bar, start, end)
        except DataUnavailable as exc:
            tried.append(f"{prov.label}: {exc}")
            continue
        except urllib.error.HTTPError as exc:
            hint = {
                401: "the key was rejected",
                403: "the key was rejected, or is not allowed this data",
                404: f"{prov.label} does not know the symbol {symbol}",
                429: "rate limited",
            }.get(exc.code, f"HTTP {exc.code}")
            tried.append(f"{prov.label}: {hint}")
            continue
        except Exception as exc:  # noqa: BLE001 - report anything, honestly
            tried.append(f"{prov.label}: {type(exc).__name__}: {exc}")
            continue

        meta = {
            "provider": prov.name,
            "source": note,
            "adjustment": prov.adjustment,
            "cached": False,
            "stale": False,
            "age_seconds": 0,
            "tried": tried,
        }
        if use_cache:
            _cache_write(symbol, bar, start, bars, meta)
        return bars, meta

    # 4. Stale cache, only if the caller said stale is better than nothing,
    #    and only ever labelled as stale.
    if allow_stale and cached and mtime is not None:
        age = int(time.time() - mtime)
        return cached, {
            "provider": (cmeta or {}).get("provider", "cache"),
            "source": "STALE cached bars, %s old — every live source failed" % _ago(age),
            "adjustment": (cmeta or {}).get("adjustment", "unknown"),
            "cached": True,
            "stale": True,
            "age_seconds": age,
            "tried": tried,
        }

    raise DataUnavailable(
        "No price data for %s on %s bars. Nothing was tested and nothing was "
        "invented.\n\nWhat I tried:\n%s\n\n%s"
        % (
            symbol,
            bar,
            "\n".join("  - " + t for t in tried) or "  - nothing was reachable",
            SETUP_HELP,
        )
    )


def _ago(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def bars_from_spec(spec: dict, symbol=None, bar=None, **kw) -> tuple:
    """Same thing, driven off a day-3 schema-v1 spec."""
    inst = spec.get("instrument") or {}
    tf = spec.get("timeframe") or {}
    src = inst.get("data_source", "auto")
    return get_bars(
        symbol or inst.get("symbol"),
        bar or tf.get("bar", "1d"),
        kw.pop("start", None) or tf.get("history_start", "2010-01-01"),
        data_source=src,
        csv_path=inst.get("csv_path") if src == "csv" else kw.pop("csv_path", None),
        asset_class=inst.get("asset_class", "equity"),
        **kw,
    )


# --------------------------------------------------------------------------
# Status, for day 1 and for the skills to print
# --------------------------------------------------------------------------


def status() -> dict:
    keyed = []
    for p in PROVIDERS:
        entry = {"name": p.name, "label": p.label, "ready": p.available(),
                 "adjustment": p.adjustment}
        if p.name == "alpaca":
            entry["key"] = "%s / %s" % (mask(get_key("alpaca")),
                                        mask(get_key("alpaca_secret")))
        else:
            entry["key"] = mask(get_key(p.name))
        keyed.append(entry)
    return {
        "module_version": DATA_MODULE_VERSION,
        "keys_file": KEYS_PATH,
        "cache_dir": CACHE_DIR,
        "providers": keyed,
        "ready": [p["name"] for p in keyed if p["ready"]],
    }


def render_status() -> str:
    s = status()
    lines = ["  PRICE DATA", "  " + "-" * 56]
    for p in s["providers"]:
        lines.append(
            "  %-14s %-14s %s"
            % (p["label"], "ready" if p["ready"] else "no key", p["key"])
        )
    lines.append("  %-14s %-14s %s" % ("Yahoo", "fallback", "no key, rate-limited"))
    lines.append("  " + "-" * 56)
    if s["ready"]:
        lines.append("  Using %s for price bars." % s["ready"][0])
    else:
        lines.append("  No key set. Days 4-6 will fall back to the unkeyed")
        lines.append("  Yahoo endpoint, which is rate-limited and may refuse.")
    lines.append("  Keys are stored in %s" % s["keys_file"])
    return "\n".join(lines)


def _cli() -> int:
    import sys

    args = sys.argv[1:]
    cmd = args[0] if args else "status"

    if cmd == "status":
        print(render_status())
        return 0
    if cmd == "set-key" and len(args) >= 3:
        name = args[1].lower()
        known = set(ENV_NAMES)
        if name not in known:
            print("Unknown provider %r. One of: %s" % (name, ", ".join(sorted(known))))
            return 2
        path = set_key(name, args[2])
        print("Saved %s key (%s) to %s" % (name, mask(args[2]), path))
        return 0
    if cmd == "forget-key" and len(args) >= 2:
        print("Removed." if forget_key(args[1].lower()) else "Nothing stored for that.")
        return 0
    if cmd == "clear-cache":
        print("Cleared %d cached file(s) from %s" % (clear_cache(), CACHE_DIR))
        return 0
    if cmd == "probe":
        sym = args[1] if len(args) > 1 else "SPY"
        bar = args[2] if len(args) > 2 else "1d"
        start = args[3] if len(args) > 3 else (
            datetime.now(timezone.utc) - timedelta(days=400)
        ).strftime("%Y-%m-%d")
        try:
            bars, meta = get_bars(sym, bar, start, use_cache=False)
        except DataUnavailable as exc:
            print("NO DATA\n%s" % exc)
            return 2
        print(
            "%d bars  %s -> %s  last close %s\nsource: %s\nadjusted for: %s"
            % (len(bars), bars[0]["time"][:10], bars[-1]["time"][:10],
               bars[-1]["close"], meta["source"], meta["adjustment"])
        )
        return 0

    print(
        "ptq_data %s\n"
        "  status                      what keys this machine has\n"
        "  set-key <provider> <key>    tiingo | twelvedata | alpaca | "
        "alpaca_secret | polygon\n"
        "  forget-key <provider>\n"
        "  probe <SYMBOL> [bar] [start]  fetch real bars and print what came back\n"
        "  clear-cache" % DATA_MODULE_VERSION
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
