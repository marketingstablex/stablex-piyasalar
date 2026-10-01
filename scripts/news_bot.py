#!/usr/bin/env python3
"""
Haber botu: index.html'deki HABERLER tarifine göre data/news.json üretir.

  - Kaynaklar: yalnızca The Block, CoinDesk, Cointelegraph Türkçe (RSS).
  - Son 24 saatin haberleri; daha önce işlenenler tekrar işlenmez.
  - Gemini ile: borsa haberlerini ayıklar, Türkçe başlık/özet (≤150 kelime) üretir,
    ilgili coinleri (yalnızca data/coins.json'daki semboller) ve 1-5 önem puanını belirler.
  - Türkçe olmayan sonuçları ayrıca çevirtir; çevrilemeyen haber yayınlanmaz, sonraki çalıştırmada yeniden denenir.
  - Farklı kaynaklardan gelen aynı olayı tek habere indirir.
  - Çıktı: { updated, bySymbol: { BTC: [ {title, summary, url, published, importance}, ... ] }, seen: {...} }

Ortam değişkenleri: GEMINI_API_KEY (zorunlu), GEMINI_MODEL (isteğe bağlı).
Gemini hata verirse haberler "görülmedi" sayılır ve bir sonraki çalıştırmada yeniden denenir.
"""
from __future__ import annotations

import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COINS_FILE = ROOT / "data" / "coins.json"
OUT_FILE = ROOT / "data" / "news.json"

