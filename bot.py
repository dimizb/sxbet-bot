"""
SX.bet Surebet Monitor — Bot de Telegram
Detecta oportunidades de arbitraje en tus apuestas abiertas y avisa por Telegram.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()  # carga variables desde .env si existe

VERSION = "1.7.4"
VERSION_DATE = "2026-02-10"
VERSION_NOTES = [
    "✅ Detección de surebets en apuestas activas",
    "✅ Monitor automático con doble intervalo (órdenes / trades)",
    "✅ Caché inteligente: órdenes cada Xs, trades cada 60s",
    "✅ Detección automática de rate limit 429",
    "✅ Comandos: /surebets /activas /stats /historial /estado /version",
    "✅ Soporte para múltiples Chat IDs (TELEGRAM_CHAT_ID separados por coma)",
    "✅ Detección de partidos en LIVE con marcador en /activas",
    "✅ /setroi — ROI mínimo configurable desde Telegram",
    "✅ MIN_ROI como variable de entorno (default 1%)",
    "✅ Fix: /activas ahora detecta correctamente partidos en LIVE",
    "✅ SUREBET CONSEGUIDA: detecta ambas patas y deja de notificar",
    "✅ Estadísticas incluyen surebets cerradas y ROI medio",
    "✅ Recomendación de stake y cuota mínima en /activas para cada apuesta",
    "✅ Fix: detección LIVE basada solo en gameTime, no en liveEnabled",
    "✅ Fix: wallet address sin .lower() — SX.bet requiere checksum format",
]

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.constants import ParseMode

from sxbet import SXBetClient, find_surebets, get_stats, get_stats_with_markets, detect_closed_surebets

# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  CONFIG  (lee variables de entorno o .env)
# ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
# Acepta uno o varios Chat IDs separados por coma: "123456,789012"
ALLOWED_CHATS = set(int(x.strip()) for x in os.environ["TELEGRAM_CHAT_ID"].split(","))
SX_API_KEY       = os.environ["SX_API_KEY"]
SX_WALLET        = os.environ["SX_WALLET"]

# Intervalos de escaneo independientes:
# - ORDERS_INTERVAL: cada cuántos segundos pide las cuotas live (puede ser 5s)
# - TRADES_INTERVAL: cada cuántos segundos refresca trades y mercados (más pesado)
ORDERS_INTERVAL = int(os.getenv("ORDERS_INTERVAL", "10"))   # default 10s
TRADES_INTERVAL = int(os.getenv("TRADES_INTERVAL", "60"))   # default 60s
MIN_ROI         = float(os.getenv("MIN_ROI", "1.0").replace(",", "."))  # % ROI mínimo para alertar

client = SXBetClient(api_key=SX_API_KEY, wallet=SX_WALLET)

# ── Cache en memoria ──────────────────────────────────────────
_cache = {
    "groups":   [],      # apuestas activas agrupadas
    "markets":  {},      # datos de mercado
    "last_trades_fetch": 0,
}

# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────

def auth(update: Update) -> bool:
    """Rechaza cualquier chat que no esté en la lista."""
    return update.effective_chat.id in ALLOWED_CHATS

def fmt_ts(ts) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(ts)

def emoji_result(result: str) -> str:
    return {"GANADA": "✅", "PERDIDA": "❌", "VOID": "↩️"}.get(result, "⏳")

def roi_emoji(roi: float) -> str:
    if roi > 3:   return "🔥"
    if roi > 0:   return "✅"
    if roi > -5:  return "⚠️"
    return "❌"

# ─────────────────────────────────────────────────────────────
#  COMANDOS
# ─────────────────────────────────────────────────────────────

async def cmd_setroi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cambia el ROI mínimo en caliente sin tocar Railway."""
    if not auth(update): return
    global MIN_ROI
    args = ctx.args
    if not args:
        await update.message.reply_text(
            f"📊 ROI mínimo actual: *`{MIN_ROI:.1f}%`*\n\n"
            "Para cambiarlo: `/setroi 3\\.5`",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    try:
        new_val = float(args[0].replace(",", "."))
        if new_val < 0 or new_val > 50:
            raise ValueError
        MIN_ROI = new_val
        await update.message.reply_text(
            f"✅ ROI mínimo actualizado a *`{MIN_ROI:.1f}%`*\n"
            f"Solo recibirás alertas de surebets con ROI ≥ `{MIN_ROI:.1f}%`",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Valor inválido\\. Usa por ejemplo: `/setroi 2\\.5`", parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_version(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    notes = "\n".join(f"  {_escape(n)}" for n in VERSION_NOTES)
    text = (
        "🤖 *SX\\.bet Surebet Bot*\n\n"
        f"Versi\u00f3n: `{VERSION}`\n"
        f"Fecha: `{VERSION_DATE}`\n\n"
        "*Cambios incluidos:*\n"
        + notes
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    text = (
        "👋 *SX\\.bet Surebet Monitor*\n\n"
        "Comandos disponibles:\n"
        "🔍 /surebets — Buscar surebets ahora\n"
        "📋 /activas — Apuestas activas\n"
        "📊 /stats — Estadísticas generales\n"
        "📅 /historial — Últimas 20 apuestas\n"
        "🔔 /monitor\\_on — Activar alertas automáticas\n"
        "🔕 /monitor\\_off — Desactivar alertas\n"
        "ℹ️ /estado — Estado del monitor\n"
        "🔢 /version — Versión del bot\n"
        "📊 /setroi — Ver o cambiar ROI mínimo\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_surebets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    msg = await update.message.reply_text("🔍 Escaneando apuestas…")
    try:
        result = await asyncio.to_thread(_scan_surebets)
        await msg.edit_text(result, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def cmd_activas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    msg = await update.message.reply_text("⏳ Cargando apuestas activas…")
    try:
        result = await asyncio.to_thread(_get_activas)
        await msg.edit_text(result, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    msg = await update.message.reply_text("📊 Calculando estadísticas…")
    try:
        result = await asyncio.to_thread(_get_stats)
        await msg.edit_text(result, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def cmd_historial(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    msg = await update.message.reply_text("📅 Cargando historial…")
    try:
        result = await asyncio.to_thread(_get_historial)
        await msg.edit_text(result, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def cmd_monitor_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    jobs = ctx.job_queue.get_jobs_by_name("surebet_monitor")
    if jobs:
        await update.message.reply_text("✅ El monitor ya está activo.")
        return
    ctx.job_queue.run_repeating(
        _monitor_job,
        interval=ORDERS_INTERVAL,
        first=5,
        name="surebet_monitor",
        chat_id=next(iter(ALLOWED_CHATS))  # job usa el primero; notifica a todos abajo
    )
    # Pre-cargar trades en segundo plano
    await asyncio.to_thread(_refresh_trades_cache)
    await update.message.reply_text(
        f"🔔 Monitor activado\\. Órdenes cada *{ORDERS_INTERVAL}s*, trades cada *{TRADES_INTERVAL}s*",
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def cmd_monitor_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    jobs = ctx.job_queue.get_jobs_by_name("surebet_monitor")
    if not jobs:
        await update.message.reply_text("ℹ️ El monitor no estaba activo.")
        return
    for job in jobs:
        job.schedule_removal()
    await update.message.reply_text("🔕 Monitor desactivado.")


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    jobs = ctx.job_queue.get_jobs_by_name("surebet_monitor")
    activo = bool(jobs)
    last   = ctx.bot_data.get("last_scan", "Nunca")
    found  = ctx.bot_data.get("last_surebets_found", 0)
    scans  = ctx.bot_data.get("total_scans", 0)
    text = (
        f"*Estado del Monitor*\n\n"
        f"{'🟢 Activo' if activo else '🔴 Inactivo'}\n"
        f"Intervalo órdenes: `{ORDERS_INTERVAL}s` | Trades: `{TRADES_INTERVAL}s`\n"
        f"ROI mínimo: `{MIN_ROI:.1f}%`\n"
        f"Último escaneo: `{last}`\n"
        f"Surebets encontradas: `{found}`\n"
        f"Escaneos totales: `{scans}`\n"
    )
    await update.message.reply_text(
        _escape(text), parse_mode=ParseMode.MARKDOWN_V2
    )

# ─────────────────────────────────────────────────────────────
#  JOB PERIÓDICO
# ─────────────────────────────────────────────────────────────

# Guardar los hashes de surebets ya notificadas para no repetir
_notified: set = set()

async def _monitor_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Se ejecuta cada CHECK_INTERVAL segundos."""
    try:
        surebets = await asyncio.to_thread(_fetch_surebets_raw)

        ctx.bot_data["last_scan"]          = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        ctx.bot_data["total_scans"]        = ctx.bot_data.get("total_scans", 0) + 1
        ctx.bot_data["last_surebets_found"] = len(surebets)

        for sb in surebets:
            key = sb["stable_key"]
            if key in _notified:
                continue  # ya avisamos de esta
            _notified.add(key)

            text = _format_surebet_alert(sb)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Ver todas", callback_data="cmd_surebets"),
                InlineKeyboardButton("📋 Activas",   callback_data="cmd_activas"),
            ]])
            for _cid in ALLOWED_CHATS:
              await ctx.bot.send_message(
                chat_id=_cid,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard
              )

        # Limpiar notificaciones de surebets que ya no existen
        current_keys = {sb["stable_key"] for sb in surebets}
        _notified.intersection_update(current_keys)

    except Exception as e:
        log.error(f"Error en monitor job: {e}")


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones inline."""
    if not auth(update): return
    query = update.callback_query
    await query.answer()

    if query.data == "cmd_surebets":
        result = await asyncio.to_thread(_scan_surebets)
        await query.message.reply_text(result, parse_mode=ParseMode.MARKDOWN_V2)
    elif query.data == "cmd_activas":
        result = await asyncio.to_thread(_get_activas)
        await query.message.reply_text(result, parse_mode=ParseMode.MARKDOWN_V2)

# ─────────────────────────────────────────────────────────────
#  FUNCIONES SÍNCRONAS (se ejecutan en thread pool)
# ─────────────────────────────────────────────────────────────

def _refresh_trades_cache():
    """Actualiza el caché de trades y mercados (operación pesada, hacerla poco frecuente)."""
    import time as _time
    now = _time.time()
    if now - _cache["last_trades_fetch"] < TRADES_INTERVAL:
        return  # aún en caché
    trades = client.fetch_all_trades(settled=False)
    if trades is None:
        return  # error de red, mantener caché anterior
    groups  = client.group_trades(trades)
    hashes  = list({g["market_hash"] for g in groups})
    markets = client.fetch_markets(hashes)
    _cache["groups"]  = groups
    _cache["markets"] = markets
    _cache["last_trades_fetch"] = now
    log.info(f"Cache trades actualizado: {len(groups)} apuestas activas")


def _fetch_surebets_raw() -> list:
    """
    Operación rápida: usa el caché de trades/mercados
    y solo pide las órdenes (cambian cada segundo).
    """
    _refresh_trades_cache()
    groups  = _cache["groups"]
    markets = _cache["markets"]
    if not groups:
        return []
    hashes = list({g["market_hash"] for g in groups})
    orders = client.fetch_orders(hashes)  # única llamada "live"
    closed = detect_closed_surebets(groups)
    closed_hashes = set(closed.keys())
    return [
        sb for sb in find_surebets(groups, markets, orders, min_roi=MIN_ROI)
        if sb["market_hash"] not in closed_hashes
    ]


def _scan_surebets() -> str:
    surebets = _fetch_surebets_raw()
    if not surebets:
        return "🔍 *Escaneo completado*\n\nNo hay surebets disponibles ahora mismo\\."

    lines = [f"🎯 *{len(surebets)} SUREBET{'S' if len(surebets)>1 else ''} DISPONIBLE{'S' if len(surebets)>1 else ''}*\n"]
    for sb in surebets:
        # Indicar si el partido está en live
        mkt = _cache["markets"].get(sb.get("market_hash", ""), {})
        from time import time as _now2
        game_time = mkt.get("gameTime", 0) or 0
        is_live = (0 < game_time <= _now2())
        sb["is_live"] = is_live
        lines.append(_format_surebet_alert(sb, compact=True))
    return "\n".join(lines)


def _hedge_recommendation(stake_a: float, odds_a: float, min_roi: float) -> dict:
    """
    Calcula el stake y cuota mínima necesaria en el lado contrario
    para conseguir exactamente min_roi% de beneficio garantizado.
    """
    potential_a = stake_a * odds_a
    r = min_roi / 100.0
    # Stake óptimo de cobertura (iguala los dos escenarios)
    stake_b = (potential_a - stake_a * (1 + r)) / (1 + r)
    if stake_b <= 0:
        return None  # cuota demasiado baja para cubrir con beneficio
    odds_b_min  = potential_a / stake_b
    total_stake = stake_a + stake_b
    guaranteed  = potential_a - stake_a - stake_b
    roi         = guaranteed / total_stake * 100
    return {
        "stake_b":      stake_b,
        "odds_b_min":   odds_b_min,
        "total_stake":  total_stake,
        "guaranteed":   guaranteed,
        "roi":          roi,
    }


def _get_activas() -> str:
    trades  = client.fetch_all_trades(settled=False)
    if not trades:
        return "📋 No tienes apuestas activas\\."

    groups  = client.group_trades(trades)
    hashes  = list({g["market_hash"] for g in groups})
    markets = client.fetch_markets(hashes)
    closed  = detect_closed_surebets(groups)  # mercados con ambas patas

    import time as _t
    now = _t.time()

    surebet_bets = []
    live_bets    = []
    pending_bets = []

    for g in groups:
        mkt       = markets.get(g["market_hash"], {})
        game_time = mkt.get("gameTime", 0) or 0
        score1    = mkt.get("teamOneScore")
        score2    = mkt.get("teamTwoScore")
        is_live   = (0 < game_time <= now)

        evento = f"{mkt.get('teamOneName','?')} vs {mkt.get('teamTwoName','?')}"
        side   = mkt.get("outcomeOneName","O1") if g["betting_outcome_one"] else mkt.get("outcomeTwoName","O2")
        sport  = mkt.get("sportLabel", "?")
        league = mkt.get("leagueLabel", "?")

        score_str = ""
        if is_live and score1 is not None and score2 is not None:
            score_str = f" \\(`{score1}\\-{score2}`\\)"

        entry = {
            "sport": sport, "league": league, "evento": evento,
            "side": side, "g": g, "is_live": is_live,
            "score_str": score_str, "game_time": game_time,
            "market_hash": g["market_hash"],
        }

        if g["market_hash"] in closed:
            # Solo añadir una vez por mercado (la pata 1)
            if g["betting_outcome_one"]:
                sb_info = closed[g["market_hash"]]
                entry["sb_info"] = sb_info
                surebet_bets.append(entry)
        elif is_live:
            live_bets.append(entry)
        else:
            pending_bets.append(entry)

    total = len(groups)
    lines = [f"📋 *{total} apuesta{'s' if total>1 else ''} activa{'s' if total>1 else ''}*\n"]

    if surebet_bets:
        lines.append(f"✅ *SUREBET CONSEGUIDA \\({len(surebet_bets)}\\)*")
        for e in surebet_bets:
            sb  = e["sb_info"]
            mkt = markets.get(e["market_hash"], {})
            side1_name = mkt.get("outcomeOneName", "O1")
            side2_name = mkt.get("outcomeTwoName", "O2")
            leg1 = sb["leg1"]
            leg2 = sb["leg2"]
            live_str = " 🔴 LIVE" + e["score_str"] if e["is_live"] else ""
            lines.append(
                f"✅ *{_escape(e['evento'])}*{_escape(live_str)}\n"
                f"   {_escape(e['sport'])} — {_escape(e['league'])}\n"
                f"   Pata 1: {_escape(side1_name)} @ `{leg1['avg_odds']:.3f}` — `{leg1['total_stake']:.2f}` USDC\n"
                f"   Pata 2: {_escape(side2_name)} @ `{leg2['avg_odds']:.3f}` — `{leg2['total_stake']:.2f}` USDC\n"
                f"   💰 Beneficio garantizado: *`{sb['guaranteed']:+.2f}` USDC* \\(`{sb['roi']:+.1f}%`\\)"
            )

    if live_bets:
        if surebet_bets: lines.append("")
        lines.append(f"🔴 *EN LIVE \\({len(live_bets)}\\)*")
        for e in sorted(live_bets, key=lambda x: x["game_time"]):
            g   = e["g"]
            rec = _hedge_recommendation(g["total_stake"], g["avg_odds"], MIN_ROI)
            mkt = markets.get(e["market_hash"], {})
            opp_side = mkt.get("outcomeTwoName","O2") if g["betting_outcome_one"] else mkt.get("outcomeOneName","O1")
            if rec:
                rec_line = (
                    f"   📌 Para ROI≥`{MIN_ROI:.0f}%`: apostar `{rec['stake_b']:.2f}` USDC"
                    f" a *{_escape(opp_side)}* @ cuota≥`{rec['odds_b_min']:.3f}`"
                    f" → `{rec['guaranteed']:+.2f}` USDC garantizados"
                )
            else:
                rec_line = f"   📌 Cuota insuficiente para cubrir con ROI≥`{MIN_ROI:.0f}%`"
            lines.append(
                f"🔴 *{_escape(e['evento'])}*{e['score_str']}\n"
                f"   {_escape(e['sport'])} — {_escape(e['league'])}\n"
                f"   {_escape(e['side'])} @ `{g['avg_odds']:.3f}` — Stake: `{g['total_stake']:.2f}` USDC\n"
                f"   Retorno potencial: `{g['potential_win']:.2f}` USDC\n"
                f"{rec_line}"
            )

    if pending_bets:
        if surebet_bets or live_bets: lines.append("")
        lines.append(f"⏳ *PENDIENTES \\({len(pending_bets)}\\)*")
        for e in sorted(pending_bets, key=lambda x: x["game_time"]):
            g   = e["g"]
            gt  = e["game_time"]
            rec = _hedge_recommendation(g["total_stake"], g["avg_odds"], MIN_ROI)
            mkt = markets.get(e["market_hash"], {})
            opp_side = mkt.get("outcomeTwoName","O2") if g["betting_outcome_one"] else mkt.get("outcomeOneName","O1")
            from datetime import datetime, timezone as tz
            fecha = datetime.fromtimestamp(gt, tz=tz.utc).strftime("%d/%m %H:%M") if gt else "?"
            if rec:
                rec_line = (
                    f"   📌 Para ROI≥`{MIN_ROI:.0f}%`: apostar `{rec['stake_b']:.2f}` USDC"
                    f" a *{_escape(opp_side)}* @ cuota≥`{rec['odds_b_min']:.3f}`"
                    f" → `{rec['guaranteed']:+.2f}` USDC garantizados"
                )
            else:
                rec_line = f"   📌 Cuota insuficiente para cubrir con ROI≥`{MIN_ROI:.0f}%`"
            lines.append(
                f"⏳ {_escape(e['evento'])}\n"
                f"   {_escape(e['sport'])} — {_escape(e['league'])}\n"
                f"   {_escape(e['side'])} @ `{g['avg_odds']:.3f}` — Stake: `{g['total_stake']:.2f}` USDC\n"
                f"   Partido: `{_escape(fecha)} UTC`\n"
                f"{rec_line}"
            )

    return "\n".join(lines)

def _get_stats() -> str:
    trades = client.fetch_all_trades()
    if not trades:
        return "📊 Sin historial de apuestas\\."
    groups  = client.group_trades(trades)
    hashes  = list({g["market_hash"] for g in groups})
    markets = client.fetch_markets(hashes)
    s = get_stats_with_markets(groups, markets)

    lines = [
        "📊 *Estadísticas Generales*\n",
        f"Total apuestas: `{s['total']}`",
        f"Activas: `{s['active']}`   Liquidadas: `{s['settled']}`",
        f"✅ Surebets conseguidas: `{s['surebets_closed']}`" + (f"  ROI medio: `{s['surebets_roi_avg']:+.1f}%`" if s['surebets_closed'] > 0 else ""),
        f"Ganadas: `{s['won']}` ✅   Perdidas: `{s['lost']}` ❌   Void: `{s['void']}` ↩️",
        f"% Acierto: `{s['win_rate']:.1f}%`",
        f"Stake total: `{s['total_stake']:.2f}` USDC",
        f"P&L neto: `{s['pnl']:+.2f}` USDC  {'📈' if s['pnl']>=0 else '📉'}",
        f"ROI: `{s['roi']:+.2f}%`",
        "",
        "*Por deporte:*",
    ]
    for sport, ss in sorted(s["by_sport"].items(), key=lambda x: -x[1]["pnl"]):
        roi_e = roi_emoji(ss["roi"])
        lines.append(
            f"{roi_e} {_escape(sport)}: `{ss['won']}W/{ss['lost']}L` "
            f"P&L: `{ss['pnl']:+.2f}` USDC"
        )
    return "\n".join(lines)


def _get_historial() -> str:
    trades = client.fetch_all_trades(settled=True)
    if not trades:
        return "📅 Sin historial\\."
    groups = client.group_trades(trades)
    hashes = list({g["market_hash"] for g in groups})
    markets = client.fetch_markets(hashes)

    settled = [g for g in groups if g["settled"]]
    settled.sort(key=lambda x: x.get("settle_date") or x.get("bet_time", 0), reverse=True)
    recent = settled[:20]

    lines = [f"📅 *Últimas {len(recent)} apuestas liquidadas*\n"]
    for g in recent:
        mkt    = markets.get(g["market_hash"], {})
        evento = f"{mkt.get('teamOneName','?')} vs {mkt.get('teamTwoName','?')}"
        side   = mkt.get("outcomeOneName","O1") if g["betting_outcome_one"] else mkt.get("outcomeTwoName","O2")
        res    = g.get("result", "-")
        pnl    = g["potential_win"] - g["total_stake"] if res == "GANADA" else -g["total_stake"] if res == "PERDIDA" else 0.0
        em     = emoji_result(res)
        lines.append(
            f"{em} {_escape(evento)}\n"
            f"   {_escape(side)} @ `{g['avg_odds']:.3f}` — `{pnl:+.2f}` USDC\n"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
#  FORMATEO
# ─────────────────────────────────────────────────────────────

_ESC = str.maketrans({
    "_": r"\_", "*": r"\*", "[": r"\[", "]": r"\]",
    "(": r"\(", ")": r"\)", "~": r"\~", "`": r"\`",
    ">": r"\>", "#": r"\#", "+": r"\+", "-": r"\-",
    "=": r"\=", "|": r"\|", "{": r"\{", "}": r"\}",
    ".": r"\.", "!": r"\!"
})

def _escape(s: str) -> str:
    return str(s).translate(_ESC)


def _format_surebet_alert(sb: dict, compact: bool = False) -> str:
    roi    = sb["roi"]
    profit = sb["guaranteed_profit"]
    event  = sb["event"]
    side   = sb["side"]
    stake  = sb["total_stake"]
    hedge  = sb["hedge_stake"]
    odd_orig = sb["avg_odds"]
    odd_opp  = sb["live_opp_odds"]
    sport  = sb["sport"]
    league = sb["league"]
    roi_e  = roi_emoji(roi)

    is_live   = sb.get("is_live", False)
    live_badge = "🔴 LIVE — " if is_live else ""

    if compact:
        return (
            f"{roi_e} {live_badge}*{_escape(event)}*\n"
            f"   {_escape(side)} @ `{odd_orig:.3f}` → cubrir `{hedge:.2f}` USDC @ `{odd_opp:.3f}`\n"
            f"   💰 Beneficio garantizado: *`{profit:+.2f}` USDC* \\({roi:+.1f}%\\)\n"
        )

    live_line = "🔴 *PARTIDO EN LIVE*\n" if is_live else ""
    return (
        f"🎯 *¡SUREBET DETECTADA\\!*\n\n"
        f"{live_line}"
        f"🏆 {_escape(sport)} — {_escape(league)}\n"
        f"⚽ {_escape(event)}\n\n"
        f"📌 *Tu apuesta:* {_escape(side)}\n"
        f"   Stake: `{stake:.2f}` USDC @ cuota `{odd_orig:.3f}`\n"
        f"   Retorno potencial: `{stake*odd_orig:.2f}` USDC\n\n"
        f"🔄 *Apuesta de cobertura \\(lado contrario\\):*\n"
        f"   Stake: `{hedge:.2f}` USDC @ cuota `{odd_opp:.3f}`\n\n"
        f"💰 *Beneficio garantizado: `{profit:+.2f}` USDC*\n"
        f"📈 ROI: `{roi:+.2f}%`\n\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
    )


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

async def cmd_debugtrades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Muestra respuesta cruda de la API de trades para diagnosticar el problema."""
    if not auth(update): return
    msg = await update.message.reply_text("🔍 Consultando API directamente...")
    try:
        import requests as _req
        wallet = client.wallet
        url = "https://api.sx.bet/trades?bettor=" + wallet + "&settled=false&pageSize=5"
        headers = {"X-Api-Key": client.api_key} if client.api_key else {}
        r = _req.get(url, headers=headers, timeout=10)
        data = r.json()
        trades = data.get("data", {}).get("trades", [])
        api_status = data.get("status", "?")
        lines = [
            "🔍 *Debug API trades*",
            "",
            "Wallet: `" + _escape(wallet[:16]) + "\\.\\.\\.\`",
            "HTTP: `" + str(r.status_code) + "`",
            "API status: `" + _escape(api_status) + "`",
            "Trades activos: `" + str(len(trades)) + "`",
        ]
        if not trades:
            lines.append("")
            lines.append("⚠️ API devuelve 0 trades")
            lines.append("Verifica `SX\\_WALLET` en Railway")
            lines.append("Debe tener mayúsculas/minúsculas exactas")
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await msg.edit_text("❌ Error: " + _escape(str(e)), parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_debugwallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Muestra la wallet que usa el bot para verificar que coincide con SX.bet."""
    if not auth(update): return
    wallet = client.wallet
    short  = f"{wallet[:8]}...{wallet[-6:]}" if len(wallet) > 14 else wallet
    await update.message.reply_text(
        f"🔑 *Wallet configurada en el bot:*\n`{_escape(wallet)}`\n\n"
        "Verifica que coincide exactamente con la que ves en SX\\.bet → Account → Overview\n"
        "\\(incluyendo mayúsculas y minúsculas\\)",
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def cmd_debuglive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Muestra los campos live de todos los mercados activos para diagnóstico."""
    if not auth(update): return
    msg = await update.message.reply_text("🔍 Inspeccionando mercados...")
    try:
        trades  = client.fetch_all_trades(settled=False)
        groups  = client.group_trades(trades)
        hashes  = list({g["market_hash"] for g in groups})
        markets = client.fetch_markets(hashes)
        import time as _t
        now = _t.time()
        lines = ["🔍 *Debug campos LIVE*\n"]
        for mh, mkt in markets.items():
            gt      = mkt.get("gameTime", 0) or 0
            s1      = mkt.get("teamOneScore", "?")
            s2      = mkt.get("teamTwoScore", "?")
            is_live = (0 < gt <= now)
            evento  = f"{mkt.get('teamOneName','?')} vs {mkt.get('teamTwoName','?')}"
            from datetime import datetime, timezone as tz
            gt_str  = datetime.fromtimestamp(gt, tz=tz.utc).strftime("%d/%m %H:%M") if gt else "0"
            lines.append(
                f"{'🔴' if is_live else '⏳'} {_escape(evento)}\n"
                f"   gameTime: `{_escape(gt_str)}` score: `{s1}-{s2}`"
            )
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {_escape(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("surebets",    cmd_surebets))
    app.add_handler(CommandHandler("activas",     cmd_activas))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("historial",   cmd_historial))
    app.add_handler(CommandHandler("monitor_on",  cmd_monitor_on))
    app.add_handler(CommandHandler("monitor_off", cmd_monitor_off))
    app.add_handler(CommandHandler("estado",      cmd_estado))
    app.add_handler(CommandHandler("version",     cmd_version))
    app.add_handler(CommandHandler("setroi",      cmd_setroi))
    app.add_handler(CommandHandler("debuglive",   cmd_debuglive))
    app.add_handler(CommandHandler("debugwallet",  cmd_debugwallet))
    app.add_handler(CommandHandler("debugtrades",  cmd_debugtrades))
    app.add_handler(CallbackQueryHandler(callback_handler))

    log.info("Bot iniciado ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
