# symbols_builder.py — Stabil ve temiz Binance USDT sembol evreni üretici v3
#
# v2 → v3 DEĞİŞİKLİKLER (Sembol Kalite Filtresi — ÖNCELİK B):
#   YENİ: 30 günlük fiyat momentum analizi
#   YENİ: _fetch_momentum_data() — 30d kapanış fiyatları + EMA hesabı
#   YENİ: _momentum_score() — 0.0-1.0 kalite puanı
#   YENİ: is_excluded_by_momentum() — hard eleme (30d < -%25 veya win_days < %35)
#   YENİ: SymbolCandidate'e momentum_score, change_30d, win_days_ratio alanları
#   DEĞİŞTİ: _candidate_score() → momentum skoru hacim skoruna %30 katkı yapar
#   DEĞİŞTİ: Meta JSON'a momentum verileri eklendi
#   AMAÇ: TRUMP/NIGHT/XPL gibi düşüş trendindeki hype coinleri elemek,
#          PORTAL/DOGE/ZEC gibi pozitif momentum'lu coinleri öne çıkarmak
#
# NEDEN DEĞİŞTİ?
# Eski sürüm yalnızca Binance spot 24h quoteVolume sıralamasına bakıyordu.
# Bu, hype/tek-gün hacim patlaması yaşayan yeni coinleri Top70'e sokup
# 90 günlük backtest sonuçlarını aşırı oynatıyordu.
#
# YENİ MANTIK:
#   1) Sadece Binance SPOT'ta aktif, TRADING durumundaki USDT pariteleri
#   2) Stable/fiat/emtia/wrapped/garip unicode sembol temizliği
#   3) Son 24h hacim yerine 7 günlük günlük mumlardan stabil hacim ölçümü
#   4) Aşırı tek-gün hacim spike'ı ve çok yeni/eksik veri filtresi
#   5) BTC/ETH/SOL/BNB/XRP/DOGE/ADA gibi ana likit coinleri koruma
#
# ÇIKTI:
#   symbols_top70.json  → backtest.py ve canlı sistem tarafından okunur.

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from statistics import median
from typing import Any, Dict, Iterable, List, Optional

import requests


BINANCE_API = "https://api.binance.com"
BINANCE_TICKER_24H = f"{BINANCE_API}/api/v3/ticker/24hr"
BINANCE_EXCHANGE_INFO = f"{BINANCE_API}/api/v3/exchangeInfo"
BINANCE_KLINES = f"{BINANCE_API}/api/v3/klines"

REQUEST_TIMEOUT = 15
REQUEST_DELAY = 0.08

# Varsayılan çıktı sayısı
DEFAULT_TOP_N = 70

# 7 günlük stabilite için günlük mum sayısı.
# 7: yeterince hızlı, ama tek günlük hype'a göre çok daha stabil.
STABILITY_DAYS = 7

# Aşırı yeni/eksik veri temizliği.
MIN_DAILY_CANDLES = 7

# Çok düşük hacimli coinleri en baştan at.
# Not: Bu filtre sadece sembol evreni için. Backtest içindeki min_notional ayrı çalışır.
MIN_24H_QUOTE_VOLUME = 5_000_000
MIN_7D_MEDIAN_QUOTE_VOLUME = 2_000_000

# Tek gün hacmi, 7 günlük median hacmin çok üstündeyse hype/spike riski say.
# Örn: TRUMP/NIGHT/XPL gibi listeler bu yüzden daha zor içeri girer.
MAX_SPIKE_RATIO = 6.0

# Momentum filtresi için kaç günlük fiyat verisi bakılır.
# 30 gün: hem yeterince uzun (trend tespiti) hem çok eski değil (dinamik).
MOMENTUM_DAYS = 30

# Hard eleme eşikleri — CORE_SYMBOLS bunlardan muaf.
MOMENTUM_MIN_CHANGE_30D = -25.0   # 30d getiri bu kadar negatifse coin eleyi
MOMENTUM_MIN_WIN_DAYS   =  0.35   # 30 günün en az %35'i pozitif kapanmalı
MOMENTUM_MAX_DAILY_STD  =   6.0   # Günlük volatilite std bu %'yi geçerse hype riski

# Çok aşırı 24h hareket edenleri evrene alma.
# Trend stratejisi için momentum iyi olabilir ama +80/-60 gibi günlük hareketler
# çoğu zaman haber/hype kaynaklıdır ve 90d backtesti bozabilir.
MAX_ABS_24H_CHANGE_PCT = 45.0

