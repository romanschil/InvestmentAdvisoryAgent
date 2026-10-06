"""
Taeglicher Investment Advisory Agent -- Portfolio-Modus mit Bestaetigung
=============================================================================
Haelt ein fiktives Portfolio in CHF. Erreicht eine Position seit Kauf
>= SELL_THRESHOLD_PCT Gewinn, wird NICHT automatisch verkauft -- stattdessen
erzeugt der Agent einen VORSCHLAG (Verkauf + Nachkauf-Idee), sichtbar im
Dashboard und in der Mail, jeweils mit einem "Quittieren"-Link. Erst wenn
der Link angeklickt und das vorausgefuellte GitHub-Issue abgeschickt wird,
fuehrt ein zweiter Workflow (confirm_action.py) die Aktion wirklich aus.
Ohne Quittierung bleibt die Position unveraendert im Portfolio, es wird
nichts verkauft und nichts Neues gekauft.

Wochenende (Sa/So): nur Krypto-Positionen werden bewertet/vorgeschlagen,
Aktien/ETFs bleiben auf dem Freitags-Stand eingefroren.

WICHTIG: Keine Anlageberatung, kein echter Handel. Simulation auf Basis
oeffentlicher Marktdaten. ISIN/Valor/Gebuehren sind Richtwerte ohne Gewaehr.
"""

import os
import json
import math
import uuid
import smtplib
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date

import anthropic
import yfinance as yf

# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

BUDGET_CHF = 5000
TOP_N = 3
SELL_THRESHOLD_PCT = 20.0
SAVINGS_ANNUAL_RATE_PCT = 0.75

TRANSACTION_FEE_TIERS = [
    (500, 3), (1000, 5), (2000, 10), (10000, 29),
    (15000, 49), (25000, 79), (50000, 129), (float("inf"), 190),
]
FX_FEE_PCT = 0.95
CRYPTO_FEE_PCT = 1.0

def stock_transaction_fee(order_value_native: float) -> float:
    for threshold, fee in TRANSACTION_FEE_TIERS:
        if order_value_native <= threshold:
            return fee
    return TRANSACTION_FEE_TIERS[-1][1]


def transaction_fee_for(order_value_native: float, asset_class: str) -> float:
    if asset_class == "crypto":
        return order_value_native * CRYPTO_FEE_PCT / 100
    return stock_transaction_fee(order_value_native)


CANDIDATE_UNIVERSE = [
    {"ticker": "AAPL",    "isin": "US0378331005", "valor": "908440",   "name": "Apple Inc.",            "currency": "USD", "asset_class": "stock"},
    {"ticker": "MSFT",    "isin": "US5949181045", "valor": "951692",   "name": "Microsoft Corp.",       "currency": "USD", "asset_class": "stock"},
    {"ticker": "GOOGL",   "isin": "US02079K3059", "valor": "29798540", "name": "Alphabet Inc. (A)",     "currency": "USD", "asset_class": "stock"},
    {"ticker": "AMZN",    "isin": "US0231351067", "valor": "645156",   "name": "Amazon.com Inc.",       "currency": "USD", "asset_class": "stock"},
    {"ticker": "NVDA",    "isin": "US67066G1040", "valor": "994529",   "name": "NVIDIA Corp.",          "currency": "USD", "asset_class": "stock"},
    {"ticker": "META",    "isin": "US30303M1027", "valor": "14917609", "name": "Meta Platforms Inc.",   "currency": "USD", "asset_class": "stock"},
    {"ticker": "NESN.SW", "isin": "CH0038863350", "valor": "3886335",  "name": "Nestle SA",             "currency": "CHF", "asset_class": "stock"},
    {"ticker": "NOVN.SW", "isin": "CH0012005267", "valor": "1200526",  "name": "Novartis AG",           "currency": "CHF", "asset_class": "stock"},
    {"ticker": "ROG.SW",  "isin": "CH0012032048", "valor": "1203204",  "name": "Roche Holding AG",      "currency": "CHF", "asset_class": "stock"},
    {"ticker": "UHR.SW",  "isin": "CH0012255151", "valor": "1225515",  "name": "Swatch Group AG",       "currency": "CHF", "asset_class": "stock"},
    {"ticker": "VOO",     "isin": "US9229083632", "valor": "–",        "name": "Vanguard S&P 500 ETF",  "currency": "USD", "asset_class": "stock"},
    {"ticker": "VWCE.DE", "isin": "IE00BK5BQT80", "valor": "–",        "name": "Vanguard FTSE All-World ETF", "currency": "EUR", "asset_class": "stock"},
    {"ticker": "BTC-USD", "isin": "Kein ISIN (Spot)", "valor": "–", "name": "Bitcoin",  "currency": "USD", "asset_class": "crypto"},
    {"ticker": "ETH-USD", "isin": "Kein ISIN (Spot)", "valor": "–", "name": "Ethereum", "currency": "USD", "asset_class": "crypto"},
    {"ticker": "SOL-USD", "isin": "Kein ISIN (Spot)", "valor": "–", "name": "Solana",   "currency": "USD", "asset_class": "crypto"},
    {"ticker": "ADA-USD", "isin": "Kein ISIN (Spot)", "valor": "–", "name": "Cardano",  "currency": "USD", "asset_class": "crypto"},
    {"ticker": "XRP-USD", "isin": "Kein ISIN (Spot)", "valor": "–", "name": "XRP",      "currency": "USD", "asset_class": "crypto"},
]
UNIVERSE_BY_TICKER = {u["ticker"]: u for u in CANDIDATE_UNIVERSE}

