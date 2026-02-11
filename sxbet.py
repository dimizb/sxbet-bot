"""
SX.bet API Client
Toda la lógica de conexión, cálculo de odds y detección de surebets.
"""

import logging
import time
from typing import Optional
import requests

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  CONSTANTES
# ─────────────────────────────────────────────────────────────

API_BASE      = "https://api.sx.bet"
ODDS_SCALE    = 1e20
USDC_SCALE    = 1e6

MARKET_NAMES = {
    1: "1X2", 2: "Over/Under", 3: "Asian HC",
    52: "Money Line", 88: "To Qualify",
    226: "ML+OT", 201: "Asian HC Games",
    342: "Asian HC+OT", 835: "Asian O/U",
    28: "O/U+OT", 29: "O/U Rounds",
    166: "O/U Games", 1536: "O/U Maps",
    274: "Outright", 63: "12 HT",
    77: "O/U HT", 866: "Set Spread", 165: "Set Total"
}

# ─────────────────────────────────────────────────────────────
#  CLIENTE
# ─────────────────────────────────────────────────────────────

class SXBetClient:
    def __init__(self, api_key: str, wallet: str):
        self.wallet  = wallet.lower()
        self.session = requests.Session()
        self.session.headers.update({
            "X-Api-Key":    api_key,
            "Content-Type": "application/json",
        })
        self._rate_limited_until = 0.0  # timestamp hasta el que no pedir

    def _get(self, url: str, params: dict = None, timeout: int = 15) -> dict | None:
        """
        GET con detección automática de rate limit (HTTP 429).
        Si hay 429, espera el tiempo indicado en el header Retry-After
        y devuelve None para que el llamador use el caché anterior.
        """
        import time as _time

        now = _time.time()
        if now < self._rate_limited_until:
            wait = self._rate_limited_until - now
            log.warning(f"Rate limit activo, esperando {wait:.1f}s más")
            return None

        try:
            r = self.session.get(url, params=params, timeout=timeout)

            if r.status_code == 429:
                # Leer cuánto hay que esperar
                retry_after = int(r.headers.get("Retry-After", 30))
                self._rate_limited_until = _time.time() + retry_after
                log.warning(
                    f"⚠️ RATE LIMIT 429 en {url} — "
                    f"esperando {retry_after}s (hasta {retry_after}s desde ahora)"
                )
                return None

            body = r.json()
            return body

        except Exception as e:
            log.error(f"Error GET {url}: {e}")
            return None

    # ── Trades ──────────────────────────────────────────────

    def fetch_all_trades(self, settled: Optional[bool] = None,
                         start_ts: Optional[int] = None,
                         end_ts:   Optional[int] = None) -> list:
        """
        Descarga todos los trades del wallet paginando automáticamente.
        settled=None → todos, settled=True → liquidadas, settled=False → activas.
        """
        params = {"bettor": self.wallet, "pageSize": 100}
        if settled is not None:
            params["settled"] = str(settled).lower()
        if start_ts:
            params["startDate"] = start_ts
        if end_ts:
            params["endDate"] = end_ts

        all_trades = []
        page = 0
        next_key = None

        while page < 20:
            page += 1
            if next_key:
                params["paginationKey"] = next_key

            try:
                body = self._get(f"{API_BASE}/trades", params=params)
                if body is None:
                    break  # rate limited, usar datos parciales
            except Exception as e:
                log.error(f"fetch_all_trades error: {e}")
                break


            if body.get("status") != "success":
                log.error(f"API /trades error: {body}")
                break

            batch    = body["data"].get("trades", [])
            next_key = body["data"].get("nextKey") if batch else None
            all_trades.extend(batch)
            log.info(f"  Página {page}: {len(batch)} trades (total: {len(all_trades)})")

            if not next_key:
                break

        return all_trades

    # ── Mercados ────────────────────────────────────────────

    def fetch_markets(self, hashes: list) -> dict:
        """Devuelve {marketHash: marketData} en batches de 30."""
        unique = list(dict.fromkeys(hashes))
        result = {}

        for i in range(0, len(unique), 30):
            batch = unique[i:i+30]
            try:
                body = self._get(
                    f"{API_BASE}/markets/find",
                    params={"marketHashes": ",".join(batch)}
                )
                if body and body.get("status") == "success":
                    for m in (body.get("data") or []):
                        result[m["marketHash"]] = m
            except Exception as e:
                log.error(f"fetch_markets error: {e}")

        return result

    # ── Órdenes activas ─────────────────────────────────────

    def fetch_orders(self, hashes: list) -> dict:
        """
        Devuelve {marketHash: [orders]} con las órdenes activas de cada mercado.
        La API devuelve un array plano; agrupamos por marketHash.
        """
        unique = list(dict.fromkeys(hashes))
        result = {}

        for i in range(0, len(unique), 30):
            batch = unique[i:i+30]
            try:
                body = self._get(
                    f"{API_BASE}/orders",
                    params={"marketHashes": ",".join(batch)}
                )
                if not body or body.get("status") != "success":
                    continue

                data = body.get("data", [])

                # La API puede devolver array o dict {marketHash: [...]}
                orders_flat = []
                if isinstance(data, list):
                    orders_flat = data
                elif isinstance(data, dict):
                    for mh, arr in data.items():
                        if isinstance(arr, list):
                            for o in arr:
                                if "marketHash" not in o:
                                    o["marketHash"] = mh
                                orders_flat.append(o)

                for order in orders_flat:
                    mh = order.get("marketHash")
                    if not mh:
                        continue
                    result.setdefault(mh, []).append(order)

            except Exception as e:
                log.error(f"fetch_orders error: {e}")

        total = sum(len(v) for v in result.values())
        log.info(f"fetch_orders: {total} órdenes en {len(result)} mercados")
        return result

    # ── Agrupación de trades ────────────────────────────────

    def group_trades(self, trades: list) -> list:
        """
        Agrupa trades por (marketHash, side, settled).
        Calcula stake total y cuota media ponderada.
        """
        groups: dict = {}

        for t in trades:
            key = (
                t["marketHash"],
                bool(t.get("bettingOutcomeOne")),
                bool(t.get("settled"))
            )
            if key not in groups:
                groups[key] = {
                    "market_hash":         t["marketHash"],
                    "betting_outcome_one": bool(t.get("bettingOutcomeOne")),
                    "settled":             bool(t.get("settled")),
                    "outcome":             t.get("outcome"),
                    "settle_date":         t.get("settleDate"),
                    "bet_time":            t.get("betTime", 0),
                    "items":               [],
                }
            g = groups[key]
            g["items"].append(t)

            # Conservar betTime más reciente y último resultado
            if (t.get("betTime") or 0) >= (g["bet_time"] or 0):
                g["bet_time"]    = t.get("betTime", 0)
                g["outcome"]     = t.get("outcome")
                g["settle_date"] = t.get("settleDate")

        result = []
        for g in groups.values():
            total_stake   = 0.0
            weighted_odds = 0.0
            for t in g["items"]:
                s = _get_stake(t)
                o = _get_odds_decimal(t.get("odds", "0"))
                total_stake   += s
                weighted_odds += s * o

            avg_odds     = weighted_odds / total_stake if total_stake > 0 else 0.0
            potential    = total_stake * avg_odds
            stable_key   = g["market_hash"] + "__" + ("1" if g["betting_outcome_one"] else "0")

            # Resultado para liquidadas
            outcome = g["outcome"]
            won = (outcome == 1 and g["betting_outcome_one"]) or \
                  (outcome == 2 and not g["betting_outcome_one"])
            void = outcome == 0
            result_str = "VOID" if void else ("GANADA" if won else ("PERDIDA" if g["settled"] else "-"))

            result.append({
                "stable_key":          stable_key,
                "market_hash":         g["market_hash"],
                "betting_outcome_one": g["betting_outcome_one"],
                "settled":             g["settled"],
                "outcome":             outcome,
                "result":              result_str,
                "settle_date":         g["settle_date"],
                "bet_time":            g["bet_time"],
                "num_trades":          len(g["items"]),
                "total_stake":         total_stake,
                "avg_odds":            avg_odds,
                "potential_win":       potential,
            })

        return result


