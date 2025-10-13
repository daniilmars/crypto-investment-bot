# Crypto Bot
Perfekt ✅ — hier ist eine **strukturierte `README.md`** für dein Projekt
**„Crypto Investment Alert Bot“**,
die du direkt in dein GitHub-Repository einfügen kannst.
Sie enthält alle wichtigen Abschnitte: Überblick, Architektur, Setup, Nutzung, Datenquellen und Erweiterungen.

---

````markdown
# 🚀 Crypto Investment Alert Bot

Ein intelligenter Bot, der On-Chain-, Markt- und Stimmungsdaten kombiniert, um automatisierte **Investment-Signale** für Kryptowährungen zu erkennen und dir **Push-Benachrichtigungen** (z. B. per Telegram oder Pushover) zu senden.

---

## 📘 Übersicht

Der Bot sammelt in Echtzeit Daten von verschiedenen APIs (z. B. Whale Alert, Binance, LunarCrush, Glassnode), analysiert Muster in Whale-Aktivitäten, Marktpreisen und Social Sentiment, und bewertet diese über regelbasierte oder ML-Modelle.

Ziel: **Rechtzeitig Krypto-Investments identifizieren**, bevor sich starke Marktbewegungen abzeichnen.

---

## 🧠 Hauptfunktionen

- 🔍 **On-Chain-Analyse:** Whale-Transfers, Exchange-In/Outflows, Wallet-Cluster
- 📈 **Marktdaten:** Echtzeitpreise, Volatilität, Trendanalyse
- 💬 **Stimmungsdaten:** Fear & Greed Index, Social-Media-Sentiment
- 🧮 **Signal Engine:** Regelbasiert oder Machine-Learning-gestützt
- 📲 **Benachrichtigungssystem:** Telegram- oder Push-Alerts bei Kauf-/Verkaufssignalen
- 📊 **Backtesting:** Simulation mit historischen Daten zur Bewertung der Strategie

---

## 🏗 Architekturübersicht

```text
+-------------------------+
|   Data Collector (APIs) |
|-------------------------|
| - Whale_Alert.py        |
| - Binance_Price.py      |
| - LunarCrush_Sent.py    |
| - Glassnode_Metrics.py  |
+-----------+-------------+
            |
            v
+-----------------------------+
|   Data Lake (SQLite / SQL)  |
+-----------+-----------------+
            |
            v
+-----------------------------+
|   Analysis & Signal Engine  |
| - Feature Engineering       |
| - Regeln / ML-Modelle       |
| - Backtesting               |
+-----------+-----------------+
            |
            v
+-----------------------------+
| Notification Service        |
| - Telegram / Pushover API   |
+-----------------------------+
````

---

## ⚙️ Setup

### Voraussetzungen

* Python ≥ 3.10
* Git + VS Code oder Jupyter Notebook
* API-Keys für:

  * Whale Alert
  * Binance
  * LunarCrush
  * (optional) Glassnode, NewsAPI

### Installation

```bash
git clone https://github.com/<your-user>/crypto-alert-bot.git
cd crypto-alert-bot
pip install -r requirements.txt
```

### Beispielhafte Verzeichnisstruktur

```text
crypto-alert-bot/
│
├── data/                     # gespeicherte CSV-/SQL-Daten
├── src/
│   ├── collectors/
│   │   ├── whale_alert.py
│   │   ├── binance_data.py
│   │   └── sentiment_data.py
│   ├── analysis/
│   │   ├── features.py
│   │   ├── signal_engine.py
│   │   └── backtest.py
│   └── notify/
│       ├── telegram_bot.py
│       └── pushover.py
│
├── config/
│   └── settings.yaml
│
├── README.md
└── requirements.txt
```

---

## 🔌 Beispiel: Datenabruf & Signal

```python
from collectors.whale_alert import get_whale_transfers
from analysis.signal_engine import evaluate_signal
from notify.telegram_bot import send_alert

transfers = get_whale_transfers(min_value=5000000)
signal = evaluate_signal(transfers)

if signal:
    send_alert(signal)
```

---

## 🧪 Backtesting

Zum Testen der Strategie mit historischen Daten:

```bash
python src/analysis/backtest.py --symbol BTCUSDT --start 2022-01-01 --end 2024-12-31
```

Ergebnisse:

* 📊 Sharpe Ratio
* 📉 Max Drawdown
* ✅ Trefferquote (True Signal Ratio)

---

## 🧮 Datenquellen

| Kategorie           | Quelle         | API                                                                                                      |
| ------------------- | -------------- | -------------------------------------------------------------------------------------------------------- |
| On-Chain            | Whale Alert    | [https://docs.whale-alert.io](https://docs.whale-alert.io)                                               |
| On-Chain / Metriken | Glassnode      | [https://api.glassnode.com](https://api.glassnode.com)                                                   |
| Marktpreise         | Binance        | [https://binance-docs.github.io/apidocs/spot/en](https://binance-docs.github.io/apidocs/spot/en)         |
| Sentiment           | LunarCrush     | [https://lunarcrush.com/developers](https://lunarcrush.com/developers)                                   |
| Fear & Greed        | Alternative.me | [https://alternative.me/crypto/fear-and-greed-index](https://alternative.me/crypto/fear-and-greed-index) |
| Nachrichten         | NewsAPI        | [https://newsapi.org](https://newsapi.org)                                                               |

---

## 📲 Benachrichtigung

### Telegram

1. Erstelle einen Bot mit **@BotFather**
2. Notiere:

   * `BOT_TOKEN`
   * `CHAT_ID`
3. Füge diese in `config/settings.yaml` ein.

```yaml
telegram:
  token: "YOUR_TELEGRAM_TOKEN"
  chat_id: "YOUR_CHAT_ID"
```

Beispiel-Nachricht:

```
🚨 BUY SIGNAL BTC/USDT
Whale Outflow: +7,200 BTC
Sentiment: Fear (Index 25)
Price: $58,400 (-3.1%)
```

---

## ☁️ Deployment

Optionen:

* **GitHub Actions / CRON:** automatischer Abruf alle 15 min
* **Docker Container:** für Cloud-Hosting
* **Streamlit / Grafana Dashboard:** visuelle Auswertung der Signale

---

## 🧱 Erweiterungen

* 🤖 ML-basierte Signal-Erkennung (z. B. RandomForest, LSTM)
* 🧩 Auto-Portfolio Management über Binance API
* 🕸 Web-Frontend mit Streamlit oder React
* 💬 GPT-gestützte News-Analyse („Warum steigt BTC gerade?“)

---

## ⚠️ Disclaimer

Dieses Projekt dient **ausschließlich zu Bildungs- und Forschungszwecken**.
Es stellt **keine Finanzberatung** oder Kaufempfehlung dar.
Investitionen in Kryptowährungen sind mit hohen Risiken verbunden.

---

## 🧑‍💻 Autor

**Crypto Investment Alert Bot**
Erstellt von Daniil Mars mit Unterstützung von GPT-5.
Lizenz: MIT

```

---

Möchtest du, dass ich dir zusätzlich  
→ eine passende **`requirements.txt`** und  
→ eine minimale **Projekt-Ordnerstruktur mit Python-Stubs** generiere,  
damit du das Repository direkt initialisieren kannst (z. B. für GitHub)?
```
