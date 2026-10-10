"""
Wird vom Workflow .github/workflows/confirm-action.yml aufgerufen, sobald ein
per Dashboard/Mail geoeffnetes "Quittieren"-Issue erstellt wird (Titel-Format
"CONFIRM:sell:<id>" oder "CONFIRM:buy:<id>").

Fuehrt die entsprechende Verkaufs- oder Kauf-Aktion wirklich aus, aktualisiert
portfolio_state.json und docs/index.html. Bei einem Kauf-Issue, dessen
zugehoeriger Verkauf noch nicht quittiert wurde, wird der Kauf nur vorgemerkt
(status "confirm_requested") und erst ausgefuehrt, sobald der Verkauf spaeter
quittiert wird (dann kaskadiert dieser Lauf automatisch weiter).
"""

import os
import re
import sys

import investment_advisory_agent as agent


def main():
    title = os.environ.get("ISSUE_TITLE", "").strip()

    if title == "RESTART":
        state = agent.restart_portfolio()
        print(f"Portfolio zurueckgesetzt. Neue Positionen: {[p['ticker'] for p in state['positions']]}")
        portfolio_total = agent.portfolio_total_value(state)
        benchmarks = agent.compute_benchmarks(state["start_date"])
        os.makedirs(agent.DASHBOARD_DIR, exist_ok=True)
        html = agent.render_dashboard(state, benchmarks, portfolio_total, agent.is_weekend())
        with open(agent.DASHBOARD_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        print("Fertig.")
        return

    match = re.match(r"CONFIRM:(sell|buy):([0-9a-f]+)", title)
    if not match:
        print(f"Kein gueltiger Confirm-/Restart-Titel ('{title}'), breche ab.")
        sys.exit(0)
    action_type, action_id = match.group(1), match.group(2)

    state = agent.load_state()
    action = next(
        (a for a in state["pending_actions"] if a["id"] == action_id and a["type"] == action_type),
        None,
    )
    if not action:
        print("Aktion nicht gefunden -- evtl. bereits verarbeitet oder abgelaufen.")
        return

    # Aktuelle Kurse/FX fuer alle betroffenen Ticker holen (inkl. aller noch
    # offenen Vorschlaege, falls eine Kaskade ausgeloest wird)
    relevant_tickers = {p["ticker"] for p in state["positions"]}
    relevant_tickers |= {a["ticker"] for a in state["pending_actions"]}
    prices = agent.fetch_prices(list(relevant_tickers))
    fx = agent.fetch_fx_rates()

    if action_type == "sell":
        tx = agent.execute_sell_action(state, action, prices, fx)
        print(f"Verkauf ausgefuehrt: {tx}" if tx else "Verkauf konnte nicht ausgefuehrt werden.")

        # Kaskade: wartet ein bereits quittierter Kauf auf genau diesen Verkauf?
        waiting_buy = next(
            (a for a in state["pending_actions"]
             if a["type"] == "buy" and a.get("linked_sell_id") == action_id and a.get("status") == "confirm_requested"),
            None,
        )
        if waiting_buy:
            tx2 = agent.execute_buy_action(state, waiting_buy, prices, fx)
            print(f"Nachkauf (kaskadiert) ausgefuehrt: {tx2}" if tx2 else "Nachkauf konnte nicht ausgefuehrt werden.")

    else:  # buy
        linked_sell_still_open = any(
            a["id"] == action.get("linked_sell_id") for a in state["pending_actions"] if a["type"] == "sell"
        )
        if linked_sell_still_open:
            action["status"] = "confirm_requested"
            print("Verkauf noch nicht quittiert -- Kauf wird vorgemerkt und bei Verkaufsbestaetigung automatisch ausgefuehrt.")
        else:
            tx = agent.execute_buy_action(state, action, prices, fx)
            print(f"Kauf ausgefuehrt: {tx}" if tx else "Kauf konnte nicht ausgefuehrt werden.")

    agent.save_state(state)

    # Dashboard neu rendern, damit die Aenderung sofort sichtbar ist
    portfolio_total = agent.portfolio_total_value(state)
    benchmarks = agent.compute_benchmarks(state["start_date"])
    os.makedirs(agent.DASHBOARD_DIR, exist_ok=True)
    html = agent.render_dashboard(state, benchmarks, portfolio_total, agent.is_weekend())
    with open(agent.DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print("Fertig.")


if __name__ == "__main__":
    main()
