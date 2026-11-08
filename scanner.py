#!/usr/bin/env python3
"""Stefan_B_Trades method scanner. Stdlib only.

Data: Binance spot klines (OHLCV + taker buy volume -> CVD), Hyperliquid perps (funding, OI, mark).
Output: JSON with per-symbol context and setup candidates. All times UTC.
"""
import json, math, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

SYMBOLS = ["BTC", "ETH", "ZEC"]
ALT_COUNT = 40          # most liquid perps (Hyperliquid daily volume) scanned for complete setups only
ALT_SKIP = {"USDC", "USDT", "FDUSD", "DAI", "USDE", "PAXG", "XAUT"}
BINANCE = "https://data-api.binance.vision/api/v3/klines?symbol={s}USDT&interval={i}&limit={n}"
HL = "https://api.hyperliquid.xyz/info"

# Sessions (UTC) for session highs/lows, and kill zones for timing labels
SESSIONS = [("Asia", 0, 7), ("London", 7, 13), ("NY", 13, 21)]
KILLZONES = [("Asia KZ", 0, 3), ("London KZ", 7, 10), ("NY KZ", 13, 16)]

PIVOT_N = 3            # 1H pivot strength
SETUP_WINDOW = 48      # 1H bars to look back for model 1
ZONE_TOL = 0.0015      # 0.15 % tolerance for confluence matching
POI_BAND = 0.015       # extreme may overshoot an HTF level by at most 1.5 % to count as "in" the POI


