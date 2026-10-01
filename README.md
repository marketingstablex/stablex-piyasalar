# Stablex Kripto Piyasalar

Stablex'te listelenen kripto varlıkların fiyat, piyasa değeri ve göstergelerini gösteren statik sayfa.

## Dosyalar

| Dosya | Açıklama |
| --- | --- |
| `index.html` | Sayfanın kendisi (HTML + CSS + JS, harici kütüphane yok) |
| `data/coins.json` | Takip edilen coin listesi. Sayfa açılışta bu dosyayı okur. |
| `scripts/sync_coins.py` | Listeyi Stablex Piyasalar sayfasıyla eşitleyen script |
| `.github/workflows/sync-coins.yml` | Script'i her gün 09:17'de (TR saati) çalıştırır |

## Coin listesi nasıl güncel kalıyor?

Her gün GitHub Actions şu adımları çalıştırır:

1. https://stablex.com.tr/piyasalar sayfasını okur.
2. Stablex'ten **kalkan** coinleri listeden çıkarır.
3. **Yeni listelenen** coinleri CoinGecko'da sembolüyle bulur ve kategori atayarak ekler.
4. Her coinin Binance fiyatını CoinGecko fiyatıyla karşılaştırır. Ticker Binance'te başka bir projeye aitse, o coin için Binance'i kapatır; grafik ve getiri verisi CoinGecko'dan gelir.
5. Değişiklik varsa `data/coins.json` dosyasını commit eder. Kontrol gereken bir şey varsa **"Coin listesi: kontrol gerekiyor"** başlıklı bir issue açar. Örnek durumlar: aynı sembolü kullanan birden fazla proje olması, kategori atanamaması.

Güvenlik sınırları:
- Stablex'ten 30'dan az coin okunursa hiçbir şey silinmez; bu, sayfa yapısının değiştiğine işaret eder.
- Tek seferde 10'dan fazla coin kalkmış görünürse yine hiçbir şey silinmez, yalnızca issue açılır.

Elle çalıştırmak için: **Actions → Coin listesini Stablex ile eşitle → Run workflow**

Bir eşlemeyi elle düzeltmek için `data/coins.json` dosyasını GitHub üzerinden düzenleyin. Her satır `["SEMBOL", "coingecko-id", "Ad", ["kategori"]]` biçimindedir.

Kullanılabilecek kategoriler: `l1, l2, defi, stake, meme, ai, depin, game, rwa, infra, pay, stable`.

## Kurulum

1. Bu klasördeki her şeyi (gizli `.github` klasörü dahil) yeni bir repoya yükleyin.
2. **Settings → Pages → Branch: main / (root)** ile yayına alın.
3. **Settings → Actions → General → Workflow permissions** ayarını **Read and write** yapın.
4. (İsteğe bağlı) Ücretsiz bir CoinGecko Demo API anahtarı alıp **Settings → Secrets → Actions** altına `CG_API_KEY` adıyla ekleyin. Bu, hız sınırına takılma riskini azaltır.