BENCHMARKS = {"SMI": "^SSMI", "S&P 500": "^GSPC", "NASDAQ": "^IXIC"}

RECIPIENT_EMAIL = "roman.schilling@bluewin.ch"
GITHUB_REPO = "romanschil/InvestmentAdvisoryAgent"
DASHBOARD_URL = "https://romanschil.github.io/InvestmentAdvisoryAgent/"
ISSUE_BASE_URL = f"https://github.com/{GITHUB_REPO}/issues/new"

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

RISK_PROFILE = "ausgewogen"
HORIZON = "mittel- bis langfristig"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "portfolio_state.json")
DASHBOARD_DIR = os.path.join(BASE_DIR, "docs")
DASHBOARD_FILE = os.path.join(DASHBOARD_DIR, "index.html")


def is_weekend() -> bool:
    return date.today().weekday() >= 5


def fmt_shares(v):
    return f"{v:.4f}" if isinstance(v, (int, float)) else "-"


def safe_value(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, float) and math.isnan(v):
        return 0.0
    return v


# ---------------------------------------------------------------------------
# MARKTDATEN / FX
# ---------------------------------------------------------------------------

def fetch_prices(tickers: list[str]) -> dict:
    prices = {}
    for ticker in sorted(set(tickers)):
        try:
            hist = yf.Ticker(ticker).history(period="10d")
            closes = hist["Close"].dropna()
            if not closes.empty:
                prices[ticker] = float(closes.iloc[-1])
        except Exception:
            pass
    return prices


def fetch_fx_rates() -> dict:
    rates = {"CHF": 1.0}
    pairs = {"USD": "USDCHF=X", "EUR": "EURCHF=X"}
    for cur, pair in pairs.items():
        try:
            hist = yf.Ticker(pair).history(period="10d")
            closes = hist["Close"].dropna()
            rates[cur] = float(closes.iloc[-1]) if not closes.empty else None
        except Exception:
            rates[cur] = None
    return rates


def fetch_index_return_pct(ticker: str, start_date: str) -> float | None:
    try:
        hist = yf.Ticker(ticker).history(start=start_date)
        if hist.empty or len(hist) < 2:
            return None
        first = float(hist["Close"].iloc[0])
        last = float(hist["Close"].iloc[-1])
        return (last - first) / first * 100
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PORTFOLIO STATE
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "start_date": date.today().isoformat(),
            "cash_chf": BUDGET_CHF,
            "positions": [],
            "transactions": [],
            "value_history": [],
            "pending_actions": [],
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("pending_actions", [])
    for pos in state.get("positions", []):
        for key in ("current_value_chf", "profit_chf", "profit_pct"):
            if isinstance(pos.get(key), float) and math.isnan(pos[key]):
                pos[key] = None
    return state


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLAUDE: NEUEN ERSATZ-PICK VORSCHLAGEN
# ---------------------------------------------------------------------------