FEEDS = [  # index.html → NEWS_SOURCES ile aynı üç kaynak
    ("theblock.co", "https://www.theblock.co/rss.xml"),
    ("coindesk.com", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("cointelegraph-tr.com", "https://cointelegraph-tr.com/rss"),
]
ALLOWED = {d for d, _ in FEEDS}

WINDOW_H = 24            # sayfa son 24 saati gösteriyor
KEEP_H = 36              # news.json'da biraz daha uzun tut
SEEN_KEEP_H = 96         # tekrar işlememek için işlenmiş URL'leri bu kadar hatırla
PER_SYMBOL = 8           # coin başına saklanacak en fazla haber (sayfa en önemli 3'ünü gösterir)
BATCH = 8                # Gemini'ye tek istekte gönderilecek haber sayısı
MAX_PER_RUN = 48         # tek çalıştırmada işlenecek en fazla yeni haber (kota koruması)
MAX_WORDS = 150

MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-lite-latest", "gemini-2.5-flash-lite", "gemini-flash-latest"] if m]
UA = "Mozilla/5.0 (compatible; stablex-haber-botu/1.0)"


# ---------------- yardımcılar ----------------
def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def domain_of(url: str) -> str | None:
    try:
        h = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return None
    h = h.removeprefix("www.")
    return next((d for d in ALLOWED if h == d or h.endswith("." + d)), None)


def clean(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def clip_words(t: str, n: int = MAX_WORDS) -> str:
    w = t.split()
    return " ".join(w[:n]) + ("…" if len(w) > n else "")


def parse_date(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_feed(raw: bytes) -> list[dict]:
    """RSS 2.0 ve Atom'u destekler."""
    root = ET.fromstring(raw)
    out = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        f = {c.tag.split("}")[-1]: c for c in it}
        link = (f.get("link").text or "").strip() if f.get("link") is not None else ""
        if not link and f.get("link") is not None:
            link = f["link"].attrib.get("href", "")
        date = None
        for k in ("pubDate", "published", "updated", "date"):
            if f.get(k) is not None and f[k].text:
                date = parse_date(f[k].text.strip())
                if date:
                    break
        summary = ""
        for k in ("description", "summary", "encoded", "content"):
            if f.get(k) is not None and (f[k].text or "").strip():
                summary = clean(f[k].text)
                break
        title = clean(f["title"].text) if f.get("title") is not None and f["title"].text else ""
        if title and link and date:
            out.append({"title": title, "url": link.split("?")[0], "published_ts": date, "source_summary": summary[:2500]})
    return out


# ---------------- Gemini ----------------
PROMPT = """Sen Stablex (Türkiye'de lisanslı, Akbank grubuna ait kripto varlık alım satım platformu) için
kripto haber editörüsün. Aşağıdaki haberlerin HER BİRİ için bir JSON nesnesi döndür. Yanıtın SADECE bir JSON dizisi olsun:

[{{"i": <haber numarası>,
   "borsa_haberi": true | false,
   "baslik": "Türkçe başlık",
   "ozet": "Türkçe özet",
   "semboller": ["BTC", ...],
   "onem": 1-5}}]

Kurallar:
- "borsa_haberi": Haberin ana konusu belirli bir kripto para BORSASI/platformu ise (Binance, Coinbase, OKX, Bybit,
  Kraken, Paribu, BtcTurk vb.: listeleme/delist duyurusu, kampanya, ürün, gelir, hack, dava, yönetici değişikliği) true.
  Borsa yalnızca yan ayrıntı olarak geçiyorsa false.
- "baslik": Doğal, akıcı Türkçe. Haber zaten Türkçeyse aynen bırak.
- "ozet": SADECE verilen metne dayan; metinde olmayan hiçbir bilgi, yorum, tahmin veya tavsiye EKLEME.
  En fazla 3 cümle ve {max_words} kelime. Metin yoksa yalnızca başlığın Türkçesini yaz.
  "Al", "sat", "kazan" gibi yatırım çağrısı içeren ifadeler kullanma.
- "semboller": Haberin DOĞRUDAN ilgili olduğu varlıklar. YALNIZCA şu listeden seç, başka sembol yazma:
  {symbols}
  Bir varlık yalnızca geçerken anılıyorsa ekleme. Hiçbiri doğrudan ilgili değilse boş dizi.
- "onem": 5 = piyasa genelini etkileyen kritik gelişme (ör. büyük düzenleme, ETF kararı, büyük hack),
  4 = ilgili varlık için önemli, 3 = kayda değer, 2 = sıradan, 1 = önemsiz.

Haberler:
{items}"""


def gemini(prompt: str):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY tanımlı değil (Settings → Secrets → Actions).")
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }).encode()
    last = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
                with urllib.request.urlopen(req, timeout=90) as r:
                    j = json.loads(r.read().decode())
                text = j["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except urllib.error.HTTPError as e:
                last = f"{model}: {e.code} {e.read().decode(errors='replace')[:200]}"
                if e.code == 404:
                    break                      # model yok → sıradaki modeli dene
                if e.code == 429:
                    time.sleep(20 * (attempt + 1))
                    continue
                time.sleep(5)
            except Exception as e:
                last = f"{model}: {e}"
                time.sleep(5)
    raise RuntimeError(f"Gemini başarısız: {last}")


# re.I KULLANMA: büyük/küçük harf duyarsız modda "ı" harfi "i" ile eşleşir ve her İngilizce metin Türkçe sayılır
TR_HINT = re.compile(r"[çğıöşüÇĞİÖŞÜ]|\b(?:[Vv]e|[Bb]ir|[Ii]le|[Ii]çin|olarak|yüzde|milyar|milyon|dolar|sonra|göre)\b")


def is_tr(item: dict) -> bool:
    return bool(TR_HINT.search(item.get("title", ""))) and bool(TR_HINT.search(item.get("summary", "")))


TR_PROMPT = """Aşağıdaki kripto para haberlerinin başlığını ve özetini TÜRKÇEYE ÇEVİR.
Anlamı değiştirme, bilgi ekleme veya çıkarma; şirket, proje ve kişi adlarını olduğu gibi bırak.
Yanıtın SADECE şu biçimde bir JSON dizisi olsun: [{{"i": <numara>, "baslik": "Türkçe başlık", "ozet": "Türkçe özet"}}]

{items}"""


def translate(items: list[dict]) -> None:
    """Türkçe olmayan başlık/özetleri yerinde Türkçeye çevirir (başaramazsa olduğu gibi bırakır)."""
    for b in range(0, len(items), 10):
        part = items[b:b + 10]
        listing = "\n\n".join(f"[{i}] Başlık: {x['title']}\nÖzet: {x['summary']}" for i, x in enumerate(part))
        try:
            res = gemini(TR_PROMPT.format(items=listing))
        except Exception as e:
            print(f"! Çeviri başarısız: {e}")
            continue
        for r in res if isinstance(res, list) else []:
            try:
                x = part[int(r["i"])]
            except Exception:
                continue
            cand = {"title": clean(r.get("baslik") or ""), "summary": clip_words(clean(r.get("ozet") or ""))}
            if cand["title"] and cand["summary"] and is_tr(cand):
                x.update(cand)
        time.sleep(4)


DUP_PROMPT = """Aşağıdaki haber başlıklarından AYNI OLAYI anlatanları grupla (farklı kaynaklardan gelen aynı haber).
Sadece gerçekten aynı olayı anlatanları grupla; aynı coin hakkında farklı gelişmeler ayrı kalmalı.
Yanıtın SADECE şu biçimde olsun: {{"gruplar": [[<numara>, <numara>], ...]}} — yalnızca 2 veya daha fazla elemanlı gruplar.

{items}"""


def dedupe(by_symbol: dict) -> int:
    """Aynı olayı anlatan haberlerden yalnızca birini tutar; atılanların coinlerini tutulana aktarır."""
    uniq: dict[str, dict] = {}
    syms_of: dict[str, set] = {}
    for s, lst in by_symbol.items():
        for x in lst:
            uniq.setdefault(x["url"], x)
            syms_of.setdefault(x["url"], set()).add(s)
    items = list(uniq.values())
    if len(items) < 2:
        return 0
    listing = "\n".join(f"[{i}] {x['title']}" for i, x in enumerate(items))
    try:
        res = gemini(DUP_PROMPT.format(items=listing))
        groups = res.get("gruplar", []) if isinstance(res, dict) else res
    except Exception as e:
        print(f"! Tekrar ayıklama atlandı: {e}")
        return 0
    drop: set[str] = set()
    for g in groups or []:
        try:
            members = [items[int(i)] for i in g if 0 <= int(i) < len(items)]
        except Exception:
            continue
        members = [m for m in members if m["url"] not in drop]
        if len(members) < 2:
            continue
        # tercih: önem → Türkçe kaynak (çeviri değil) → en yeni
        keep = max(members, key=lambda m: (m["importance"], domain_of(m["url"]) == "cointelegraph-tr.com", m["published"]))
        for m in members:
            if m is keep:
                continue
            drop.add(m["url"])
            for s in syms_of[m["url"]] - syms_of[keep["url"]]:
                by_symbol.setdefault(s, []).append(keep)
            syms_of[keep["url"]] |= syms_of[m["url"]]
    for s in list(by_symbol):
        by_symbol[s] = [x for x in by_symbol[s] if x["url"] not in drop]
        if not by_symbol[s]:
            del by_symbol[s]
    return len(drop)


# ---------------- ana akış ----------------
def main() -> int:
    symbols = [c[0] for c in json.loads(COINS_FILE.read_text(encoding="utf-8"))["coins"]]
    sym_set = set(symbols)
    prev = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}
    now = time.time()

    seen: dict[str, float] = {u: t for u, t in prev.get("seen", {}).items() if now - t < SEEN_KEEP_H * 3600}
    tries: dict[str, int] = {u: n for u, n in prev.get("tries", {}).items() if u in seen or n < 3}
    by_symbol: dict[str, list] = prev.get("bySymbol", {})

    # 1) RSS'leri çek
    fresh = []
    for dom, url in FEEDS:
        try:
            items = parse_feed(get(url))
            print(f"{dom}: {len(items)} haber")
        except Exception as e:
            print(f"! {dom} okunamadı: {e}")
            continue
        for it in items:
            if domain_of(it["url"]) != dom:
                continue
            if now - it["published_ts"] > WINDOW_H * 3600 or it["url"] in seen:
                continue
            fresh.append(it)
    fresh.sort(key=lambda x: x["published_ts"], reverse=True)
    fresh = fresh[:MAX_PER_RUN]
    print(f"İşlenecek yeni haber: {len(fresh)}")

    # 2) Gemini ile işle
    added = skipped_exchange = no_symbol = not_tr = 0
    candidates: list[tuple[dict, list]] = []
    for b in range(0, len(fresh), BATCH):
        batch = fresh[b:b + BATCH]
        listing = "\n\n".join(
            f"[{i}] Başlık: {it['title']}\nMetin: {it['source_summary'] or '(yok)'}" for i, it in enumerate(batch))
        try:
            res = gemini(PROMPT.format(max_words=MAX_WORDS, symbols=", ".join(symbols), items=listing))
        except Exception as e:
            print(f"! {e} — bu grup bir sonraki çalıştırmada tekrar denenecek.")
            continue
        for r in res if isinstance(res, list) else res.get("items", []):
            try:
                it = batch[int(r["i"])]
            except Exception:
                continue
            seen[it["url"]] = now
            if r.get("borsa_haberi"):
                skipped_exchange += 1
                continue
            syms = [s for s in dict.fromkeys(str(x).upper() for x in r.get("semboller") or []) if s in sym_set]
            if not syms:
                no_symbol += 1
                continue
            item = {
                "title": clean(r.get("baslik") or it["title"]),
                "summary": clip_words(clean(r.get("ozet") or "")),
                "url": it["url"],
                "published": iso(it["published_ts"]),
                "importance": max(1, min(5, int(r.get("onem") or 2))),
            }
            candidates.append((item, syms))
        time.sleep(4)   # ücretsiz kota için istekler arası bekleme

    # 2b) Türkçe kontrolü: yeni adaylar + daha önce kaydedilmiş ama Türkçe olmayanlar
    stored_en = {x["url"]: x for lst in by_symbol.values() for x in lst if not is_tr(x)}
    need = [c[0] for c in candidates if not is_tr(c[0])] + list(stored_en.values())
    if need:
        print(f"Türkçe olmayan {len(need)} haber çeviriye gönderiliyor")
        translate(need)
    for s in list(by_symbol):          # çevrilemeyen eski kayıtları yayından kaldır
        by_symbol[s] = [x for x in by_symbol[s] if is_tr(x)]
    for item, syms in candidates:
        if not is_tr(item):
            not_tr += 1
            tries[item["url"]] = tries.get(item["url"], 0) + 1
            if tries[item["url"]] < 3:
                seen.pop(item["url"], None)   # sonraki çalıştırmada tekrar dene
            continue
        tries.pop(item["url"], None)
        for s in syms:
            lst = by_symbol.setdefault(s, [])
            if all(x["url"] != item["url"] for x in lst):
                lst.append(item)
        added += 1

    # 3) Eskileri temizle, aynı olayın tekrarlarını ayıkla, sırala, sınırla
    cutoff = now - KEEP_H * 3600
    for s in list(by_symbol):
        by_symbol[s] = [x for x in by_symbol[s] if (parse_date(x["published"]) or 0) >= cutoff]
    dup = dedupe(by_symbol) if (added or need) else 0
    for s in list(by_symbol):
        if s not in sym_set:
            del by_symbol[s]
            continue
        lst = [x for x in by_symbol[s] if (parse_date(x["published"]) or 0) >= cutoff and domain_of(x["url"])]
        lst.sort(key=lambda x: (x["importance"], x["published"]), reverse=True)
        if lst:
            by_symbol[s] = lst[:PER_SYMBOL]
        else:
            del by_symbol[s]

    OUT_FILE.write_text(json.dumps({
        "updated": iso(now),
        "sources": sorted(ALLOWED),
        "bySymbol": dict(sorted(by_symbol.items())),
        "seen": seen,
        "tries": tries,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Eklendi: {added} · borsa haberi elendi: {skipped_exchange} · coin ilgisiz: {no_symbol} · "
          f"Türkçe yapılamadı: {not_tr} · tekrar ayıklandı: {dup} · haberi olan coin: {len(by_symbol)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
