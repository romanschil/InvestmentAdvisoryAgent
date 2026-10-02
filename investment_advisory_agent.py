"""
Taeglicher Investment Advisory Agent -- Portfolio-Modus
=========================================================
Haelt ein fiktives Portfolio in CHF (Start: BUDGET_CHF), investiert gleich
gewichtet in TOP_N Titel aus einem kuratierten, einfachen Anlageuniversum
(keine Hebelprodukte, keine strukturierten Produkte, keine Derivate).

Taeglicher Ablauf:
1. Bestehende Positionen zu aktuellen Kursen bewerten (CHF)
2. Falls eine Position seit Kauf >= SELL_THRESHOLD_PCT im Plus liegt: fiktiv
   verkaufen, Gewinn verbuchen, mit dem freien Cash sofort einen neuen Pick
   (via Claude) fiktiv nachkaufen
3. Performance-Vergleich: Portfolio vs. SMI, S&P 500, NASDAQ, Sparkonto-Zins
4. Kurzmail mit Zusammenfassung + Link zum Dashboard (docs/index.html)
5. Dashboard mit Positionen (inkl. ISIN), Verlauf-Chart, Transaktions-Historie

WICHTIG: Dies ist KEINE Anlageberatung und kein echter Handel. Alles ist
eine Simulation auf Basis oeffentlicher Marktdaten. ISINs im Universum sind
nach bestem Wissen hinterlegt, aber nicht garantiert fehlerfrei -- vor
echten Entscheidungen immer selbst verifizieren.
"""

import os
import json
import smtplib
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
SELL_THRESHOLD_PCT = 20.0          # ab diesem Gewinn wird fiktiv verkauft
SAVINGS_ANNUAL_RATE_PCT = 0.75     # Vergleichs-Zinssatz Sparkonto, anpassbar

# Kuratiertes Anlageuniversum -- NUR einfache, liquide Aktien/ETFs.
# Keine Hebel-/Inverse-ETFs, keine Derivate, keine strukturierten Produkte.
# ISINs nach bestem Wissen hinterlegt -- vor Echtgeld-Nutzung verifizieren.
CANDIDATE_UNIVERSE = [
    {"ticker": "AAPL",    "isin": "US0378331005", "name": "Apple Inc.",            "currency": "USD"},
    {"ticker": "MSFT",    "isin": "US5949181045", "name": "Microsoft Corp.",       "currency": "USD"},
    {"ticker": "GOOGL",   "isin": "US02079K3059", "name": "Alphabet Inc. (A)",     "currency": "USD"},
    {"ticker": "AMZN",    "isin": "US0231351067", "name": "Amazon.com Inc.",       "currency": "USD"},
    {"ticker": "NVDA",    "isin": "US67066G1040", "name": "NVIDIA Corp.",          "currency": "USD"},
    {"ticker": "META",    "isin": "US30303M1027", "name": "Meta Platforms Inc.",   "currency": "USD"},
    {"ticker": "NESN.SW", "isin": "CH0038863350", "name": "Nestle SA",             "currency": "CHF"},
    {"ticker": "NOVN.SW", "isin": "CH0012005267", "name": "Novartis AG",           "currency": "CHF"},
    {"ticker": "ROG.SW",  "isin": "CH0012032048", "name": "Roche Holding AG",      "currency": "CHF"},
    {"ticker": "UHR.SW",  "isin": "CH0012255151", "name": "Swatch Group AG",       "currency": "CHF"},
    {"ticker": "VOO",     "isin": "US9229083632", "name": "Vanguard S&P 500 ETF",  "currency": "USD"},
    {"ticker": "VWCE.DE", "isin": "IE00BK5BQT80", "name": "Vanguard FTSE All-World ETF", "currency": "EUR"},
]
UNIVERSE_BY_TICKER = {u["ticker"]: u for u in CANDIDATE_UNIVERSE}

BENCHMARKS = {
    "SMI": "^SSMI",
    "S&P 500": "^GSPC",
    "NASDAQ": "^IXIC",
}

RECIPIENT_EMAIL = "roman.schilling@bluewin.ch"
DASHBOARD_URL = "https://romanschil.github.io/InvestmentAdvisoryAgent/"

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


# ---------------------------------------------------------------------------
# MARKTDATEN / FX
# ---------------------------------------------------------------------------

def fetch_prices(tickers: list[str]) -> dict:
    """Aktueller Kurs je Ticker in Originalwaehrung."""
    prices = {}
    for ticker in sorted(set(tickers)):
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if not hist.empty:
                prices[ticker] = float(hist["Close"].iloc[-1])
        except Exception:
            pass
    return prices