# Ana likit coinleri koru: hacim sıralaması günlük oynasa bile evrende kalmaları iyi.
CORE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT",
    "LTCUSDT", "BCHUSDT", "DOTUSDT", "NEARUSDT", "SUIUSDT",
]

# Önceki başarılı backtestte sistemin taşıyıcı coinlerinden bazıları.
# Bunları zorla ilk sıraya koymuyoruz; sadece uygun veri/hacim varsa
# günlük hacim düşüşü yüzünden tamamen kaybolmasını zorlaştırıyoruz.
PREFERRED_SYMBOLS = [
    "WLDUSDT", "ZECUSDT", "SUIUSDT", "DOGEUSDT", "NEARUSDT", "PORTALUSDT",
]

BLACKLIST = {
    # Stable / fiat pegged
    "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT",
    "DAIUSDT", "USD1USDT", "RLUSDUSDT", "PYUSDUSDT", "USDEUSDT",
    "EURUSDT", "EURIUSDT", "AEURUSDT", "UUSDT", "TRYUSDT",

    # Altın / emtia / wrapped / özel varlıklar
    "XAUTUSDT", "PAXGUSDT", "XAUUSDT", "WBETHUSDT",
}

STABLE_OR_FIAT_KEYWORDS = (
    "USD", "EUR", "GBP", "TRY", "BRL", "AUD", "PAXG", "XAU", "XAUT", "WBETH",
)

# Sadece klasik ASCII Binance sembol formatı.
# Bu, symbols_top70 içinde görülen unicode/garip sembol riskini temizler.
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")


@dataclass
class SymbolCandidate:
    symbol: str
    quote_volume_24h: float
    price_change_pct_24h: float
    median_quote_volume_7d: float
    avg_quote_volume_7d: float
    spike_ratio: float
    score: float
    # v3: momentum alanları
    momentum_score:   float = 0.0
    change_30d:       float = 0.0
    win_days_ratio:   float = 0.0
    daily_vol_std:    float = 0.0
    ema_ok:           bool  = False


