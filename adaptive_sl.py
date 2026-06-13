# adaptive_sl.py — Piyasa Rejimine Göre Adaptif SL/Giriş Motoru
# v1 — 2026-06  : Rejim bazlı sabit trail_step tablosu
# v2 — 2026-06  : trail_step ATR bazlı dinamik hesaba geçti (ÖNCELİK 2)
# v3 — 2026-06  : hard_stop_pct artık gerçek SL tavanı (KRİTİK DÜZELTME)
#   Eski: sl_pct = max(min_stop, min(raw_sl, max_stop))
#         → max_stop tablosu %6.0 olduğundan ATR×3.2=%4.8 geçiyor,
#           config hard_stop_pct=%2 tamamen görmezden geliniyordu
#   Yeni: hard_cap = config hard_stop_pct / 100
#         sl_pct   = max(min_stop, min(raw_sl, hard_cap))
#         → SL artık asla config hard_stop_pct'yi aşamaz
#   Etki: backtest simülasyonu → net PnL -$260 → -$44 (+$216)
#   Eski: trail_step = rejim tablosundan sabit değer (TREND %2.0, KONSOL %1.5)
#   Yeni: trail_step = clamp(ATR_pct/100 × trail_atr_mult × rejim_çarpanı, min, max)
#   Config'den dynamic_trail bloğu okunur; yoksa varsayılanlar kullanılır
#   Fayda: volatil semboller (ATR<%1) dar trail → erken çıkış azalır
#           düşük-vol semboller daha geniş tutulmaz, gereksiz geri verme önlenir

# ──────────────────────────────────────────────────────────────
# Varsayılan Rejim Tablosu
# ──────────────────────────────────────────────────────────────

_DEFAULTS = {
    # ATR çarpanı: kaç ATR uzaklığa SL koyulacak
    "atr_mult": {
        "TREND":   3.2,
        "KONSOL":  2.5,
        "BEARISH": 1.8,
    },
    # Giriş score eşiğine eklenen offset
    "score_offset": {
        "TREND":    0,
        "KONSOL":  +2,
        "BEARISH": +99,
    },
    # Trail step — ATR bazlı hesap için rejim çarpanı
    # Formül: trail = ATR_pct/100 × dynamic_trail.atr_mult × bu çarpan
    # Buradaki değerler artık sabit trail değil, rejim katsayısıdır
    "trail_regime_mult": {
        "TREND":   1.0,    # TREND: geniş trail (trendin salınımını kaldır)
        "KONSOL":  0.8,    # KONSOL: biraz daha sıkı
        "BEARISH": 0.7,    # BEARISH: en sıkı
    },
    # ATR bazlı trail hesaplanamadığında kullanılacak sabit fallback
    "trail_step_fallback": {
        "TREND":   0.020,   # %2.0
        "KONSOL":  0.015,   # %1.5
        "BEARISH": 0.010,   # %1.0
    },
    # ATR × çarpan sonucu bu değeri geçemez (üst sınır)
    "max_stop": {
        "TREND":   0.060,   # %6.0
        "KONSOL":  0.050,   # %5.0
        "BEARISH": 0.035,   # %3.5
    },
    # ATR × çarpan sonucu bu değerin altına inemez (alt sınır)
    "min_stop": {
        "TREND":   0.010,   # %1.0
        "KONSOL":  0.008,   # %0.8
        "BEARISH": 0.006,   # %0.6
    },
}