def fetch_fx_rates() -> dict:
    """Umrechnungskurse Fremdwaehrung -> CHF."""
    rates = {"CHF": 1.0}
    pairs = {"USD": "USDCHF=X", "EUR": "EURCHF=X"}
    for cur, pair in pairs.items():
        try:
            hist = yf.Ticker(pair).history(period="5d")
            rates[cur] = float(hist["Close"].iloc[-1]) if not hist.empty else None
        except Exception:
            rates[cur] = None
    return rates


def fetch_index_return_pct(ticker: str, start_date: str) -> float | None:
    """Prozentuale Kursveraenderung eines Index seit start_date (eigene Waehrung,
    FX-Effekte nicht beruecksichtigt -- reiner Preisvergleich)."""
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
            "positions": [],       # {ticker, isin, name, currency, shares, buy_price_native, buy_price_chf, buy_date}
            "transactions": [],    # {date, action, ticker, isin, name, shares, price_chf, profit_chf, profit_pct}
            "value_history": [],   # {date, portfolio_chf, SMI, S&P 500, NASDAQ, savings_chf}
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLAUDE: NEUE PICKS AUSWAEHLEN
# ---------------------------------------------------------------------------

def select_new_picks(needed: int, exclude_tickers: set, universe_prices: dict) -> list:
    """Laesst Claude 'needed' Titel aus dem Universum (ohne exclude_tickers)
    auswaehlen. Gibt eine Liste von {ticker, rationale} zurueck."""
    available = [u for u in CANDIDATE_UNIVERSE if u["ticker"] not in exclude_tickers]
    if not available:
        return []

    lines = []
    for u in available:
        p = universe_prices.get(u["ticker"])
        if p is None:
            continue
        lines.append(f"- {u['ticker']} ({u['name']}, {u['currency']}): Kurs {round(p, 2)}")
    candidates_summary = "\n".join(lines)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_prompt = f"""Du bist ein nuechterner Investment-Analyse-Assistent.
Anlageprofil: {RISK_PROFILE}, Horizont: {HORIZON}. Es geht um einfache,
unkomplizierte Aktien/ETFs -- keine Hebelprodukte, keine Derivate.

Waehle aus dem gegebenen Universum genau {needed} Titel aus, die aus Sicht
der Marktdaten aktuell am interessantesten erscheinen. Waehle NUR aus den
gelisteten Tickern, erfinde keine neuen.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt, keine Einleitung, keine
Markdown-Codebloecke. Format:
{{"picks": [{{"ticker": "XXX", "rationale": "kurze Begruendung auf Deutsch, 1-2 Saetze"}}]}}"""

    user_prompt = f"Verfuegbares Anlageuniversum:\n{candidates_summary}\n\nWaehle {needed} Titel."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
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

def mark_to_market(state: dict, prices: dict, fx: dict):
    """Berechnet aktuellen CHF-Wert und Gewinn/Verlust je Position."""
    for pos in state["positions"]:
        price_native = prices.get(pos["ticker"])
        rate = fx.get(pos["currency"], 1.0)
        if price_native is not None and rate is not None:
            value_chf = pos["shares"] * price_native * rate
            buy_value_chf = pos["shares"] * pos["buy_price_chf"]
            pos["current_value_chf"] = round(value_chf, 2)
            pos["profit_chf"] = round(value_chf - buy_value_chf, 2)
            pos["profit_pct"] = round((value_chf - buy_value_chf) / buy_value_chf * 100, 2)
        else:
            pos["current_value_chf"] = None
            pos["profit_chf"] = None
            pos["profit_pct"] = None


