# Simple Table Tennis Scoreboard

A clean, separate scoreboard prototype. It does not replace the existing `tt` application.

## Features

- Race to 11
- Win by two clear points
- Automatic service indicator
- TV scoreboard at `/`
- Tablet controls at `/control`
- Add and subtract points
- Manual server change
- New game reset
- JSON API ready for a Flic bridge

## Run

```bash
cd simple-scoreboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open:

- TV: `http://PI-IP-ADDRESS:5052/`
- Controls: `http://PI-IP-ADDRESS:5052/control`

## API

- `GET /api/state`
- `POST /api/score` with `{"side":"left","operation":"add"}`
- `POST /api/new-game`
- `POST /api/server`
