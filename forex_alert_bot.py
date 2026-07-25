#!/usr/bin/env python3
"""
Bot d'alertes Forex -> Telegram (version GitHub Actions)
==========================================================

Ce script vérifie une fois les paires forex configurées (via l'API gratuite
Twelve Data), calcule deux indicateurs techniques (croisement EMA 9/21 + RSI 14),
et envoie une alerte Telegram si un signal est présent. GitHub Actions relance
ce script tout seul toutes les 15 minutes -> pas de boucle infinie, pas de
serveur à garder allumé.

CE QUE CE BOT NE FAIT PAS :
- Il n'exécute AUCUN trade. Il ne se connecte pas à Pocket Option (impossible,
  pas d'API publique). C'est un outil d'information, pas d'automatisation.
- Il ne garantit AUCUN gain. Les indicateurs techniques donnent des signaux
  avec retard et un fort taux de faux positifs, surtout sur des échéances
  courtes type options binaires.

MISE EN PLACE SUR GITHUB (tout se fait depuis Safari, aucune édition de code) :
1. Crée un compte gratuit sur https://github.com
2. Crée un nouveau repo (peut être privé)
3. Ajoute ce fichier tel quel (forex_alert_bot.py) à la racine du repo
4. Ajoute le fichier .github/workflows/forex-alerts.yml (fourni séparément)
5. Va dans Settings -> Secrets and variables -> Actions -> New repository secret,
   et crée ces 3 secrets (colle juste la valeur dans le formulaire) :
   - TELEGRAM_BOT_TOKEN
   - TELEGRAM_CHAT_ID
   - TWELVEDATA_API_KEY
6. Onglet "Actions" -> le workflow tourne automatiquement toutes les 15 min.
   Tu peux aussi le lancer manuellement via "Run workflow" pour tester tout de suite.
"""

import json
import os
import sys
import urllib.request
import urllib.parse

# ============================================================
# CONFIGURATION
# ============================================================
CONFIG = {
    "PAIRS": ["EUR/USD", "GBP/USD"],
    "CANDLE_INTERVAL": "15min",

    "RSI_PERIOD": 14,
    "RSI_OVERSOLD": 30,
    "RSI_OVERBOUGHT": 70,

    "EMA_FAST": 9,
    "EMA_SLOW": 21,
}

TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
TELEGRAM_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"


