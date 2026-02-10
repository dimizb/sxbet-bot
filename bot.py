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

VERSION = "1.3.0"
VERSION_DATE = "2026-02-10"
VERSION_NOTES = [
    "✅ Detección de surebets en apuestas activas",
    "✅ Monitor automático con doble intervalo (órdenes / trades)",
    "✅ Caché inteligente: órdenes cada Xs, trades cada 60s",
    "✅ Detección automática de rate limit 429",
    "✅ Comandos: /surebets /activas /stats /historial /estado /version",
]

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.constants import ParseMode

from sxbet import SXBetClient, find_surebets, get_stats, get_stats_with_markets

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
ALLOWED_CHAT     = int(os.environ["TELEGRAM_CHAT_ID"])
SX_API_KEY       = os.environ["SX_API_KEY"]
SX_WALLET        = os.environ["SX_WALLET"]

# Intervalos de escaneo independientes:
# - ORDERS_INTERVAL: cada cuántos segundos pide las cuotas live (puede ser 5s)
# - TRADES_INTERVAL: cada cuántos segundos refresca trades y mercados (más pesado)
ORDERS_INTERVAL = int(os.getenv("ORDERS_INTERVAL", "10"))   # default 10s
TRADES_INTERVAL = int(os.getenv("TRADES_INTERVAL", "60"))   # default 60s

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
    """Rechaza cualquier chat que no sea el tuyo."""
    return update.effective_chat.id == ALLOWED_CHAT

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
        chat_id=ALLOWED_CHAT
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
            await ctx.bot.send_message(
                chat_id=ALLOWED_CHAT,
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
    return find_surebets(groups, markets, orders)


def _scan_surebets() -> str:
    surebets = _fetch_surebets_raw()
    if not surebets:
        return "🔍 *Escaneo completado*\n\nNo hay surebets disponibles ahora mismo\\."

    lines = [f"🎯 *{len(surebets)} SUREBET{'S' if len(surebets)>1 else ''} DISPONIBLE{'S' if len(surebets)>1 else ''}*\n"]
    for i, sb in enumerate(surebets, 1):
        lines.append(_format_surebet_alert(sb, compact=True))
    return "\n".join(lines)


def _get_activas() -> str:
    trades  = client.fetch_all_trades(settled=False)
    if not trades:
        return "📋 No tienes apuestas activas\\."

    groups  = client.group_trades(trades)
    hashes  = list({g["market_hash"] for g in groups})
    markets = client.fetch_markets(hashes)

    lines = [f"📋 *{len(groups)} apuesta{'s' if len(groups)>1 else ''} activa{'s' if len(groups)>1 else ''}*\n"]
    for g in sorted(groups, key=lambda x: x.get("bet_time", 0), reverse=True):
        mkt    = markets.get(g["market_hash"], {})
        evento = f"{mkt.get('teamOneName','?')} vs {mkt.get('teamTwoName','?')}"
        side   = mkt.get("outcomeOneName","O1") if g["betting_outcome_one"] else mkt.get("outcomeTwoName","O2")
        sport  = mkt.get("sportLabel", "?")
        lines.append(
            f"• {_escape(sport)} \\| {_escape(evento)}\n"
            f"  {_escape(side)} @ `{g['avg_odds']:.3f}` — Stake: `{g['total_stake']:.2f}` USDC\n"
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

    if compact:
        return (
            f"{roi_e} *{_escape(event)}*\n"
            f"   {_escape(side)} @ `{odd_orig:.3f}` → cubrir `{hedge:.2f}` USDC @ `{odd_opp:.3f}`\n"
            f"   💰 Beneficio garantizado: *`{profit:+.2f}` USDC* \\({roi:+.1f}%\\)\n"
        )

    return (
        f"🎯 *¡SUREBET DETECTADA\\!*\n\n"
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
    app.add_handler(CallbackQueryHandler(callback_handler))

    log.info("Bot iniciado ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
