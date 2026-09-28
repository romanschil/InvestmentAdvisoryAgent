"""
Taeglicher Investment Advisory Agent
=====================================
Screent ein Anlageuniversum anhand von Marktdaten, laesst Claude die Top 3
Titel auswaehlen und begruenden, und fuehrt eine History mit, wie sich diese
Tipps entwickelt haetten (hypothetischer Kauf, kein echter Trade).

Setup:
1. pip install anthropic yfinance
2. Umgebungsvariablen setzen: ANTHROPIC_API_KEY, SMTP_USER, SMTP_PASSWORD
3. Als Cronjob einrichten, z.B. taeglich um 07:00:
   0 7 * * * /usr/bin/python3 /pfad/zu/investment_advisory_agent.py

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

# ---------------------------------------------------------------------------
# KONFIGURATION - hier anpassen
# ---------------------------------------------------------------------------

BUDGET_CHF = 5000       # hypothetisches Startkapital fuer die Simulation
TOP_N = 3                # Anzahl Titel, die taeglich vorgeschlagen werden

# Anlageuniversum: aus diesen Titeln waehlt der Agent aus.
# Beliebig erweiterbar/anpassbar (Aktien, ETFs, Rohstoffe, Indizes ...)
CANDIDATE_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "VOO", "VWCE.DE", "GLD", "NESN.SW", "NOVN.SW", "ASML",
]

RECIPIENT_EMAIL = "roman.schilling@bluewin.ch"

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

RISK_PROFILE = "ausgewogen"
HORIZON = "mittel- bis langfristig"

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "advisory_history.json")


# ---------------------------------------------------------------------------
# 1. MARKTDATEN HOLEN
# ---------------------------------------------------------------------------

def fetch_market_data(tickers: list[str]) -> dict:
    """Holt aktuellen Kurs, Tages- und Monatsveraenderung fuer eine Tickerliste."""
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
# 2. HISTORY / TRACKRECORD
# ---------------------------------------------------------------------------

def load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_history(history: list):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def compute_track_record(history: list) -> str:
    """Berechnet fuer alle bisherigen Picks die Performance bis heute
    (hypothetisch, gleich gewichteter Einsatz von BUDGET_CHF / TOP_N pro Pick)."""
    if not history:
        return "Noch keine frueheren Tipps vorhanden -- Start der History heute."

    all_tickers = sorted({p["ticker"] for entry in history for p in entry["picks"]})
    current = fetch_market_data(all_tickers)

    lines = []
    total_invested = 0.0
    total_now = 0.0
    for entry in history:
        entry_date = entry["date"]
        for pick in entry["picks"]:
            ticker = pick["ticker"]
            entry_price = pick["entry_price"]
            cur = current.get(ticker, {})
            cur_price = cur.get("price")
            if cur_price is None or entry_price in (None, 0):
                continue
            stake = BUDGET_CHF / TOP_N
            shares = stake / entry_price
            now_value = shares * cur_price
            perf_pct = (cur_price - entry_price) / entry_price * 100
            total_invested += stake
            total_now += now_value
            lines.append(
                f"- {entry_date} | {ticker}: Einstieg {entry_price} -> "
                f"aktuell {cur_price} ({perf_pct:+.2f}%)"
            )

    if total_invested > 0:
        overall_pct = (total_now - total_invested) / total_invested * 100
        lines.append(
            f"\nGesamt (alle Picks, je {BUDGET_CHF/TOP_N:.0f} CHF Einsatz): "
            f"{total_invested:.2f} CHF -> {total_now:.2f} CHF ({overall_pct:+.2f}%)"
        )

    return "\n".join(lines)


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
        # Fallback: falls Claude kein valides JSON liefert, roh zurueckgeben
        return {"picks": [], "commentary": text}


# ---------------------------------------------------------------------------
# 4. E-MAIL VERSENDEN
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
    track_record = compute_track_record(history)

    market_data = fetch_market_data(CANDIDATE_UNIVERSE)
    candidates_summary = build_candidates_summary(market_data)

    result = select_top_picks(candidates_summary, track_record)
    picks = result.get("picks", [])
    commentary = result.get("commentary", "")

    # Heutige Picks in History aufnehmen (mit Einstiegskurs von heute)
    today_entry = {"date": date.today().isoformat(), "picks": []}
    pick_lines = []
    for p in picks:
        ticker = p.get("ticker")
        info = market_data.get(ticker, {})
        entry_price = info.get("price")
        today_entry["picks"].append({"ticker": ticker, "entry_price": entry_price})
        pick_lines.append(
            f"- {ticker} (Kurs heute: {entry_price}): {p.get('rationale', '')}"
        )

    if today_entry["picks"]:
        history.append(today_entry)
        save_history(history)

    subject = f"Investment Advisory Report - {date.today().strftime('%d.%m.%Y')}"
    body = (
        f"MARKTKOMMENTAR\n{commentary}\n\n"
        f"TOP {TOP_N} HEUTE (je ca. {BUDGET_CHF/TOP_N:.0f} CHF von {BUDGET_CHF} CHF Budget)\n"
        + "\n".join(pick_lines) +
        f"\n\nTRACKRECORD BISHERIGER TIPPS\n{track_record}\n\n"
        "---\nHinweis: Keine Anlageberatung. Automatisiert generierte Analyse "
        "auf Basis oeffentlicher Marktdaten, keine Kauf-/Verkaufsempfehlung."
    )

    send_email(subject, body)
    print("Report erfolgreich versendet.")


if __name__ == "__main__":
    main()