def select_new_picks(needed: int, exclude_tickers: set, universe_prices: dict, allowed_classes: set) -> list:
    available = [
        u for u in CANDIDATE_UNIVERSE
        if u["ticker"] not in exclude_tickers and u["asset_class"] in allowed_classes
    ]
    if not available:
        return []
    lines = []
    for u in available:
        p = universe_prices.get(u["ticker"])
        if p is None:
            continue
        lines.append(f"- {u['ticker']} ({u['name']}, {u['asset_class']}, {u['currency']}): Kurs {round(p, 2)}")
    candidates_summary = "\n".join(lines)
    if not candidates_summary:
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_prompt = f"""Du bist ein nuechterner Investment-Analyse-Assistent.
Anlageprofil: {RISK_PROFILE}, Horizont: {HORIZON}. Einfache, unkomplizierte
Aktien/ETFs/Kryptowaehrungen -- keine Hebelprodukte, keine Derivate.

Waehle aus dem gegebenen Universum genau {needed} Titel aus, die aus Sicht
der Marktdaten aktuell am interessantesten erscheinen. Waehle NUR aus den
gelisteten Tickern, erfinde keine neuen.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt, keine Einleitung, keine
Markdown-Codebloecke. Format:
{{"picks": [{{"ticker": "XXX", "rationale": "kurze Begruendung auf Deutsch, 1-2 Saetze"}}]}}"""
    user_prompt = f"Verfuegbares Anlageuniversum:\n{candidates_summary}\n\nWaehle {needed} Titel."

    response = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=600,
        system=system_prompt, messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    clean = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(clean).get("picks", [])
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# PORTFOLIO-LOGIK
# ---------------------------------------------------------------------------

def mark_to_market(state: dict, prices: dict, fx: dict, allowed_classes: set):
    for pos in state["positions"]:
        if pos.get("asset_class", "stock") not in allowed_classes:
            continue
        price_native = prices.get(pos["ticker"])
        rate = fx.get(pos["currency"], 1.0)
        valid_price = price_native is not None and not math.isnan(price_native)
        valid_rate = rate is not None and not math.isnan(rate)
        if valid_price and valid_rate:
            value_chf = pos["shares"] * price_native * rate
            buy_value_chf = pos.get("cost_basis_chf", pos["shares"] * pos["buy_price_chf"])
            pos["current_value_chf"] = round(value_chf, 2)
            pos["profit_chf"] = round(value_chf - buy_value_chf, 2)
            pos["profit_pct"] = round((value_chf - buy_value_chf) / buy_value_chf * 100, 2)


def detect_pending_actions(state: dict, universe_prices: dict, allowed_classes: set):
    """Erzeugt Verkaufs-/Nachkauf-VORSCHLAEGE fuer Positionen >= SELL_THRESHOLD_PCT.
    Fuehrt NICHTS aus -- das passiert erst bei Quittierung via confirm_action.py.
    Veraltete Vorschlaege (Position wieder unter der Schwelle) werden entfernt."""
    today = date.today().isoformat()

    # 1) veraltete SELL-Vorschlaege entfernen, falls Position nicht mehr >= Schwelle
    kept_sells = []
    kept_sell_ids = set()
    for a in state["pending_actions"]:
        if a["type"] != "sell":
            continue
        pos = next((p for p in state["positions"] if p["ticker"] == a["ticker"]), None)
        if pos and pos.get("profit_pct") is not None and pos["profit_pct"] >= SELL_THRESHOLD_PCT:
            kept_sells.append(a)
            kept_sell_ids.add(a["id"])

    # 2) BUY-Vorschlaege behalten, wenn schon vom User quittiert ODER zugehoeriger Sell noch gueltig
    kept_buys = [
        a for a in state["pending_actions"]
        if a["type"] == "buy" and (a.get("status") == "confirm_requested" or a.get("linked_sell_id") in kept_sell_ids)
    ]
    state["pending_actions"] = kept_sells + kept_buys

    # 3) neue Vorschlaege fuer Positionen, die NEU >= Schwelle sind
    existing_sell_tickers = {a["ticker"] for a in state["pending_actions"] if a["type"] == "sell"}
    for pos in state["positions"]:
        if pos.get("asset_class", "stock") not in allowed_classes:
            continue
        if pos["ticker"] in existing_sell_tickers:
            continue
        if pos.get("profit_pct") is None or pos["profit_pct"] < SELL_THRESHOLD_PCT:
            continue

        sell_id = uuid.uuid4().hex[:8]
        state["pending_actions"].append({
            "id": sell_id, "type": "sell", "ticker": pos["ticker"], "isin": pos["isin"],
            "valor": pos.get("valor", "–"), "name": pos["name"],
            "asset_class": pos.get("asset_class", "stock"), "detected_date": today,
            "profit_pct": pos["profit_pct"], "profit_chf": pos.get("profit_chf"),
        })

        held_and_pending = {p["ticker"] for p in state["positions"]} | existing_sell_tickers
        picks = select_new_picks(1, held_and_pending, universe_prices, allowed_classes)
        if picks:
            meta = UNIVERSE_BY_TICKER.get(picks[0].get("ticker"))
            if meta:
                state["pending_actions"].append({
                    "id": uuid.uuid4().hex[:8], "type": "buy", "ticker": meta["ticker"],
                    "isin": meta["isin"], "valor": meta.get("valor", "–"), "name": meta["name"],
                    "asset_class": meta["asset_class"], "detected_date": today,
                    "rationale": picks[0].get("rationale", ""), "linked_sell_id": sell_id,
                })
        existing_sell_tickers.add(pos["ticker"])


