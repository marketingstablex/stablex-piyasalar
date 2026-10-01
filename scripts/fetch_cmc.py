#!/usr/bin/env python3
"""
CoinMarketCap'ten piyasa verisini çekip data/markets.json dosyasına yazar.
Sayfa (index.html) bu dosyayı ana kaynak olarak okur; dosya yoksa/eskiyse CoinGecko'ya döner.

Kredi bütçesi (Basic plan: 10.000 kredi/ay):
  her çalıştırmada   : quotes/latest (≤100 coin = 1) + global-metrics (1)        = 2 kredi
  2 saatte bir       : fear-and-greed (1) + listings (altcoin sezonu, 1) + USD/TRY (1)
  15 dakikada bir çalışınca ≈ 2.880 × 2 + 360 × 3 ≈ 7.000 kredi/ay
CMC id eşlemesi data/cmc_ids.json'da tutulur; yalnızca yeni coin geldiğinde map çağrısı yapılır.

Ortam değişkenleri: CMC_API_KEY (zorunlu), REVIEW_PATH (isteğe bağlı).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COINS_FILE = ROOT / "data" / "coins.json"
IDS_FILE = ROOT / "data" / "cmc_ids.json"
OUT_FILE = ROOT / "data" / "markets.json"
REVIEW_FILE = Path(os.environ.get("REVIEW_PATH") or ROOT / "review_cmc.md")

API = "https://pro-api.coinmarketcap.com"
SLOW_EVERY_S = 2 * 3600          # F&G, altcoin sezonu, kur yenileme aralığı

STABLES = {"usdt", "usdc", "dai", "usde", "usds", "fdusd", "tusd", "pyusd", "usd1", "usdd", "busd", "gusd",
           "frax", "lusd", "susds", "susde", "rlusd", "usdtb", "usdf", "usdg", "bfusd", "eurc", "usd0",
           "buidl", "ustb", "usyc", "xaut", "paxg"}
WRAPPED = {"wbtc", "cbbtc", "weth", "steth", "wsteth", "weeth", "reth", "meth", "wbeth", "lbtc", "solvbtc",
           "jitosol", "msol", "bnsol", "jupsol", "clbtc", "tbtc", "fbtc", "ebtc", "rseth", "ezeth", "oseth",
           "cmeth", "lseth", "sweth", "btcb", "unibtc", "wbnb", "wsol", "bsol", "stsol", "sfrxeth", "frxeth",
           "ethx", "pufeth", "rsweth", "sdai", "syrupusdc", "bbsol", "hbtc"}

class PlanError(Exception):
    """Endpoint bu planda yok (403/1006 vb.)."""


def cmc(path: str, **params):
    key = os.environ.get("CMC_API_KEY")
    if not key:
        raise SystemExit("CMC_API_KEY tanımlı değil (Settings → Secrets → Actions).")
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-CMC_PRO_API_KEY": key, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())["data"]
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code in (401, 402, 403):
                raise PlanError(f"{path}: {e.code} {body}")
            if e.code == 429:
                time.sleep(30)
                continue
            if attempt == 2:
                raise RuntimeError(f"{path}: {e.code} {body}")
        except urllib.error.URLError as e:
            if attempt == 2:
                raise RuntimeError(f"{path}: {e}")
        time.sleep(3)
    raise RuntimeError(f"{path}: başarısız")


def norm(s: str) -> str:
    s = re.sub(r"\(.*?\)", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def pick(candidates: list, name: str):
    """Aynı sembollü projeler içinden ad eşleşmesine, yoksa CMC sırasına göre seçer."""
    act = [c for c in candidates if c.get("is_active", 1)] or candidates
    n = norm(name)
    first = n.split(" ")[0] if n else ""
    byname = [c for c in act if norm(c["name"]) == n or (n and (n in norm(c["name"]) or norm(c["name"]) in n))]
    if not byname and first:
        byname = [c for c in act if norm(c["name"]).split(" ")[0] == first]
    pool = byname or act
    pool.sort(key=lambda c: c.get("rank") or 10**9)
    return pool[0], bool(byname), len(act)


def resolve_ids(coins: list, ids: dict, review: list) -> dict:
    notfound = ids.setdefault("_bulunamayan", {})          # {SYM: son deneme zamanı} → günde bir tekrar dene
    now = int(time.time())
    missing = [c for c in coins if c[0] not in ids and now - notfound.get(c[0], 0) >= 86400]
    if not missing:
        return ids
    data = cmc("/v1/cryptocurrency/map", symbol=",".join(c[0] for c in missing))
    by_sym: dict[str, list] = {}
    for d in data:
        by_sym.setdefault(d["symbol"].upper(), []).append(d)
    for sym, _cg, name, *_ in missing:
        cands = by_sym.get(sym, [])
        if not cands:
            notfound[sym] = now
            review.append(f"- **{sym}** CoinMarketCap'te bulunamadı; bu coin için CoinGecko kullanılacak.")
            continue
        best, by_name, n = pick(cands, name)
        ids[sym] = best["id"]
        notfound.pop(sym, None)
        if n > 1 or not by_name:
            others = ", ".join(f"{c['name']} (id {c['id']})" for c in cands if c is not best)[:300]
            review.append(f"- **{sym}** → CMC `{best['id']}` ({best['name']}) seçildi"
                          f"{'' if by_name else ' (ad eşleşmedi, CMC sırasına göre)'}. Diğer adaylar: {others}. "
                          f"Yanlışsa `data/cmc_ids.json` içinde düzeltin.")
    return ids


def main() -> int:
    coins = json.loads(COINS_FILE.read_text(encoding="utf-8"))["coins"]
    ids = json.loads(IDS_FILE.read_text(encoding="utf-8")) if IDS_FILE.exists() else {}
    prev = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}
    review: list[str] = []

    # Stablex'ten kalkanları eşlemeden temizle
    live = {c[0] for c in coins}
    nf = {k: v for k, v in ids.get("_bulunamayan", {}).items() if k in live}
    ids = {k: v for k, v in ids.items() if k in live}
    ids["_bulunamayan"] = nf
    ids = resolve_ids(coins, ids, review)
    IDS_FILE.write_text(json.dumps(dict(sorted(ids.items())), indent=1) + "\n", encoding="utf-8")

    # --- fiyatlar (her çalıştırma) ---
    real = {k: v for k, v in ids.items() if not k.startswith("_")}
    q = cmc("/v2/cryptocurrency/quotes/latest", id=",".join(str(v) for v in real.values()), convert="USD")
    out_coins = {}
    inv = {v: k for k, v in real.items()}
    for cid, d in q.items():
        sym = inv.get(int(cid))
        if not sym:
            continue
        u = d["quote"]["USD"]
        out_coins[sym] = {
            "id": d["id"], "rank": d.get("cmc_rank"), "name": d.get("name"),
            "price": u.get("price"), "ch24": u.get("percent_change_24h"),
            "mcap": u.get("market_cap"), "vol": u.get("volume_24h"),
            "supply": d.get("circulating_supply"), "max": d.get("max_supply"),
        }

    # --- toplam piyasa (her çalıştırma) ---
    g = cmc("/v1/global-metrics/quotes/latest", convert="USD")
    gu = g["quote"]["USD"]
    glob = {
        "mcap": gu.get("total_market_cap"), "vol": gu.get("total_volume_24h"),
        "ch": gu.get("total_market_cap_yesterday_percentage_change"),
        "btc": g.get("btc_dominance"), "eth": g.get("eth_dominance"),
    }

    # --- yavaş göstergeler (2 saatte bir) ---
    slow = prev.get("slow", {})
    now = time.time()
    if now - slow.get("ts", 0) >= SLOW_EVERY_S:
        new_slow = {"ts": int(now)}
        try:
            f = cmc("/v3/fear-and-greed/historical", limit=2)
            new_slow["fng"] = {"v": int(f[0]["value"]), "prev": int(f[1]["value"]) if len(f) > 1 else None}
        except PlanError as e:
            print(f"! Korku & Açgözlülük alınamadı (plan): {e}")
        except Exception as e:
            print(f"! Korku & Açgözlülük alınamadı: {e}")
        try:
            lst = cmc("/v1/cryptocurrency/listings/latest", limit=150, convert="USD")
            btc = next(d for d in lst if d["symbol"] == "BTC")["quote"]["USD"]["percent_change_30d"]
            pool = [d for d in lst if d["symbol"] != "BTC"
                    and d["symbol"].lower() not in STABLES and d["symbol"].lower() not in WRAPPED
                    and "stablecoin" not in (d.get("tags") or [])
                    and d["quote"]["USD"].get("percent_change_30d") is not None][:100]
            new_slow["alt"] = round(sum(d["quote"]["USD"]["percent_change_30d"] > btc for d in pool) / len(pool) * 100)
        except Exception as e:
            print(f"! Altcoin sezonu hesaplanamadı: {e}")
        try:
            pc = cmc("/v2/tools/price-conversion", amount=1, id=2781, convert="TRY")   # 2781 = USD
            pc = pc[0] if isinstance(pc, list) else pc
            new_slow["usdtry"] = pc["quote"]["TRY"]["price"]
        except Exception as e:
            print(f"! USD/TRY alınamadı: {e}")
        slow = {**slow, **new_slow}

    missing = [c[0] for c in coins if c[0] not in out_coins]
    if missing:
        print(f"! CMC verisi olmayan coinler (sayfa bunlar için CoinGecko kullanır): {', '.join(missing)}")

    OUT_FILE.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "CoinMarketCap",
        "global": glob,
        "fng": slow.get("fng"), "alt": slow.get("alt"), "usdtry": slow.get("usdtry"),
        "slow": slow,
        "coins": dict(sorted(out_coins.items())),
    }, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"markets.json yazıldı: {len(out_coins)}/{len(coins)} coin.")

    if review:
        REVIEW_FILE.write_text("## CoinMarketCap eşlemesi: kontrol edilecekler\n\n" + "\n".join(review) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