# ─────────────────────────────────────────────────────────────
#  DETECCIÓN DE SUREBETS
# ─────────────────────────────────────────────────────────────

def find_surebets(groups: list, markets: dict, orders: dict, min_roi: float = 0.0) -> list:
    """
    Para cada apuesta activa calcula si hay surebet disponible.
    Devuelve lista ordenada por ROI descendente.
    """
    surebets = []

    for g in groups:
        if g["settled"]:
            continue

        mkt        = markets.get(g["market_hash"], {})
        mkt_orders = orders.get(g["market_hash"], [])

        # Cuota mejor disponible en el lado contrario (para cubrir)
        live_opp = _best_taker_odds(mkt_orders, not g["betting_outcome_one"])
        if live_opp <= 1.01:
            continue  # sin liquidez para cubrir

        # Cálculo de cobertura
        potential  = g["potential_win"]
        hedge      = potential / live_opp
        profit_a   = potential - g["total_stake"] - hedge          # si gana apuesta original
        profit_b   = hedge * live_opp - g["total_stake"] - hedge   # si gana cobertura
        guaranteed = min(profit_a, profit_b)
        roi        = (guaranteed / g["total_stake"]) * 100

        if guaranteed <= 0:
            continue  # no es surebet real
        if roi < min_roi:
            continue  # por debajo del ROI mínimo configurado

        live_same = _best_taker_odds(mkt_orders, g["betting_outcome_one"])

        side = mkt.get("outcomeOneName", "O1") if g["betting_outcome_one"] \
               else mkt.get("outcomeTwoName", "O2")

        surebets.append({
            "stable_key":        g["stable_key"],
            "market_hash":       g["market_hash"],
            "event":             f"{mkt.get('teamOneName','?')} vs {mkt.get('teamTwoName','?')}",
            "sport":             mkt.get("sportLabel", "?"),
            "league":            mkt.get("leagueLabel", "?"),
            "market_type":       _market_type(mkt.get("type"), mkt.get("line")),
            "side":              side,
            "total_stake":       g["total_stake"],
            "avg_odds":          g["avg_odds"],
            "potential_win":     potential,
            "live_same_odds":    live_same,
            "live_opp_odds":     live_opp,
            "hedge_stake":       hedge,
            "guaranteed_profit": guaranteed,
            "roi":               roi,
        })

    surebets.sort(key=lambda x: x["roi"], reverse=True)
    return surebets