# ---------------------------------------------------------------------------
# AUSFUEHRUNG NACH QUITTIERUNG (wird von confirm_action.py aufgerufen)
# ---------------------------------------------------------------------------

def execute_sell_action(state: dict, action: dict, prices: dict, fx: dict) -> dict | None:
    today = date.today().isoformat()
    pos = next((p for p in state["positions"] if p["ticker"] == action["ticker"]), None)
    if not pos:
        state["pending_actions"] = [a for a in state["pending_actions"] if a["id"] != action["id"]]
        return None

    price_native = prices.get(pos["ticker"], pos.get("buy_price_native"))
    rate = fx.get(pos["currency"], 1.0) or 1.0
    gross_value_chf = pos["shares"] * price_native * rate
    order_value_native = gross_value_chf / rate if pos["currency"] != "CHF" else gross_value_chf
    fee_native = transaction_fee_for(order_value_native, pos.get("asset_class", "stock"))
    fee_chf = fee_native * rate if pos["currency"] != "CHF" else fee_native
    fx_fee_chf = gross_value_chf * FX_FEE_PCT / 100 if pos["currency"] != "CHF" else 0.0
    total_fee_chf = fee_chf + fx_fee_chf
    net_proceeds_chf = gross_value_chf - total_fee_chf
    state["cash_chf"] += net_proceeds_chf

    cost_basis = pos.get("cost_basis_chf", pos["shares"] * pos["buy_price_chf"])
    profit_chf = net_proceeds_chf - cost_basis
    profit_pct = profit_chf / cost_basis * 100

    tx = {
        "date": today, "action": "sell", "ticker": pos["ticker"],
        "isin": pos["isin"], "valor": pos.get("valor", "–"), "name": pos["name"],
        "asset_class": pos.get("asset_class", "stock"), "shares": pos["shares"],
        "price_chf": round(gross_value_chf / pos["shares"], 2) if pos["shares"] else None,
        "fee_chf": round(total_fee_chf, 2),
        "profit_chf": round(profit_chf, 2), "profit_pct": round(profit_pct, 2),
    }
    state["transactions"].append(tx)
    state["positions"] = [p for p in state["positions"] if p["ticker"] != pos["ticker"]]
    state["pending_actions"] = [a for a in state["pending_actions"] if a["id"] != action["id"]]
    return tx


def execute_buy_action(state: dict, action: dict, prices: dict, fx: dict) -> dict | None:
    today = date.today().isoformat()
    meta = UNIVERSE_BY_TICKER.get(action["ticker"])
    price_native = prices.get(action["ticker"])
    if not meta or not price_native or state["cash_chf"] <= 0:
        return None

    rate = fx.get(meta["currency"], 1.0) or 1.0
    asset_class = meta["asset_class"]
    stake_chf = state["cash_chf"]  # 1:1-Ersatz -> gesamtes freies Cash wird eingesetzt

    if meta["currency"] == "CHF":
        fee_native = transaction_fee_for(stake_chf, asset_class)
        fee_chf = fee_native
        fx_fee_chf = 0.0
    else:
        order_value_native_est = stake_chf / rate
        fee_native = transaction_fee_for(order_value_native_est, asset_class)
        fee_chf = fee_native * rate
        fx_fee_chf = stake_chf * FX_FEE_PCT / 100
    total_fee_chf = fee_chf + fx_fee_chf
    investable_chf = stake_chf - total_fee_chf

    price_chf = price_native * rate
    shares = investable_chf / price_chf
    new_pos = {
        "ticker": action["ticker"], "isin": meta["isin"], "valor": meta.get("valor", "–"),
        "name": meta["name"], "currency": meta["currency"], "asset_class": asset_class,
        "shares": round(shares, 6), "buy_price_native": round(price_native, 2),
        "buy_price_chf": round(price_chf, 4), "cost_basis_chf": round(stake_chf, 2),
        "fee_chf": round(total_fee_chf, 2), "buy_date": today,
        "rationale": action.get("rationale", ""),
    }
    state["positions"].append(new_pos)
    state["cash_chf"] = round(state["cash_chf"] - stake_chf, 2)

    tx = {
        "date": today, "action": "buy", "ticker": action["ticker"],
        "isin": meta["isin"], "valor": meta.get("valor", "–"), "name": meta["name"],
        "asset_class": asset_class, "shares": round(shares, 6), "price_chf": round(price_chf, 2),
        "fee_chf": round(total_fee_chf, 2), "rationale": action.get("rationale", ""),
    }
    state["transactions"].append(tx)
    state["pending_actions"] = [a for a in state["pending_actions"] if a["id"] != action["id"]]
    return tx


