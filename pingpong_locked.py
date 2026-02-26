#!/usr/bin/env python3
# Table Tennis Scoreboard – public "Quick Edit" (names / sets to win / who serves),
# admin-only full Setup behind a PIN. Scoreboard is the default screen.
# A/L keys simulate Flic (single=+1, double=−1, long on key-up=next).
# Banner stays until input; larger names & ENDS; wider score gap; big serve arrows.
# Auto-serve every 2 points; at deuce every point. Continuous play if sets_to_win=0/blank.
# Uses RLock to avoid deadlocks on long-press -> next game.

import json, os, threading, time, hashlib
from dataclasses import dataclass, asdict
from typing import Optional
from flask import Flask, request, redirect, url_for, render_template_string, jsonify, session, abort

from signal import SIGINT, SIGTERM, signal

# ----- Optional (for Pi) -----
try:
    import fliclib  # type: ignore
except Exception:
    fliclib = None

# ======================================================================
# Config & State
# ======================================================================

CONFIG_PATH = os.path.expanduser("~/pingpong_config.json")

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

@dataclass
class Config:
    player_a_name: str = "Player A"
    player_b_name: str = "Player B"
    points_per_game: int = 3
    win_by_two: bool = True
    sets_to_win: int = 3          # 0/blank = continuous play; ends still tracked
    # Flic only (admin)
    flic_mac_a: str = ""
    flic_mac_b: str = ""
    double_click_ms: int = 350
    long_press_ms: int = 1000
    initial_server: str = "A"     # A or B (starting server for Game 1)
    serve_every_points: int = 2
    # Admin PIN (SHA256). Default "2468"
    admin_pin_hash: str = sha256("2468")

def load_config() -> Config:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)
        return Config(**{**asdict(Config()), **data})
    return Config()

def save_config(cfg: Config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(asdict(cfg), f, indent=2)

class MatchState:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.RLock()   # IMPORTANT: re-entrant to prevent deadlocks
        self.reset_match()

    def reset_match(self):
        with self.lock:
            self.game = 1
            self.sets_a = 0   # ends won by A
            self.sets_b = 0   # ends won by B
            self.a = 0
            self.b = 0
            self.total_points_in_current_game = 0
            self.server = self.cfg.initial_server
            self.game_over = False
            self.match_over = False
            self.last_action = "Ready"
            self.winner_text = ""
            self.banner = ""

    def reset_game(self):
        with self.lock:
            self.a = 0
            self.b = 0
            self.total_points_in_current_game = 0
            # Alternate initial server each new game relative to chosen starter
            self.server = 'A' if ((self.game % 2) == (1 if self.cfg.initial_server == 'A' else 0)) else 'B'
            self.game_over = False
            self.last_action = f"New game started (Game {self.game})"
            self.winner_text = ""
            self.banner = ""

    # ---------------- Serve Logic ----------------
    def _should_switch_every_point(self) -> bool:
        return (self.a >= self.cfg.points_per_game - 1) and (self.b >= self.cfg.points_per_game - 1)

    def _maybe_rotate_serve_after_point(self):
        self.total_points_in_current_game += 1
        if self._should_switch_every_point():
            self.server = 'A' if self.server == 'B' else 'B'
        else:
            if (self.total_points_in_current_game % self.cfg.serve_every_points) == 0:
                self.server = 'A' if self.server == 'B' else 'B'

    def _maybe_recalculate_server_after_correction(self):
        total = self.a + self.b
        start_server = 'A' if ((self.game % 2) == (1 if self.cfg.initial_server == 'A' else 0)) else 'B'
        server = start_server
        for i in range(1, total + 1):
            if (self.a >= self.cfg.points_per_game - 1) and (self.b >= self.cfg.points_per_game - 1):
                server = 'A' if server == 'B' else 'B'
            else:
                if (i % self.cfg.serve_every_points) == 0:
                    server = 'A' if server == 'B' else 'B'
        self.total_points_in_current_game = total
        self.server = server

    # -------------- Game/Match Logic --------------
    def _check_game_over(self):
        ppg = self.cfg.points_per_game
        if not self.cfg.win_by_two:
            return (self.a >= ppg) or (self.b >= ppg)
        else:
            if self.a >= ppg or self.b >= ppg:
                return abs(self.a - self.b) >= 2
            return False

    def add_point(self, who: str):
        with self.lock:
            if self.game_over and not self.match_over:
                self.last_action = "Game finished – hold to start the next game."
                return
            if who == "A":
                if self.game_over: return
                self.a += 1
                self.last_action = f"{self.cfg.player_a_name} +1"
            else:
                if self.game_over: return
                self.b += 1
                self.last_action = f"{self.cfg.player_b_name} +1"

            self._maybe_rotate_serve_after_point()

            if self._check_game_over():
                self.game_over = True
                if self.a > self.b:
                    self.sets_a += 1
                    winner = self.cfg.player_a_name
                else:
                    self.sets_b += 1
                    winner = self.cfg.player_b_name
                if self.cfg.sets_to_win == 0:
                    self.winner_text = f"{self.a}-{self.b}"
                    self.banner = self.winner_text
                else:
                    self.winner_text = f"Game {self.game} to {winner} ({self.a}-{self.b})"
                    self.banner = self.winner_text
                self.last_action = self.winner_text

                if self.cfg.sets_to_win and (
                    self.sets_a >= self.cfg.sets_to_win or self.sets_b >= self.cfg.sets_to_win
                ):
                    self.match_over = True
                    self.last_action = f"Match finished – {winner} wins {self.sets_a}-{self.sets_b}"

    def remove_point(self, who: str):
        with self.lock:
            changed = False
            if who == "A" and self.a > 0:
                self.a -= 1; changed = True; self.last_action = f"{self.cfg.player_a_name} -1"
            elif who == "B" and self.b > 0:
                self.b -= 1; changed = True; self.last_action = f"{self.cfg.player_b_name} -1"
            if changed:
                self.game_over = False
                self.winner_text = ""
                self._maybe_recalculate_server_after_correction()

    def next_game(self):
        with self.lock:
            if self.match_over and self.cfg.sets_to_win:
                self.last_action = "Match over – return to Admin Setup or press N for a new match."
                return
            # Record end score for match system
            if match_system.active and (self.a > 0 or self.b > 0):
                match_system.current_ends.append((self.a, self.b))
            self.game += 1
            self.reset_game()
            import datetime
            with open(os.path.expanduser("~/pingpong_usage.log"), "a") as f:
                f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + f" - Game {self.game} started\n")

    def toggle_server(self):
        with self.lock:
            self.server = 'A' if self.server == 'B' else 'B'
            self.last_action = f"Server switched to {'Player ' + self.server}"

# ======================================================================
# Input Backends (Flic on Pi; keyboard works everywhere)
# ======================================================================

class InputBackend:
    def start(self): ...
    def stop(self): ...