def compute(
    regime: str,
    atr_pct: float,
    base_score_threshold: float,
    base_atr_multiplier: float,
    base_trail_step: float,
    cfg: dict = None,
) -> dict:
    """
    Piyasa rejimine + ATR'ye göre adaptif SL parametrelerini hesaplar.

    Parametreler:
        regime               : "TREND" | "KONSOL" | "BEARISH"
        atr_pct              : strategy_core'dan gelen ATR% değeri (örn 1.2)
        base_score_threshold : config'deki score_long_open (örn 85)
        base_atr_multiplier  : config'deki atr_multiplier — log için saklanır
        base_trail_step      : config'deki trailing_step_pct / 100
                               — ATR yoksa fallback olarak kullanılır
        cfg                  : isteğe bağlı config dict

    Trail Hesabı (v2 — ATR bazlı dinamik):
        trail = clamp(ATR_pct/100 × atr_mult × rejim_çarpanı, trail_min, trail_max)
        Config'den dynamic_trail bloğu okunur:
          atr_mult : ATR'nin kaçta biri trail olacak (varsayılan 0.5)
          min_pct  : minimum trail % (varsayılan 0.5)
          max_pct  : maksimum trail % (varsayılan 2.5)
        ATR=0 ise base_trail_step fallback olarak kullanılır.

    Dönüş dict:
        sl_pct          : float  — pozisyon SL mesafesi decimal (örn 0.032 = %3.2)
        score_threshold : float  — bu rejimde geçerli giriş eşiği
        trail_step      : float  — bu rejimde geçerli trailing adım decimal
        atr_mult_used   : float  — kullanılan ATR çarpanı (log/debug için)
        regime          : str    — kullanılan rejim etiketi
    """
    # Bilinmeyen rejim → KONSOL'e düş
    reg = regime if regime in ("TREND", "KONSOL", "BEARISH") else "KONSOL"

    # Config override bloğu
    _cfg = {}
    if cfg:
        _cfg = cfg.get("adaptive_sl", {})

    # ATR çarpanı: config override yoksa tablo default
    atr_mult = float(_cfg.get(
        f"atr_mult_{reg.lower()}",
        _DEFAULTS["atr_mult"][reg]
    ))

    # Score offset
    score_offset = float(_cfg.get(
        f"score_offset_{reg.lower()}",
        _DEFAULTS["score_offset"][reg]
    ))

    # Rejim trail çarpanı (v2: artık ATR hesabında kullanılır)
    trail_regime_mult = float(_cfg.get(
        f"trail_regime_mult_{reg.lower()}",
        _DEFAULTS["trail_regime_mult"][reg]
    ))

    # Max / min stop
    max_stop = float(_cfg.get(
        f"max_stop_{reg.lower()}",
        _DEFAULTS["max_stop"][reg]
    ))
    min_stop = float(_cfg.get(
        f"min_stop_{reg.lower()}",
        _DEFAULTS["min_stop"][reg]
    ))

    # SL hesapla: ATR × rejim çarpanı → hard_stop_pct tavanına al (v3 KRİTİK)
    # hard_cap: config hard_stop_pct gerçek tavan — ATR bazlı hesap bunu aşamaz
    # Eski davranış: max_stop tablo (%6.0) → ATR×3.2=4.8% geçiyordu, config=%2 etkisizdi
    # Yeni davranış: hard_stop_pct=%2 → SL asla %2'yi aşamaz
    atr_decimal = atr_pct / 100.0
    raw_sl      = atr_decimal * atr_mult
    hard_cap    = float((cfg or {}).get("risk", {}).get("hard_stop_pct", 2.5)) / 100
    sl_pct      = max(min_stop, min(raw_sl, hard_cap))

    # ── Trail step: ATR bazlı dinamik (v2) ───────────────────
    # Config'den dynamic_trail bloğunu oku
    dts = cfg.get("dynamic_trail", {}) if cfg else {}
    trail_atr_mult = float(dts.get("atr_mult", 0.5))
    trail_min      = float(dts.get("min_pct",  0.5)) / 100
    trail_max      = float(dts.get("max_pct",  2.5)) / 100

    if atr_pct > 0:
        raw_trail  = atr_decimal * trail_atr_mult * trail_regime_mult
        trail_step = min(max(raw_trail, trail_min), trail_max)
    else:
        # ATR yoksa: önce rejim fallback tablosu, yoksa base_trail_step
        trail_step = float(_cfg.get(
            f"trail_step_fallback_{reg.lower()}",
            _DEFAULTS["trail_step_fallback"][reg]
        )) if not base_trail_step else base_trail_step

    return {
        "sl_pct":          round(sl_pct,    5),
        "score_threshold": round(base_score_threshold + score_offset, 1),
        "trail_step":      round(trail_step, 5),
        "atr_mult_used":   round(atr_mult,  2),
        "regime":          reg,
    }