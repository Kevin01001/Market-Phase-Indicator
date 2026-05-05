"""Trading Journal Telegram Bot — @Forexrama_bot"""
import os
import re
import csv
import io
import sys
import time
import asyncio
import sqlite3
import logging
import fcntl
from datetime import datetime
from collections import Counter

import json
import secrets as _secrets

import httpx

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DB_PATH       = os.path.join(os.path.dirname(__file__), "data", "journal.db")
PID_FILE      = os.path.join(os.path.dirname(__file__), "data", "bot.pid")
MT5_TOKENS    = os.path.join(os.path.dirname(__file__), "data", "mt5_tokens.json")
MT5_PENDING   = os.path.join(os.path.dirname(__file__), "data", "mt5_pending.json")

SHOULD_RESTART = True

COPPIA, TIPO, ENTRY, SL, TP, MOTIVO, PSICOLOGIA, PNL, DELETE_CONFIRM, EDIT_SELECT, EDIT_VALUE = range(11)
MT5_STRAT, MT5_PSYCH, MT5_NOTES = 11, 12, 13

STRATEGIES = [
    "Breakout",
    "Pullback",
    "Trend following",
    "Scalping",
    "Intraday",
    "Swing",
]

PSYCHOLOGY_OPTIONS = [
    ("😰", "Ansioso", 3),
    ("😐", "Neutrale", 5),
    ("😎", "Fiducioso", 7),
    ("🤩", "Entusiasta", 9),
    ("😡", "Arrabbiato", 2),
]

