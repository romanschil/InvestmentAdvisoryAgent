"""
Taeglicher Investment Advisory Agent
=====================================
Screent ein Anlageuniversum anhand von Marktdaten, laesst Claude die Top 3
Titel auswaehlen und begruenden, fuehrt eine History (hypothetischer Kauf)
und erzeugt ein Web-Dashboard (dashboard/index.html fuer GitHub Pages) mit
Performance-Kurve, S&P-500-Vergleich und Win-Rate. Die E-Mail bleibt kurz
und verlinkt aufs Dashboard.

Setup:
1. pip install anthropic yfinance pandas
2. Umgebungsvariablen setzen: ANTHROPIC_API_KEY, SMTP_USER, SMTP_PASSWORD
3. DASHBOARD_URL unten auf deine echte GitHub-Pages-URL anpassen
4. GitHub Pages aktivieren: Repo -> Settings -> Pages -> Source: "Deploy
   from a branch" -> Branch "main", Ordner "/docs"
5. Als taeglichen Workflow (GitHub Actions) laufen lassen

WICHTIG: Dies ist KEINE Anlageberatung. Der Agent fuehrt keine echten Trades
aus. Alle Ausgaben sind automatisiert generierte Analysen auf Basis
oeffentlicher Marktdaten -- keine Kauf-/Verkaufsempfehlung. Jede
Anlageentscheidung bleibt bei dir.
"""

import os
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date

import anthropic
import yfinance as yf
import pandas as pd

# ---------------------------------------------------------------------------
# KONFIGURATION - hier anpassen
# ---------------------------------------------------------------------------

BUDGET_CHF = 5000
TOP_N = 3

CANDIDATE_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "VOO", "VWCE.DE", "GLD", "NESN.SW", "NOVN.SW", "ASML",
]

BENCHMARK_TICKER = "^GSPC"  # S&P 500 zum Vergleich

RECIPIENT_EMAIL = "roman.schilling@bluewin.ch"

# WICHTIG: nach Aktivieren von GitHub Pages auf die echte URL anpassen
# Format: https://<dein-github-username>.github.io/<repo-name>/
DASHBOARD_URL = "https://romanschil.github.io/InvestmentAdvisoryAgent/"

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

RISK_PROFILE = "ausgewogen"
HORIZON = "mittel- bis langfristig"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "advisory_history.json")
DASHBOARD_DIR = os.path.join(BASE_DIR, "docs")
DASHBOARD_FILE = os.path.join(DASHBOARD_DIR, "index.html")


# ---------------------------------------------------------------------------
# 1. MARKTDATEN HOLEN
# ---------------------------------------------------------------------------

def fetch_market_data(tickers: list[str]) -> dict:
    data = {}
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="1mo")
            if hist.empty:
                continue
            last_close = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else last_close
            month_ago = float(hist["Close"].iloc[0])
            data[ticker] = {
                "price": round(last_close, 2),
                "change_1d_pct": round((last_close - prev_close) / prev_close * 100, 2),
                "change_1mo_pct": round((last_close - month_ago) / month_ago * 100, 2),
            }
        except Exception as e:
            data[ticker] = {"error": str(e)}
    return data