# ─────────────────────────────────────────────────────────────
#  SUREBETS CERRADAS (dos patas activas en mismo mercado)
# ─────────────────────────────────────────────────────────────

def detect_closed_surebets(groups: list) -> dict:
    """
    Detecta mercados donde tenemos apuestas activas en AMBOS lados.
    Retorna {market_hash: {leg1, leg2, total_stake, guaranteed_profit, roi}}
    """
    active = [g for g in groups if not g["settled"]]

    # Agrupar activas por market_hash
    by_market: dict = {}
    for g in active:
        by_market.setdefault(g["market_hash"], []).append(g)

    closed: dict = {}
    for mh, legs in by_market.items():
        # Buscar si hay una pata en cada lado
        side1 = next((l for l in legs if     l["betting_outcome_one"]), None)
        side2 = next((l for l in legs if not l["betting_outcome_one"]), None)

        if not side1 or not side2:
            continue  # Solo un lado apostado

        total_stake = side1["total_stake"] + side2["total_stake"]
        # Beneficio garantizado = min(retorno1 - total, retorno2 - total)
        profit1 = side1["potential_win"] - total_stake
        profit2 = side2["potential_win"] - total_stake
        guaranteed = min(profit1, profit2)
        roi = (guaranteed / total_stake * 100) if total_stake > 0 else 0.0

        closed[mh] = {
            "market_hash":    mh,
            "leg1":           side1,
            "leg2":           side2,
            "total_stake":    total_stake,
            "profit1":        profit1,
            "profit2":        profit2,
            "guaranteed":     guaranteed,
            "roi":            roi,
        }

    return closed


# ─────────────────────────────────────────────────────────────
#  ESTADÍSTICAS
# ─────────────────────────────────────────────────────────────