def process_sells_and_buys(state: dict, universe_prices: dict, fx: dict) -> dict:
    """Verkauft Positionen >= SELL_THRESHOLD_PCT, kauft danach (und beim
    allerersten Lauf) wieder bis TOP_N Positionen voll sind. Gibt eine
    Zusammenfassung der heutigen Aktionen zurueck."""
    today = date.today().isoformat()
    sold, bought = [], []

    still_open = []
    for pos in state["positions"]:
        if pos.get("profit_pct") is not None and pos["profit_pct"] >= SELL_THRESHOLD_PCT:
            proceeds = pos["current_value_chf"]
            state["cash_chf"] += proceeds
            tx = {
                "date": today, "action": "sell", "ticker": pos["ticker"],
                "isin": pos["isin"], "name": pos["name"], "shares": pos["shares"],
                "price_chf": round(proceeds / pos["shares"], 2),
                "profit_chf": pos["profit_chf"], "profit_pct": pos["profit_pct"],
            }
            state["transactions"].append(tx)
            sold.append(tx)
        else:
            still_open.append(pos)
    state["positions"] = still_open

    needed = TOP_N - len(state["positions"])
    if needed > 0 and state["cash_chf"] > 0:
        held_tickers = {p["ticker"] for p in state["positions"]}
        picks = select_new_picks(needed, held_tickers, universe_prices)
        if picks:
            stake = state["cash_chf"] / len(picks)
            for p in picks:
                ticker = p.get("ticker")
                meta = UNIVERSE_BY_TICKER.get(ticker)
                price_native = universe_prices.get(ticker)
                if not meta or not price_native:
                    continue
                rate = fx.get(meta["currency"], 1.0) or 1.0
                price_chf = price_native * rate
                shares = stake / price_chf
                new_pos = {
                    "ticker": ticker, "isin": meta["isin"], "name": meta["name"],
                    "currency": meta["currency"], "shares": round(shares, 6),
                    "buy_price_native": round(price_native, 2),
                    "buy_price_chf": round(price_chf, 4),
                    "buy_date": today, "rationale": p.get("rationale", ""),
                }
                state["positions"].append(new_pos)
                state["cash_chf"] -= stake
                tx = {
                    "date": today, "action": "buy", "ticker": ticker,
                    "isin": meta["isin"], "name": meta["name"],
                    "shares": round(shares, 6), "price_chf": round(price_chf, 2),
                    "rationale": p.get("rationale", ""),
                }
                state["transactions"].append(tx)
                bought.append(tx)
            state["cash_chf"] = round(state["cash_chf"], 2)

    return {"sold": sold, "bought": bought}


def compute_benchmarks(start_date: str) -> dict:
    """Hypothetischer Wert von BUDGET_CHF, waere es am start_date in SMI,
    S&P 500, NASDAQ oder zum Sparzins angelegt worden (reine Preis-Rendite,
    FX-Effekte bei Fremdwaehrungs-Indizes nicht beruecksichtigt)."""
    results = {}
    for name, ticker in BENCHMARKS.items():
        ret_pct = fetch_index_return_pct(ticker, start_date)
        results[name] = round(BUDGET_CHF * (1 + ret_pct / 100), 2) if ret_pct is not None else None

    days_elapsed = (date.today() - date.fromisoformat(start_date)).days
    savings_value = BUDGET_CHF * (1 + SAVINGS_ANNUAL_RATE_PCT / 100 * days_elapsed / 365)
    results["Sparkonto"] = round(savings_value, 2)
    return results


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------

