"""
Análisis de oportunidades pre-partido
Escanea mercados y recomienda dónde apostar para maximizar probabilidad de surebet.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Scoring weights
WEIGHTS = {
    "odds_quality":   0.35,   # Cuota alta = mejor
    "liquidity":      0.25,   # Más liquidez = mejor
    "spread":         0.20,   # Spread estrecho = mercado activo
    "timing":         0.15,   # 1-3h antes = ideal
    "sport_bonus":    0.05,   # Tenis/basket = bonus
}

# Sports con más volatilidad (mejores para surebets)
HIGH_VOLATILITY_SPORTS = {"Tennis", "Basketball", "Baseball"}
LOW_VOLATILITY_SPORTS  = {"Soccer"}  # solo cuando es underdog claro

# Umbral mínimo de liquidez (USDC)
MIN_LIQUIDITY = 50.0


def analyze_prematches(markets: dict, orders: dict, min_roi: float = 3.0) -> list:
    """
    Analiza mercados pre-partido y devuelve oportunidades ordenadas por score.
    
    Args:
        markets: {marketHash: market_data}
        orders:  {marketHash: [order1, order2, ...]}
        min_roi: ROI mínimo objetivo
        
    Returns:
        Lista de oportunidades ordenadas por score (100 = mejor)
    """
    if not markets or not isinstance(markets, dict):
        log.warning("analyze_prematches: markets vacío o no es dict")
        return []
    
    now = time.time()
    opportunities = []
    
    for mh, mkt in markets.items():
        if not isinstance(mkt, dict):
            log.warning(f"Market {mh} no es dict, skipping")
            continue
        
        # Solo pre-partido (gameTime en el futuro)
        game_time = mkt.get("gameTime", 0) or 0
        if game_time <= now or game_time <= 0:
            continue
        
        # Tiempo hasta el partido
        time_until = game_time - now
        hours_until = time_until / 3600
        
        # Filtro: solo 0.5h - 6h antes (demasiado pronto o tarde = skip)
        if hours_until < 0.5 or hours_until > 6:
            continue
        
        sport = mkt.get("sportLabel", "Unknown")
        team1 = mkt.get("teamOneName", "Team1")
        team2 = mkt.get("teamTwoName", "Team2")
        league = mkt.get("leagueLabel", "Unknown")
        
        # Analizar ambos lados
        mkt_orders = orders.get(mh, [])
        
        for side_one in [True, False]:
            side_name = team1 if side_one else team2
            opp_name  = team2 if side_one else team1
            
            # Mejor cuota taker disponible para este lado
            taker_odds = _best_taker_odds(mkt_orders, side_one)
            if taker_odds < 1.5:
                continue  # cuotas muy bajas no interesan
            
            # Liquidez disponible
            liquidity = _total_liquidity(mkt_orders, side_one)
            if liquidity < MIN_LIQUIDITY:
                continue
            
            # Cuota contraria
            opp_odds = _best_taker_odds(mkt_orders, not side_one)
            if opp_odds < 1.01:
                continue  # no hay liquidez del otro lado
            
            # Calcular cuota mínima requerida para cerrar surebet
            required_opp_odds = _required_odds_for_roi(taker_odds, min_roi)
            
            # Score individual de cada factor
            odds_score     = _score_odds(taker_odds)
            liquidity_score = _score_liquidity(liquidity)
            spread_score    = _score_spread(taker_odds, opp_odds)
            timing_score    = _score_timing(hours_until)
            sport_score     = _score_sport(sport)
            
            # Score total ponderado
            total_score = (
                odds_score     * WEIGHTS["odds_quality"] +
                liquidity_score * WEIGHTS["liquidity"] +
                spread_score    * WEIGHTS["spread"] +
                timing_score    * WEIGHTS["timing"] +
                sport_score     * WEIGHTS["sport_bonus"]
            ) * 100
            
            # Determinar viabilidad
            viable = opp_odds >= required_opp_odds * 0.95  # margen 5%
            
            opportunities.append({
                "market_hash":    mh,
                "sport":          sport,
                "league":         league,
                "team1":          team1,
                "team2":          team2,
                "side":           side_name,
                "opp_side":       opp_name,
                "odds":           taker_odds,
                "opp_odds":       opp_odds,
                "required_odds":  required_opp_odds,
                "liquidity":      liquidity,
                "hours_until":    hours_until,
                "game_time":      game_time,
                "score":          total_score,
                "viable":         viable,
                "recommendation": _get_recommendation(total_score, viable, liquidity),
            })
    
    # Ordenar por score
    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities


def _best_taker_odds(orders: list, want_outcome_one: bool) -> float:
    """Mejor cuota taker disponible."""
    best = 0.0
    for o in orders:
        maker_side = bool(o.get("isMakerBettingOutcomeOne"))
        if maker_side == want_outcome_one:
            continue  # necesitamos lado contrario
        try:
            pct = float(o["percentageOdds"]) / 1e20
            if 0 < pct < 1:
                taker = 1.0 / (1.0 - pct)
                if taker > best:
                    best = taker
        except Exception:
            pass
    return best


def _total_liquidity(orders: list, want_outcome_one: bool) -> float:
    """Liquidez total disponible (USDC)."""
    total = 0.0
    for o in orders:
        maker_side = bool(o.get("isMakerBettingOutcomeOne"))
        if maker_side == want_outcome_one:
            continue
        try:
            fillable = float(o.get("fillAmount", 0)) / 1e6
            total += fillable
        except Exception:
            pass
    return total


def _required_odds_for_roi(odds_a: float, min_roi: float) -> float:
    """Cuota mínima necesaria del otro lado para conseguir min_roi%."""
    if odds_a <= 1.01:
        return 999.0
    r = min_roi / 100.0
    # stake_b / stake_a = (odds_a - (1+r)) / (1+r)
    # odds_b_min = odds_a / (odds_a - (1+r)) * (1+r)
    denominator = odds_a - 1 - r
    if denominator <= 0:
        return 999.0
    return odds_a * (1 + r) / denominator


def _score_odds(odds: float) -> float:
    """Score 0-1 basado en la cuota (más alta = mejor)."""
    # Óptimo: 3.0-5.0
    if 3.0 <= odds <= 5.0:
        return 1.0
    if 2.5 <= odds < 3.0:
        return 0.9
    if 5.0 < odds <= 7.0:
        return 0.85
    if 2.0 <= odds < 2.5:
        return 0.7
    if 7.0 < odds <= 10.0:
        return 0.75
    if 1.5 <= odds < 2.0:
        return 0.4
    if odds > 10.0:
        return 0.6  # muy alta = riesgo de cancelación
    return 0.2


def _score_liquidity(liq: float) -> float:
    """Score 0-1 basado en liquidez disponible."""
    if liq >= 500:
        return 1.0
    if liq >= 300:
        return 0.9
    if liq >= 150:
        return 0.75
    if liq >= 75:
        return 0.6
    if liq >= 50:
        return 0.4
    return 0.2


def _score_spread(odds_a: float, odds_b: float) -> float:
    """Score 0-1 basado en spread (diferencia entre ambas cuotas)."""
    # Spread estrecho = mercado activo y eficiente
    implied_a = 1.0 / odds_a if odds_a > 0 else 0
    implied_b = 1.0 / odds_b if odds_b > 0 else 0
    total_implied = implied_a + implied_b
    overround = total_implied - 1.0  # cuanto más bajo, mejor
    
    if overround < 0.02:    # <2% = excelente
        return 1.0
    if overround < 0.05:    # <5% = muy bueno
        return 0.9
    if overround < 0.08:    # <8% = bueno
        return 0.75
    if overround < 0.12:    # <12% = aceptable
        return 0.6
    return 0.3


def _score_timing(hours_until: float) -> float:
    """Score 0-1 basado en tiempo hasta el partido."""
    # Óptimo: 1-3 horas antes
    if 1.0 <= hours_until <= 3.0:
        return 1.0
    if 0.5 <= hours_until < 1.0:
        return 0.85
    if 3.0 < hours_until <= 4.5:
        return 0.9
    if 4.5 < hours_until <= 6.0:
        return 0.7
    return 0.5


def _score_sport(sport: str) -> float:
    """Bonus por deporte con alta volatilidad."""
    if sport in HIGH_VOLATILITY_SPORTS:
        return 1.0
    if sport in LOW_VOLATILITY_SPORTS:
        return 0.5
    return 0.7  # neutral


def _get_recommendation(score: float, viable: bool, liquidity: float) -> str:
    """Texto de recomendación basado en score."""
    if score >= 85 and viable:
        return "🔥 EXCELENTE"
    if score >= 75 and viable:
        return "✅ MUY BUENA"
    if score >= 65:
        return "🟢 BUENA"
    if score >= 50:
        return "🟡 ACEPTABLE"
    if not viable:
        return "⚠️ CUOTA CONTRARIA INSUFICIENTE"
    if liquidity < 100:
        return "⚠️ POCA LIQUIDEZ"
    return "❌ EVITAR"