def _get_json(url: str, params: Optional[dict] = None, timeout: int = REQUEST_TIMEOUT) -> Any:
    response = requests.get(url, params=params or {}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _is_ascii_symbol(symbol: str) -> bool:
    try:
        symbol.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def is_excluded_symbol(symbol: str) -> bool:
    """Stable/fiat/emtia/wrapped/geçersiz format sembolleri dışlar."""
    symbol = (symbol or "").upper().strip()

    if not symbol:
        return True

    if symbol in BLACKLIST:
        return True

    if not _is_ascii_symbol(symbol):
        return True

    if not SYMBOL_RE.match(symbol):
        return True

    if not symbol.endswith("USDT"):
        return True

    base = symbol[:-4]

    # BTCUSDT gibi ana paritelerde base=BTC olduğu için USD içermez.
    # USDCUSDT gibi stable paritelerde base=USDC olduğu için elenir.
    for keyword in STABLE_OR_FIAT_KEYWORDS:
        if keyword in base:
            return True

    return False


def _load_spot_trading_symbols() -> set[str]:
    """Binance exchangeInfo'dan aktif SPOT USDT sembollerini alır."""
    data = _get_json(BINANCE_EXCHANGE_INFO)
    valid = set()

    for item in data.get("symbols", []):
        symbol = item.get("symbol", "")
        if is_excluded_symbol(symbol):
            continue
        if item.get("status") != "TRADING":
            continue
        if item.get("quoteAsset") != "USDT":
            continue

        permissions = set(item.get("permissions", []) or [])
        is_spot_allowed = item.get("isSpotTradingAllowed", True)
        if permissions and "SPOT" not in permissions and not is_spot_allowed:
            continue

        valid.add(symbol)

    return valid


def _fetch_daily_quote_volumes(symbol: str, days: int = STABILITY_DAYS) -> List[float]:
    """
    Son N günlük mumun quote asset volume değerlerini döndürür.
    Binance kline index 7 = quote asset volume.
    """
    try:
        raw = _get_json(BINANCE_KLINES, params={
            "symbol": symbol,
            "interval": "1d",
            "limit": days,
        }, timeout=10)
    except Exception:
        return []

    out: List[float] = []
    for k in raw:
        try:
            out.append(float(k[7]))
        except Exception:
            continue
    return out


def _fetch_momentum_data(symbol: str, days: int = MOMENTUM_DAYS) -> dict:
    """
    Son N günlük günlük kapanış fiyatlarından momentum metrikleri hesaplar.

    Döndürür:
        change_30d      : son N günde toplam fiyat değişimi (%)
        win_days_ratio  : pozitif kapanan gün oranı (0-1)
        daily_vol_std   : günlük getirilerin standart sapması (%)
        ema_ok          : EMA5 > EMA20 ise True (kısa vadeli uptrend)

    Yeterli veri yoksa None döner → filtreden geç (fail-open).
    """
    try:
        raw = _get_json(BINANCE_KLINES, params={
            "symbol": symbol,
            "interval": "1d",
            "limit": days,
        }, timeout=10)
    except Exception:
        return {}

    closes: List[float] = []
    for k in raw:
        try:
            closes.append(float(k[4]))   # index 4 = close
        except Exception:
            continue

    if len(closes) < max(days // 2, 10):
        return {}   # yetersiz veri → geç

    # 30d toplam değişim
    change_30d = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] > 0 else 0.0

    # Pozitif kapanan gün oranı
    daily_returns = [(closes[i] - closes[i-1]) / closes[i-1] * 100
                     for i in range(1, len(closes)) if closes[i-1] > 0]
    win_days_ratio = sum(1 for r in daily_returns if r > 0) / len(daily_returns) if daily_returns else 0.5

    # Günlük volatilite std
    if len(daily_returns) >= 2:
        mean_r = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
        daily_vol_std = variance ** 0.5
    else:
        daily_vol_std = 0.0

    # EMA5 > EMA20 kontrolü (uptrend momenti)
    def _ema(prices: List[float], period: int) -> float:
        k = 2 / (period + 1)
        e = prices[0]
        for p in prices[1:]:
            e = p * k + e * (1 - k)
        return e

    ema5  = _ema(closes, 5)  if len(closes) >= 5  else closes[-1]
    ema20 = _ema(closes, 20) if len(closes) >= 20 else closes[-1]
    ema_ok = ema5 > ema20

    return {
        "change_30d":     round(change_30d,    2),
        "win_days_ratio": round(win_days_ratio, 3),
        "daily_vol_std":  round(daily_vol_std,  2),
        "ema_ok":         ema_ok,
    }


def _calc_momentum_score(mdata: dict, symbol: str) -> float:
    """
    Momentum kalite puanı hesaplar. Aralık: -0.5 ile +0.5.
    _candidate_score()'a eklenerek hacim skoru üzerinde ±%30'a kadar etki yapar.

    Pozitif katkılar:
      +0.25 change_30d > +5%    (pozitif trend)
      +0.15 win_days_ratio > %55 (tutarlı yükseliş)
      +0.25 ema_ok = True        (kısa vadeli uptrend)

    Negatif katkılar:
      -0.25 change_30d < -10%   (düşüş trendi)
      -0.15 win_days_ratio < %40 (tutarsız / düşüş ağırlıklı)
      -0.15 daily_vol_std > %5  (hype seviyesi volatilite)
    """
    if not mdata:
        return 0.0   # veri yoksa nötr

    ms = 0.0
    c30  = mdata.get("change_30d",    0.0)
    wdr  = mdata.get("win_days_ratio", 0.5)
    std  = mdata.get("daily_vol_std",  0.0)
    ema  = mdata.get("ema_ok",        False)

    if c30 > 5.0:   ms += 0.25
    if wdr > 0.55:  ms += 0.15
    if ema:         ms += 0.25

    if c30 < -10.0: ms -= 0.25
    if wdr < 0.40:  ms -= 0.15
    if std > 5.0:   ms -= 0.15

    return round(ms, 3)


def _is_excluded_by_momentum(mdata: dict, symbol: str) -> bool:
    """
    Hard eleme: momentum verileri bu eşikleri geçemezse coin listeden çıkar.
    CORE_SYMBOLS bu filtreye takılmaz — büyük cap'ler her zaman içeride.
    """
    if symbol in CORE_SYMBOLS:
        return False   # BTC, ETH vs. muaf

    if not mdata:
        return False   # veri yoksa geç (fail-open)

    c30 = mdata.get("change_30d",    0.0)
    wdr = mdata.get("win_days_ratio", 0.5)

    if c30 < MOMENTUM_MIN_CHANGE_30D:
        return True   # 30 günde -%25'ten fazla düştü

    if wdr < MOMENTUM_MIN_WIN_DAYS:
        return True   # 30 günün %35'inden azı yeşil kapandı

    return False


def _candidate_score(
    quote_volume_24h: float,
    median_quote_volume_7d: float,
    avg_quote_volume_7d: float,
    spike_ratio: float,
    change_abs: float,
    symbol: str,
    momentum_score: float = 0.0,   # v3: momentum katkısı
) -> float:
    """
    Stabil coin seçimi skoru.
    Amaç: tek günlük hacim değil, sürdürülebilir likidite ve düşük hype riski.
    v3: momentum_score (-0.5 ile +0.5) hacim skoruna ±%30 etki eder.
    """
    # Log ölçeği kullanmadan basit ve yorumlanabilir skor.
    score = 0.0

    # 7d median hacim ana ağırlık.
    score += median_quote_volume_7d * 0.55

    # 7d ortalama hacim ikinci ağırlık.
    score += avg_quote_volume_7d * 0.25

    # 24h hacim hâlâ önemli ama tek başına belirleyici değil.
    score += quote_volume_24h * 0.20

    # Spike cezası: tek gün hacmi median'ın çok üstündeyse ceza.
    if spike_ratio > 2.5:
        score /= min(spike_ratio / 2.5, 4.0)

    # Aşırı günlük hareket cezası.
    if change_abs > 25:
        score *= 0.70
    if change_abs > 35:
        score *= 0.55

    # Ana likit coinlere küçük stabilite primi.
    if symbol in CORE_SYMBOLS:
        score *= 1.15

    # Önceki iyi çalışan coinlere çok küçük koruma primi.
    # Bu overfit olmasın diye düşük tutuldu.
    if symbol in PREFERRED_SYMBOLS:
        score *= 1.08

    # v3: Momentum katkısı — pozitif momentum boost, negatif momentum ceza
    # momentum_score aralığı -0.5 ile +0.5 → score ±%30 değişir
    score *= (1.0 + momentum_score * 0.6)
    score  = max(score, 0.0)   # negatife düşmesin

    return score


def build_top_usdt(n: int = DEFAULT_TOP_N, outfile: str = "symbols_top70.json") -> list[str]:
    """
    Stabil USDT sembol evreni üretir.

    Eski davranış: sadece 24h quoteVolume.
    Yeni davranış: aktif SPOT sembol + format temizliği + 7d hacim stabilitesi + spike filtresi.
    """
    try:
        valid_spot_symbols = _load_spot_trading_symbols()
        ticker_data = _get_json(BINANCE_TICKER_24H)
    except Exception as e:
        print(f"[HATA] Binance verisi alınamadı: {e}")
        return []

    ticker_by_symbol: Dict[str, dict] = {
        item.get("symbol", ""): item
        for item in ticker_data
        if item.get("symbol") in valid_spot_symbols
    }

    candidates: List[SymbolCandidate] = []

    # Önce yüksek hacimli adayları al. Tüm piyasaya kline çekmek gereksiz yavaş olur.
    preliminary = []
    for symbol, item in ticker_by_symbol.items():
        if is_excluded_symbol(symbol):
            continue

        quote_volume_24h = _safe_float(item.get("quoteVolume"))
        change_pct = _safe_float(item.get("priceChangePercent"))

        if quote_volume_24h < MIN_24H_QUOTE_VOLUME:
            continue
        if abs(change_pct) > MAX_ABS_24H_CHANGE_PCT:
            continue

        preliminary.append((symbol, quote_volume_24h, change_pct))

    preliminary.sort(key=lambda x: x[1], reverse=True)

    # n=70 için 160 aday yeterli. Böylece PORTAL gibi 24h hacimde biraz geriye düşen
    # ama 7d stabilitesi iyi olan coinler hâlâ değerlendirilebilir.
    scan_limit = max(n * 3, 160)
    preliminary = preliminary[:scan_limit]

    # CORE/PREFERRED semboller listede yoksa yine de denemeye al.
    protected = list(dict.fromkeys(CORE_SYMBOLS + PREFERRED_SYMBOLS))
    for sym in protected:
        if sym in ticker_by_symbol and all(sym != x[0] for x in preliminary):
            item = ticker_by_symbol[sym]
            preliminary.append((sym, _safe_float(item.get("quoteVolume")), _safe_float(item.get("priceChangePercent"))))

    print(f"[INFO] Ön aday sayısı: {len(preliminary)}")

    for idx, (symbol, quote_volume_24h, change_pct) in enumerate(preliminary, 1):
        daily_vols = _fetch_daily_quote_volumes(symbol, STABILITY_DAYS)
        time.sleep(REQUEST_DELAY)

        if len(daily_vols) < MIN_DAILY_CANDLES:
            continue

        med_7d = median(daily_vols)
        avg_7d = sum(daily_vols) / len(daily_vols)

        if med_7d < MIN_7D_MEDIAN_QUOTE_VOLUME:
            continue

        spike_ratio = quote_volume_24h / max(med_7d, 1.0)
        if spike_ratio > MAX_SPIKE_RATIO and symbol not in CORE_SYMBOLS:
            continue

        # ── v3: Momentum filtresi ─────────────────────────────
        mdata = _fetch_momentum_data(symbol, MOMENTUM_DAYS)
        time.sleep(REQUEST_DELAY)   # API rate limit

        if _is_excluded_by_momentum(mdata, symbol):
            print(f"  [MOMENTUM_ELE] {symbol:<16} "
                  f"30d={mdata.get('change_30d','?')}%  "
                  f"win_days={mdata.get('win_days_ratio','?'):.0%}")
            continue

        mom_score = _calc_momentum_score(mdata, symbol)
        # ─────────────────────────────────────────────────────

        score = _candidate_score(
            quote_volume_24h=quote_volume_24h,
            median_quote_volume_7d=med_7d,
            avg_quote_volume_7d=avg_7d,
            spike_ratio=spike_ratio,
            change_abs=abs(change_pct),
            symbol=symbol,
            momentum_score=mom_score,
        )

        candidates.append(SymbolCandidate(
            symbol=symbol,
            quote_volume_24h=quote_volume_24h,
            price_change_pct_24h=change_pct,
            median_quote_volume_7d=med_7d,
            avg_quote_volume_7d=avg_7d,
            spike_ratio=spike_ratio,
            score=score,
            momentum_score=mom_score,
            change_30d=mdata.get("change_30d", 0.0),
            win_days_ratio=mdata.get("win_days_ratio", 0.0),
            daily_vol_std=mdata.get("daily_vol_std", 0.0),
            ema_ok=mdata.get("ema_ok", False),
        ))

        if idx % 25 == 0:
            print(f"[INFO] Stabilite taraması: {idx}/{len(preliminary)}")

    candidates.sort(key=lambda x: x.score, reverse=True)

    top_symbols = [c.symbol for c in candidates[:n]]

    # Güvenlik: duplicate temizliği ve kesin format kontrolü.
    top_symbols = [s for s in dict.fromkeys(top_symbols) if not is_excluded_symbol(s)]

    try:
        with open(outfile, "w", encoding="utf-8") as f:
            json.dump(top_symbols, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[HATA] Dosya yazılamadı: {e}")
        return []

    # Detay raporu: sonraki analizlerde neden seçildiğini görmek için.
    meta_path = outfile.replace(".json", "_meta.json")
    try:
        meta = [
            {
                "symbol":             c.symbol,
                "score":              round(c.score, 2),
                "quote_volume_24h":   round(c.quote_volume_24h, 2),
                "price_change_pct_24h": round(c.price_change_pct_24h, 2),
                "median_quote_volume_7d": round(c.median_quote_volume_7d, 2),
                "avg_quote_volume_7d": round(c.avg_quote_volume_7d, 2),
                "spike_ratio":        round(c.spike_ratio, 2),
                # v3: momentum alanları
                "momentum_score":     round(c.momentum_score, 3),
                "change_30d":         round(c.change_30d, 2),
                "win_days_ratio":     round(c.win_days_ratio, 3),
                "daily_vol_std":      round(c.daily_vol_std, 2),
                "ema_ok":             c.ema_ok,
            }
            for c in candidates[:n]
        ]
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[UYARI] Meta dosyası yazılamadı: {e}")

    print(f"[OK] {len(top_symbols)} sembol yazıldı → {outfile}")
    print(f"[OK] Meta rapor yazıldı → {meta_path}")
    print(f"[OK] İlk 20 sembol: {top_symbols[:20]}")

    # v3: Momentum özet raporu
    if candidates:
        pos_mom  = [c for c in candidates[:n] if c.momentum_score > 0]
        neg_mom  = [c for c in candidates[:n] if c.momentum_score < 0]
        neutral  = [c for c in candidates[:n] if c.momentum_score == 0.0]
        ema_ok_c = [c for c in candidates[:n] if c.ema_ok]
        print(f"\n[MOMENTUM] Liste kalite özeti:")
        print(f"  Pozitif momentum (+): {len(pos_mom)} sembol")
        print(f"  Nötr momentum  (=): {len(neutral)} sembol")
        print(f"  Negatif momentum (-): {len(neg_mom)} sembol")
        print(f"  EMA5 > EMA20 (uptrend): {len(ema_ok_c)} sembol")
        if candidates[:n]:
            avg_c30 = sum(c.change_30d for c in candidates[:n]) / len(candidates[:n])
            print(f"  Ort 30d getiri: %{avg_c30:.1f}")

    return top_symbols


if __name__ == "__main__":
    try:
        n = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TOP_N
    except Exception:
        n = DEFAULT_TOP_N

    build_top_usdt(n=n)