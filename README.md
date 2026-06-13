# Cycling Alarm Bot

Een Telegram-bot die je waarschuwt wanneer je een wielrenkoers moet inschakelen op HBO Max — op het juiste moment, gebaseerd op live events uit de ProcyclingStats-ticker.

## Hoe het werkt

1. **06:00** — haalt het koersschema van vandaag op van ProcyclingStats
2. **06:30** — logt in op HBO Max en checkt of de koers live beschikbaar is
3. **Elke 60 seconden** — pollt de live ticker van actieve koersen
4. Als de rule-based scorer een score ≥ 70 berekent → stuurt een Telegram-notificatie
5. Jij geeft feedback (te vroeg / goed / te laat / onnodig) → de drempel past zich automatisch aan

---

## Installatie

### Vereisten

- Python 3.11+
- pip

### Stappen

```bash
# 1. Kloon of kopieer de map
cd cycling-alarm

# 2. Maak een virtualenv aan
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
.venv\Scripts\activate           # Windows

# 3. Installeer dependencies
pip install -r requirements.txt

# 4. Installeer de Playwright browser (eenmalig)
playwright install chromium

# 5. Maak je .env bestand
cp .env.example .env
# Vul TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HBO_MAX_EMAIL en HBO_MAX_PASSWORD in

# 6. Start de bot
python main.py
```

---

## HBO Max instellen

Vul in `.env` je HBO Max inloggegevens in:

```env
HBO_MAX_EMAIL=jouw@email.nl
HBO_MAX_PASSWORD=jouwwachtwoord
```

De bot logt de eerste keer in via een headless Chromium-browser en slaat de sessie op in `hbomax_session.json`. Daarna wordt de sessie hergebruikt totdat cookies verlopen.

**Login-debug:** als het inloggen mislukt, wordt automatisch een screenshot opgeslagen als `hbomax_login_error.png` in de projectmap — handig om te zien wat er mis gaat (bijv. twee-factor-authenticatie of een captcha).

> **Twee-factor-authenticatie (2FA):** als jouw HBO Max account 2FA heeft ingeschakeld, schakel dat dan tijdelijk uit of gebruik een apart account zonder 2FA. De headless browser kan geen authenticator-app of SMS-code invoeren.

---

## Telegram bot instellen

### Bot token aanmaken via BotFather

1. Open Telegram en zoek naar **@BotFather**
2. Stuur `/newbot`
3. Kies een naam (bijv. `Cycling Alarm`)
4. Kies een gebruikersnaam dat eindigt op `bot` (bijv. `MijnCyclingAlarmBot`)
5. BotFather stuurt je een **token** — dit is je `TELEGRAM_BOT_TOKEN`

### Chat ID vinden

1. Stuur een willekeurig bericht naar je bot
2. Open in je browser:
   ```
   https://api.telegram.org/bot<JOUW_TOKEN>/getUpdates
   ```
3. Zoek in de JSON naar `"chat": {"id": <NUMMER>}` — dit is je `TELEGRAM_CHAT_ID`

---

## Commando's

| Commando | Functie |
|----------|---------|
| `/status` | Koersen van vandaag + HBO-beschikbaarheid |
| `/drempel` | Huidige notificatiedrempel (0–100) |
| `/help` | Help-overzicht |

---

## Notificatie-voorbeeld

```
🚴 Tour de France — Etappe 14
📍 Nog 18 km te gaan
⚡ Aanval/solo voorbij beslissende col
📺 Zet HBO Max aan!

Hoe was deze notificatie?
[✅ Goed moment] [⏰ Te vroeg]
[⏳ Te laat]    [❌ Onnodig]
```

---

## Drempelaanpassing

De bot leert van jouw feedback:

- Na elke **20 feedbacks** wordt de drempel automatisch bijgesteld
- > 50% negatief (te vroeg / te laat / onnodig) → drempel +5 (max 90)
- > 60% positief → drempel -3 (min 55)

---

## Raspberry Pi — draaien als systemd service

### 1. SSH naar de Pi en clone de repo

```bash
ssh pi@raspberrypi.local
```

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/emieldejager-cloud/cycling-alarm.git
cd cycling-alarm
```

### 2. Installeer dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
# Raspberry Pi heeft extra system-libs nodig voor Chromium:
sudo apt-get install -y libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
  libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2
cp .env.example .env
nano .env   # vul token, chat_id, hbo email+password in
```

### 3. Updates binnenhalen

```bash
cd ~/cycling-alarm
git pull
sudo systemctl restart cycling-alarm
```

### 4. Maak een systemd service

```bash
sudo nano /etc/systemd/system/cycling-alarm.service
```

Inhoud:

```ini
[Unit]
Description=Cycling Alarm Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/cycling-alarm
ExecStart=/home/pi/cycling-alarm/.venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 5. Activeer en start de service

```bash
sudo systemctl daemon-reload
sudo systemctl enable cycling-alarm
sudo systemctl start cycling-alarm

# Logs bekijken
sudo journalctl -u cycling-alarm -f
```

---

## Projectstructuur

```
cycling-alarm/
├── main.py                  # Entrypoint
├── .env.example             # Template voor secrets
├── requirements.txt
├── cycling-alarm.log        # Log (aangemaakt bij eerste run)
├── cycling-alarm.db         # SQLite database (aangemaakt bij eerste run)
├── db/
│   └── database.py          # SQLAlchemy modellen
├── scrapers/
│   ├── pcs_scraper.py       # ProcyclingStats koersschema + ticker
│   └── hbo_checker.py       # tvgids.nl uitzendingen check
├── scorer/
│   └── rule_scorer.py       # Rule-based scorer
├── bot/
│   └── telegram_bot.py      # Telegram notificaties + feedback
└── scheduler/
    └── jobs.py              # APScheduler cron jobs
```