def build_candidates_summary(market_data: dict) -> str:
    lines = []
    for ticker, info in market_data.items():
        if "error" in info:
            continue
        lines.append(
            f"- {ticker}: Kurs {info['price']}, "
            f"1 Tag {info['change_1d_pct']:+.2f}%, "
            f"1 Monat {info['change_1mo_pct']:+.2f}%"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. HISTORY
# ---------------------------------------------------------------------------

def load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_history(history: list):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def compute_track_record_text(history: list) -> str:
    if not history:
        return "Noch keine frueheren Tipps vorhanden -- Start der History heute."

    all_tickers = sorted({p["ticker"] for entry in history for p in entry["picks"]})
    current = fetch_market_data(all_tickers)

    lines = []
    total_invested = 0.0
    total_now = 0.0
    for entry in history:
        for pick in entry["picks"]:
            ticker = pick["ticker"]
            entry_price = pick["entry_price"]
            cur_price = current.get(ticker, {}).get("price")
            if cur_price is None or not entry_price:
                continue
            stake = BUDGET_CHF / TOP_N
            shares = stake / entry_price
            now_value = shares * cur_price
            perf_pct = (cur_price - entry_price) / entry_price * 100
            total_invested += stake
            total_now += now_value
            lines.append(f"- {entry['date']} | {ticker}: {entry_price} -> {cur_price} ({perf_pct:+.2f}%)")

    if total_invested > 0:
        overall_pct = (total_now - total_invested) / total_invested * 100
        lines.append(f"\nGesamt: {total_invested:.2f} CHF -> {total_now:.2f} CHF ({overall_pct:+.2f}%)")

    return "\n".join(lines)


def compute_win_rate(history: list):
    all_tickers = sorted({p["ticker"] for entry in history for p in entry["picks"]})
    if not all_tickers:
        return None
    current = fetch_market_data(all_tickers)
    wins, total = 0, 0
    for entry in history:
        for pick in entry["picks"]:
            cur_price = current.get(pick["ticker"], {}).get("price")
            if cur_price is None or not pick["entry_price"]:
                continue
            total += 1
            if cur_price > pick["entry_price"]:
                wins += 1
    if total == 0:
        return None
    return {"wins": wins, "total": total, "pct": round(wins / total * 100, 1)}


def build_performance_series(history: list):
    """Baut eine taegliche Zeitreihe: Portfolio-Wert (alle Picks, gleich
    gewichtet, gehalten seit Kaufdatum) vs. Benchmark (S&P 500), simuliert
    mit denselben Einzahlungsbetraegen an denselben Tagen."""
    if not history:
        return None

    first_date = min(entry["date"] for entry in history)
    all_tickers = sorted({p["ticker"] for entry in history for p in entry["picks"]})

    try:
        raw = yf.download(all_tickers, start=first_date, progress=False)["Close"]
    except Exception:
        return None
    if isinstance(raw, pd.Series):
        raw = raw.to_frame(name=all_tickers[0])
    prices = raw.ffill()

    try:
        bench_raw = yf.download(BENCHMARK_TICKER, start=first_date, progress=False)["Close"]
        if isinstance(bench_raw, pd.DataFrame):
            bench_raw = bench_raw.iloc[:, 0]  # auf Series reduzieren, falls DataFrame zurueckkommt
    except Exception:
        bench_raw = None
    if bench_raw is not None:
        bench = bench_raw.reindex(prices.index).ffill()
    else:
        bench = None

    stake = BUDGET_CHF / TOP_N
    dates_out, portfolio_out, benchmark_out, invested_out = [], [], [], []

    bench_shares = 0.0
    invested_so_far = 0.0

    for current_date in prices.index:
        date_str = current_date.strftime("%Y-%m-%d")

        # neu investierte Betraege an diesem Tag (fuer Benchmark-Simulation)
        newly_invested = sum(
            stake * len(entry["picks"]) for entry in history if entry["date"] == date_str
        )
        invested_so_far += newly_invested

        # Portfolio-Wert: Summe aller bisherigen Picks zu heutigen Kursen
        total = 0.0
        for entry in history:
            if entry["date"] > date_str:
                continue
            for pick in entry["picks"]:
                ticker = pick["ticker"]
                entry_price = pick["entry_price"]
                if not entry_price or ticker not in prices.columns:
                    continue
                price_now = prices.loc[current_date, ticker]
                if pd.isna(price_now):
                    continue
                shares = stake / entry_price
                total += shares * price_now

        # Benchmark-Wert: gleiche Einzahlungen, aber in S&P 500 investiert
        bench_price = bench.loc[current_date] if bench is not None else None
        if newly_invested > 0 and bench_price is not None and not pd.isna(bench_price):
            bench_shares += newly_invested / float(bench_price)
        bench_value = bench_shares * float(bench_price) if bench_price is not None and not pd.isna(bench_price) else None

        dates_out.append(date_str)
        portfolio_out.append(round(total, 2))
        benchmark_out.append(round(bench_value, 2) if bench_value is not None else None)
        invested_out.append(round(invested_so_far, 2))

    return {"dates": dates_out, "portfolio": portfolio_out, "benchmark": benchmark_out, "invested": invested_out}


# ---------------------------------------------------------------------------
# 3. CLAUDE: TOP 3 AUSWAHL
# ---------------------------------------------------------------------------

def select_top_picks(candidates_summary: str, track_record: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt = f"""Du bist ein nuechterner Investment-Analyse-Assistent.
Anlageprofil: {RISK_PROFILE}, Horizont: {HORIZON}.
Hypothetisches Budget: {BUDGET_CHF} CHF.

Waehle aus dem gegebenen Anlageuniversum genau {TOP_N} Titel aus, die aus
Sicht der Marktdaten aktuell am interessantesten erscheinen. Waehle NUR aus
den gelisteten Tickern, erfinde keine neuen.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt, keine Einleitung, keine
Markdown-Codebloecke, kein Text davor oder danach. Format:
{{
  "picks": [
    {{"ticker": "XXX", "rationale": "kurze Begruendung auf Deutsch, 1-2 Saetze"}}
  ],
  "commentary": "kurzer genereller Marktkommentar auf Deutsch, max. 4 Saetze"
}}"""

    user_prompt = f"""Anlageuniversum mit aktuellen Marktdaten:
{candidates_summary}

Bisherige Trackrecord (frueherer Tipps, hypothetisch):
{track_record}

Waehle die Top {TOP_N} Titel und antworte im vorgegebenen JSON-Format."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = "".join(block.text for block in response.content if block.type == "text")
    clean = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return {"picks": [], "commentary": text}


# ---------------------------------------------------------------------------
# 4. DASHBOARD (HTML fuer GitHub Pages)
# ---------------------------------------------------------------------------

def render_dashboard(picks_today, commentary, win_rate, perf_series, market_data):
    win_rate_html = (
        f"{win_rate['wins']} von {win_rate['total']} Picks im Plus ({win_rate['pct']}%)"
        if win_rate else "Noch keine Daten"
    )

    picks_html = "".join(
        f"""<div class="card">
              <h3>{p.get('ticker')}</h3>
              <p class="price">Kurs heute: {market_data.get(p.get('ticker'), {}).get('price', 'n/a')}</p>
              <p>{p.get('rationale', '')}</p>
            </div>"""
        for p in picks_today
    )

    if perf_series:
        chart_labels = json.dumps(perf_series["dates"])
        chart_portfolio = json.dumps(perf_series["portfolio"])
        chart_benchmark = json.dumps(perf_series["benchmark"])
        chart_invested = json.dumps(perf_series["invested"])
    else:
        chart_labels = chart_portfolio = chart_benchmark = chart_invested = "[]"

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
  .card h3 {{ margin: 0 0 8px 0; color: #38bdf8; }}
  .card .price {{ color: #94a3b8; font-size: 0.85rem; }}
  .commentary {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 24px; line-height: 1.5; }}
  .chart-box {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 24px; }}
  .disclaimer {{ color: #64748b; font-size: 0.75rem; line-height: 1.4; }}
</style>
</head>
<body>
  <h1>Investment Advisory Dashboard</h1>
  <div class="updated">Letztes Update: {date.today().strftime('%d.%m.%Y')}</div>

  <div class="stat-row">
    <div class="stat"><div class="label">Budget (hypothetisch)</div><div class="value">{BUDGET_CHF} CHF</div></div>
    <div class="stat"><div class="label">Win-Rate</div><div class="value">{win_rate_html}</div></div>
  </div>

  <div class="commentary"><strong>Marktkommentar</strong><br>{commentary}</div>

  <h2>Top {TOP_N} heute</h2>
  <div class="cards">{picks_html}</div>

  <div class="chart-box">
    <h2>Performance vs. S&amp;P 500</h2>
    <canvas id="perfChart" height="260"></canvas>
  </div>

  <p class="disclaimer">
    Keine Anlageberatung. Automatisiert generierte Analyse auf Basis oeffentlicher
    Marktdaten. Die Performance-Kurve ist eine Simulation (hypothetischer Kauf zu
    Empfehlungskurs, gleich gewichtet, keine Gebuehren/Steuern beruecksichtigt) und
    keine Garantie fuer zukuenftige Ergebnisse.
  </p>

<script>
  const labels = {chart_labels};
  const portfolio = {chart_portfolio};
  const benchmark = {chart_benchmark};
  const invested = {chart_invested};

  new Chart(document.getElementById('perfChart'), {{
    type: 'line',
    data: {{
      labels: labels,
      datasets: [
        {{ label: 'Agent-Portfolio', data: portfolio, borderColor: '#38bdf8', tension: 0.2, pointRadius: 0 }},
        {{ label: 'S&P 500 (gleiche Einzahlungen)', data: benchmark, borderColor: '#f472b6', tension: 0.2, pointRadius: 0 }},
        {{ label: 'Eingezahlt', data: invested, borderColor: '#64748b', borderDash: [4,4], tension: 0, pointRadius: 0 }}
      ]
    }},
    options: {{
      responsive: true,
      scales: {{
        x: {{ ticks: {{ color: '#94a3b8', maxTicksLimit: 8 }} }},
        y: {{ ticks: {{ color: '#94a3b8' }} }}
      }},
      plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }}
    }}
  }});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 5. E-MAIL (kurz, mit Link zum Dashboard)
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
    history = load_history()
    track_record = compute_track_record_text(history)

    market_data = fetch_market_data(CANDIDATE_UNIVERSE)
    candidates_summary = build_candidates_summary(market_data)

    result = select_top_picks(candidates_summary, track_record)
    picks = result.get("picks", [])
    commentary = result.get("commentary", "")

    today_entry = {"date": date.today().isoformat(), "picks": []}
    for p in picks:
        ticker = p.get("ticker")
        entry_price = market_data.get(ticker, {}).get("price")
        today_entry["picks"].append({"ticker": ticker, "entry_price": entry_price})

    if today_entry["picks"]:
        history.append(today_entry)
        save_history(history)

    win_rate = compute_win_rate(history)
    perf_series = build_performance_series(history)

    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    dashboard_html = render_dashboard(picks, commentary, win_rate, perf_series, market_data)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(dashboard_html)

    pick_names = ", ".join(p.get("ticker", "?") for p in picks)
    subject = f"Investment Advisory - {date.today().strftime('%d.%m.%Y')}: {pick_names}"
    body = (
        f"Heutige Top {TOP_N}: {pick_names}\n\n"
        f"{commentary}\n\n"
        f"Volles Dashboard mit Charts, Trackrecord und S&P-500-Vergleich:\n{DASHBOARD_URL}\n\n"
        "---\nHinweis: Keine Anlageberatung. Automatisiert generierte Analyse, keine Kauf-/Verkaufsempfehlung."
    )

    send_email(subject, body)
    print("Report erfolgreich versendet, Dashboard aktualisiert.")


if __name__ == "__main__":
    main()