# ─── DB ───────────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            coppia TEXT,
            tipo TEXT,
            entry REAL,
            sl REAL,
            tp REAL,
            motivo TEXT,
            psicologia TEXT,
            pnl REAL,
            source TEXT DEFAULT 'bot',
            ticket TEXT,
            close_price REAL,
            open_price REAL,
            volume REAL,
            swap REAL,
            commission REAL,
            close_time TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_user ON trades(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_coppia ON trades(coppia)")
    # Migrate older DBs that lack new columns
    for col, definition in [
        ("source",     "TEXT DEFAULT 'bot'"),
        ("ticket",     "TEXT"),
        ("close_price","REAL"),
        ("open_price", "REAL"),
        ("volume",     "REAL"),
        ("swap",       "REAL"),
        ("commission", "REAL"),
        ("close_time", "TEXT"),
    ]:
        try:
            c.execute(f"ALTER TABLE trades ADD COLUMN {col} {definition}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def save_trade(user_id, username, data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO trades
        (user_id, username, coppia, tipo, entry, sl, tp, motivo, psicologia, pnl,
         source, ticket, open_price, close_price, volume, swap, commission, close_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            username,
            data.get("coppia"),
            data.get("tipo"),
            data.get("entry"),
            data.get("sl"),
            data.get("tp"),
            data.get("motivo"),
            data.get("psicologia"),
            data.get("pnl"),
            data.get("source", "bot"),
            data.get("ticket"),
            data.get("open_price"),
            data.get("close_price"),
            data.get("volume"),
            data.get("swap"),
            data.get("commission"),
            data.get("close_time"),
        ),
    )
    conn.commit()
    conn.close()


def fetch_trades(user_id, limit=50):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT * FROM trades WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def fetch_last_trade(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT * FROM trades WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    row = c.fetchone()
    conn.close()
    return row


def fetch_leaderboard(limit=5):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """SELECT username, SUM(pnl) as total_pnl
        FROM trades
        GROUP BY user_id
        ORDER BY total_pnl DESC
        LIMIT ?""",
        (limit,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def fetch_trades_by_pair(coppia):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT * FROM trades WHERE coppia = ? ORDER BY created_at DESC",
        (coppia.upper(),),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def delete_trade(trade_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()


def update_trade_field(trade_id, field, value):
    allowed = {"coppia", "tipo", "entry", "sl", "tp", "motivo", "psicologia", "pnl"}
    if field not in allowed:
        raise ValueError(f"Campo non valido: {field}")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE trades SET {field} = ? WHERE id = ?", (value, trade_id))
    conn.commit()
    conn.close()


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def _parse_price(text):
    text = text.replace(",", ".")
    m = re.search(r"(\d+\.?\d*)", text)
    if not m:
        return None
    return float(m.group(1))


def _parse_pnl(text):
    text = text.replace(",", ".")
    m = re.search(r"(-?\d+\.?\d*)", text)
    if not m:
        return None
    return float(m.group(1))


def _build_stats(rows):
    if not rows:
        return None
    pnls = [r[10] for r in rows if r[10] is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = len(pnls)
    if total == 0:
        return None
    win_rate = len(wins) / total * 100
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")
    total_pnl = sum(pnls)
    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "total_pnl": total_pnl,
    }


def _format_stats(stats):
    pf = f"{stats['profit_factor']:.2f}" if stats['profit_factor'] != float("inf") else "∞"
    sign = "+" if stats['total_pnl'] >= 0 else ""
    return (
        f"📊 <b>Le tue Statistiche</b>\n\n"
        f"📈 Trades totali: <b>{stats['total']}</b>\n"
        f"✅ Win: {stats['wins']}  ❌ Loss: {stats['losses']}\n"
        f"🎯 Win Rate: <b>{stats['win_rate']:.1f}%</b>\n"
        f"💚 Avg Win: {stats['avg_win']:.2f}\n"
        f"❤️ Avg Loss: {stats['avg_loss']:.2f}\n"
        f"⚡ Profit Factor: <b>{pf}</b>\n"
        f"💰 P&L Totale: <b>{sign}{stats['total_pnl']:.2f}</b>"
    )


def _format_mood_block(rows):
    if not rows:
        return ""
    moods = [r[9] for r in rows if r[9]]
    if not moods:
        return ""
    counter = Counter(moods)
    lines = ["\n🧠 <b>Psicologia prevalente</b>"]
    for mood, count in counter.most_common():
        lines.append(f"  {mood} × {count}")
    return "\n".join(lines)


def _format_summary(data, include_psychology=True):
    lines = [
        f"📋 <b>Trade Registrato</b>",
        f"Coppia: <b>{data.get('coppia', 'N/A')}</b>",
        f"Strategia: {data.get('tipo', 'N/A')}",
        f"Entry: {data.get('entry', 'N/A')}",
        f"SL: {data.get('sl', 'N/A')}",
        f"TP: {data.get('tp', 'N/A')}",
        f"Motivo: {data.get('motivo', 'N/A')}",
    ]
    if include_psychology:
        lines.append(f"Psicologia: {data.get('psicologia', 'N/A')}")
    pnl = data.get('pnl', 0)
    emoji = "💚" if pnl > 0 else ("❤️" if pnl < 0 else "⚪")
    lines.append(f"P&L: {emoji} <b>{pnl}</b>")
    return "\n".join(lines)


def _format_summary_plain(row):
    pnl = row[10]
    emoji = "💚" if pnl and pnl > 0 else ("❤️" if pnl and pnl < 0 else "⚪")
    dt = row[11][:16] if row[11] else "—"
    return (
        f"<b>#{row[0]}</b> | {row[3]} {row[4]}\n"
        f"Entry: {row[5]}  SL: {row[6]}  TP: {row[7]}\n"
        f"P&L: {emoji} <b>{pnl}</b>\n"
        f"📅 {dt}"
    )


def _similarity_bar(ratio):
    filled = int(ratio * 10)
    empty = 10 - filled
    return "█" * filled + "░" * empty


def _similarity_emoji(ratio):
    if ratio >= 0.8:
        return "🔥"
    elif ratio >= 0.5:
        return "👀"
    return "🌱"


def _trade_similarity(trade1, trade2):
    e1, sl1, tp1 = trade1[5], trade1[6], trade1[7]
    e2, sl2, tp2 = trade2[5], trade2[6], trade2[7]
    if None in (e1, sl1, tp1, e2, sl2, tp2):
        return 0.0
    diffs = []
    for a, b in ((e1, e2), (sl1, sl2), (tp1, tp2)):
        avg = (abs(a) + abs(b)) / 2
        if avg == 0:
            diffs.append(0.0 if a == b else 1.0)
        else:
            diffs.append(abs(a - b) / avg)
    similarity = max(0.0, 1.0 - sum(diffs) / len(diffs))
    return round(similarity, 2)


def _build_chart(user_id):
    rows = fetch_trades(user_id)
    if not rows:
        return None
    pnls = [r[10] for r in reversed(rows) if r[10] is not None]
    if len(pnls) < 2:
        return None

    cumulative = []
    total = 0
    for p in pnls:
        total += p
        cumulative.append(total)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 7), facecolor="#0f172a")
    for ax in (ax1, ax2):
        ax.set_facecolor("#1e293b")
        ax.tick_params(colors="#94a3b8")
        ax.spines[:].set_color("#334155")

    # P&L per trade
    colors = ["#10b981" if p > 0 else "#ef4444" for p in pnls]
    ax1.bar(range(1, len(pnls) + 1), pnls, color=colors)
    ax1.axhline(0, color="#64748b", linestyle="--", linewidth=0.8)
    ax1.set_title("P&L per Trade", color="#e2e8f0", fontsize=11)
    ax1.set_xlabel("Trade #", color="#94a3b8", fontsize=9)
    ax1.set_ylabel("P&L", color="#94a3b8", fontsize=9)

    # Curva equity
    ax2.plot(range(1, len(cumulative) + 1), cumulative, color="#0ea5e9", linewidth=2, marker="o", markersize=3)
    ax2.fill_between(range(1, len(cumulative) + 1), cumulative, alpha=0.2, color="#0ea5e9")
    ax2.axhline(0, color="#64748b", linestyle="--", linewidth=0.8)
    ax2.set_title("Curva Equity", color="#e2e8f0", fontsize=11)
    ax2.set_xlabel("Trade #", color="#94a3b8", fontsize=9)
    ax2.set_ylabel("P&L Cumulativo", color="#94a3b8", fontsize=9)

    fig.tight_layout(pad=2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf


# ─── CONVERSATION: /journal ───────────────────────────────────────────────────
async def ask_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 <b>Nuovo Trade</b>\n\nInserisci la coppia (es. <code>EURUSD</code>):",
        parse_mode="HTML"
    )
    return COPPIA


async def ask_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["coppia"] = update.message.text.upper().strip()
    keyboard = [[InlineKeyboardButton(s, callback_data=s)] for s in STRATEGIES]
    await update.message.reply_text(
        "🎯 Scegli la strategia:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return TIPO


async def ask_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["tipo"] = query.data
    await query.edit_message_text("💵 Inserisci il prezzo di <b>Entry</b>:", parse_mode="HTML")
    return ENTRY


async def ask_stop_loss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = _parse_price(update.message.text)
    if price is None:
        await update.message.reply_text("⚠️ Prezzo non valido. Riprova:")
        return ENTRY
    context.user_data["entry"] = price
    await update.message.reply_text("🛑 Inserisci lo <b>Stop Loss</b>:", parse_mode="HTML")
    return SL


async def ask_take_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = _parse_price(update.message.text)
    if price is None:
        await update.message.reply_text("⚠️ Prezzo non valido. Riprova:")
        return SL
    context.user_data["sl"] = price
    await update.message.reply_text("🎯 Inserisci il <b>Take Profit</b>:", parse_mode="HTML")
    return TP


async def ask_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = _parse_price(update.message.text)
    if price is None:
        await update.message.reply_text("⚠️ Prezzo non valido. Riprova:")
        return TP
    context.user_data["tp"] = price
    await update.message.reply_text("📝 Inserisci il <b>motivo</b> del trade:", parse_mode="HTML")
    return MOTIVO


async def ask_psychology(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["motivo"] = update.message.text.strip()
    keyboard = [
        [InlineKeyboardButton(f"{emoji} {label}", callback_data=emoji)]
        for emoji, label, _ in PSYCHOLOGY_OPTIONS
    ]
    await update.message.reply_text(
        "🧠 Come ti sentivi durante il trade?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PSICOLOGIA


async def ask_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["psicologia"] = query.data
    await query.edit_message_text("💰 Inserisci il <b>P&L</b> del trade (es. <code>+50</code> o <code>-30</code>):", parse_mode="HTML")
    return PNL


async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pnl = _parse_pnl(update.message.text)
    if pnl is None:
        await update.message.reply_text("⚠️ P&L non valido. Riprova (es. 50 o -25):")
        return PNL
    context.user_data["pnl"] = pnl
    data = context.user_data
    user = update.effective_user
    save_trade(user.id, user.username or user.first_name, data)
    summary = _format_summary(data, include_psychology=True)
    await update.message.reply_text(summary, parse_mode="HTML")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operazione annullata.")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import Conflict
    if isinstance(context.error, Conflict):
        logger.warning("409 Conflict — attendo 35s e riprovo (vecchia connessione ancora attiva)")
        return
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Si è verificato un errore imprevisto. Riprova tra poco."
        )


# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 <b>Benvenuto nel Trading Journal Bot!</b> 🚀\n\n"
        f"Sono il bot ufficiale di <b>@Forexrama_bot</b>.\n\n"
        f"📌 <b>Il tuo Chat ID:</b> <code>{chat_id}</code>\n"
        f"Copialo nelle impostazioni dell'app per ricevere notifiche.\n\n"
        f"📋 Usa /help per vedere tutti i comandi disponibili.",
        parse_mode="HTML"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 <b>Comandi disponibili</b>\n\n"
        "🟢 <b>Trade</b>\n"
        "/journal — Registra un nuovo trade\n"
        "/last — Ultimo trade registrato\n"
        "/delete — Elimina l'ultimo trade\n"
        "/edit — Modifica l'ultimo trade\n"
        "/export — Esporta trades in CSV\n\n"
        "📊 <b>Analisi</b>\n"
        "/stats — Le tue statistiche personali\n"
        "/chart — Grafico P&L e curva equity\n"
        "/leaderboard — Classifica globale P&L\n"
        "/similar &lt;COPPIA&gt; — Trades simili di altri utenti\n\n"
        "⚙️ <b>Altro</b>\n"
        "/chatid — Mostra il tuo Chat ID\n"
        "/status — Stato del bot\n"
        "/cancel — Annulla operazione in corso",
        parse_mode="HTML"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot attivo e funzionante!")


async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"🆔 <b>Il tuo Chat ID:</b>\n\n<code>{chat_id}</code>\n\n"
        f"Copialo nelle <b>Impostazioni → Telegram</b> dell'app per ricevere notifiche automatiche.",
        parse_mode="HTML"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = fetch_trades(update.effective_user.id)
    stats = _build_stats(rows)
    if not stats:
        await update.message.reply_text(
            "📊 Nessun trade trovato.\n\nUsa /journal per registrare il tuo primo trade!"
        )
        return
    text = _format_stats(stats)
    mood = _format_mood_block(rows)
    if mood:
        text += "\n" + mood
    await update.message.reply_text(text, parse_mode="HTML")


async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = fetch_last_trade(update.effective_user.id)
    if not row:
        await update.message.reply_text("📋 Nessun trade trovato.\n\nUsa /journal per registrarne uno!")
        return
    await update.message.reply_text(_format_summary_plain(row), parse_mode="HTML")


async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Generazione grafico in corso...")
    buf = _build_chart(update.effective_user.id)
    if not buf:
        await update.message.reply_text(
            "Non ci sono abbastanza trade per generare il grafico.\n"
            "Registra almeno 2 trade con /journal."
        )
        return
    await update.message.reply_photo(photo=buf, caption="📈 P&L per trade e curva equity")


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = fetch_leaderboard()
    if not rows:
        await update.message.reply_text("🏆 Nessun dato disponibile per la classifica.")
        return
    lines = ["🏆 <b>Leaderboard — Top P&L</b>\n"]
    for i, (username, total) in enumerate(rows, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        sign = "+" if total >= 0 else ""
        lines.append(f"{medal} @{username or 'anonimo'}: <b>{sign}{total:.2f}</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = fetch_trades(update.effective_user.id, limit=500)
    if not rows:
        await update.message.reply_text("📤 Nessun trade da esportare.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "UserID", "Username", "Coppia", "Strategia", "Entry", "SL", "TP", "Motivo", "Psicologia", "PnL", "Data"])
    for r in rows:
        writer.writerow(r)
    buf.seek(0)
    username = update.effective_user.id
    await update.message.reply_document(
        document=io.BytesIO(buf.getvalue().encode("utf-8")),
        filename=f"trades_{username}_{datetime.now().strftime('%Y%m%d')}.csv",
        caption=f"📊 {len(rows)} trade esportati"
    )


async def similar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("ℹ️ Uso: /similar EURUSD")
        return
    coppia = args[0].upper()
    rows = fetch_trades_by_pair(coppia)
    if not rows:
        await update.message.reply_text(f"🔍 Nessun trade trovato per <b>{coppia}</b>.", parse_mode="HTML")
        return
    user_id = update.effective_user.id
    user_trades = [r for r in rows if r[1] == user_id]
    others_trades = [r for r in rows if r[1] != user_id]
    if not user_trades:
        await update.message.reply_text(f"Non hai trade su {coppia}. Registrane uno con /journal.")
        return
    if not others_trades:
        await update.message.reply_text(f"Nessun altro utente ha trade su {coppia} da confrontare.")
        return
    similarities = []
    for ut in user_trades[:5]:
        for ot in others_trades[:20]:
            sim = _trade_similarity(ut, ot)
            if sim > 0:
                similarities.append((sim, ut, ot))
    similarities.sort(key=lambda x: x[0], reverse=True)
    if not similarities:
        await update.message.reply_text("Nessun trade simile trovato.")
        return
    lines = [f"🔍 <b>Trades simili su {coppia}</b>\n"]
    for sim, ut, ot in similarities[:5]:
        bar = _similarity_bar(sim)
        emoji = _similarity_emoji(sim)
        lines.append(
            f"{emoji} <b>{int(sim*100)}%</b> {bar}\n"
            f"  Tu: Entry {ut[5]}  SL {ut[6]}  TP {ut[7]}\n"
            f"  @{ot[2]}: Entry {ot[5]}  SL {ot[6]}  TP {ot[7]}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─── CONVERSATION: /delete ─────────────────────────────────────────────────────
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = fetch_last_trade(update.effective_user.id)
    if not row:
        await update.message.reply_text("Nessun trade da eliminare.")
        return ConversationHandler.END
    context.user_data["delete_id"] = row[0]
    keyboard = [[
        InlineKeyboardButton("✅ Sì, elimina", callback_data="yes"),
        InlineKeyboardButton("❌ No, annulla", callback_data="no")
    ]]
    await update.message.reply_text(
        f"🗑 Eliminare il trade <b>#{row[0]}</b>?\n{row[3]} {row[4]} — P&L: {row[10]}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return DELETE_CONFIRM


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "yes":
        trade_id = context.user_data.get("delete_id")
        if trade_id:
            delete_trade(trade_id)
            await query.edit_message_text(f"✅ Trade #{trade_id} eliminato.")
        else:
            await query.edit_message_text("❌ Errore: ID non trovato.")
    else:
        await query.edit_message_text("Eliminazione annullata.")
    return ConversationHandler.END


# ─── CONVERSATION: /edit ───────────────────────────────────────────────────────
EDIT_FIELDS = {
    "coppia":     ("Coppia", str),
    "tipo":       ("Strategia", str),
    "entry":      ("Entry", _parse_price),
    "sl":         ("Stop Loss", _parse_price),
    "tp":         ("Take Profit", _parse_price),
    "motivo":     ("Motivo", str),
    "psicologia": ("Psicologia", str),
    "pnl":        ("P&L", _parse_pnl),
}


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = fetch_last_trade(update.effective_user.id)
    if not row:
        await update.message.reply_text("Nessun trade da modificare.")
        return ConversationHandler.END
    context.user_data["edit_trade_id"] = row[0]
    keyboard = [
        [InlineKeyboardButton(label, callback_data=field)]
        for field, (label, _) in EDIT_FIELDS.items()
    ]
    keyboard.append([InlineKeyboardButton("❌ Annulla", callback_data="cancel_edit")])
    await update.message.reply_text(
        f"📝 Modifica trade <b>#{row[0]}</b> — {row[3]} {row[4]}\n\nSeleziona il campo:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return EDIT_SELECT


async def edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data
    if choice == "cancel_edit":
        await query.edit_message_text("Modifica annullata.")
        return ConversationHandler.END
    context.user_data["edit_field"] = choice
    label, _ = EDIT_FIELDS[choice]
    await query.edit_message_text(f"Inserisci il nuovo valore per <b>{label}</b>:", parse_mode="HTML")
    return EDIT_VALUE


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    trade_id = context.user_data.get("edit_trade_id")
    if not field or not trade_id:
        await update.message.reply_text("Sessione scaduta. Riprova con /edit.")
        return ConversationHandler.END
    _, parser = EDIT_FIELDS[field]
    raw = update.message.text.strip()
    parsed = parser(raw)
    if parsed is None and parser != str:
        await update.message.reply_text("⚠️ Valore non valido. Riprova:")
        return EDIT_VALUE
    value = parsed if parsed is not None else raw
    try:
        update_trade_field(trade_id, field, value)
        label, _ = EDIT_FIELDS[field]
        await update.message.reply_text(f"✅ <b>{label}</b> aggiornato a: {value}", parse_mode="HTML")
    except Exception as exc:
        logger.exception("Errore aggiornamento trade %s", trade_id)
        await update.message.reply_text(f"❌ Errore: {exc}")
    return ConversationHandler.END


# ─── MT5 HELPERS ──────────────────────────────────────────────────────────────

def _load_json(path_: str) -> dict:
    try:
        with open(path_, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path_: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path_), exist_ok=True)
    with open(path_, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _get_or_create_mt5_token(user_id: int, username: str) -> str:
    tokens = _load_json(MT5_TOKENS)
    for tok, info in tokens.items():
        if info.get("chat_id") == user_id:
            return tok
    token = _secrets.token_hex(16)
    tokens[token] = {
        "chat_id": user_id,
        "username": username,
        "created_at": datetime.now().isoformat(),
    }
    _save_json(MT5_TOKENS, tokens)
    return token


def _generate_ea_code(webhook_url: str, token: str) -> str:
    return f"""//+------------------------------------------------------------------+
//|  ForexJournal_MT5.mq5                                          |
//|  Invia automaticamente ogni trade chiuso al Trading Journal    |
//|  Richiede: Strumenti → Opzioni → EA → Abilita richieste Web   |
//+------------------------------------------------------------------+
#property copyright "Trading Journal — @Forexrama_bot"
#property version   "1.1"
#property strict

input string WebhookURL = "{webhook_url}";
input string Token      = "{token}";

string _sentTickets[];

void OnInit()   {{ ArrayResize(_sentTickets, 0); }}
void OnDeinit(const int reason) {{}}
void OnTick()   {{}}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest     &request,
                        const MqlTradeResult      &result)
{{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   ulong ticket = trans.deal;
   if(!HistoryDealSelect(ticket)) return;

   long entry = HistoryDealGetInteger(ticket, DEAL_ENTRY);
   if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) return;

   string tickStr = IntegerToString((long)ticket);
   for(int i = 0; i < ArraySize(_sentTickets); i++)
      if(_sentTickets[i] == tickStr) return;
   int sz = ArraySize(_sentTickets);
   ArrayResize(_sentTickets, sz + 1);
   _sentTickets[sz] = tickStr;

   string   symbol     = HistoryDealGetString(ticket,  DEAL_SYMBOL);
   long     dealType   = HistoryDealGetInteger(ticket, DEAL_TYPE);
   double   volume     = HistoryDealGetDouble(ticket,  DEAL_VOLUME);
   double   closePrice = HistoryDealGetDouble(ticket,  DEAL_PRICE);
   double   profit     = HistoryDealGetDouble(ticket,  DEAL_PROFIT);
   double   swap       = HistoryDealGetDouble(ticket,  DEAL_SWAP);
   double   commission = HistoryDealGetDouble(ticket,  DEAL_COMMISSION);
   string   comment    = HistoryDealGetString(ticket,  DEAL_COMMENT);
   datetime closeTime  = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);

   // Recupera prezzo di apertura dalla posizione originale
   ulong posId = HistoryDealGetInteger(ticket, DEAL_POSITION_ID);
   double openPrice = 0;
   string openTimeStr = "";
   if(HistorySelectByPosition(posId))
   {{
      for(int i = 0; i < HistoryDealsTotal(); i++)
      {{
         ulong d = HistoryDealGetTicket(i);
         if(HistoryDealGetInteger(d, DEAL_ENTRY) == DEAL_ENTRY_IN &&
            HistoryDealGetInteger(d, DEAL_POSITION_ID) == (long)posId)
         {{
            openPrice   = HistoryDealGetDouble(d, DEAL_PRICE);
            datetime ot = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
            openTimeStr = TimeToString(ot, TIME_DATE|TIME_MINUTES);
            break;
         }}
      }}
   }}

   string direction = (dealType == DEAL_TYPE_BUY) ? "buy" : "sell";

   string json = StringFormat(
      "{{\\\"token\\\":\\\"%s\\\",\\\"ticket\\\":\\\"%s\\\","
      "\\\"symbol\\\":\\\"%s\\\",\\\"type\\\":\\\"%s\\\","
      "\\\"volume\\\":%.2f,\\\"openPrice\\\":%.5f,"
      "\\\"closePrice\\\":%.5f,\\\"profit\\\":%.2f,"
      "\\\"swap\\\":%.2f,\\\"commission\\\":%.2f,"
      "\\\"comment\\\":\\\"%s\\\","
      "\\\"openTime\\\":\\\"%s\\\","
      "\\\"closeTime\\\":\\\"%s\\\"}}",
      Token, tickStr, symbol, direction,
      volume, openPrice, closePrice, profit,
      swap, commission, comment,
      openTimeStr,
      TimeToString(closeTime, TIME_DATE|TIME_MINUTES)
   );

   char   post[];
   StringToCharArray(json, post, 0, StringLen(json));
   char   resp[];
   string respHeaders;
   string headers = "Content-Type: application/json\\r\\n";

   int code = WebRequest("POST", WebhookURL, headers, 5000, post, resp, respHeaders);
   if(code == -1)
      Print("[ForexJournal] Errore WebRequest (codice: ", GetLastError(),
            "). Verifica Tools → Opzioni → EA → URL consentiti.");
   else
      Print("[ForexJournal] Trade inviato: ", symbol, " ", direction,
            " PnL=", profit, " ticket=", tickStr);
}}
//+------------------------------------------------------------------+
"""


# ─── MT5 COMMANDS & CONVERSATION ──────────────────────────────────────────────

async def mt5connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    token  = _get_or_create_mt5_token(user.id, user.username or user.first_name)

    domain = os.environ.get("REPLIT_DOMAINS", "").split(",")[0].strip()
    webhook_url = (
        f"https://{domain}/api/mt5/trade" if domain else "https://<YOUR_DOMAIN>/api/mt5/trade"
    )

    text = (
        f"🔗 <b>Connessione MT5 attivata!</b>\n\n"
        f"Il tuo <b>Token personale</b>:\n"
        f"<code>{token}</code>\n\n"
        f"<b>URL Webhook:</b>\n"
        f"<code>{webhook_url}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Istruzioni rapide:</b>\n"
        f"1. Apri MetaTrader 5\n"
        f"2. <i>Strumenti → Opzioni → Expert Advisor</i>\n"
        f"   ☑️ Abilita le richieste Web per questi URL\n"
        f"   Aggiungi: <code>{domain or 'tuo-dominio.replit.app'}</code>\n"
        f"3. Apri MetaEditor (F4) → crea nuovo EA\n"
        f"4. Incolla il codice che ti invio come file\n"
        f"5. Compila (F7) e carica su un grafico qualsiasi\n\n"
        f"Ogni trade chiuso su MT5 ti arriverà qui in chat con il bottone "
        f"<b>Completa il Journal</b> per rispondere alle domande di rito! 🎯\n\n"
        f"<i>/mt5connect — per rigenerare il token</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

    ea_bytes = io.BytesIO(_generate_ea_code(webhook_url, token).encode("utf-8"))
    await update.message.reply_document(
        document=ea_bytes,
        filename="ForexJournal_MT5.mq5",
        caption=(
            "📄 <b>Expert Advisor per MT5</b>\n"
            "Incolla il codice in MetaEditor → compila → carica su un grafico.\n"
            "<i>Ricordati di abilitare l'URL nelle opzioni EA (passo 2).</i>"
        ),
        parse_mode="HTML",
    )


async def mt5_trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Utente preme 'Completa il Journal' dopo aver ricevuto il DM di un trade MT5."""
    query = update.callback_query
    await query.answer()

    trade_id = query.data.replace("mt5_trade_", "")
    pending  = _load_json(MT5_PENDING)
    trade    = pending.get(trade_id)

    if not trade:
        await query.message.reply_text(
            "⚠️ Trade non trovato — potrebbe essere già stato completato.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    context.user_data["mt5_trade_id"] = trade_id
    context.user_data["mt5_trade"]    = trade

    keyboard = [[InlineKeyboardButton(s, callback_data=f"mt5_strat_{s}")] for s in STRATEGIES]
    await query.message.reply_text(
        f"📝 <b>Journal — {trade.get('symbol', '?')}</b>\n\n"
        f"<b>1️⃣ Quale strategia descrive meglio questo trade?</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return MT5_STRAT


async def mt5_ask_psych(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["mt5_strategy"] = query.data.replace("mt5_strat_", "")

    keyboard = [
        [InlineKeyboardButton(f"{e} {n}", callback_data=f"mt5_psych_{n}")]
        for e, n, _ in PSYCHOLOGY_OPTIONS
    ]
    await query.message.reply_text(
        "<b>2️⃣ Come ti sentivi durante questo trade?</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    return MT5_PSYCH


async def mt5_ask_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["mt5_psychology"] = query.data.replace("mt5_psych_", "")

    await query.message.reply_text(
        "<b>3️⃣ Note aggiuntive:</b> setup, confluenze, errori commessi...\n"
        "<i>(scrivi 'skip' per saltare)</i>",
        parse_mode="HTML",
    )
    return MT5_NOTES


async def mt5_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw   = update.message.text.strip()
    notes = "" if raw.lower() == "skip" else raw

    trade      = context.user_data.get("mt5_trade", {})
    trade_id   = context.user_data.get("mt5_trade_id", "")
    strategy   = context.user_data.get("mt5_strategy", "—")
    psychology = context.user_data.get("mt5_psychology", "—")
    profit     = trade.get("profit", 0) or 0
    symbol     = trade.get("symbol", "—")
    ticket     = trade.get("ticket", "—")

    motivo = f"MT5 #{ticket}" + (f" · {notes}" if notes else "")

    # Remove from pending
    pending = _load_json(MT5_PENDING)
    pending.pop(trade_id, None)
    _save_json(MT5_PENDING, pending)

    sign  = "+" if profit >= 0 else ""
    emoji = "💚" if profit > 0 else ("❤️" if profit < 0 else "⚪")

    save_trade(
        user_id=update.effective_user.id,
        username=update.effective_user.username or update.effective_user.first_name,
        data={
            "coppia":      symbol,
            "tipo":        strategy,
            "entry":       trade.get("open_price", 0),
            "sl":          0,
            "tp":          0,
            "motivo":      motivo,
            "psicologia":  psychology,
            "pnl":         profit,
            "source":      "mt5",
            "ticket":      ticket,
            "open_price":  trade.get("open_price"),
            "close_price": trade.get("close_price"),
            "volume":      trade.get("volume"),
            "swap":        trade.get("swap"),
            "commission":  trade.get("commission"),
            "close_time":  trade.get("close_time"),
        },
    )

    await update.message.reply_text(
        f"✅ <b>Trade MT5 salvato nel Journal!</b>\n\n"
        f"📊 <b>{symbol}</b>  ·  {strategy}\n"
        f"🧠 Psicologia: {psychology}\n"
        f"{emoji} P&L: <b>{sign}{profit:.2f}</b>\n"
        + (f"📝 Note: <i>{notes}</i>\n" if notes else "")
        + f"\n<i>Ottimo! Continua a tracciare ogni trade 💪</i>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ─── /segnali COMMAND ─────────────────────────────────────────────────────────

def _conf_bar(confidence: int) -> str:
    filled = round(confidence / 20)
    return "█" * filled + "░" * (5 - filled)


def _rsi_icon(rsi: float) -> str:
    if rsi <= 30:  return "🔵"   # ipervenduto
    if rsi <= 42:  return "🟢"
    if rsi >= 70:  return "🔴"   # ipercomprato
    if rsi >= 58:  return "🟠"
    return "⚪"


def _bias_line(bias: str) -> str:
    return {
        "strong_bullish": "⬆️⬆️ Fortemente Rialzista",
        "bullish":        "⬆️  Rialzista",
        "neutral":        "➡️  Neutro",
        "bearish":        "⬇️  Ribassista",
        "strong_bearish": "⬇️⬇️ Fortemente Ribassista",
    }.get(bias, "➡️  Neutro")


def _format_signal_card(sig: dict) -> str:
    pair       = sig["pair"]
    price      = sig["price"]
    direction  = sig["direction"]
    bias       = sig["bias"]
    rsi        = sig["rsi"]
    confidence = sig["confidence"]
    atr14      = sig["atr14"]
    dec        = sig["decimals"]
    tips       = sig.get("signals", [])

    is_long   = direction == "long"
    dir_emoji = "📈" if is_long else "📉"
    dir_label = "LONG  🟢" if is_long else "SHORT 🔴"

    entry = price
    sl    = price - atr14 if is_long else price + atr14
    tp    = price + 2 * atr14 if is_long else price - 2 * atr14
    sl_d  = abs(entry - sl)
    tp_d  = abs(tp - entry)
    rr    = tp_d / sl_d if sl_d > 0 else 0

    bar      = _conf_bar(confidence)
    rsi_icon = _rsi_icon(rsi)
    bias_txt = _bias_line(bias)

    tip_line = ""
    if tips:
        tip_line = "\n💬 <i>" + " · ".join(tips[:3]) + "</i>"

    return (
        f"┌{'─' * 26}┐\n"
        f"│  {dir_emoji} <b>{pair}</b>  ·  <b>{dir_label}</b>\n"
        f"└{'─' * 26}┘\n"
        f"💰 Prezzo  <code>{price:.{dec}f}</code>\n"
        f"{rsi_icon} RSI14   <b>{rsi:.0f}</b>   "
        f"📶 {bar} <b>{confidence}%</b>\n"
        f"🔰 Bias    {bias_txt}\n"
        f"\n"
        f"🎯 Entry   <code>{entry:.{dec}f}</code>\n"
        f"🛑 Stop    <code>{sl:.{dec}f}</code>  "
        f"<i>({'-' if is_long else '+'}{sl_d:.{dec}f})</i>\n"
        f"✅ Target  <code>{tp:.{dec}f}</code>  "
        f"<i>({'+' if is_long else '-'}{tp_d:.{dec}f})</i>\n"
        f"⚡ R:R  <b>1:{rr:.1f}</b>"
        f"{tip_line}"
    )


def _format_summary_table(signals: list) -> str:
    lines = ["📋 <b>TUTTI I PAIR · PANORAMICA</b>\n"]
    for sig in signals:
        d = sig["direction"]
        icon = "📈" if d == "long" else ("📉" if d == "short" else "➡️")
        rsi_icon = _rsi_icon(sig["rsi"])
        bar = _conf_bar(sig["confidence"])
        pair = sig["pair"]
        lines.append(
            f"{icon} <b>{pair:<8}</b> {rsi_icon} RSI <b>{sig['rsi']:.0f}</b>  "
            f"{bar} <b>{sig['confidence']}%</b>"
        )
    lines.append(
        "\n<i>⚠️ Analisi tecnica automatica — non costituisce consulenza finanziaria."
        " Gestisci sempre il rischio.</i>"
    )
    return "\n".join(lines)


async def segnali_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra i segnali tecnici Forex in tempo reale."""
    msg = await update.message.reply_text(
        "🔄 <i>Carico i segnali di mercato...</i>", parse_mode="HTML"
    )
    await _send_segnali(update, context, msg)


async def segnali_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback per il bottone Aggiorna."""
    query = update.callback_query
    await query.answer("🔄 Aggiornamento in corso...")
    msg = await query.message.reply_text(
        "🔄 <i>Aggiorno i segnali...</i>", parse_mode="HTML"
    )
    await _send_segnali(update, context, msg)


async def _send_segnali(update: Update, context: ContextTypes.DEFAULT_TYPE, loading_msg):
    """Logica condivisa per /segnali e il callback di refresh."""
    # Origine messaggio (funziona sia da command che da callback)
    chat = (
        update.message.chat
        if update.message
        else update.callback_query.message.chat
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "http://localhost:80/api/forex-signals", timeout=15.0
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("segnali fetch error: %s", exc)
        await loading_msg.edit_text(
            "❌ <b>Impossibile caricare i segnali.</b>\n"
            "<i>Assicurati che il server API sia attivo e riprova.</i>",
            parse_mode="HTML",
        )
        return

    signals    = data.get("signals", [])
    fetched_at = data.get("fetchedAt", "")
    try:
        dt       = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        time_str = dt.strftime("%d/%m · %H:%M UTC")
    except Exception:
        time_str = "—"

    directional = [s for s in signals if s["direction"] != "neutral"]
    neutral_cnt = len(signals) - len(directional)

    # ── header ──────────────────────────────────────────────────────────────
    header = (
        f"🌐 <b>SEGNALI FOREX · LIVE</b>\n"
        f"{'═' * 26}\n"
        f"📅 <i>{time_str}</i>\n"
        f"📊 <b>{len(directional)}</b> segnali direzionali  ·  "
        f"<b>{neutral_cnt}</b> neutrali\n"
        f"<i>Fonte: Yahoo Finance · RSI14 · SMA20 · ATR14</i>"
    )
    await loading_msg.edit_text(header, parse_mode="HTML")

    if not directional:
        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                "😶 <b>Nessun segnale direzionale disponibile</b>\n"
                "<i>Tutti i pair sono in fase neutra al momento."
                " Riprova tra qualche minuto.</i>"
            ),
            parse_mode="HTML",
        )
        return

    # ── una card per ogni pair direzionale ──────────────────────────────────
    for sig in directional:
        await context.bot.send_message(
            chat_id=chat.id,
            text=_format_signal_card(sig),
            parse_mode="HTML",
        )

    # ── riepilogo tutti i pair + bottone aggiorna ────────────────────────────
    app_domain = os.environ.get("REPLIT_DOMAINS", "").split(",")[0].strip()
    app_url    = f"https://{app_domain}/signal-diary" if app_domain else None

    kb_buttons = [InlineKeyboardButton("🔄 Aggiorna segnali", callback_data="segnali_refresh")]
    if app_url:
        kb_buttons.append(InlineKeyboardButton("📓 Diario Segnali", url=app_url))

    await context.bot.send_message(
        chat_id=chat.id,
        text=_format_summary_table(signals),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([kb_buttons]),
    )


# ─── BUILD & MAIN ──────────────────────────────────────────────────────────────
async def _post_init(application: Application) -> None:
    """Cancella webhook e forza la chiusura di sessioni getUpdates precedenti."""
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook cancellato — forzo chiusura sessioni precedenti...")
    except Exception as e:
        logger.warning("delete_webhook: %s", e)

    # Chiama getUpdates con timeout=0 finché non otteniamo una risposta senza 409.
    # Questo "reclama" la sessione Telegram e invalida quella precedente.
    for attempt in range(25):
        try:
            await application.bot.get_updates(timeout=0, limit=1, allowed_updates=[])
            logger.info("Sessione Telegram acquisita (tentativo %d)", attempt + 1)
            break
        except Exception as e:
            err = str(e)
            if "409" in err or "Conflict" in err:
                logger.warning(
                    "Sessione precedente ancora attiva — attendo 3s (tentativo %d/25)", attempt + 1
                )
                await asyncio.sleep(3)
            else:
                # Errore diverso dal 409: non blocchiamo l'avvio
                logger.warning("get_updates pre-start: %s", e)
                break


def build_application():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Imposta la variabile d'ambiente TELEGRAM_BOT_TOKEN.")

    application = Application.builder().token(token).post_init(_post_init).build()
    application.add_error_handler(error_handler)

    # Simple commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("last", last_command))
    application.add_handler(CommandHandler("chart", chart_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("similar", similar_command))
    application.add_handler(CommandHandler("chatid", chatid_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("segnali", segnali_command))
    application.add_handler(CallbackQueryHandler(segnali_refresh_callback, pattern="^segnali_refresh$"))
    application.add_handler(CommandHandler("mt5connect", mt5connect_command))

    # /mt5 conversation (triggered by inline button on trade DM)
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(mt5_trade_callback, pattern="^mt5_trade_")],
        states={
            MT5_STRAT: [CallbackQueryHandler(mt5_ask_psych, pattern="^mt5_strat_")],
            MT5_PSYCH: [CallbackQueryHandler(mt5_ask_notes, pattern="^mt5_psych_")],
            MT5_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, mt5_save)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    ))

    # /journal conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("journal", ask_pair)],
        states={
            COPPIA:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_type)],
            TIPO:      [CallbackQueryHandler(ask_entry)],
            ENTRY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_stop_loss)],
            SL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_take_profit)],
            TP:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_reason)],
            MOTIVO:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_psychology)],
            PSICOLOGIA:[CallbackQueryHandler(ask_pnl)],
            PNL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, finish)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # /delete conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("delete", delete_command)],
        states={DELETE_CONFIRM: [CallbackQueryHandler(delete_confirm)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # /edit conversation
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("edit", edit_command)],
        states={
            EDIT_SELECT: [CallbackQueryHandler(edit_select)],
            EDIT_VALUE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    return application


def _acquire_pid_lock() -> "io.IOBase":
    """Tenta di acquisire un lock esclusivo sul PID file.
    Termina il processo se un'altra istanza è già in esecuzione."""
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    f = open(PID_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Un'altra istanza del bot è già in esecuzione. Uscita.")
        f.close()
        sys.exit(1)
    f.write(str(os.getpid()))
    f.flush()
    return f


def main():
    # Non avviare il bot in ambienti di deployment Replit (autoscale / VM).
    # Il bot usa polling long-poll che richiede una sola istanza attiva.
    # In deployment usare webhook (vedi API server /api/telegram/webhook).
    subcluster = os.environ.get("REPLIT_SUBCLUSTER", "")
    if subcluster and subcluster != "interactive":
        logger.info("Ambiente non-interattivo (%s) — bot in polling non avviato.", subcluster)
        return

    init_db()

    # Impedisce doppie istanze (causa Conflict su Telegram)
    lock_file = _acquire_pid_lock()

    try:
        logger.info("🚀 Avvio bot @Forexrama_bot con polling...")
        application = build_application()
        application.run_polling(drop_pending_updates=True)
        logger.info("Polling terminato.")
    except KeyboardInterrupt:
        logger.info("Interruzione manuale. Uscita.")
    except Exception as exc:
        logger.exception("Errore critico: %s", exc)
        sys.exit(1)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