def load_secrets():
    """Les secrets viennent uniquement des variables d'environnement
    (injectées par GitHub Actions à partir des repository secrets)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    api_key = os.environ.get("TWELVEDATA_API_KEY")

    missing = [name for name, val in [
        ("TELEGRAM_BOT_TOKEN", token),
        ("TELEGRAM_CHAT_ID", chat_id),
        ("TWELVEDATA_API_KEY", api_key),
    ] if not val]

    if missing:
        print(f"Secrets manquants : {', '.join(missing)}. "
              f"Vérifie Settings > Secrets and variables > Actions sur GitHub.")
        sys.exit(1)

    return token, chat_id, api_key


# ============================================================
# RÉCUPÉRATION DES DONNÉES
# ============================================================
def fetch_closes(pair, interval, api_key, outputsize=100):
    """Récupère les prix de clôture récents pour une paire, du plus ancien au plus récent."""
    params = {
        "symbol": pair,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": api_key,
    }
    url = TWELVEDATA_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as response:
        data = json.loads(response.read().decode())

    if data.get("status") == "error":
        raise RuntimeError(f"Erreur API Twelve Data pour {pair}: {data.get('message')}")

    values = data.get("values", [])
    if not values:
        raise RuntimeError(f"Aucune donnée reçue pour {pair}")

    values = list(reversed(values))  # du plus ancien au plus récent
    closes = [float(v["close"]) for v in values]
    last_time = values[-1]["datetime"]
    return closes, last_time


# ============================================================
# INDICATEURS TECHNIQUES (pure Python, sans dépendance)
# ============================================================
def compute_ema(values, period):
    if len(values) < period:
        return []
    emas = [sum(values[:period]) / period]
    multiplier = 2 / (period + 1)
    for price in values[period:]:
        emas.append((price - emas[-1]) * multiplier + emas[-1])
    return emas


def compute_rsi(values, period=14):
    if len(values) < period + 1:
        return []
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def rsi_from_avg(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100 - (100 / (1 + rs))

    rsis = [rsi_from_avg(avg_gain, avg_loss)]
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsis.append(rsi_from_avg(avg_gain, avg_loss))
    return rsis


# ============================================================
# TELEGRAM
# ============================================================
def send_telegram_message(token, chat_id, text):
    url = TELEGRAM_URL_TEMPLATE.format(token=token)
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


# ============================================================
# ANALYSE D'UNE PAIRE (exécution unique, pas d'état entre les runs)
# ============================================================
def analyze_pair(pair, config, token, chat_id):
    closes, last_time = fetch_closes(pair, config["CANDLE_INTERVAL"], config["_api_key"])

    ema_fast = compute_ema(closes, config["EMA_FAST"])
    ema_slow = compute_ema(closes, config["EMA_SLOW"])
    rsi = compute_rsi(closes, config["RSI_PERIOD"])

    if len(ema_fast) < 2 or len(ema_slow) < 2 or not rsi:
        print(f"{pair}: pas assez de données pour analyser.")
        return

    fast_prev, fast_now = ema_fast[-2], ema_fast[-1]
    slow_prev, slow_now = ema_slow[-2], ema_slow[-1]
    rsi_now = rsi[-1]
    price_now = closes[-1]

    alerts_sent = 0

    # --- Croisement EMA sur la toute dernière bougie ---
    cross = None
    if fast_prev <= slow_prev and fast_now > slow_now:
        cross = "haussier"
    elif fast_prev >= slow_prev and fast_now < slow_now:
        cross = "baissier"

    if cross:
        emoji = "🟢" if cross == "haussier" else "🔴"
        direction = "ACHAT (croisement haussier)" if cross == "haussier" else "VENTE (croisement baissier)"
        text = (
            f"{emoji} <b>{pair}</b> — {direction}\n"
            f"Prix: {price_now:.5f}\n"
            f"RSI: {rsi_now:.1f}\n"
            f"EMA{config['EMA_FAST']}/{config['EMA_SLOW']} viennent de se croiser\n"
            f"Bougie: {last_time}\n\n"
            f"⚠️ Signal technique automatique, pas un conseil financier."
        )
        send_telegram_message(token, chat_id, text)
        alerts_sent += 1

    # --- Zone RSI extrême sur la dernière bougie ---
    if rsi_now < config["RSI_OVERSOLD"] or rsi_now > config["RSI_OVERBOUGHT"]:
        zone = "survente" if rsi_now < config["RSI_OVERSOLD"] else "surachat"
        emoji = "🟢" if zone == "survente" else "🔴"
        text = (
            f"{emoji} <b>{pair}</b> — RSI en zone de {zone} ({rsi_now:.1f})\n"
            f"Prix: {price_now:.5f}\n"
            f"Bougie: {last_time}\n"
            f"(peut se répéter tant que le RSI reste dans cette zone)\n\n"
            f"⚠️ Signal technique automatique, pas un conseil financier."
        )
        send_telegram_message(token, chat_id, text)
        alerts_sent += 1

    print(f"{pair}: prix={price_now:.5f} RSI={rsi_now:.1f} alertes_envoyées={alerts_sent}")


def main():
    token, chat_id, api_key = load_secrets()
    config = dict(CONFIG)
    config["_api_key"] = api_key

    for pair in config["PAIRS"]:
        try:
            analyze_pair(pair, config, token, chat_id)
        except Exception as e:
            print(f"Erreur sur {pair}: {e}")


if __name__ == "__main__":
    main()