# ---------------------------------------------------------------- data
def get(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "scanner/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)


def klines(sym, interval, limit):
    raw = get(BINANCE.format(s=sym, i=interval, n=limit))
    out = []
    for k in raw:
        out.append({"t": k[0] // 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                    "c": float(k[4]), "v": float(k[5]), "tbv": float(k[9]), "T": k[6] // 1000})
    return out


def hl_ctx():
    """Funding / OI / mark for every Hyperliquid perp, plus the alt list ranked by daily volume."""
    m, ctx = get(HL, json.dumps({"type": "metaAndAssetCtxs"}).encode())
    res, vol = {}, []
    for i, a in enumerate(m["universe"]):
        c = ctx[i]
        try:
            mark = float(c["markPx"])
        except (TypeError, ValueError, KeyError):
            continue
        res[a["name"]] = {"funding_8h_pct": float(c["funding"]) * 8 * 100,
                          "oi_usd": float(c["openInterest"]) * mark,
                          "mark": mark, "prev_day": float(c["prevDayPx"])}
        if not a.get("isDelisted"):
            vol.append((float(c.get("dayNtlVlm") or 0), a["name"]))
    alts = [n for _, n in sorted(vol, reverse=True) if n not in SYMBOLS and n not in ALT_SKIP and n.isalnum() and not n.startswith("k")]
    return res, alts


def closed(cs, now):
    return [c for c in cs if c["T"] < now]


def dt(t):
    return datetime.fromtimestamp(t, timezone.utc)


def iso(t):
    return dt(t).strftime("%Y-%m-%dT%H:%MZ")


def rnd(x, ref):
    if x is None:
        return None
    d = 1 if ref > 1000 else 2 if ref > 10 else 4
    return round(x, d)


# ---------------------------------------------------------------- levels
def htf_levels(d1, w1, h1, now):
    """Previous day / week / month levels as they stood at time `now`."""
    L = []
    d1 = [c for c in d1 if c["t"] <= now]
    w1 = [c for c in w1 if c["t"] <= now]
    if len(d1) >= 2:
        pd = d1[-2]
        L += [("PDH", pd["h"], "high"), ("PDL", pd["l"], "low")]
    if len(w1) >= 2:
        pw = w1[-2]
        L += [("PWH", pw["h"], "high"), ("PWL", pw["l"], "low")]
        cur_w = w1[-1]
        mon = [c for c in d1 if c["t"] == cur_w["t"] and c["T"] < now]
        if mon and dt(now).weekday() > 0:
            L += [("Monday high", mon[0]["h"], "high"), ("Monday low", mon[0]["l"], "low")]
    # previous month from daily
    cur = dt(now)
    first_this = datetime(cur.year, cur.month, 1, tzinfo=timezone.utc)
    prev_last = first_this - timedelta(days=1)
    first_prev = datetime(prev_last.year, prev_last.month, 1, tzinfo=timezone.utc)
    pm = [c for c in d1 if first_prev.timestamp() <= c["t"] < first_this.timestamp()]
    if pm:
        L += [("PMH", max(c["h"] for c in pm), "high"), ("PML", min(c["l"] for c in pm), "low")]
    return L


def session_levels(h1, now):
    """Most recent completed Asia / London / NY session high and low."""
    L = []
    for name, a, b in SESSIONS:
        for back in range(0, 3):
            day = (dt(now) - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
            start, end = day + timedelta(hours=a), day + timedelta(hours=b)
            if end.timestamp() > now:
                continue
            cs = [c for c in h1 if start.timestamp() <= c["t"] < end.timestamp()]
            if cs:
                L += [(f"{name} high", max(c["h"] for c in cs), "high"),
                      (f"{name} low", min(c["l"] for c in cs), "low")]
                break
    return L


def fvgs(cs, label, price, maxdist=0.15, keep=4):
    """Unfilled 3-candle fair value gaps. Bullish: c1.high < c3.low. Bearish: c1.low > c3.high."""
    out = []
    for i in range(1, len(cs) - 1):
        a, c = cs[i - 1], cs[i + 1]
        later = cs[i + 2:]
        if a["h"] < c["l"]:  # bullish gap (support)
            lo, hi = a["h"], c["l"]
            if later:
                lo_seen = min(x["l"] for x in later)
                if lo_seen <= lo:
                    continue
                hi = min(hi, lo_seen)
            if hi > lo:
                out.append({"label": f"{label} bull FVG", "lo": lo, "hi": hi, "side": "low", "t": cs[i]["t"]})
        elif a["l"] > c["h"]:  # bearish gap (resistance)
            lo, hi = c["h"], a["l"]
            if later:
                hi_seen = max(x["h"] for x in later)
                if hi_seen >= hi:
                    continue
                lo = max(lo, hi_seen)
            if hi > lo:
                out.append({"label": f"{label} bear FVG", "lo": lo, "hi": hi, "side": "high", "t": cs[i]["t"]})
    out = [f for f in out if abs((f["lo"] + f["hi"]) / 2 - price) / price < maxdist]
    return sorted(out, key=lambda f: -f["t"])[:keep]


def volume_profile(cs, bins=120):
    lo, hi = min(c["l"] for c in cs), max(c["h"] for c in cs)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for c in cs:
        a = int((c["l"] - lo) / step)
        b = min(int((c["h"] - lo) / step), bins - 1)
        a = min(a, bins - 1)
        n = b - a + 1
        for k in range(a, b + 1):
            vol[k] += c["v"] / n
    poc = max(range(bins), key=lambda k: vol[k])
    total, acc = sum(vol), vol[poc]
    i, j = poc, poc
    while acc < 0.7 * total and (i > 0 or j < bins - 1):
        up = vol[j + 1] if j < bins - 1 else -1
        dn = vol[i - 1] if i > 0 else -1
        if up >= dn:
            j += 1; acc += up
        else:
            i -= 1; acc += dn
    mid = lambda k: lo + (k + 0.5) * step
    return {"poc": mid(poc), "vah": lo + (j + 1) * step, "val": lo + i * step}


def daily_profiles(m15, now, days=10):
    """Per UTC-day session profiles from 15m candles, with naked POC detection."""
    out = []
    today0 = dt(now).replace(hour=0, minute=0, second=0, microsecond=0)
    for back in range(1, days + 1):
        s = (today0 - timedelta(days=back)).timestamp()
        e = s + 86400
        cs = [c for c in m15 if s <= c["t"] < e]
        if len(cs) < 40:
            continue
        vp = volume_profile(cs, bins=75)
        after = [c for c in m15 if c["t"] >= e]
        naked = bool(after) and not any(c["l"] <= vp["poc"] <= c["h"] for c in after)
        out.append({"day": dt(s).strftime("%d.%m."), "t": s, **vp, "naked": naked})
    return out


def pivots(cs, n=PIVOT_N):
    ph, pl = [], []
    for i in range(n, len(cs) - n):
        win = cs[i - n:i + n + 1]
        if cs[i]["h"] == max(c["h"] for c in win):
            ph.append(i)
        if cs[i]["l"] == min(c["l"] for c in win):
            pl.append(i)
    return ph, pl


def cvd(cs):
    out, s = [], 0.0
    for c in cs:
        s += 2 * c["tbv"] - c["v"]
        out.append(s)
    return out


def atr(cs, n=14):
    trs = []
    for i in range(1, len(cs)):
        p = cs[i - 1]["c"]
        trs.append(max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - p), abs(cs[i]["l"] - p)))
    return sum(trs[-n:]) / n if len(trs) >= n else None


def zone_of(t):
    h = dt(t).hour
    for name, a, b in KILLZONES:
        if a <= h < b:
            return name
    for name, a, b in SESSIONS:
        if a <= h < b:
            return name + " seja"
    return "izven sej"


def range_position(d1, days=60):
    cs = d1[-days:]
    hi, lo = max(c["h"] for c in cs), min(c["l"] for c in cs)
    return hi, lo


def trend_of(cs, n=3):
    """Structure trend from the last two pivot highs and lows: bull (HH+HL), bear (LH+LL), else range."""
    ph, pl = pivots(cs, n)
    if len(ph) < 2 or len(pl) < 2:
        return "range"
    hh = cs[ph[-1]]["h"] > cs[ph[-2]]["h"]
    hl = cs[pl[-1]]["l"] > cs[pl[-2]]["l"]
    if hh and hl:
        return "bull"
    if not hh and not hl:
        return "bear"
    return "range"


def extra_htf_levels(d1, w1, now):
    """Previous year / quarter extremes and weekly swing levels."""
    L = []
    cur = dt(now)
    y0 = datetime(cur.year, 1, 1, tzinfo=timezone.utc).timestamp()
    py0 = datetime(cur.year - 1, 1, 1, tzinfo=timezone.utc).timestamp()
    py = [c for c in d1 if py0 <= c["t"] < y0]
    if len(py) > 200:
        L += [("PYH", max(c["h"] for c in py), "high"), ("PYL", min(c["l"] for c in py), "low")]
    q = (cur.month - 1) // 3
    q0 = datetime(cur.year, q * 3 + 1, 1, tzinfo=timezone.utc)
    pq_start = datetime(cur.year - 1, 10, 1, tzinfo=timezone.utc) if q == 0 else datetime(cur.year, (q - 1) * 3 + 1, 1, tzinfo=timezone.utc)
    pq = [c for c in d1 if pq_start.timestamp() <= c["t"] < q0.timestamp()]
    if pq:
        L += [("PQH", max(c["h"] for c in pq), "high"), ("PQL", min(c["l"] for c in pq), "low")]
    wc = [c for c in w1 if c["T"] < now]
    ph, pl = pivots(wc, 2)
    for i in ph[-3:]:
        L.append(("tedenski swing high", wc[i]["h"], "high"))
    for i in pl[-3:]:
        L.append(("tedenski swing low", wc[i]["l"], "low"))
    return L


def order_blocks(h4, price, keep=4):
    """4H order blocks: last opposite candle before a displacement that broke structure, not yet mitigated."""
    out = []
    a = atr(h4)
    if not a:
        return out
    for k in range(2, len(h4) - 1):
        c = h4[k]
        rngc, body = c["h"] - c["l"], abs(c["c"] - c["o"])
        if rngc < 1.5 * a or body < 0.6 * rngc:
            continue
        prev = h4[k - 1]
        later = h4[k + 1:]
        if c["c"] > c["o"] and prev["c"] < prev["o"]:      # bullish OB = last down candle
            lo, hi = prev["l"], prev["h"]
            if later and min(x["l"] for x in later) <= lo:
                continue
            out.append({"label": "4H bull OB", "lo": lo, "hi": hi, "side": "low", "t": prev["t"]})
        elif c["c"] < c["o"] and prev["c"] > prev["o"]:    # bearish OB = last up candle
            lo, hi = prev["l"], prev["h"]
            if later and max(x["h"] for x in later) >= hi:
                continue
            out.append({"label": "4H bear OB", "lo": lo, "hi": hi, "side": "high", "t": prev["t"]})
    out = [o for o in out if abs((o["lo"] + o["hi"]) / 2 - price) / price < 0.15]
    return sorted(out, key=lambda o: -o["t"])[:keep]


def trendline_at(cs, piv_idx, key, at_i):
    """Line through the last two pivots (indices into cs), evaluated at index at_i."""
    if len(piv_idx) < 2:
        return None
    i1, i2 = piv_idx[-2], piv_idx[-1]
    if i2 == i1:
        return None
    y1, y2 = cs[i1][key], cs[i2][key]
    return y2 + (y2 - y1) / (i2 - i1) * (at_i - i2)


def loc_label(pos):
    if pos >= 0.75: return "deep premium"
    if pos <= 0.25: return "deep discount"
    if pos >= 0.5: return "premium"
    return "discount"


# ---------------------------------------------------------------- confluence
def confluences(lo, hi, points, zones, exclude=()):
    """Levels (label, price) and zones (label, lo, hi) that sit inside [lo, hi] (with tolerance)."""
    tl, th = lo * (1 - ZONE_TOL), hi * (1 + ZONE_TOL)
    hits = []
    for lab, p in points:
        if lab not in exclude and tl <= p <= th:
            hits.append(lab)
    for lab, a, b in zones:
        if a <= th and b >= tl:
            hits.append(lab)
    return sorted(set(hits))


# ---------------------------------------------------------------- model 1: SS / asymmetric
def model1(sym, h1, cv, d1, w1, fv_all, points, zones, rng, side, tf="1H", window=SETUP_WINDOW, trend_d="range", extra=()):
    out = []
    n = len(h1)
    if n < window + 10:
        return out
    w0 = n - window
    short = side == "short"
    win = range(w0, n)
    ext_i = max(win, key=lambda i: h1[i]["h"]) if short else min(win, key=lambda i: h1[i]["l"])
    ext = h1[ext_i]["h"] if short else h1[ext_i]["l"]
    ref_t = h1[w0]["t"]                      # levels as they stood when the window opened
    htf = htf_levels(d1, w1, h1, ref_t) + list(extra)
    sess = session_levels([c for c in h1 if c["t"] < ref_t], ref_t)

    # 1. HTF POI: the extreme reached a higher timeframe level or HTF FVG on the right side
    poi = []
    # the extreme must reach the level and not overshoot it by more than POI_BAND (price is "in" the POI)
    for lab, p, s in htf:
        if short and s == "high" and p * (1 - 0.001) <= ext <= p * (1 + POI_BAND):
            poi.append(lab)
        if not short and s == "low" and p * (1 - POI_BAND) <= ext <= p * (1 + 0.001):
            poi.append(lab)
    for f in fv_all:
        if f["label"].startswith(("4H", "1D")):
            if short and f["side"] == "high" and f["lo"] <= ext <= f["hi"] * (1 + POI_BAND):
                poi.append(f["label"])
            if not short and f["side"] == "low" and f["lo"] * (1 - POI_BAND) <= ext <= f["hi"]:
                poi.append(f["label"])
    poi = sorted(set(poi))

    # 2. LTF sweep (SFP): a candle in the window wicks through a level and closes back
    sweeps = []
    for i in win:
        c = h1[i]
        for lab, p, s in sess + htf:
            if short and s == "high" and c["h"] > p and c["c"] < p and h1[i - 1]["h"] <= p * 1.002 and abs(c["h"] - ext) / ext < 0.01:
                sweeps.append((lab, i))
            if not short and s == "low" and c["l"] < p and c["c"] > p and h1[i - 1]["l"] >= p * 0.998 and abs(c["l"] - ext) / ext < 0.01:
                sweeps.append((lab, i))
    sweep_labels = sorted(set(l for l, _ in sweeps))
    sweep_t = h1[sweeps[-1][1]]["t"] if sweeps else None

    if not poi and not sweeps:
        return out

    # 3. MSS: close beyond the pivot that created the extreme
    ph, pl = pivots(h1)
    piv = [i for i in (pl if short else ph) if i < ext_i]
    if not piv:
        return out
    p0_i = piv[-1]
    p0 = h1[p0_i]["l"] if short else h1[p0_i]["h"]
    mss_i = None
    for i in range(ext_i + 1, n):
        if (short and h1[i]["c"] < p0) or (not short and h1[i]["c"] > p0):
            mss_i = i
            break

    # divergence: extreme vs previous pivot on the same side, compared with CVD
    div = False
    prev = [i for i in (ph if short else pl) if i < ext_i - 1 and i >= ext_i - window * 1.5]
    if prev:
        j = prev[-1]
        if short and h1[ext_i]["h"] > h1[j]["h"] and cv[ext_i] < cv[j]:
            div = True
        if not short and h1[ext_i]["l"] < h1[j]["l"] and cv[ext_i] > cv[j]:
            div = True

    # Fib: 0 = displaced pivot, 1 = extreme
    span = ext - p0
    if (short and span <= 0) or (not short and span >= 0) or abs(span) / ext < 0.004:   # no real impulse leg
        return out
    lvl = lambda r: p0 + r * span
    gp_lo, gp_hi = sorted([lvl(0.618), lvl(0.658)])
    entry = lvl(0.638)
    sl = ext * (1.001 if short else 0.999)
    targets = [("-1", lvl(-1)), ("-2", lvl(-2)), ("-2.5", lvl(-2.5))]
    risk = abs(sl - entry)
    rr = [(lab, round(abs(entry - p) / risk, 1)) for lab, p in targets] if risk else []

    # status
    last = h1[-1]
    after = h1[ext_i + 1:]
    stage, status = 1, "cena v HTF POI" if poi else "sweep brez HTF POI"
    if sweeps:
        stage = 2; status = "sweep, čaka MSS"
    if mss_i is not None:
        stage = 3; status = "MSS potrjen, čaka retest golden pocketa"
        post = h1[mss_i + 1:]
        touched = any((c["h"] >= gp_lo) if short else (c["l"] <= gp_hi) for c in post)
        if touched:
            stage = 4; status = "cena je bila v golden pocketu"
        if any((c["h"] > ext) if short else (c["l"] < ext) for c in post):
            stage = 0; status = "neveljavno, ekstrem prebit"
        elif any((c["l"] <= targets[0][1]) if short else (c["h"] >= targets[0][1]) for c in post):
            if not touched:
                stage = 0; status = "zamujeno, cilj -1 dosežen brez retesta"
            else:
                status = "cilj -1 dosežen"
    elif any((c["h"] > ext) if short else (c["l"] < ext) for c in after):
        stage = 0; status = "neveljavno"
    missing = [n for n, ok in (("HTF POI", poi), ("LTF sweep", sweeps)) if not ok]
    if trend_d != "range" and (trend_d == "bull") == short:
        missing.append("dnevni trend " + ("navzgor" if trend_d == "bull" else "navzdol"))
    if stage >= 3 and missing:
        status += " (manjka: " + ", ".join(missing) + ")"

    # location rule: deep premium -> no new longs, deep discount -> no new shorts
    hi, lo = rng
    pos = (ext - lo) / (hi - lo) if hi > lo else 0.5
    loc_ok = (pos >= 0.5) if short else (pos <= 0.5)

    conf = confluences(gp_lo, gp_hi, points, zones)
    # approximate trendline: through the last two pivots on the opposite side before the extreme, projected to now
    tl_piv = [i for i in (ph if short else pl) if i <= ext_i]
    tl = trendline_at(h1, tl_piv[:-1] if len(tl_piv) > 2 and tl_piv[-1] == ext_i else tl_piv, "h" if short else "l", n - 1)
    if tl and abs(tl - last["c"]) / last["c"] > 0.05:
        tl = None
    if tl and gp_lo * (1 - ZONE_TOL * 2) <= tl <= gp_hi * (1 + ZONE_TOL * 2):
        conf = sorted(set(conf + ["trendline"]))
    # higher timeframe direction: the daily structure must not point against the trade
    htf_ok = trend_d == "range" or (trend_d == "bull") == (not short)
    checklist = {
        "htf_poi": bool(poi), "ltf_sweep": bool(sweeps), "mss": mss_i is not None,
        "divergence": div, "lokacija": loc_ok, "htf_smer": htf_ok, "konfluence_v_gp": len(conf),
    }
    score = sum([bool(poi), bool(sweeps), mss_i is not None, div, loc_ok, htf_ok]) + min(len(conf), 3)

    out.append({
        "id": f"m1{'' if tf == '1H' else tf}-{sym}-{side}-{h1[ext_i]['t']}",
        "model": f"SS / Model 1 ({tf})", "tf": tf, "symbol": sym, "side": side, "stage": stage, "status": status,
        "extreme": rnd(ext, ext), "extreme_t": iso(h1[ext_i]["t"]),
        "p0": rnd(p0, ext), "mss_t": iso(h1[mss_i]["t"]) if mss_i is not None else None,
        "htf_poi": poi, "sweeps": sweep_labels, "sweep_zone": zone_of(sweep_t) if sweep_t else None,
        "divergence": div, "range_pos": round(pos, 2), "location": loc_label(pos), "location_ok": loc_ok,
        "entry_zone": [rnd(gp_lo, ext), rnd(gp_hi, ext)], "entry": rnd(entry, ext), "sl": rnd(sl, ext),
        "eq_partial": rnd(lvl(0.5), ext),
        "targets": [[l, rnd(p, ext)] for l, p in targets], "rr": rr,
        "confluences": conf, "checklist": checklist, "score": score, "price": rnd(last["c"], ext),
        "trend_1d": trend_d, "trendline": rnd(tl, ext) if tl else None,
    })
    return out


# ---------------------------------------------------------------- main
def scan_symbol(sym, now, hl):
    m15 = klines(sym, "15m", 1000)
    h1 = klines(sym, "1h", 1000)
    h4 = klines(sym, "4h", 500)
    d1 = klines(sym, "1d", 400)
    w1 = klines(sym, "1w", 60)
    price = h1[-1]["c"]
    h1c, h4c, m15c = closed(h1, now), closed(h4, now), closed(m15, now)

    htf = htf_levels(d1, w1, h1, now)
    extra = extra_htf_levels(closed(d1, now), w1, now)
    sess = session_levels(h1c, now)
    fv = fvgs(closed(d1, now), "1D", price) + fvgs(h4c, "4H", price) + fvgs(h1c[-200:], "1H", price, 0.05) + order_blocks(h4c, price)
    trend_d, trend_4h = trend_of(closed(d1, now)), trend_of(h4c)
    vp30 = volume_profile(h1c[-720:])
    prof = daily_profiles(m15c, now)
    naked = [p for p in prof if p["naked"]]
    rng = range_position(d1)
    cv = cvd(h1c)

    points = [(l, p) for l, p, _ in htf + extra + sess]
    points += [("VP30 POC", vp30["poc"]), ("VP30 VAH", vp30["vah"]), ("VP30 VAL", vp30["val"])]
    if prof:
        points += [("včeraj POC", prof[0]["poc"]), ("včeraj VAH", prof[0]["vah"]), ("včeraj VAL", prof[0]["val"])]
    points += [(f"naked POC {p['day']}", p["poc"]) for p in naked]
    zones = [(f["label"], f["lo"], f["hi"]) for f in fv]

    setups = []
    for side in ("short", "long"):
        setups += model1(sym, h1c, cv, d1, w1, fv, points, zones, rng, side, trend_d=trend_d, extra=extra)
        setups += model1(sym, m15c, cvd(m15c), d1, w1, fv, points, zones, rng, side, "15m", 96, trend_d=trend_d, extra=extra)
        setups += model1(sym, h4c[-300:], cvd(h4c[-300:]), d1, w1, fv, points, zones, rng, side, "4H", 42, trend_d=trend_d, extra=extra)

    hi, lo = rng
    pos = (price - lo) / (hi - lo) if hi > lo else 0.5
    near = sorted([{"label": l, "price": rnd(p, price), "dist_pct": round((p - price) / price * 100, 2)}
                   for l, p in points], key=lambda x: abs(x["dist_pct"]))[:10]
    # 24h CVD slope sign vs price change
    d_price = h1c[-1]["c"] - h1c[-25]["c"]
    d_cvd = cv[-1] - cv[-25]
    ctx = {
        "price": rnd(price, price), "range_hi": rnd(hi, price), "range_lo": rnd(lo, price),
        "range_pos": round(pos, 2), "location": loc_label(pos), "trend_1d": trend_d, "trend_4h": trend_4h,
        "gann": {k: rnd(lo + f * (hi - lo), price) for k, f in (("0.25", .25), ("0.5", .5), ("0.75", .75))},
        "vp30": {k: rnd(v, price) for k, v in vp30.items()},
        "naked_pocs": [{"day": p["day"], "poc": rnd(p["poc"], price)} for p in naked],
        "fvgs": [{"label": f["label"], "lo": rnd(f["lo"], price), "hi": rnd(f["hi"], price)} for f in fv],
        "near_levels": near,
        "cvd_24h": "raste" if d_cvd > 0 else "pada",
        "price_24h_pct": round(d_price / h1c[-25]["c"] * 100, 2),
        "cvd_divergence_24h": (d_price > 0) != (d_cvd > 0),
        **({k: (round(v, 4) if k == "funding_8h_pct" else round(v)) for k, v in hl.get(sym, {}).items()}),
    }
    return ctx, setups


HIGH_POI = ("PW", "PM", "PQ", "PY", "tedenski", "Monday")


def rule_flags(x, btc_trend):
    """Extra conditions from the backtest: BTC daily trend not against the trade; altcoins need a weekly or higher POI."""
    x["btc_trend_1d"] = btc_trend
    x["btc_ok"] = not ((x["side"] == "long" and btc_trend == "bear") or (x["side"] == "short" and btc_trend == "bull"))
    x["poi_high"] = any(p.startswith(HIGH_POI) for p in x.get("htf_poi") or [])


MAX_OPEN = 5


def conditions_ok(x):
    """Stefan conditions kept by the backtest: HTF POI, LTF sweep, MSS, coin daily trend not against.
    The premium/discount location rule is dropped (it removed more good trades than bad ones)."""
    c = x.get("checklist") or {}
    return x.get("stage", 0) >= 3 and c.get("htf_poi") and c.get("ltf_sweep") and c.get("mss") and c.get("htf_smer") is not False


def is_ready(x, prev_ready, open_syms, btc_trend="range", n_open=0):
    """Tradeable under the backtested rule: Model 1 on 1H or 4H, conditions met right after the MSS (before price came back
    to the zone), BTC daily trend not against, weekly+ POI for altcoins, no open trade on the same coin, fewer than 5 open trades.
    A setup flagged ready at stage 3 stays ready when price then reaches the zone."""
    if x.get("tf") not in ("1H", "4H") or not x["model"].startswith("SS") or not conditions_ok(x) or x["symbol"] in open_syms:
        return False
    if n_open >= MAX_OPEN:
        return False
    # the limit order lives 48 h (1H) or 7 days (4H) after the MSS, as in the backtest
    try:
        mss_age = time.time() - datetime.strptime(x["mss_t"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return False
    if mss_age > (7 * 86400 if x["tf"] == "4H" else 2 * 86400):
        return False
    rule_flags(x, btc_trend)
    if not x["btc_ok"] or (x["symbol"] not in SYMBOLS and not x["poi_high"]):
        return False
    return x["stage"] == 3 or (x["stage"] == 4 and x["id"] in prev_ready)


def main():
    from concurrent.futures import ThreadPoolExecutor
    now = int(time.time())
    prev_ready, open_syms, n_open = set(), set(), 0
    try:
        prev_ready = {x["id"] for x in json.load(open(sys.argv[2])).get("setups", []) if x.get("ready")}
    except Exception:
        pass
    try:
        ot = json.load(open(sys.argv[3]))
        open_syms = {t["symbol"] for t in ot if t.get("symbol")}
        n_open = len(ot)
    except Exception:
        pass
    hl, alts = {}, []
    try:
        hl, alts = hl_ctx()
    except Exception as e:
        print(f"hyperliquid error: {e}", file=sys.stderr)
    result = {"generated_at": iso(now), "generated_ts": now, "symbols": {}, "setups": [], "errors": [], "alts": {}}
    for s in SYMBOLS:
        try:
            ctx, st = scan_symbol(s, now, hl)
            result["symbols"][s] = ctx
            result["setups"] += st
        except Exception as e:
            result["errors"].append(f"{s}: {e}")

    btc_trend = (result["symbols"].get("BTC") or {}).get("trend_1d", "range")
    # altcoins: only setups that are ready under the rules are kept
    def scan_alt(sym):
        try:
            ctx, st = scan_symbol(sym, now, hl)
            return sym, ctx, [x for x in st if is_ready(x, prev_ready, open_syms, btc_trend, n_open)], None
        except Exception as e:
            return sym, None, [], str(e)
    picked, scanned = [], 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for sym, ctx, st, err in ex.map(scan_alt, alts[:ALT_COUNT + 15]):
            if err or ctx is None:
                continue                       # not on Binance or not enough history: skip silently
            if scanned >= ALT_COUNT:
                break
            scanned += 1
            if st:
                result["setups"] += st
                result["alts"][sym] = {k: ctx.get(k) for k in ("price", "location", "range_pos", "trend_1d", "trend_4h", "funding_8h_pct", "oi_usd")}
            picked.append(sym)
    result["alt_universe"] = picked
    result["open_count"] = n_open
    for x in result["setups"]:
        x["ready"] = is_ready(x, prev_ready, open_syms, btc_trend, n_open)
        if x["model"].startswith("SS"):
            rule_flags(x, btc_trend)
        if x["symbol"] not in SYMBOLS:
            x["alt"] = True
    result["setups"].sort(key=lambda x: (-x["stage"], -x["score"]))
    json.dump(result, sys.stdout if len(sys.argv) < 2 else open(sys.argv[1], "w"), indent=1)


if __name__ == "__main__":
    main()
