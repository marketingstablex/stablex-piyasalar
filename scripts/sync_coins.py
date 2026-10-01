#!/usr/bin/env python3
"""
Stablex Piyasalar sayfasındaki coin listesini data/coins.json ile eşitler.

  - Stablex'ten kalkan (delist) coinleri listeden çıkarır.
  - Yeni listelenen coinleri CoinGecko'da sembolle bulur (aynı sembollü birden çok
    proje varsa piyasa değeri en yüksek olanı seçer), kategori atar ve listeye ekler.
  - Her coin için Binance SEMBOLUSDT çiftinin fiyatını CoinGecko fiyatıyla karşılaştırır;
    %10'dan fazla sapma varsa (ticker başka projeye aitse) o coin için Binance'i kapatır.
  - İnsan kontrolü gereken her şeyi data/review.md'ye yazar (workflow bunu issue olarak açar).

Sadece Python standart kütüphanesi kullanır.
Ortam değişkeni (isteğe bağlı): CG_API_KEY -> CoinGecko Demo API anahtarı.
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
REVIEW_FILE = Path(os.environ.get("REVIEW_PATH") or ROOT / "review.md")   # repoya commit edilmez

STABLEX_URL = "https://stablex.com.tr/piyasalar"
CG = "https://api.coingecko.com/api/v3"
BINANCE_TICKERS = "https://data-api.binance.vision/api/v3/ticker/price"

MIN_EXPECTED = 30          # Stablex'ten bundan az coin gelirse sayfa yapısı değişmiş say, hiçbir şeyi silme
MAX_REMOVE_AT_ONCE = 10    # tek seferde bundan fazla coin kalkıyorsa şüpheli: silme, sadece raporla
PRICE_TOLERANCE = 0.10     # Binance / CoinGecko fiyat sapma sınırı

UA = "Mozilla/5.0 (compatible; stablex-piyasalar-sync/1.0)"

# CoinGecko kategori adı (küçük harf, içerir) -> sayfadaki kategori anahtarı. Sıra önceliği belirler.
CATEGORY_RULES = [
    ("stablecoin", "stable"),
    ("meme", "meme"),
    ("artificial intelligence", "ai"),
    ("ai agent", "ai"),
    ("real world assets", "rwa"),
    ("tokenized gold", "rwa"),
    ("depin", "depin"),
    ("gaming", "game"),
    ("metaverse", "game"),
    ("liquid staking", "stake"),
    ("restaking", "stake"),
    ("layer 2", "l2"),
    ("layer 1", "l1"),
    ("decentralized finance", "defi"),
    ("decentralized exchange", "defi"),
    ("lending", "defi"),
    ("oracle", "infra"),
    ("interoperability", "infra"),
    ("infrastructure", "infra"),
    ("payment", "pay"),
]


def http_get(url: str, *, json_out: bool = True, cg: bool = False, retries: int = 3):
    headers = {"User-Agent": UA, "Accept": "application/json" if json_out else "text/html"}
    if cg and os.environ.get("CG_API_KEY"):
        headers["x-cg-demo-api-key"] = os.environ["CG_API_KEY"]
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", errors="replace")
                return json.loads(body) if json_out else body
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:                    # CoinGecko hız sınırı
                time.sleep(20 * (attempt + 1))
                continue
            if 400 <= e.code < 500:
                raise
        except Exception as e:                   # ağ hatası
            last = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"İstek başarısız: {url} ({last})")


# ---------------- Stablex ----------------
def parse_stablex(html: str) -> set[str]:
    """Piyasalar sayfasındaki /piyasalar/<sembol>try bağlantılarından sembolleri çıkarır."""
    found = re.findall(r"/piyasalar/([a-z0-9]+)try(?=[\"'/?#\s>])", html)
    return {s.upper() for s in found}


def fetch_stablex() -> set[str]:
    return parse_stablex(http_get(STABLEX_URL, json_out=False))


# ---------------- CoinGecko ----------------
def cg_lookup_symbol(sym: str):
    """Sembolle eşleşen projeler içinden piyasa değeri en yüksek olanı döndürür."""
    q = urllib.parse.urlencode({
        "vs_currency": "usd", "symbols": sym.lower(), "include_tokens": "all",
        "order": "market_cap_desc", "per_page": 50,
    })
    data = http_get(f"{CG}/coins/markets?{q}", cg=True)
    data = [d for d in data if (d.get("symbol") or "").lower() == sym.lower()]
    if not data:
        return None, []
    data.sort(key=lambda d: d.get("market_cap") or 0, reverse=True)
    return data[0], data


def cg_categories(cid: str) -> list[str]:
    q = "localization=false&tickers=false&market_data=false&community_data=false&developer_data=false&sparkline=false"
    try:
        d = http_get(f"{CG}/coins/{cid}?{q}", cg=True)
    except Exception:
        return []
    names = [c.lower() for c in (d.get("categories") or []) if c]
    out: list[str] = []
    for needle, key in CATEGORY_RULES:
        if key not in out and any(needle in n for n in names):
            out.append(key)
        if len(out) == 2:
            break
    return out


def cg_prices(ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 200):
        q = urllib.parse.urlencode({"vs_currency": "usd", "ids": ",".join(ids[i:i + 200]), "per_page": 250})
        for d in http_get(f"{CG}/coins/markets?{q}", cg=True):
            out[d["id"]] = d
        time.sleep(2)
    return out


# ---------------- Binance ----------------
def binance_overrides(coins: list, cg_by_id: dict, previous: dict) -> tuple[dict, list[str]]:
    """Fiyatı CoinGecko ile tutmayan veya Binance'te olmayan çiftler için {SYM: null} döndürür."""
    try:
        tickers = {t["symbol"]: float(t["price"]) for t in http_get(BINANCE_TICKERS)}
    except Exception as e:
        print(f"! Binance'e ulaşılamadı, önceki eşleme korunuyor: {e}")
        return previous, []
    out: dict[str, None] = {}
    notes: list[str] = []
    for sym, cid, *_ in coins:
        pair = sym + "USDT"
        cgp = (cg_by_id.get(cid) or {}).get("current_price")
        bnp = tickers.get(pair)
        if bnp is None:
            out[sym] = None                       # Binance'te çift yok
        elif cgp and abs(bnp / cgp - 1) > PRICE_TOLERANCE:
            out[sym] = None
            if sym not in previous:               # sadece yeni tespitleri raporla
                notes.append(f"- **{sym}**: Binance `{pair}` fiyatı ({bnp:g} $) CoinGecko `{cid}` fiyatından "
                             f"({cgp:g} $) çok farklı → bu ticker Binance'te başka bir projeye ait olabilir, Binance kapatıldı.")
    return out, notes