def render_dashboard(state: dict, benchmarks: dict, today_actions: dict, portfolio_total: float) -> str:
    profit_chf = portfolio_total - BUDGET_CHF
    profit_pct = profit_chf / BUDGET_CHF * 100

    position_cards = "".join(f"""
        <div class="card">
          <h3>{p['ticker']} <span class="isin">{p['isin']}</span></h3>
          <p class="name">{p['name']}</p>
          <p>Wert: {p.get('current_value_chf', 'n/a')} CHF
             ({(p.get('profit_pct') or 0):+.2f}%)</p>
          <p class="rationale">{p.get('rationale', '')}</p>
        </div>""" for p in state["positions"])

    bench_rows = "".join(
        f"<tr><td>{name}</td><td>{val if val is not None else 'n/a'} CHF</td></tr>"
        for name, val in benchmarks.items()
    )

    tx_rows = "".join(
        f"""<tr>
              <td>{tx['date']}</td><td>{tx['action'].upper()}</td>
              <td>{tx['ticker']}</td><td>{tx.get('isin','')}</td><td>{tx.get('name','')}</td>
              <td>{tx.get('profit_chf', '-') if tx['action']=='sell' else '-'}</td>
              <td>{f"{tx.get('profit_pct',0):+.2f}%" if tx['action']=='sell' else '-'}</td>
            </tr>"""
        for tx in reversed(state["transactions"])
    )

    history = state["value_history"]
    chart_labels = json.dumps([h["date"] for h in history])
    chart_portfolio = json.dumps([h.get("portfolio_chf") for h in history])
    chart_smi = json.dumps([h.get("SMI") for h in history])
    chart_sp500 = json.dumps([h.get("S&P 500") for h in history])
    chart_nasdaq = json.dumps([h.get("NASDAQ") for h in history])
    chart_savings = json.dumps([h.get("Sparkonto") for h in history])

    sold_html = "".join(
        f"<li>{tx['ticker']} ({tx['isin']}, {tx['name']}): verkauft, Gewinn {tx['profit_chf']} CHF ({tx['profit_pct']:+.2f}%)</li>"
        for tx in today_actions["sold"]
    )
    bought_html = "".join(
        f"<li>{tx['ticker']} ({tx['isin']}, {tx['name']}): neu gekauft</li>"
        for tx in today_actions["bought"]
    )
    actions_block = ""
    if sold_html or bought_html:
        actions_block = f"""<div class="commentary">
          <strong>Heutige Aktionen</strong>
          <ul>{sold_html}{bought_html}</ul>
        </div>"""

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
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 16px; flex: 1; min-width: 220px; }}
  .card h3 {{ margin: 0 0 4px 0; color: #38bdf8; }}
  .card .isin {{ color: #64748b; font-size: 0.75rem; font-weight: normal; }}
  .card .name {{ color: #94a3b8; font-size: 0.85rem; margin: 0 0 8px 0; }}
  .card .rationale {{ font-size: 0.85rem; color: #cbd5e1; }}
  .commentary {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 24px; line-height: 1.5; }}
  .chart-box {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 24px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #334155; }}
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

  {actions_block}

  <h2>Aktuelle Positionen</h2>
  <div class="cards">{position_cards}</div>

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
      <tr><th>Datum</th><th>Aktion</th><th>Ticker</th><th>ISIN</th><th>Name</th><th>Gewinn CHF</th><th>Gewinn %</th></tr>
      {tx_rows}
    </table>
  </div>

  <p class="disclaimer">
    Keine Anlageberatung, kein echter Handel. Simulation auf Basis oeffentlicher
    Marktdaten. Index-Vergleiche sind reine Preis-Renditen (ohne Dividenden,
    ohne FX-Effekte bei Fremdwaehrungs-Indizes). ISINs nach bestem Wissen
    hinterlegt, vor echten Entscheidungen selbst verifizieren.
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

def send_email(subject: str, body: str):
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD nicht gesetzt (als Umgebungsvariablen).")
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = RECIPIENT_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, RECIPIENT_EMAIL, msg.as_string())


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    today = date.today().isoformat()

    universe_tickers = [u["ticker"] for u in CANDIDATE_UNIVERSE]
    held_tickers = [p["ticker"] for p in state["positions"]]
    universe_prices = fetch_prices(universe_tickers + held_tickers)
    fx = fetch_fx_rates()

    mark_to_market(state, universe_prices, fx)
    today_actions = process_sells_and_buys(state, universe_prices, fx)
    mark_to_market(state, universe_prices, fx)  # neue Positionen auch bewerten

    portfolio_total = state["cash_chf"] + sum(p.get("current_value_chf") or 0 for p in state["positions"])
    benchmarks = compute_benchmarks(state["start_date"])

    state["value_history"].append({
        "date": today, "portfolio_chf": round(portfolio_total, 2), **benchmarks
    })
    save_state(state)

    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    dashboard_html = render_dashboard(state, benchmarks, today_actions, portfolio_total)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(dashboard_html)

    profit_chf = portfolio_total - BUDGET_CHF
    profit_pct = profit_chf / BUDGET_CHF * 100

    action_lines = []
    for tx in today_actions["sold"]:
        action_lines.append(f"VERKAUFT: {tx['ticker']} ({tx['isin']}, {tx['name']}) -- Gewinn {tx['profit_chf']} CHF ({tx['profit_pct']:+.2f}%)")
    for tx in today_actions["bought"]:
        action_lines.append(f"GEKAUFT: {tx['ticker']} ({tx['isin']}, {tx['name']})")
    actions_text = "\n".join(action_lines) if action_lines else "Keine Transaktionen heute (alle Positionen unter +{:.0f}% Schwelle).".format(SELL_THRESHOLD_PCT)

    bench_lines = "\n".join(
        f"- {name}: {val if val is not None else 'n/a'} CHF" for name, val in benchmarks.items()
    )

    subject = f"Portfolio Update {today}: {portfolio_total:.0f} CHF ({profit_pct:+.1f}%)"
    body = (
        f"Portfolio-Wert: {portfolio_total:.2f} CHF (Start: {BUDGET_CHF} CHF)\n"
        f"Gewinn/Verlust: {profit_chf:+.2f} CHF ({profit_pct:+.2f}%)\n\n"
        f"Vergleich (gleicher Betrag seit {state['start_date']}):\n{bench_lines}\n\n"
        f"Aktionen heute:\n{actions_text}\n\n"
        f"Volles Dashboard mit Positionen, ISINs und Historie:\n{DASHBOARD_URL}\n\n"
        "---\nKeine Anlageberatung, kein echter Handel. Simulation auf Basis oeffentlicher Marktdaten."
    )

    send_email(subject, body)
    print("Report versendet, Portfolio aktualisiert.")


if __name__ == "__main__":
    main()