class FlicBackend(InputBackend):
    def __init__(self, cfg: Config, state: MatchState):
        self.cfg = cfg; self.state = state
        self.client: Optional["fliclib.FlicClient"] = None
        a = cfg.flic_mac_a.lower().strip() if cfg.flic_mac_a else ""
        b = cfg.flic_mac_b.lower().strip() if cfg.flic_mac_b else ""
        self.want = {x for x in (a, b) if x}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _on_button_event(self, bdaddr: str, click_type: str):
        if not bdaddr: return
        swapped = (self.state.game % 2 == 0) if self.cfg.sets_to_win > 0 else False
        is_mac_a = bdaddr.lower() == (self.cfg.flic_mac_a or "").lower()
        is_mac_b = bdaddr.lower() == (self.cfg.flic_mac_b or "").lower()
        if is_mac_a:
            who = "B" if swapped else "A"
        elif is_mac_b:
            who = "A" if swapped else "B"
        else:
            who = None
        if not who: return
        # any click clears the banner
        self.state.banner = ""
        if click_type == "ButtonSingleClick":
            self.state.add_point(who)
        elif click_type == "ButtonDoubleClick":
            # if score is 0-0 switch serve, otherwise remove point
            if self.state.a == 0 and self.state.b == 0:
                self.state.toggle_server()
            else:
                self.state.remove_point(who)
        elif click_type == "ButtonHold":
            self.state.next_game()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.client = fliclib.FlicClient("localhost")
                print("Connected to flicd!")

                def got_button(b):
                    if b.lower() not in self.want: return
                    ch = fliclib.ButtonConnectionChannel(b)
                    ch.on_button_single_or_double_click_or_hold = \
                        (lambda channel, ct, was_queued, time_diff: self._on_button_event(b, ct.name))
                    self.client.add_connection_channel(ch)

                def got_info(info):
                    for b in info["bd_addr_of_verified_buttons"]:
                        got_button(b)

                self.client.get_info(got_info)
                self.client.on_new_verified_button = lambda b: got_button(b)

                while not self._stop.is_set():
                    self.client.handle_events()
                    time.sleep(0.1)

            except Exception as e:
                print("flicd error, retrying in 5s:", e)
                time.sleep(5)


    def start(self):
        if fliclib is None:
            print("FLIC backend requested but fliclib not available."); return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("FLIC backend started (listening for clicks).")

    def stop(self): self._stop.set()

# ======================================================================
# Web UI
# ======================================================================

app = Flask(__name__)
app.secret_key = "tabletennis-scoreboard-please-change-this"  # session for admin login

cfg = load_config()
state_obj = MatchState(cfg)
backend: Optional["InputBackend"] = None

# -------------------- Templates --------------------