def compute_benchmarks(start_date: str) -> dict:
    results = {}
    for name, ticker in BENCHMARKS.items():
        ret_pct = fetch_index_return_pct(ticker, start_date)
        results[name] = round(BUDGET_CHF * (1 + ret_pct / 100), 2) if ret_pct is not None else None
    days_elapsed = (date.today() - date.fromisoformat(start_date)).days
    savings_value = BUDGET_CHF * (1 + SAVINGS_ANNUAL_RATE_PCT / 100 * days_elapsed / 365)
    results["Sparkonto"] = round(savings_value, 2)
    return results


def portfolio_total_value(state: dict) -> float:
    return state["cash_chf"] + sum(safe_value(p.get("current_value_chf")) for p in state["positions"])


# ---------------------------------------------------------------------------
# QUITTIER-LINKS (GitHub Issue)
# ---------------------------------------------------------------------------

def confirm_url(action: dict) -> str:
    title = f"CONFIRM:{action['type']}:{action['id']}"
    lines = [
        "Automatisch generiert -- bitte Titel NICHT aendern.", "",
        f"Ticker: {action['ticker']}", f"Name: {action['name']}", f"ISIN: {action['isin']}",
    ]
    if action["type"] == "sell":
        lines.append(f"Gewinn: {action.get('profit_pct', 0):+.2f}%")
    else:
        lines.append(f"Begruendung: {action.get('rationale', '')}")
    body = "\n".join(lines)
    params = urllib.parse.urlencode({"title": title, "body": body, "labels": "confirm"})
    return f"{ISSUE_BASE_URL}?{params}"


def render_pending_actions(pending_actions: list) -> str:
    sells = [a for a in pending_actions if a["type"] == "sell"]
    if not sells:
        return "<p style='color:#94a3b8;'>Keine offenen Vorschlaege -- keine Position ueber der Gewinn-Schwelle.</p>"

    blocks = []
    for sell in sells:
        buy = next((a for a in pending_actions if a["type"] == "buy" and a.get("linked_sell_id") == sell["id"]), None)
        sell_line = (
            f"<div style='margin-bottom:4px;'>"
            f"<strong>{sell['name']}</strong> ({sell['isin']}) hat {sell.get('profit_pct', 0):+.2f}% erreicht. "
            f"<a href='{confirm_url(sell)}' style='color:#38bdf8;'>Titel verkaufen (quittieren)</a>"
            f"</div>"
        )
        if buy:
            buy_status = " (bereits von dir quittiert, wartet auf Verkaufsbestaetigung)" if buy.get("status") == "confirm_requested" else ""
            buy_line = (
                f"<div style='margin-bottom:14px;margin-left:12px;color:#cbd5e1;'>"
                f"Vorschlag Nachkauf: <strong>{buy['name']}</strong> ({buy['isin']}) -- {buy.get('rationale','')} "
                f"<a href='{confirm_url(buy)}' style='color:#38bdf8;'>Kauf quittieren</a>{buy_status}"
                f"</div>"
            )
        else:
            buy_line = "<div style='margin-bottom:14px;margin-left:12px;color:#94a3b8;'>Kein Ersatz-Vorschlag verfuegbar.</div>"
        blocks.append(sell_line + buy_line)
    return "".join(blocks)


# ---------------------------------------------------------------------------
# TABELLEN (Dashboard + E-Mail)
# ---------------------------------------------------------------------------

def positions_table_rows(positions: list, html: bool) -> str:
    rows = []
    for p in positions:
        profit_pct = p.get("profit_pct") or 0
        color = "#16a34a" if profit_pct >= 0 else "#dc2626"
        if html:
            rows.append(
                f"<tr>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #334155;'>{p['name']}</td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #334155;'>{p['isin']}</td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #334155;'>{fmt_shares(p.get('shares'))}</td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #334155;'>{p.get('current_value_chf','n/a')} CHF</td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #334155;color:{color};'>{p.get('profit_chf',0):+.2f} CHF ({profit_pct:+.2f}%)</td>"
                f"</tr>"
            )
        else:
            rows.append(
                f"- {p['name']} | ISIN {p['isin']} | {fmt_shares(p.get('shares'))} Stueck | "
                f"{p.get('current_value_chf','n/a')} CHF | {p.get('profit_chf',0):+.2f} CHF ({profit_pct:+.2f}%)"
            )
    return "".join(rows) if html else "\n".join(rows)