def get_stats(groups: list) -> dict:
    settled = [g for g in groups if g["settled"]]
    active  = [g for g in groups if not g["settled"]]

    won  = [g for g in settled if g["result"] == "GANADA"]
    lost = [g for g in settled if g["result"] == "PERDIDA"]
    void = [g for g in settled if g["result"] == "VOID"]

    total_stake = sum(g["total_stake"] for g in settled)
    pnl = sum(
        (g["potential_win"] - g["total_stake"]) if g["result"] == "GANADA"
        else (-g["total_stake"])                if g["result"] == "PERDIDA"
        else 0.0
        for g in settled
    )
    decididos = len(won) + len(lost)
    win_rate = (len(won) / decididos * 100) if decididos > 0 else 0.0
    roi      = (pnl / total_stake * 100) if total_stake > 0 else 0.0

    # Por deporte
    by_sport: dict = {}
    for g in settled:
        # No tenemos markets aquí, así que usamos el market_hash como fallback
        sport = "Desconocido"  # Se enriquece en bot.py si quieres pasar markets
        by_sport.setdefault(sport, {"won":0,"lost":0,"void":0,"stake":0.0,"pnl":0.0})
        s = by_sport[sport]
        s["stake"] += g["total_stake"]
        if g["result"] == "GANADA":
            s["won"]  += 1
            s["pnl"]  += g["potential_win"] - g["total_stake"]
        elif g["result"] == "PERDIDA":
            s["lost"] += 1
            s["pnl"]  -= g["total_stake"]
        else:
            s["void"] += 1
        dec = s["won"] + s["lost"]
        s["roi"] = (s["pnl"] / s["stake"] * 100) if s["stake"] > 0 else 0.0

    # Surebets cerradas activas (ambas patas sin liquidar)
    closed_sb = detect_closed_surebets(groups)

    return {
        "total":           len(groups),
        "active":          len(active),
        "settled":         len(settled),
        "won":             len(won),
        "lost":            len(lost),
        "void":            len(void),
        "win_rate":        win_rate,
        "total_stake":     total_stake,
        "pnl":             pnl,
        "roi":             roi,
        "by_sport":        by_sport,
        "surebets_closed": len(closed_sb),
        "surebets_roi_avg": (
            sum(v["roi"] for v in closed_sb.values()) / len(closed_sb)
            if closed_sb else 0.0
        ),
    }


def get_stats_with_markets(groups: list, markets: dict) -> dict:
    """Versión enriquecida con datos de mercado para stats por deporte/liga."""
    s = get_stats(groups)

    # Recalcular by_sport y by_league con datos reales
    by_sport:  dict = {}
    by_league: dict = {}

    for g in groups:
        if not g["settled"]:
            continue
        mkt    = markets.get(g["market_hash"], {})
        sport  = mkt.get("sportLabel",  "Desconocido")
        league = mkt.get("leagueLabel", "Desconocido")
        stake  = g["total_stake"]
        pnl    = (g["potential_win"] - stake) if g["result"] == "GANADA" \
                 else (-stake)                 if g["result"] == "PERDIDA" else 0.0

        for bucket, key in [(by_sport, sport), (by_league, league)]:
            bucket.setdefault(key, {"won":0,"lost":0,"void":0,"stake":0.0,"pnl":0.0,"roi":0.0})
            b = bucket[key]
            b["stake"] += stake
            b["pnl"]   += pnl
            if   g["result"] == "GANADA":  b["won"]  += 1
            elif g["result"] == "PERDIDA": b["lost"] += 1
            else:                          b["void"] += 1
            b["roi"] = (b["pnl"] / b["stake"] * 100) if b["stake"] > 0 else 0.0

    s["by_sport"]  = by_sport
    s["by_league"] = by_league
    return s


# ─────────────────────────────────────────────────────────────
#  HELPERS INTERNOS
# ─────────────────────────────────────────────────────────────

def _get_stake(trade: dict) -> float:
    if trade.get("normalizedStake"):
        return float(trade["normalizedStake"])
    if trade.get("betTimeValue"):
        return float(trade["betTimeValue"])
    if trade.get("stake"):
        return float(trade["stake"]) / USDC_SCALE
    return 0.0


def _get_odds_decimal(odds_str: str) -> float:
    try:
        implied = float(odds_str) / ODDS_SCALE
        return 1.0 / implied if 0 < implied < 1 else 0.0
    except Exception:
        return 0.0


def _best_taker_odds(orders: list, want_outcome_one: bool) -> float:
    """
    Mejor cuota taker disponible para apostar 'want_outcome_one'.
    Necesita makers en el lado CONTRARIO.
    takerOdds = 1 / (1 - makerImplied)
    """
    best = 0.0
    for o in orders:
        if bool(o.get("isMakerBettingOutcomeOne")) == want_outcome_one:
            continue  # necesitamos el lado contrario
        try:
            maker_implied = float(o["percentageOdds"]) / ODDS_SCALE
            if 0 < maker_implied < 1:
                taker = 1.0 / (1.0 - maker_implied)
                if taker > best:
                    best = taker
        except Exception:
            pass
    return best


def _market_type(type_id, line) -> str:
    name = MARKET_NAMES.get(type_id, f"Tipo {type_id}")
    return f"{name} ({line})" if line is not None else name