LOGIN_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Login</title>
<style>
 body{font-family:system-ui,Arial;background:#0b0e14;color:#fff;margin:0;display:grid;place-items:center;min-height:100vh}
 .card{background:#121826;border:1px solid #2a3140;border-radius:14px;padding:24px;min-width:min(90vw,420px)}
 h1{margin:0 0 12px;font-size:24px}
 label{display:grid;gap:6px;margin:10px 0}
 input,button{font-size:18px;padding:10px;border-radius:10px;border:1px solid #2a3140;background:#0b0e14;color:#fff}
 .row{display:flex;gap:10px;justify-content:flex-end;margin-top:12px}
 .err{color:#ff8686;margin:8px 0 0 0}
 a{color:#9ad7ff}
</style>
</head><body>
  <div class="card">
    <h1>Enter Admin PIN</h1>
    <form method="post" action="{{ url_for('admin_login') }}">
      <label>PIN
        <input name="pin" type="password" autofocus>
      </label>
      <div class="row">
        <a href="{{ url_for('score') }}">&larr; Cancel</a>
        <button type="submit">Unlock</button>
      </div>
      {% if error %}<div class="err">{{error}}</div>{% endif %}
    </form>
  </div>
</body></html>
"""

SETUP_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Setup</title>
<style>
 body{font-family:system-ui,Arial;background:#0b0e14;color:#fff;margin:0}
 .wrap{max-width:880px;margin:0 auto;padding:28px}
 h1{margin:0 0 12px;font-size:28px}
 form{display:grid;gap:14px}
 label{display:grid;gap:6px}
 input,select,button{font-size:18px;padding:10px;border-radius:10px;border:1px solid #2a3140;background:#121826;color:#fff}
 .row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
 .hint{opacity:.85;font-size:14px}
 .bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
 a{color:#9ad7ff}
</style>
</head><body>
  <div class="wrap">
    <div class="bar">
      <h1>Admin Setup</h1>
      <div><a href="{{ url_for('logout') }}">Log out</a> · <a href="{{ url_for('score') }}">Scoreboard</a></div>
    </div>
    <form method="post" action="{{ url_for('apply_setup') }}">
      <div class="row">
        <label>Player A Name
          <input name="player_a_name" value="{{cfg.player_a_name}}">
        </label>
        <label>Player B Name
          <input name="player_b_name" value="{{cfg.player_b_name}}">
        </label>
      </div>
      <div class="row">
        <label>Points per Game
          <input type="number" name="points_per_game" value="{{cfg.points_per_game}}" min="1">
        </label>
        <label>Sets to Win (0 = continuous)
          <input type="number" name="sets_to_win" value="{{cfg.sets_to_win}}" min="0" placeholder="0">
        </label>
      </div>
      <label>
        <input type="checkbox" name="win_by_two" {% if cfg.win_by_two %}checked{% endif %}>
        Win by two
      </label>
      <div class="row">
        <label>Initial Server
          <select name="initial_server">
            <option value="A" {% if cfg.initial_server=='A' %}selected{% endif %}>{{cfg.player_a_name}} (A)</option>
            <option value="B" {% if cfg.initial_server=='B' %}selected{% endif %}>{{cfg.player_b_name}} (B)</option>
          </select>
        </label>
        <label>Serve change every (points)
          <input type="number" name="serve_every_points" value="{{cfg.serve_every_points}}" min="1">
        </label>
      </div>
      <div class="row">
        <label>Flic MAC – Player A
          <input name="flic_mac_a" value="{{cfg.flic_mac_a}}" placeholder="80:e4:da:..:..:..">
        </label>
        <label>Flic MAC – Player B
          <input name="flic_mac_b" value="{{cfg.flic_mac_b}}" placeholder="80:e4:da:..:..:..">
        </label>
      </div>
      <div class="row">
        <label>Double Click Window (ms)
          <input type="number" name="double_click_ms" value="{{cfg.double_click_ms}}">
        </label>
        <label>Long Press Duration (ms)
          <input type="number" name="long_press_ms" value="{{cfg.long_press_ms}}">
        </label>
      </div>
      <div class="row">
        <label>Admin PIN (enter new PIN to change)
          <input type="password" name="admin_pin" placeholder="leave blank to keep 2468 or current">
        </label>
        <div class="hint">Tip: after changing the PIN, don’t forget to note it!</div>
      </div>
      <button type="submit">Save</button>
    </form>
  </div>
</body></html>
"""

SCORE_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Table Tennis – Scoreboard</title>
<style>
  :root{ --fg:#fff; --bg:#0b0e14; --mut:#dbe5ee; --accent:#ffd938; --outline:#000; }
  *{box-sizing:border-box}
  body{font-family:system-ui,Arial,sans-serif;background:var(--bg);color:var(--fg);margin:0}
  .wrap{display:grid;place-items:center;min-height:100vh;padding:16px}
  .grid{display:grid;gap:0;justify-items:center}
  .names{display:flex;gap:11rem;align-items:center}
  .name{font-size:4.2rem;opacity:.99;display:flex;gap:1.1rem;align-items:center;text-shadow:0 4px 14px rgba(0,0,0,.5);margin:0;padding:0;line-height:1}

  .score{display:flex;gap:clamp(1rem, 4vw, 8rem);align-items:center;justify-content:center;margin-top:12px;width:100%;overflow:hidden}
  .big{font-size:clamp(60px,38vw,500px);font-weight:900;line-height:0.9;text-shadow:0 6px 24px rgba(0,0,0,.55);margin:0;padding:0;text-align:center;width:44vw}
  .meta{margin-top:12px;opacity:.98;text-align:center;font-size:1.45rem}
  .controls{position:fixed;bottom:12px;left:0;right:0;text-align:center;color:var(--mut);font-size:1rem}

  /* Buttons bottom-right: public quick edit (pencil) + admin gear */
  .fab{position:fixed;right:12px;bottom:12px;display:flex;gap:10px}
  .btn{background:#121826;border:1px solid #2a3140;color:#fff;border-radius:999px;width:46px;height:46px;display:grid;place-items:center;font-size:22px;cursor:pointer}
  .btn:hover{filter:brightness(1.1)}

  /* Serve ball */
  @keyframes servepulse{0%,100%{box-shadow:0 0 20px rgba(240,192,64,0.6);transform:scale(1)}50%{box-shadow:0 0 60px rgba(240,192,64,1);transform:scale(1.2)}}

  @media (max-width:900px){
    .big{font-size:48vw}

    .name{font-size:8.8vw}
    .sets{font-size:5vw}
  }

  /* Banner: stays visible until cleared by input */
  .banner{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;opacity:0;transition:opacity .2s ease}
  .banner.show{opacity:1}
  .banner .inner{background:rgba(0,0,0,.80);border:3px solid var(--accent);border-radius:32px;padding:36px 56px;text-align:center;box-shadow:0 28px 72px rgba(0,0,0,.55)}
  .banner h2{margin:0;font-size:clamp(50px,10vw,120px);color:var(--accent)}

  /* Quick Edit modal */
  dialog{border:none;border-radius:16px;padding:0;max-width:min(92vw,520px)}
  .modal{background:#121826;color:#fff;border:1px solid #2a3140;border-radius:16px;padding:18px}
  .modal h2{margin:0 0 10px 0;font-size:22px}
  .modal form{display:grid;gap:12px}
  label{display:grid;gap:6px}
  input,select,button{font-size:18px;padding:10px;border-radius:10px;border:1px solid #2a3140;background:#0b0e14;color:#fff}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
  .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:6px}
</style>
</head><body>
  <div class="wrap">
    <div id="matchbar" style="text-align:center;font-size:1.8rem;font-weight:700;color:#9ad7ff;padding:6px 0;display:none"><span id="matchscore"></span> <a href="/match/abandon" onclick="return confirm('Abandon match?')" style="font-size:1rem;color:#ff6666;margin-left:20px">✕ Abandon</a></div>
    <div class="grid" aria-live="polite">
      <div class="names">
        <div style="text-align:center"><div class="name"><span id="nameA">{{cfg.player_a_name}}</span> (<span id="setsA">0</span>)</div></div>
        <div style="text-align:center"><div class="name"><span id="nameB">{{cfg.player_b_name}}</span> (<span id="setsB">0</span>)</div></div>
      </div>

      <div class="score" role="group" aria-label="Scores and current server">
        <div id="scoreA" class="big" aria-label="{{cfg.player_a_name}} score">0</div>
        <div id="scoreB" class="big" aria-label="{{cfg.player_b_name}} score">0</div>
      </div>


    </div>

    <div class="controls">Keyboard: A/L = Flic-style (single +1, double −1, long next) · G=next game · N=new match · S=toggle serve · E=Quick Edit · C=Match mode · Esc=Admin (PIN)</div>
  </div>

  <!-- Floating buttons -->
  <div class="fab">
    <button class="btn" title="Quick Edit (names / sets / server)" id="btnQuick">✎</button>
    <button class="btn" title="Admin Setup (PIN)" id="btnAdmin">⚙</button>
  </div>

  <!-- Serve dots fixed to screen edges -->
  <div id="arrowA" style="position:fixed;left:30px;top:50%;transform:translateY(-50%);width:80px;height:80px;border-radius:50%;background:var(--accent);margin:0;z-index:10;display:none;animation:servepulse 1s ease-in-out infinite"></div>
  <div id="arrowB" style="position:fixed;right:30px;top:50%;transform:translateY(-50%);width:80px;height:80px;border-radius:50%;background:var(--accent);margin:0;z-index:10;display:none;animation:servepulse 1s ease-in-out infinite"></div>
  <!-- Win banner -->
  <div id="banner" class="banner" role="status" aria-live="assertive" aria-atomic="true">
    <div class="inner"><h2 id="bannerText"></h2></div>
  </div>

  <!-- Quick Edit modal -->
  <dialog id="dlgQuick" autocomplete="off">
    <div class="modal">
      <h2>Quick Edit</h2>
      <form id="quickForm" method="dialog">
        <div class="row">
          <label>Player A Name
            <input name="player_a_name" id="qa" value="{{cfg.player_a_name}}">
          </label>
          <label>Player B Name
            <input name="player_b_name" id="qb" value="{{cfg.player_b_name}}">
          </label>
        </div>
        <div class="row">
          <label>Sets to Win (0 = continuous)
            <input type="number" name="sets_to_win" id="qs" value="{{cfg.sets_to_win}}" min="0" placeholder="0">
          </label>
          <label>Who serves now
            <select name="server_now" id="qserv">
              <option value="A">A ({{cfg.player_a_name}})</option>
              <option value="B">B ({{cfg.player_b_name}})</option>
            </select>
          </label>
        </div>
        <div class="actions">
          <button type="button" id="qCancel">Cancel</button>
          <button type="submit" id="qSave">Save</button>
        </div>
      </form>
    </div>
  </dialog>

<script>
  const scoreA = document.getElementById('scoreA');
  const scoreB = document.getElementById('scoreB');
  const setsA  = document.getElementById('setsA');
  const setsB  = document.getElementById('setsB');
  const gameNo = document.getElementById('gameNo');
  const meta   = document.getElementById('meta');
  const arrowA = document.getElementById('arrowA');
  const arrowB = document.getElementById('arrowB');
  const banner = document.getElementById('banner');
  const bannerText = document.getElementById('bannerText');

  // Quick edit elements
  const btnQuick = document.getElementById('btnQuick');
  const btnAdmin = document.getElementById('btnAdmin');
  const dlgQuick = document.getElementById('dlgQuick');
  const qForm = document.getElementById('quickForm');
  const qCancel = document.getElementById('qCancel');
  const qSave = document.getElementById('qSave');
  const qA = document.getElementById('qa');
  const qB = document.getElementById('qb');
  const qS = document.getElementById('qs');
  const qServ = document.getElementById('qserv');

  // Polling
  async function poll() {
    try {
      const r = await fetch('{{ url_for("get_state") }}', {cache:'no-store'});
      const s = await r.json();
      // Match score bar
      const mb = document.getElementById('matchbar');
      if (s.match_active) {
        mb.style.display = 'block';
        document.getElementById('matchscore').textContent = s.match_home_team + ' ' + s.match_home_score + ' — ' + s.match_away_score + ' ' + s.match_away_team;
      } else {
        mb.style.display = 'none';
      }
      // Hide ends if sets_to_win is 0
      const setsDisplay = s.sets_to_win === 0 ? 'none' : 'inline';
      document.getElementById('setsA').parentElement.style.display = setsDisplay;
      document.getElementById('setsB').parentElement.style.display = setsDisplay;
      // Shrink score font only when both sides are 10+
      const bothDouble = s.a >= 10 && s.b >= 10;
      const bigSize = bothDouble ? 'clamp(60px,26vw,400px)' : 'clamp(60px,38vw,500px)';
      document.querySelectorAll('.big').forEach(el => el.style.fontSize = bigSize);
      const sw = s.sets_to_win > 0 ? s.swapped : false;
      scoreA.textContent = sw ? s.b : s.a;
      scoreB.textContent = sw ? s.a : s.b;
      setsA.textContent  = sw ? s.sets_b : s.sets_a;
      setsB.textContent  = sw ? s.sets_a : s.sets_b;
      document.getElementById('nameA').textContent = sw ? s.name_b : s.name_a;
      document.getElementById('nameB').textContent = sw ? s.name_a : s.name_b;

      const leftServes = (s.server === 'A' && !sw) || (s.server === 'B' && sw);
      arrowA.style.display = leftServes ? 'block' : 'none';
      arrowB.style.display = leftServes ? 'none' : 'block';

      if (s.banner && s.banner.length) {
        bannerText.textContent = s.banner;
        banner.classList.add('show');
      } else {
        banner.classList.remove('show');
      }
    } catch(e) { }
    finally { setTimeout(poll, 120); }
  }
  poll();

  async function cmd(c) {
    try {
      const r = await fetch('{{url_for("key_action")}}?cmd='+c, {cache:'no-store'});
      if (r.status === 200) {
        const d = await r.json();
        if (d.redirect) { window.location.href = d.redirect; return; }
      }
    } catch(e) {}
  }

  // A/L keys -> single/double/long on key-UP
  const dblMs  = {{cfg.double_click_ms|int}};
  const longMs = {{cfg.long_press_ms|int}};
  const keyState = { a:{down:false,t0:0,singleT:null,waitingDbl:false}, l:{down:false,t0:0,singleT:null,waitingDbl:false} };
  function now(){ return performance.now(); }

  function handleDown(k, e){
    e.preventDefault();
    const s = keyState[k];
    if (s.down) return;
    s.down = true; s.t0 = now();
  }
  function handleUp(k, plusCmd, minusCmd, e){
    e.preventDefault();
    const s = keyState[k];
    if (!s.down) return;
    s.down = false;
    const held = now() - s.t0;
    if (held >= Math.max(400, longMs)) { if (s.singleT){ clearTimeout(s.singleT);} s.waitingDbl=false; cmd('next'); return; }
    if (!s.waitingDbl){
      s.waitingDbl = true;
      s.singleT = setTimeout(()=>{ s.waitingDbl=false; s.singleT=null; cmd(plusCmd); }, Math.max(150, dblMs));
    } else {
      if (s.singleT){ clearTimeout(s.singleT); s.singleT=null; }
      s.waitingDbl = false;
      cmd(minusCmd);
    }
  }

  // Secret screen - press H 8 times
  let hCount = 0, hTimer = null;
  document.addEventListener('keydown',(e)=>{
    // Ignore keypresses when typing in a form field
    const tag = document.activeElement.tagName.toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
    // Protect Escape key in match mode - must press 5 times
    if (e.key === 'Escape') {
      const matchActive = document.getElementById('matchbar') && document.getElementById('matchbar').style.display !== 'none';
      if (matchActive) {
        window._escCount = (window._escCount || 0) + 1;
        window._escTimer && clearTimeout(window._escTimer);
        if (window._escCount < 5) {
          window._escTimer = setTimeout(() => { window._escCount = 0; }, 3000);
          const remaining = 5 - window._escCount;
          alert('Press Escape ' + remaining + ' more time' + (remaining > 1 ? 's' : '') + ' to exit match mode');
          e.preventDefault(); return;
        }
        window._escCount = 0;
      }
    }
    if (e.key.toLowerCase() === 'h') {
      hCount++;
      clearTimeout(hTimer);
      hTimer = setTimeout(() => { hCount = 0; }, 4000);
      if (hCount >= 6) { hCount = 0; window.location.href = '/secret'; return; }
    }
    const k = e.key.toLowerCase();
    if(k==='escape'){ e.preventDefault(); window.location.href='{{url_for("admin")}}'; }
    else if(k==='g'){ e.preventDefault(); cmd('next'); }
    else if(k==='n'){ e.preventDefault(); if(confirm('Start a NEW match? This resets ends and scores.')) cmd('resetmatch'); }
    else if(k==='s'){ e.preventDefault(); cmd('toggleserve'); }
    else if(k==='c'){ e.preventDefault(); window.location.href='/match/setup'; }
    else if(k==='e'){ e.preventDefault(); dlgQuick.showModal(); qA.focus(); }
    else if(k==='a'){ handleDown('a', e); }
    else if(k==='l'){ handleDown('l', e); }
  });
  document.addEventListener('keyup',(e)=>{
    const k = e.key.toLowerCase();
    if     (k==='a'){ handleUp('a','aplus','aminus',e); }
    else if(k==='l'){ handleUp('l','bplus','bminus',e); }
  });

  // Quick Edit open/close
  btnQuick.addEventListener('click', ()=>{ dlgQuick.showModal(); qA.focus(); });
  btnAdmin.addEventListener('click', ()=>{ window.location.href='{{url_for("admin")}}'; });
  qCancel.addEventListener('click', ()=> dlgQuick.close());

  // Quick Edit submit -> POST /quick
  qForm.addEventListener('submit', async (e)=>{
    e.preventDefault();
    const fd = new FormData(qForm);
    try {
      await fetch('{{ url_for("quick_update") }}', { method:'POST', body: fd });
      dlgQuick.close();
      // no page reload; polling will pick up changes
    } catch(err) {
      alert('Failed to save.');
    }
  });
</script>
</body></html>
"""

# -------------------- Routes --------------------

@app.route("/")
def root():
    return redirect(url_for("score"))

@app.route("/score")
def score():
    return render_template_string(SCORE_HTML, cfg=cfg)

@app.route("/state")
def get_state():
    with state_obj.lock:
        out = dict(
            a=state_obj.a, b=state_obj.b, game=state_obj.game,
            sets_a=state_obj.sets_a, sets_b=state_obj.sets_b,
            swapped=False if cfg.sets_to_win == 0 else state_obj.game % 2 == 0,
            sets_to_win=cfg.sets_to_win,
            match_active=match_system.active,
            match_home_team=match_system.home_team,
            match_away_team=match_system.away_team,
            match_home_score=match_system.home_score,
            match_away_score=match_system.away_score,
            name_a=cfg.player_a_name,
            name_b=cfg.player_b_name,
            status=state_obj.last_action, server=state_obj.server,
            banner=state_obj.banner
        )
    return jsonify(out)

# Public quick update (no PIN): names, sets_to_win, current server
def parse_sets_to_win(raw: str) -> int:
    raw = (raw or "").strip()
    if raw == "": return 0
    try:
        v = int(raw); return max(0, v)
    except ValueError:
        return 0

@app.route("/quick", methods=["POST"])
def quick_update():
    a = (request.form.get("player_a_name") or "").strip() or cfg.player_a_name
    b = (request.form.get("player_b_name") or "").strip() or cfg.player_b_name
    stw = parse_sets_to_win(request.form.get("sets_to_win", "0"))
    serv = (request.form.get("server_now") or state_obj.server).strip().upper()
    if serv not in ("A","B"): serv = state_obj.server

    # Apply to cfg + state safely
    cfg.player_a_name = a
    cfg.player_b_name = b
    cfg.sets_to_win = stw
    save_config(cfg)
    with state_obj.lock:
        state_obj.cfg = cfg
        state_obj.server = serv
        # Do NOT reset scores; this is an in-play change.
        state_obj.last_action = f"Quick update: names/sets/server"
    return ("", 204)

# Admin login + setup
@app.route("/admin", methods=["GET"])
def admin():
    if session.get("admin", False):
        return redirect(url_for("setup"))
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/admin/login", methods=["POST"])
def admin_login():
    pin = request.form.get("pin", "")
    if sha256(pin) == cfg.admin_pin_hash:
        session["admin"] = True
        return redirect(url_for("setup"))
    return render_template_string(LOGIN_HTML, error="Incorrect PIN.")

@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("score"))

@app.route("/setup", methods=["GET"])
def setup():
    if not session.get("admin", False):
        return redirect(url_for("admin"))
    return render_template_string(SETUP_HTML, cfg=cfg)

@app.route("/apply", methods=["POST"])
def apply_setup():
    if not session.get("admin", False):
        abort(403)

    cfg.player_a_name = request.form.get("player_a_name", cfg.player_a_name).strip() or "Player A"
    cfg.player_b_name = request.form.get("player_b_name", cfg.player_b_name).strip() or "Player B"
    cfg.points_per_game = int(request.form.get("points_per_game", cfg.points_per_game))
    cfg.sets_to_win = parse_sets_to_win(request.form.get("sets_to_win", "0"))
    cfg.win_by_two = True if request.form.get("win_by_two") == "on" else False
    cfg.initial_server = request.form.get("initial_server", cfg.initial_server)
    cfg.serve_every_points = max(1, int(request.form.get("serve_every_points", cfg.serve_every_points)))
    cfg.flic_mac_a = request.form.get("flic_mac_a", cfg.flic_mac_a).strip()
    cfg.flic_mac_b = request.form.get("flic_mac_b", cfg.flic_mac_b).strip()
    cfg.double_click_ms = int(request.form.get("double_click_ms", cfg.double_click_ms))
    cfg.long_press_ms = int(request.form.get("long_press_ms", cfg.long_press_ms))

    new_pin = (request.form.get("admin_pin") or "").strip()
    if new_pin:
        cfg.admin_pin_hash = sha256(new_pin)

    save_config(cfg)
    state_obj.cfg = cfg
    # Do not reset match automatically on admin save; keep flow stable
    return redirect(url_for("setup"))

# Key actions (keyboard/Flic). Any action clears the banner.
@app.route("/key")
def key_action():
    cmd = request.args.get("cmd","")
    state_obj.banner = ""
    swapped = state_obj.game % 2 == 0
    left  = "B" if swapped else "A"
    right = "A" if swapped else "B"
    if   cmd == "aplus":  state_obj.add_point(left)
    elif cmd == "aminus": state_obj.remove_point(left)
    elif cmd == "bplus":  state_obj.add_point(right)
    elif cmd == "bminus": state_obj.remove_point(right)
    elif cmd == "next":
        if match_system.active and state_obj.match_over:
            return jsonify({"redirect": "/match/result"})
        elif not state_obj.match_over:
            state_obj.next_game()
    elif cmd == "toggleserve": state_obj.toggle_server()
    elif cmd == "resetmatch": state_obj.reset_match()
    # Check if match just ended after a point
    if match_system.active and state_obj.match_over:
        return jsonify({"redirect": "/match/result"})
    return ("", 204)



# ======================================================================
# MATCH SYSTEM
# ======================================================================

MATCH_ORDER = [
    (1, 2), (3, 1), (2, 3),
    (1, 1), (2, 2), (3, 3),
    (2, 1), (1, 3), (3, 2),
    (0, 0),
]

class MatchSystem:
    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.home_team = ""
        self.away_team = ""
        self.home_players = ["", "", ""]
        self.away_players = ["", "", ""]
        self.home_end = "clock"
        self.away_end = "window"
        self.doubles_home = ["", ""]
        self.doubles_away = ["", ""]
        self.current_match = 0
        self.home_score = 0
        self.away_score = 0
        self.match_results = []
        self.state = "idle"
        self.division = ""
        self.date = ""
        self.venue = ""
        self.current_ends = []  # list of (home_pts, away_pts) per end

    def get_current_players(self):
        if self.current_match >= len(MATCH_ORDER):
            return ("", "")
        h, a = MATCH_ORDER[self.current_match]
        if h == 0:
            return (
                f"{self.doubles_home[0]} & {self.doubles_home[1]}",
                f"{self.doubles_away[0]} & {self.doubles_away[1]}"
            )
        return (self.home_players[h-1], self.away_players[a-1])

match_system = MatchSystem()

# ======================================================================
# MATCH HTML TEMPLATES
# ======================================================================

MATCH_SETUP_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Match Setup</title>
<style>
 *{box-sizing:border-box}
 body{font-family:system-ui,Arial;background:#0b0e14;color:#fff;margin:0;padding:20px;font-size:18px}
 h1{font-size:32px;text-align:center;color:#f0c040;margin-bottom:20px}
 h2{font-size:32px;color:#9ad7ff;margin:16px 0 8px;font-weight:700}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
 .col{background:#121826;padding:16px;border-radius:12px}
 label{display:block;margin-bottom:16px;font-size:40px;font-weight:600}
 input,select{width:100%;padding:20px;font-size:40px;background:#1a2030;color:#fff;border:2px solid #2a3140;border-radius:8px;margin-top:8px} input:focus,select:focus{outline:6px solid #f0c040;border-color:#f0c040;box-shadow:0 0 40px rgba(240,192,64,1)}
 .hint{font-size:13px;color:#888;margin-top:4px}
 .actions{display:flex;gap:12px;justify-content:center;margin-top:20px}
 button{font-size:40px;padding:20px 50px;border:none;border-radius:10px;cursor:pointer;background:#f0c040;color:#000;font-weight:bold} button:focus{outline:6px solid #fff;box-shadow:0 0 40px rgba(240,192,64,1)}
 button.cancel{background:#444;color:#fff}
 .nav{text-align:center;margin-bottom:16px;font-size:14px;color:#888}
 .nav kbd{background:#1a2030;padding:2px 8px;border-radius:4px;color:#fff}
</style>
</head><body>
<h1>🏓 Match Setup</h1>
<div class="nav">Use <kbd>Tab</kbd> to move between fields, <kbd>Enter</kbd> to save</div>
<form method="post" action="/match/setup" autocomplete="off">
<div style="background:#121826;padding:16px;border-radius:12px;margin-bottom:16px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px">
  <label>Division<input name="division" value="{{ms.division}}" placeholder="e.g. Div 1"></label>
  <label>Date<input name="date" type="text" value="{{ms.date}}"></label>
  <label>Venue<input name="venue" value="{{ms.venue}}" placeholder="e.g. Trowbridge TTC"></label>
</div>
<div class="grid">
  <div class="col">
    <h2>🏠 Home Team</h2>
    <label>Team Name<input name="home_team" value="{{ms.home_team}}" autofocus></label>
    <label>Player 1<input name="home_p1" value="{{ms.home_players[0]}}"></label>
    <label>Player 2<input name="home_p2" value="{{ms.home_players[1]}}"></label>
    <label>Player 3<input name="home_p3" value="{{ms.home_players[2]}}"></label>

  </div>
  <div class="col">
    <h2>✈️ Away Team</h2>
    <label>Team Name<input name="away_team" value="{{ms.away_team}}"></label>
    <label>Player 1<input name="away_p1" value="{{ms.away_players[0]}}"></label>
    <label>Player 2<input name="away_p2" value="{{ms.away_players[1]}}"></label>
    <label>Player 3<input name="away_p3" value="{{ms.away_players[2]}}"></label>

  </div>
</div>
<div class="actions">
  <button type="submit" style="font-size:52px;padding:24px 60px;background:#f0c040;color:#000;font-weight:900;border:none;border-radius:16px;cursor:pointer;box-shadow:0 0 60px rgba(240,192,64,0.9);width:100%">Start Match ▶</button>
  <a href="/score"><button type="button" class="cancel">Cancel</button></a>
</div>
</form>
</body></html>
"""

MATCH_UPNEXT_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Up Next</title>
<style>
 *{box-sizing:border-box}
 body{font-family:system-ui,Arial;background:#0b0e14;color:#fff;margin:0;padding:20px;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
 .match-num{font-size:2rem;color:#888;margin-bottom:6px}
 .overall{font-size:1.8rem;color:#9ad7ff;margin-bottom:16px}
 .vs{font-size:clamp(25px,5vw,50px);color:#888;margin:8px 0}
 .player{font-size:clamp(60px,10vw,120px);font-weight:900;color:#f0c040;line-height:1.1}
 .team{font-size:1.3rem;color:#9ad7ff;margin-bottom:4px}
 .options{display:flex;gap:20px;margin-top:20px;flex-wrap:wrap;justify-content:center}
 .opt{background:#121826;border-radius:12px;padding:16px 20px;min-width:200px}
 .opt h3{margin:0 0 10px;color:#9ad7ff;font-size:60px}
 select{width:100%;padding:20px;font-size:60px;background:#1a2030;color:#fff;border:2px solid #2a3140;border-radius:8px} select:focus{outline:6px solid #f0c040;border-color:#f0c040;box-shadow:0 0 40px rgba(240,192,64,1)}
 .hint{font-size:1.3rem;color:#888;margin-top:24px;animation:pulse 1.5s ease-in-out infinite}
 @keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}
 button{font-size:20px;padding:12px 32px;border:none;border-radius:10px;cursor:pointer;background:#f0c040;color:#000;font-weight:bold;margin-top:16px}
</style>
</head><body>
<div class="match-num">Match {{match_num}} of 10</div>
<div class="overall">{{ms.home_team}} <strong>{{ms.home_score}}</strong> — <strong>{{ms.away_score}}</strong> {{ms.away_team}}</div>
<div class="team">{{ms.home_team}}</div>
<div class="player">{{home_player}}</div>
<div class="vs">v</div>
<div class="team">{{ms.away_team}}</div>
<div class="player">{{away_player}}</div>
<form method="post" action="/match/startgame">
<div class="options">
  <div class="opt">
    <h3>{{home_player}} starts at</h3>
    <select name="home_end">
      <option value="clock">Clock end</option>
      <option value="window">Window end</option>
    </select>
  </div>
  <div class="opt">
    <h3>First server</h3>
    <select name="server">
      <option value="A">{{home_player}}</option>
      <option value="B">{{away_player}}</option>
    </select>
  </div>
</div>
<button type="submit" style="font-size:52px;padding:24px 60px;background:#f0c040;color:#000;font-weight:900;border:none;border-radius:16px;cursor:pointer;box-shadow:0 0 60px rgba(240,192,64,0.9);width:100%;margin-top:20px">Start Match ▶</button>
</form>
</body></html>
"""

MATCH_DOUBLES_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Doubles Selection</title>
<style>
 *{box-sizing:border-box}
 body{font-family:system-ui,Arial;background:#0b0e14;color:#fff;margin:0;padding:20px;font-size:18px}
 h1{font-size:32px;text-align:center;color:#f0c040;margin-bottom:20px}
 h2{font-size:32px;color:#9ad7ff;margin:16px 0 8px;font-weight:700}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
 .col{background:#121826;padding:16px;border-radius:12px}
 label{display:block;margin-bottom:12px;font-size:40px;font-weight:600}
 select{width:100%;padding:20px;font-size:40px;background:#1a2030;color:#fff;border:2px solid #2a3140;border-radius:8px;margin-top:4px} select:focus{outline:6px solid #f0c040;border-color:#f0c040;box-shadow:0 0 40px rgba(240,192,64,1)}
 .actions{display:flex;gap:12px;justify-content:center;margin-top:20px}
 button{font-size:40px;padding:20px 50px;border:none;border-radius:10px;cursor:pointer;background:#f0c040;color:#000;font-weight:bold} button:focus{outline:6px solid #fff;box-shadow:0 0 40px rgba(240,192,64,1)}
 .score{text-align:center;font-size:24px;margin-bottom:20px;color:#9ad7ff}
</style>
</head><body>
<h1>🏓 Doubles Selection</h1>
<div class="score">{{ms.home_team}} {{ms.home_score}} — {{ms.away_score}} {{ms.away_team}}</div>
<form method="post" action="/match/doubles">
<div class="grid">
  <div class="col">
    <h2>🏠 {{ms.home_team}}</h2>
    <label>Player 1<select name="home_d1">
      {% for p in ms.home_players %}<option value="{{p}}">{{p}}</option>{% endfor %}
    </select></label>
    <label>Player 2<select name="home_d2">
      {% for p in ms.home_players %}<option value="{{p}}">{{p}}</option>{% endfor %}
    </select></label>
  </div>
  <div class="col">
    <h2>✈️ {{ms.away_team}}</h2>
    <label>Player 1<select name="away_d1">
      {% for p in ms.away_players %}<option value="{{p}}">{{p}}</option>{% endfor %}
    </select></label>
    <label>Player 2<select name="away_d2">
      {% for p in ms.away_players %}<option value="{{p}}">{{p}}</option>{% endfor %}
    </select></label>
  </div>
</div>
<div class="actions">
  <button type="submit">Confirm Doubles ▶</button>
</div>
</form>
</body></html>
"""

MATCH_RESULT_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Match Result</title>
<style>
 body{font-family:system-ui,Arial;background:#0b0e14;color:#fff;margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;flex-direction:column;text-align:center}
 .result{font-size:clamp(20px,4vw,40px);color:#888;margin-bottom:10px}
 .players{font-size:clamp(30px,5vw,55px);font-weight:700;color:#fff;margin:8px 0}
 .score{font-size:clamp(60px,12vw,130px);font-weight:900;color:#f0c040;line-height:1}
 .winner{font-size:clamp(45px,7vw,90px);color:#4cff91;margin-top:16px;font-weight:900}
 .overall{font-size:clamp(20px,3vw,36px);color:#9ad7ff;margin-top:20px}
 .hint{font-size:1.5rem;color:#888;margin-top:30px;animation:pulse 1.5s ease-in-out infinite}
 @keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}
</style>
<script>
document.addEventListener('keydown', function(e) {
  if (e.code === 'Space' || e.key === 'Enter') {
    window.location.href = '/match/next';
  }
});
</script>
</head><body>
<div class="result">Match {{match_num}} Result</div>
<div class="players">{{home_player}} v {{away_player}}</div>
<div class="score">{{ends_home}} — {{ends_away}}</div>
<div class="winner">{{winner}} wins!</div>
<div class="overall">{{ms.home_team}} <span style="font-weight:900">{{ms.home_score}}</span> — <span style="font-weight:900">{{ms.away_score}}</span> {{ms.away_team}}</div>
<div class="hint">Press SPACE or ENTER to continue</div>
</body></html>
"""

MATCH_FINAL_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Final Result</title>
<style>
 *{box-sizing:border-box}
 body{font-family:system-ui,Arial;background:#0b0e14;color:#fff;margin:0;padding:20px}
 h1{font-size:clamp(30px,5vw,60px);color:#f0c040;text-align:center;margin-bottom:4px}
 .meta{text-align:center;color:#888;font-size:1.1rem;margin-bottom:16px}
 .teams{font-size:clamp(20px,3vw,40px);font-weight:700;text-align:center;margin:8px 0}
 .finalscore{font-size:clamp(60px,12vw,140px);font-weight:900;color:#f0c040;line-height:1;text-align:center}
 .winner{font-size:clamp(25px,4vw,50px);color:#4cff91;font-weight:900;text-align:center;margin:10px 0 20px}
 table{width:100%;border-collapse:collapse;font-size:clamp(14px,2vw,20px)}
 th{background:#121826;color:#9ad7ff;padding:10px;border:1px solid #2a3140;text-align:center}
 td{padding:10px;border:1px solid #2a3140;text-align:center}
 .end-scores{font-size:1.1em;color:#f0c040;font-weight:600;letter-spacing:1px}
 .hw{color:#4cff91;font-weight:700}
 .aw{color:#ff6b6b;font-weight:700}
 .btn{font-size:20px;padding:12px 32px;border:none;border-radius:10px;cursor:pointer;background:#f0c040;color:#000;font-weight:bold;margin:20px auto;display:block}
 @media print{.btn{display:none}}
</style>
</head><body>
<h1>🏆</h1>
<div class="meta">
  {% if ms.division %}{{ms.division}} · {% endif %}
  {% if ms.date %}{{ms.date}} · {% endif %}
  {% if ms.venue %}{{ms.venue}}{% endif %}
</div>
<div class="teams">{{ms.home_team}} v {{ms.away_team}}</div>
<div class="finalscore">{{ms.home_score}} — {{ms.away_score}}</div>
<div class="winner">{{winner}} win!</div>
<table>
  <tr><th>#</th><th>Players</th><th>End Scores</th><th>Result</th></tr>
  {% for r in ms.match_results %}
  <tr>
    <td>{{loop.index}}</td>
    <td><span class="{{'hw' if r.home_ends > r.away_ends else ''}}">{{r.home_player}}</span> v <span class="{{'hw' if r.away_ends > r.home_ends else ''}}">{{r.away_player}}</span></td>
    <td class="end-scores">{% for e in r.ends %}{{e[0]}}-{{e[1]}}{% if not loop.last %} · {% endif %}{% endfor %}</td>
    <td><strong class="{{'hw' if r.home_ends > r.away_ends else 'aw'}}">{{r.home_ends}}-{{r.away_ends}}</strong></td>
  </tr>
  {% endfor %}
</table>
<a href="/score"><button class="btn">Back to Scoreboard</button></a>
</body></html>
"""


SECRET_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Usage Log</title>
<style>
 body{font-family:system-ui,Arial;background:#0b0e14;color:#fff;margin:0;padding:20px}
 h1{font-size:24px;margin-bottom:8px}
 h2{font-size:18px;margin:24px 0 8px 0;color:#9ad7ff}
 table{width:100%;border-collapse:collapse;font-size:15px;margin-bottom:16px}
 th,td{padding:8px;border:1px solid #2a3140;text-align:left}
 th{background:#121826}
 .del{background:#ff4444;color:#fff;border:none;padding:5px 12px;cursor:pointer;border-radius:6px}
 .delall{background:#ff4444;color:#fff;border:none;padding:8px 18px;cursor:pointer;border-radius:6px;margin-top:8px;font-size:15px}
 .savebtn{background:#2a7aff;color:#fff;border:none;padding:8px 18px;cursor:pointer;border-radius:6px;font-size:15px}
 input[type=number]{background:#1a2030;color:#fff;border:1px solid #2a3140;padding:6px;border-radius:6px;width:80px}
 a{color:#9ad7ff}
</style>
</head><body>
<h1>📊 Usage Log <a href="/score" style="font-size:14px;margin-left:20px">Back to scoreboard</a></h1>

<h2>🎮 Games Played ({{count}})</h2>
<form method="post" action="/secret/deleteall">
  <button class="delall" onclick="return confirm('Delete ALL game entries?')">Delete All Games</button>
</form>
<br>
<table>
  <tr><th>#</th><th>Time</th><th>Delete</th></tr>
  {% for i, entry in entries %}
  <tr>
    <td>{{i+1}}</td>
    <td>{{entry}}</td>
    <td><form method="post" action="/secret/delete"><input type="hidden" name="index" value="{{i}}"><button class="del">X</button></form></td>
  </tr>
  {% endfor %}
</table>

<h2>💡 Lights Log ({{lights_count}})</h2>
<form method="post" action="/secret/setlux" style="margin-bottom:10px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
  <span>Threshold: <input type="number" name="threshold" value="{{threshold}}" step="1" min="1" max="500"> lux</span>
  <button class="savebtn">Save</button>
</form>
<form method="post" action="/secret/deletelights">
  <button class="delall" onclick="return confirm('Delete ALL light entries?')">Delete All Lights</button>
</form>
<br>
<table>
  <tr><th>#</th><th>Time</th></tr>
  {% for i, entry in lights %}
  <tr><td>{{i+1}}</td><td>{{entry}}</td></tr>
  {% endfor %}
</table>
</body></html>
"""


@app.route("/secret")
def secret():
    log_path = os.path.expanduser("~/pingpong_usage.log")
    entries = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            entries = [l.strip() for l in f.readlines() if l.strip()]
    lights_path = os.path.expanduser("~/pingpong_lights.log")
    lights = []
    if os.path.exists(lights_path):
        with open(lights_path) as f:
            lights = [l.strip() for l in f.readlines() if l.strip()]
    threshold = get_lux_threshold()
    return render_template_string(SECRET_HTML, entries=list(enumerate(entries)), count=len(entries), lights=list(enumerate(lights)), lights_count=len(lights), threshold=threshold)

@app.route("/secret/setlux", methods=["POST"])
def secret_setlux():
    val = float(request.form.get("threshold", 30))
    set_lux_threshold(val)
    return redirect(url_for("secret"))

@app.route("/secret/deletelights", methods=["POST"])
def secret_deletelights():
    log_path = os.path.expanduser("~/pingpong_lights.log")
    if os.path.exists(log_path):
        open(log_path, "w").close()
    return redirect(url_for("secret"))

@app.route("/secret/delete", methods=["POST"])
def secret_delete():
    idx = int(request.form.get("index", -1))
    log_path = os.path.expanduser("~/pingpong_usage.log")
    if os.path.exists(log_path):
        with open(log_path) as f:
            entries = [l for l in f.readlines() if l.strip()]
        if 0 <= idx < len(entries):
            entries.pop(idx)
        with open(log_path, "w") as f:
            f.writelines(entries)
    return redirect(url_for("secret"))

@app.route("/secret/deleteall", methods=["POST"])
def secret_deleteall():
    log_path = os.path.expanduser("~/pingpong_usage.log")
    if os.path.exists(log_path):
        open(log_path, "w").close()
    return redirect(url_for("secret"))

# ======================================================================
# Light Monitor
# Load lux threshold from file
LUX_THRESHOLD_FILE = os.path.expanduser("~/pingpong_lux_threshold.txt")
def get_lux_threshold():
    try:
        with open(LUX_THRESHOLD_FILE) as f:
            return float(f.read().strip())
    except:
        return 30.0

def set_lux_threshold(val):
    with open(LUX_THRESHOLD_FILE, "w") as f:
        f.write(str(val))

def light_monitor():
    import smbus2
    import datetime
    last_state = None
    bus = None
    last_lux = 0.0
    while True:
        try:
            if bus is None:
                bus = smbus2.SMBus(1)
            bus.write_byte(0x23, 0x01)
            time.sleep(0.1)
            bus.write_byte(0x23, 0x10)
            time.sleep(0.5)
            data = bus.read_i2c_block_data(0x23, 0x10, 2)
            lux = (data[0] << 8 | data[1]) / 1.2
            last_lux = lux
            threshold = get_lux_threshold()
            state = "ON" if lux >= threshold else "OFF"
            if state != last_state:
                last_state = state
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                with open(os.path.expanduser("~/pingpong_lights.log"), "a") as f:
                    f.write(f"{ts} - Lights {state} ({lux:.0f} lux)\n")
                print(f"Lights {state} - {lux:.0f} lux")
        except Exception as e:
            print("Light sensor error:", e)
            bus = None
        time.sleep(10)

light_thread = threading.Thread(target=light_monitor, daemon=True)
light_thread.start()


# ======================================================================
# MATCH ROUTES
# ======================================================================

@app.route("/match/setup", methods=["GET", "POST"])
def match_setup():
    if request.method == "POST":
        match_system.division = request.form.get("division", "")
        match_system.date = request.form.get("date", "")
        match_system.venue = request.form.get("venue", "")
        match_system.home_team = request.form.get("home_team", "Home")
        match_system.away_team = request.form.get("away_team", "Away")
        match_system.home_players = [
            request.form.get("home_p1", "Player 1"),
            request.form.get("home_p2", "Player 2"),
            request.form.get("home_p3", "Player 3"),
        ]
        match_system.away_players = [
            request.form.get("away_p1", "Player 1"),
            request.form.get("away_p2", "Player 2"),
            request.form.get("away_p3", "Player 3"),
        ]
        match_system.home_end = request.form.get("home_end", "clock")
        match_system.away_end = request.form.get("away_end", "window")
        match_system.current_match = 0
        match_system.home_score = 0
        match_system.away_score = 0
        match_system.match_results = []
        match_system.active = True
        match_system.state = "upnext"
        return redirect(url_for("match_upnext"))
    import datetime
    if not match_system.date:
        match_system.date = datetime.datetime.now().strftime("%d/%m/%y")
    if not match_system.venue:
        match_system.venue = "The Hut"
    home_end = match_system.home_end or "clock"
    away_end = "window" if home_end == "clock" else "clock"
    return render_template_string(MATCH_SETUP_HTML, ms=match_system, home_end=home_end, away_end=away_end)

@app.route("/match/upnext")
def match_upnext():
    hp, ap = match_system.get_current_players()
    match_num = match_system.current_match + 1
    # Determine ends based on game number (swap each game)
    if match_system.current_match % 2 == 0:
        home_player_end = match_system.home_end
        away_player_end = match_system.away_end
    else:
        home_player_end = match_system.away_end
        away_player_end = match_system.home_end
    return render_template_string(MATCH_UPNEXT_HTML, ms=match_system,
        home_player=hp, away_player=ap, match_num=match_num,
        home_player_end=home_player_end, away_player_end=away_player_end)

@app.route("/match/startgame", methods=["GET", "POST"])
def match_startgame():
    hp, ap = match_system.get_current_players()
    home_end = request.form.get("home_end", "clock") if request.method == "POST" else "clock"
    server = request.form.get("server", "A") if request.method == "POST" else "A"
    # Reset scoreboard for new game
    with state_obj.lock:
        state_obj.a = 0
        state_obj.b = 0
        state_obj.sets_a = 0
        state_obj.sets_b = 0
        state_obj.game = 1
        state_obj.game_over = False
        state_obj.match_over = False
        state_obj.winner_text = ""
        state_obj.banner = ""
        state_obj.last_action = "Ready"
        state_obj.server = server
    cfg.player_a_name = hp
    cfg.player_b_name = ap
    cfg.sets_to_win = 3
    match_system.state = "playing"
    match_system.home_end_current = home_end
    return redirect(url_for("score"))

@app.route("/match/result")
def match_result():
    ends_home = state_obj.sets_a
    ends_away = state_obj.sets_b
    hp, ap = match_system.get_current_players()
    if ends_home > ends_away:
        winner = hp
        match_system.home_score += 1
    else:
        winner = ap
        match_system.away_score += 1
    # Record final end score
    if state_obj.a > 0 or state_obj.b > 0:
        match_system.current_ends.append((state_obj.a, state_obj.b))
    match_system.match_results.append({
        "home_player": hp,
        "away_player": ap,
        "home_ends": ends_home,
        "away_ends": ends_away,
        "winner": winner,
        "ends": list(match_system.current_ends)
    })
    match_system.current_ends = []
    return render_template_string(MATCH_RESULT_HTML, ms=match_system,
        home_player=hp, away_player=ap,
        ends_home=ends_home, ends_away=ends_away,
        winner=winner, match_num=match_system.current_match + 1)

@app.route("/match/next")
def match_next():
    match_system.current_match += 1
    if match_system.current_match >= len(MATCH_ORDER):
        return redirect(url_for("match_final"))
    if MATCH_ORDER[match_system.current_match] == (0, 0):
        return redirect(url_for("match_doubles"))
    return redirect(url_for("match_upnext"))

@app.route("/match/doubles", methods=["GET", "POST"])
def match_doubles():
    if request.method == "POST":
        match_system.doubles_home = [
            request.form.get("home_d1", ""),
            request.form.get("home_d2", "")
        ]
        match_system.doubles_away = [
            request.form.get("away_d1", ""),
            request.form.get("away_d2", "")
        ]
        return redirect(url_for("match_upnext"))
    return render_template_string(MATCH_DOUBLES_HTML, ms=match_system)

@app.route("/match/abandon")
def match_abandon():
    match_system.reset()
    with state_obj.lock:
        state_obj.game_over = False
        state_obj.match_over = False
        state_obj.banner = ""
    return redirect(url_for("score"))

@app.route("/match/final")
def match_final():
    match_system.active = False
    match_system.state = "finished"
    if match_system.home_score > match_system.away_score:
        winner = match_system.home_team
    elif match_system.away_score > match_system.home_score:
        winner = match_system.away_team
    else:
        winner = "It's a draw —"
    return render_template_string(MATCH_FINAL_HTML, ms=match_system, winner=winner)



# ======================================================================
# EMAIL AND WIFI MONITOR
# ======================================================================
def send_usage_report():
    import smtplib
    import json
    import datetime
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    cfg_path = os.path.expanduser("~/pingpong_email.json")
    if not os.path.exists(cfg_path):
        print("No email config found")
        return

    cfg = json.load(open(cfg_path))

    lights_path = os.path.expanduser("~/pingpong_lights.log")
    lights = []
    if os.path.exists(lights_path):
        with open(lights_path) as f:
            lights = [l.strip() for l in f.readlines() if l.strip()]

    if not lights:
        print("Nothing to report")
        return

    now = datetime.datetime.now().strftime("%d/%m/%y %H:%M")
    body = f"Table Tennis Hut - Lights Report\nGenerated: {now}\n{'='*40}\n\n"
    body += f"LIGHTS LOG ({len(lights)} entries)\n" + "-"*30 + "\n"
    for l in lights:
        body += f"  {l}\n"

    msg = MIMEMultipart()
    msg['From'] = cfg['email_from']
    msg['To'] = cfg['email_to']
    msg['Subject'] = f"TT Hut Lights Report - {now}"
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(cfg['email_from'], cfg['email_pass'])
        server.sendmail(cfg['email_from'], cfg['email_to'], msg.as_string())
        server.quit()
        print("Report email sent!")
    except Exception as e:
        print("Email error:", e)

def wifi_monitor():
    import subprocess
    last_connected = False
    first_check = True
    while True:
        try:
            result = subprocess.run(['nmcli', '-t', '-f', 'ACTIVE,SSID', 'dev', 'wifi'],
                capture_output=True, text=True)
            connected = 'yes:Jamiehotspot' in result.stdout
            if connected and (not last_connected or first_check):
                print("Connected to Jamiehotspot - sending report...")
                time.sleep(5)
                send_usage_report()
            last_connected = connected
            first_check = False
        except Exception as e:
            print("WiFi monitor error:", e)
        time.sleep(30)

wifi_thread = threading.Thread(target=wifi_monitor, daemon=True)
wifi_thread.start()
# ======================================================================
# Lifecycle
# ======================================================================

def start_backend():
    global backend
    # Start Flic only if library is present and at least one MAC is set
    if fliclib and (cfg.flic_mac_a.strip() or cfg.flic_mac_b.strip()):
        backend = FlicBackend(cfg, state_obj)
        try:
            backend.start()
        except Exception as e:
            print("Backend start error:", e)
    else:
        backend = None
        if cfg.flic_mac_a.strip() or cfg.flic_mac_b.strip():
            print("Flic MACs set but fliclib not available; keyboard still works.")

def shutdown(*_):
    try:
        if backend: backend.stop()
    finally:
        os._exit(0)

def main():
    start_backend()
    signal(SIGINT, shutdown)
    signal(SIGTERM, shutdown)
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)

if __name__ == "__main__":
    main()