def render_tx_row(tx: dict) -> str:
    profit_cell = tx.get("profit_chf", "-") if tx["action"] == "sell" else "-"
    pct = tx.get("profit_pct")
    pct_cell = f"{pct:+.2f}%" if (tx["action"] == "sell" and pct is not None) else "-"
    return (
        f"<tr><td>{tx['date']}</td><td>{tx['action'].upper()}</td>"
        f"<td>{tx['ticker']}</td><td>{tx.get('isin','')}</td><td>{tx.get('valor','–')}</td><td>{tx.get('name','')}</td>"
        f"<td>{tx.get('asset_class','stock')}</td><td>{fmt_shares(tx.get('shares'))}</td>"
        f"<td>{tx.get('fee_chf', '-')}</td><td>{profit_cell}</td><td>{pct_cell}</td></tr>"
    )


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------

def render_dashboard(state: dict, benchmarks: dict, portfolio_total: float, weekend: bool) -> str:
    profit_chf = portfolio_total - BUDGET_CHF
    profit_pct = profit_chf / BUDGET_CHF * 100

    pending_html = render_pending_actions(state["pending_actions"])
    positions_rows_html = positions_table_rows(state["positions"], html=True)
    bench_rows = "".join(f"<tr><td>{n}</td><td>{v if v is not None else 'n/a'} CHF</td></tr>" for n, v in benchmarks.items())
    tx_rows = "".join(render_tx_row(tx) for tx in reversed(state["transactions"]))

    history = state["value_history"]
    chart_labels = json.dumps([h["date"] for h in history])
    chart_portfolio = json.dumps([h.get("portfolio_chf") for h in history])
    chart_smi = json.dumps([h.get("SMI") for h in history])
    chart_sp500 = json.dumps([h.get("S&P 500") for h in history])
    chart_nasdaq = json.dumps([h.get("NASDAQ") for h in history])
    chart_savings = json.dumps([h.get("Sparkonto") for h in history])

    weekend_note = ""
    if weekend:
        weekend_note = """<div class="commentary"><strong>Wochenende</strong> -- Boersen fuer Aktien/ETFs
          sind geschlossen. Nur Krypto-Positionen wurden heute aktualisiert.</div>"""

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Investment Advisory Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 900px;
          margin: 0 auto; padding: 24px 16px; background: #0f172a; color: #e2e8f0; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .updated {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 24px; }}
  .stat-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }}
  .stat {{ background: #1e293b; border-radius: 12px; padding: 16px; flex: 1; min-width: 140px; }}
  .stat .label {{ font-size: 0.8rem; color: #94a3b8; }}
  .stat .value {{ font-size: 1.3rem; font-weight: 600; margin-top: 4px; }}
  .commentary {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 24px; line-height: 1.5; }}
  .chart-box {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 24px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #334155; white-space: nowrap; }}
  th {{ color: #94a3b8; font-weight: 500; }}
  .table-wrap {{ overflow-x: auto; }}
  .disclaimer {{ color: #64748b; font-size: 0.75rem; line-height: 1.4; }}
</style>
</head>
<body>
  <h1>Investment Advisory Dashboard</h1>
  <div class="updated">Letztes Update: {date.today().strftime('%d.%m.%Y')} | Start: {state['start_date']}</div>

  <div class="stat-row">
    <div class="stat"><div class="label">Portfolio-Wert</div><div class="value">{portfolio_total:.2f} CHF</div></div>
    <div class="stat"><div class="label">Gewinn/Verlust</div><div class="value">{profit_chf:+.2f} CHF ({profit_pct:+.2f}%)</div></div>
    <div class="stat"><div class="label">Cash (nicht investiert)</div><div class="value">{state['cash_chf']:.2f} CHF</div></div>
  </div>

  {weekend_note}

  <div class="chart-box">
    <h2>Heutige Aktionen</h2>
    {pending_html}
  </div>

  <div class="chart-box table-wrap">
    <h2>Aktuelle Positionen</h2>
    <table>
      <tr><th>Titel Name</th><th>ISIN</th><th>Stueckzahl</th><th>Wert</th><th>Gewinn/Verlust</th></tr>
      {positions_rows_html}
    </table>
  </div>

  <div class="chart-box">
    <h2>Portfolio vs. Indizes &amp; Sparkonto</h2>
    <canvas id="perfChart" height="260"></canvas>
  </div>

  <div class="chart-box">
    <h2>Aktueller Vergleich (hypothetisch, {BUDGET_CHF} CHF seit {state['start_date']})</h2>
    <table><tr><th>Anlage</th><th>Wert heute</th></tr>{bench_rows}</table>
  </div>

  <div class="chart-box table-wrap">
    <h2>Transaktions-Historie</h2>
    <table>
      <tr><th>Datum</th><th>Aktion</th><th>Ticker</th><th>ISIN</th><th>Valor</th><th>Name</th><th>Klasse</th><th>Stueck</th><th>Gebuehren CHF</th><th>Gewinn CHF</th><th>Gewinn %</th></tr>
      {tx_rows}
    </table>
  </div>

  <p class="disclaimer">
    Keine Anlageberatung, kein echter Handel. Simulation auf Basis oeffentlicher Marktdaten.
    Verkauf/Nachkauf erfolgen erst nach Quittierung via GitHub-Issue. Courtage-, Krypto- und
    FX-Gebuehren sind Swissquote-Richtwerte (Stand 2026). ISIN/Valor nach bestem Wissen hinterlegt,
    vor echten Entscheidungen selbst verifizieren.
  </p>

<script>
  new Chart(document.getElementById('perfChart'), {{
    type: 'line',
    data: {{
      labels: {chart_labels},
      datasets: [
        {{ label: 'Portfolio', data: {chart_portfolio}, borderColor: '#38bdf8', tension: 0.2, pointRadius: 0 }},
        {{ label: 'SMI', data: {chart_smi}, borderColor: '#f472b6', tension: 0.2, pointRadius: 0 }},
        {{ label: 'S&P 500', data: {chart_sp500}, borderColor: '#facc15', tension: 0.2, pointRadius: 0 }},
        {{ label: 'NASDAQ', data: {chart_nasdaq}, borderColor: '#a78bfa', tension: 0.2, pointRadius: 0 }},
        {{ label: 'Sparkonto', data: {chart_savings}, borderColor: '#64748b', borderDash: [4,4], pointRadius: 0 }}
      ]
    }},
    options: {{
      responsive: true,
      scales: {{ x: {{ ticks: {{ color: '#94a3b8', maxTicksLimit: 8 }} }}, y: {{ ticks: {{ color: '#94a3b8' }} }} }},
      plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }}
    }}
  }});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# E-MAIL
# ---------------------------------------------------------------------------

def render_email_html(state, benchmarks, portfolio_total, weekend) -> str:
    profit_chf = portfolio_total - BUDGET_CHF
    profit_pct = profit_chf / BUDGET_CHF * 100
    profit_color = "#16a34a" if profit_chf >= 0 else "#dc2626"

    pending_html = render_pending_actions(state["pending_actions"])
    positions_rows_html = positions_table_rows(state["positions"], html=True)
    bench_rows = "".join(
        f"<tr><td style='padding:4px 10px;'>{n}</td><td style='padding:4px 10px;'>{v if v is not None else 'n/a'} CHF</td></tr>"
        for n, v in benchmarks.items()
    )

    weekend_banner = ""
    if weekend:
        weekend_banner = (
            "<div style='background:#334155;color:#e2e8f0;padding:10px 14px;border-radius:8px;"
            "font-size:0.85rem;margin-bottom:16px;'>Wochenende -- nur Krypto-Positionen aktualisiert.</div>"
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9;margin:0;padding:20px;">
  <div style="max-width:640px;margin:0 auto;background:#0f172a;color:#e2e8f0;border-radius:14px;overflow:hidden;">
    <div style="background:#1e293b;padding:20px 24px;">
      <h1 style="margin:0;font-size:1.3rem;">Investment Advisory -- {date.today().strftime('%d.%m.%Y')}</h1>
      <p style="margin:6px 0 0 0;color:#94a3b8;font-size:0.85rem;">Start: {state['start_date']} | Budget: {BUDGET_CHF} CHF</p>
    </div>
    <div style="padding:20px 24px;">
      {weekend_banner}
      <table style="width:100%;margin-bottom:16px;">
        <tr>
          <td style="padding:10px;background:#1e293b;border-radius:8px;">
            <div style="color:#94a3b8;font-size:0.75rem;">Portfolio-Wert</div>
            <div style="font-size:1.2rem;font-weight:600;">{portfolio_total:.2f} CHF</div>
          </td>
          <td style="width:12px;"></td>
          <td style="padding:10px;background:#1e293b;border-radius:8px;">
            <div style="color:#94a3b8;font-size:0.75rem;">Gewinn/Verlust</div>
            <div style="font-size:1.2rem;font-weight:600;color:{profit_color};">{profit_chf:+.2f} CHF ({profit_pct:+.2f}%)</div>
          </td>
        </tr>
      </table>

      <h2 style="font-size:1rem;margin:18px 0 8px 0;">Heutige Aktionen</h2>
      {pending_html}

      <h2 style="font-size:1rem;margin:18px 0 8px 0;">Aktuelle Positionen</h2>
      <table style="width:100%;border-collapse:collapse;font-size:0.8rem;">
        <tr style="color:#94a3b8;"><th style="text-align:left;padding:6px 10px;">Titel Name</th><th style="text-align:left;padding:6px 10px;">ISIN</th><th style="text-align:left;padding:6px 10px;">Stueckzahl</th><th style="text-align:left;padding:6px 10px;">Wert</th><th style="text-align:left;padding:6px 10px;">Gewinn/Verlust</th></tr>
        {positions_rows_html}
      </table>

      <h2 style="font-size:1rem;margin:18px 0 8px 0;">Vergleich seit Start</h2>
      <table style="width:100%;border-collapse:collapse;font-size:0.8rem;">{bench_rows}</table>

      <div style="margin-top:24px;text-align:center;">
        <a href="{DASHBOARD_URL}" style="display:inline-block;background:#38bdf8;color:#0f172a;
           text-decoration:none;font-weight:600;padding:10px 20px;border-radius:8px;font-size:0.9rem;">
           Volles Dashboard oeffnen
        </a>
      </div>

      <p style="color:#64748b;font-size:0.7rem;line-height:1.4;margin-top:24px;">
        Keine Anlageberatung, kein echter Handel. Verkauf/Nachkauf erst nach Quittierung.
        Simulation inkl. Swissquote-Richtgebuehren, ohne Gewaehr.
      </p>
    </div>
  </div>
</body></html>"""


def send_email(subject: str, html_body: str, text_body: str):
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD nicht gesetzt (als Umgebungsvariablen).")
    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_USER
    msg["To"] = RECIPIENT_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, RECIPIENT_EMAIL, msg.as_string())


# ---------------------------------------------------------------------------
# MAIN (taeglicher Lauf: bewerten, Vorschlaege erzeugen, Mail/Dashboard)
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    today = date.today().isoformat()
    weekend = is_weekend()
    allowed_classes = {"crypto"} if weekend else {"stock", "crypto"}

    universe_tickers = [u["ticker"] for u in CANDIDATE_UNIVERSE]
    held_tickers = [p["ticker"] for p in state["positions"]]
    universe_prices = fetch_prices(universe_tickers + held_tickers)
    fx = fetch_fx_rates()

    mark_to_market(state, universe_prices, fx, allowed_classes)
    detect_pending_actions(state, universe_prices, allowed_classes)

    portfolio_total = portfolio_total_value(state)
    benchmarks = compute_benchmarks(state["start_date"])
    state["value_history"].append({"date": today, "portfolio_chf": round(portfolio_total, 2), **benchmarks})
    save_state(state)

    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    dashboard_html = render_dashboard(state, benchmarks, portfolio_total, weekend)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(dashboard_html)

    profit_chf = portfolio_total - BUDGET_CHF
    profit_pct = profit_chf / BUDGET_CHF * 100
    text_positions = positions_table_rows(state["positions"], html=False)
    text_bench = "\n".join(f"- {n}: {v if v is not None else 'n/a'} CHF" for n, v in benchmarks.items())
    pending_sells = [a for a in state["pending_actions"] if a["type"] == "sell"]
    text_pending = "\n".join(f"- {a['name']} ({a['isin']}): {a.get('profit_pct',0):+.2f}% -- Dashboard zum Quittieren oeffnen" for a in pending_sells) or "Keine offenen Vorschlaege."

    text_body = (
        f"Portfolio-Wert: {portfolio_total:.2f} CHF (Start: {BUDGET_CHF} CHF)\n"
        f"Gewinn/Verlust: {profit_chf:+.2f} CHF ({profit_pct:+.2f}%)\n\n"
        f"Heutige Aktionen:\n{text_pending}\n\n"
        f"Positionen:\n{text_positions}\n\n"
        f"Vergleich seit {state['start_date']}:\n{text_bench}\n\n"
        f"Dashboard: {DASHBOARD_URL}\n\nSimulation, keine Anlageberatung."
    )
    html_body = render_email_html(state, benchmarks, portfolio_total, weekend)

    subject = f"Portfolio Update {today}: {portfolio_total:.0f} CHF ({profit_pct:+.1f}%)"
    send_email(subject, html_body, text_body)
    print("Report versendet, Portfolio aktualisiert.")


if __name__ == "__main__":
    main()
