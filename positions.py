#!/usr/bin/env python3
"""Hourly check of open journal positions against the system's own management signals. Self-contained (stdlib only).
Reads open.json (list of open trades) and, if present, out.json (scanner result, for OI); writes "positions" into out.json
(or a new file with only "positions"). Signals only, no instructions."""
import calendar, json, os, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

BINANCE = "https://data-api.binance.vision/api/v3/klines?symbol={s}USDT&interval={i}&limit={n}"
PIVOT_N = 3
HIGH_POI = ("PW", "PM", "PQ", "PY", "tedenski", "Monday")


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


out_path = sys.argv[1] if len(sys.argv) > 1 else "out.json"
open_path = sys.argv[2] if len(sys.argv) > 2 else "open.json"
prev_path = sys.argv[3] if len(sys.argv) > 3 else "prev.json"
try:
    res = json.load(open(out_path))
except Exception:
    res = {}
try:
    prev = json.load(open(prev_path))
except Exception:
    prev = {}
try:
    trades = json.load(open(open_path))
except Exception:
    trades = []
now = int(time.time())
cache, positions, lv_cache = {}, {}, {}

for t in trades:
    sym, side = t.get("symbol"), t.get("side")
    entry = float(t.get("entry_fill") or t.get("entry") or 0)
    sl = float(t.get("sl_now") or t.get("sl") or 0)
    sl0 = float(t.get("sl") or 0)
    tp1 = t.get("tp1")
    if not (sym and side and entry and sl0):
        continue
    try:
        if sym not in cache:
            cache[sym] = (closed(klines(sym, "15m", 400), now), closed(klines(sym, "1h", 200), now))
        m15, h1 = cache[sym]
    except Exception as e:
        positions[t["id"]] = {"error": str(e)}
        continue
    long = side == "long"
    created = t.get("created", "")
    try:
        t0 = calendar.timegm(time.strptime(created[:16], "%Y-%m-%dT%H:%M"))
    except Exception:
        t0 = m15[-60]["t"]
    since = [i for i, c in enumerate(m15) if c["t"] >= t0 - 3600]
    i0 = since[0] if since else len(m15) - 40
    price = m15[-1]["c"]
    risk = abs(entry - sl0)
    r_now = ((price - entry) if long else (entry - price)) / risk
    seg = m15[i0:]
    best = max(c["h"] for c in seg) if long else min(c["l"] for c in seg)
    r_best = ((best - entry) if long else (entry - best)) / risk

    # structure on 15m since entry: last pivot on the protective side, and whether a close broke it
    ph, pl = pivots(m15, 3)
    prot = [i for i in (pl if long else ph) if i >= i0]
    against, prot_lvl = False, None
    if prot:
        p = prot[-1]
        prot_lvl = m15[p]["l"] if long else m15[p]["h"]
        against = any((c["c"] < prot_lvl) if long else (c["c"] > prot_lvl) for c in m15[p + 1:])
    # higher lows (long) / lower highs (short) since entry
    same = [i for i in (pl if long else ph) if i >= i0]
    building = len(same) >= 2 and ((m15[same[-1]]["l"] > m15[same[-2]]["l"]) if long else (m15[same[-1]]["h"] < m15[same[-2]]["h"]))
    trend_1h = trend_of(h1, 2)
    cv = cvd(m15)
    cvd_4h = cv[-1] - cv[-17]
    cvd_with = (cvd_4h > 0) == long
    # volume: last 4h vs the 24h before it, and which side carried it
    v4 = sum(c["v"] for c in m15[-16:])
    v24 = sum(c["v"] for c in m15[-112:-16]) / 6 or 1
    vol_ratio = v4 / v24
    up_v = sum(c["v"] for c in m15[-16:] if c["c"] >= c["o"])
    vol_with = (up_v >= v4 / 2) == long
    # open interest change since the previous hourly scan, read together with the price change
    oi_now = (res.get("symbols", {}).get(sym) or {}).get("oi_usd")
    oi_prev = (prev.get("symbols", {}).get(sym) or {}).get("oi_usd")
    px_prev = (prev.get("symbols", {}).get(sym) or {}).get("price")
    oi_note, oi_with = None, None
    if oi_now and oi_prev and px_prev:
        d_oi = (oi_now - oi_prev) / oi_prev * 100
        up = price >= px_prev
        if abs(d_oi) >= 0.3:
            if up and d_oi > 0:
                oi_note, oi_with = "OI raste s ceno (novi longi)", long
            elif up:
                oi_note, oi_with = "OI pada ob rasti (zapiranje shortov, šibkejši dvig)", None
            elif d_oi > 0:
                oi_note, oi_with = "OI raste ob padcu (novi shorti)", not long
            else:
                oi_note, oi_with = "OI pada ob padcu (zapiranje longov)", None
            oi_note += f" {d_oi:+.1f} % v 1h".replace(".", ",")

    notes = []
    if against:
        notes.append(f"15m struktura obrnjena proti poziciji (zaprtje {'pod' if long else 'nad'} {rnd(prot_lvl, price)})")
    elif building:
        notes.append("15m struktura v tvojo smer (" + ("višja dna" if long else "nižji vrhovi") + ")")
    if trend_1h != "range":
        notes.append("1H trend " + ("navzgor" if trend_1h == "bull" else "navzdol"))
    notes.append("CVD 4h " + ("podpira smer" if cvd_with else "ne podpira smeri"))
    notes.append(f"volumen 4h {vol_ratio:.1f}x povprečja, ".replace(".", ",") + ("večinoma v tvojo smer" if vol_with else "večinoma proti tebi"))
    if oi_note:
        notes.append(oi_note)
    if tp1:
        d_tp1 = (float(tp1) - price) / price * 100 * (1 if long else -1)
        if d_tp1 <= 0:
            notes.append("TP1 je dosežen")
        elif d_tp1 < 0.3:
            notes.append(f"TP1 je blizu ({d_tp1:.2f} %)".replace(".", ","))
    # room to the next higher-timeframe level in the trade direction (information only, not a target)
    room = None
    try:
        if sym not in lv_cache:
            d1 = klines(sym, "1d", 400); w1 = klines(sym, "1w", 60)
            lv_cache[sym] = htf_levels(d1, w1, h1, now) + extra_htf_levels(closed(d1, now), w1, now)
        ahead = [(lab, p) for lab, p, _ in lv_cache[sym] if lab.startswith(HIGH_POI) and (p > price * 1.002 if long else p < price * 0.998)]
        if ahead:
            lab, p = min(ahead, key=lambda z: abs(z[1] - price))
            r_at = ((p - entry) if long else (entry - p)) / risk
            room = {"label": lab, "price": rnd(p, price), "r_at": round(r_at, 1)}
    except Exception:
        room = None
    trend_against = trend_1h == ("bear" if long else "bull")
    warn = sum([against, trend_against, not cvd_with, not vol_with and vol_ratio > 1.2, oi_with is False])
    label = "struktura proti poziciji" if against else ("pozor" if warn >= 3 else "struktura drži")
    room_alert = bool(room and room["r_at"] >= 4 and label == "struktura drži" and cvd_with and vol_with)
    if room:
        txt = f"naslednji HTF nivo {room['label']} {room['price']} (+{room['r_at']}R od vstopa)".replace(".", ",")
        notes.append(("🚀 prostor: " + txt + ", struktura, CVD in volumen v smer") if room_alert else txt)
    positions[t["id"]] = {
        "symbol": sym, "side": side, "price": rnd(price, price), "r_now": round(r_now, 2), "r_best": round(r_best, 2),
        "sl_now": rnd(sl, price), "d_sl_pct": round(abs(price - sl) / price * 100, 2),
        "tp1": tp1, "protect_level": rnd(prot_lvl, price) if prot_lvl else None,
        "structure_against": against, "structure_building": building, "trend_1h": trend_1h, "cvd_with": cvd_with,
        "vol_ratio": round(vol_ratio, 2), "vol_with": vol_with, "oi_with": oi_with,
        "label": label, "notes": notes, "checked_at": iso(now), "room": room, "room_alert": room_alert,
    }

res["positions"] = positions
json.dump(res, open(out_path, "w"), indent=1)
print(json.dumps(positions, ensure_ascii=False, indent=1))