# ---------------- ana akış ----------------
def main() -> int:
    current = json.loads(COINS_FILE.read_text(encoding="utf-8"))
    coins: list = current["coins"]
    by_sym = {c[0]: c for c in coins}
    review: list[str] = []

    live = fetch_stablex()
    print(f"Stablex'te {len(live)} coin bulundu.")
    if len(live) < MIN_EXPECTED:
        print("! Beklenenden az coin geldi; Stablex sayfa yapısı değişmiş olabilir. Hiçbir değişiklik yapılmadı.")
        REVIEW_FILE.write_text(
            f"## Coin listesi senkronu durdu\n\nStablex Piyasalar sayfasından yalnızca {len(live)} coin okunabildi "
            f"(beklenen en az {MIN_EXPECTED}). Sayfa yapısı değişmiş olabilir; `scripts/sync_coins.py` içindeki "
            f"`parse_stablex` fonksiyonunu kontrol edin.\n", encoding="utf-8")
        return 0

    removed = sorted(set(by_sym) - live)
    added = sorted(live - set(by_sym))

    if len(removed) > MAX_REMOVE_AT_ONCE:
        review.append(f"- Tek seferde {len(removed)} coin kalkmış görünüyor ({', '.join(removed)}). "
                      f"Şüpheli olduğu için hiçbiri silinmedi; Stablex sayfasını elle kontrol edin.")
        removed = []

    for sym in removed:
        print(f"- Çıkarıldı (Stablex'te yok): {sym}")
        del by_sym[sym]

    pending = []
    for sym in added:
        try:
            best, all_matches = cg_lookup_symbol(sym)
        except Exception as e:
            best, all_matches = None, []
            print(f"! {sym} için CoinGecko sorgusu başarısız: {e}")
        time.sleep(3)
        if not best:
            pending.append(sym)
            review.append(f"- **{sym}** Stablex'te listelendi ama CoinGecko'da bulunamadı. "
                          f"`data/coins.json` içine elle ekleyin: `[\"{sym}\", \"<coingecko-id>\", \"<Ad>\", [\"<kategori>\"]]`")
            continue
        cats = cg_categories(best["id"])
        time.sleep(3)
        by_sym[sym] = [sym, best["id"], best.get("name") or sym, cats]
        print(f"+ Eklendi: {sym} -> {best['id']} {cats}")
        others = [d["id"] for d in all_matches[1:4]]
        line = (f"- **{sym}** eklendi → CoinGecko `{best['id']}` ({best.get('name')}), kategori: "
                f"{', '.join(cats) if cats else '— (atanamadı)'}.")
        if others:
            line += f" Aynı sembolü kullanan diğer projeler: {', '.join(f'`{o}`' for o in others)} — doğru eşleşmeyi kontrol edin."
        review.append(line)

    new_coins = sorted(by_sym.values(), key=lambda c: c[0])

    # Tüm id'leri doğrula ve Binance çiftlerini kontrol et
    try:
        cg_by_id = cg_prices([c[1] for c in new_coins])
        missing = [c[0] for c in new_coins if c[1] not in cg_by_id]
        for sym in missing:
            review.append(f"- **{sym}**: CoinGecko id `{by_sym[sym][1]}` artık veri döndürmüyor (id değişmiş olabilir).")
        binance, notes = binance_overrides(new_coins, cg_by_id, current.get("binance", {}))
        review.extend(notes)
    except Exception as e:
        print(f"! CoinGecko fiyat kontrolü yapılamadı, Binance eşlemesi korunuyor: {e}")
        binance = current.get("binance", {})

    changed = (new_coins != coins) or (binance != current.get("binance", {}))
    if changed:
        out = {
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": STABLEX_URL,
            "note": "Bu dosya scripts/sync_coins.py tarafından otomatik güncellenir. Sıra: sembol, CoinGecko id, ad, kategoriler. "
                    "binance: Binance çifti kullanılmayacak coinler (null).",
            "coins": new_coins,
            "binance": dict(sorted(binance.items())),
        }
        COINS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"coins.json güncellendi: {len(new_coins)} coin.")
    else:
        print("Değişiklik yok.")

    if removed:
        review.insert(0, f"- Stablex'ten kalktığı için listeden çıkarıldı: {', '.join(removed)}")
    if review:
        REVIEW_FILE.write_text("## Coin listesi senkronu: kontrol edilecekler\n\n" + "\n".join(review) + "\n", encoding="utf-8")
    elif REVIEW_FILE.exists():
        REVIEW_FILE.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
