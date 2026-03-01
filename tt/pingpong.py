#!/usr/bin/env python3
# Table Tennis Scoreboard
# Run with: python pingpong.py
# Then open: http://localhost:5000/score

import json, os, threading, time, hashlib
from dataclasses import dataclass, asdict
from typing import Optional
from flask import Flask, request, redirect, url_for, render_template, jsonify, session, abort
from signal import SIGINT, SIGTERM, signal

try:
    import fliclib
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
    points_per_game: int = 11
    win_by_two: bool = True
    sets_to_win: int = 0
    flic_mac_a: str = ""
    flic_mac_b: str = ""
    double_click_ms: int = 350
    long_press_ms: int = 1000
    initial_server: str = "A"
    serve_every_points: int = 2
    admin_pin_hash: str = sha256("2468")

def load_config() -> Config:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)
        valid_keys = set(Config.__dataclass_fields__.keys())
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return Config(**{**asdict(Config()), **filtered})
    return Config()

def save_config(cfg: Config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(asdict(cfg), f, indent=2)

class MatchState:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.reset_match()

    def reset_match(self):
        with self.lock:
            self.game = 1
            self.sets_a = 0
            self.sets_b = 0
            self.a = 0
            self.b = 0
            self.total_points_in_current_game = 0
            self.server = self.cfg.initial_server
            self.game_over = False
            self.match_over = False
            self.last_action = "Ready"
            self.winner_text = ""
            self.banner = ""
            self.end_history = []
            self.mid_end_swapped = False
            self.mid_end_display_swapped = False
            self.start_swapped = False

    def reset_game(self):
        with self.lock:
            self.a = 0
            self.b = 0
            self.total_points_in_current_game = 0
            self.server = 'A' if ((self.game % 2) == (1 if self.cfg.initial_server == 'A' else 0)) else 'B'
            self.game_over = False
            self.last_action = f"New game started (Game {self.game})"
            self.winner_text = ""
            self.banner = ""
            self.mid_end_swapped = False
            self.mid_end_display_swapped = False

    def _should_switch_every_point(self) -> bool:
        return (self.a >= self.cfg.points_per_game - 1) and (self.b >= self.cfg.points_per_game - 1)

    def _maybe_rotate_serve_after_point(self):
        self.total_points_in_current_game += 1
        total = self.a + self.b
        # At deuce switch every point
        if self.a >= self.cfg.points_per_game - 1 and self.b >= self.cfg.points_per_game - 1:
            self.server = 'A' if self.server == 'B' else 'B'
        else:
            # Switch every serve_every_points based on total points
            if total % self.cfg.serve_every_points == 0:
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
                self.last_action = "Game finished - hold to start the next game."
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

            # Mid-end swap in deciding game when total points reaches 5
            deciding_game = (
                self.cfg.sets_to_win > 0 and
                self.sets_a + 1 == self.cfg.sets_to_win and
                self.sets_b + 1 == self.cfg.sets_to_win
            )
            if deciding_game and (self.a >= 5 or self.b >= 5) and not self.mid_end_swapped:
                self.mid_end_swapped = True
                self.mid_end_display_swapped = True
                self.banner = "⇄ Swap ends!"

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
                    self.end_history.append(f"{self.a}-{self.b} ({winner})")
                    history_text = "   ".join(self.end_history)
                    self.winner_text = f"Game {self.game} to {winner} ({self.a}-{self.b})"
                    self.banner = self.winner_text + "\n" + history_text
                self.last_action = self.winner_text

                if self.cfg.sets_to_win and (
                    self.sets_a >= self.cfg.sets_to_win or self.sets_b >= self.cfg.sets_to_win
                ):
                    self.match_over = True
                    self.last_action = f"Match finished - {winner} wins {self.sets_a}-{self.sets_b}"

    def remove_point(self, who: str):
        with self.lock:
            changed = False
            if who == "A" and self.a > 0:
                if self.game_over and self.a > self.b:
                    self.sets_a = max(0, self.sets_a - 1)
                    if self.end_history:
                        self.end_history.pop()
                self.a -= 1; changed = True; self.last_action = f"{self.cfg.player_a_name} -1"
            elif who == "B" and self.b > 0:
                if self.game_over and self.b > self.a:
                    self.sets_b = max(0, self.sets_b - 1)
                    if self.end_history:
                        self.end_history.pop()
                self.b -= 1; changed = True; self.last_action = f"{self.cfg.player_b_name} -1"
            if changed:
                self.game_over = False
                self.match_over = False
                self.winner_text = ""
                self.banner = ""
                self._maybe_recalculate_server_after_correction()

    def next_game(self):
        with self.lock:
            if self.match_over and self.cfg.sets_to_win:
                self.last_action = "Match over - press N for a new match."
                return
            if match_system.active and (self.a > 0 or self.b > 0):
                match_system.current_ends.append((self.a, self.b))
            self.game += 1
            self.reset_game()
            import datetime
            log_path = os.path.expanduser("~/pingpong_usage.log")
            with open(log_path, "a") as f:
                f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + f" - Game {self.game} started\n")

    def toggle_server(self):
        with self.lock:
            self.server = 'A' if self.server == 'B' else 'B'
            self.last_action = f"Server switched to {'Player ' + self.server}"

# ======================================================================
# Flic Backend
# ======================================================================

class FlicBackend:
    def __init__(self, cfg: Config, state: MatchState):
        self.cfg = cfg; self.state = state
        self.client = None
        a = cfg.flic_mac_a.lower().strip() if cfg.flic_mac_a else ""
        b = cfg.flic_mac_b.lower().strip() if cfg.flic_mac_b else ""
        self.want = {x for x in (a, b) if x}
        self._thread = None
        self._stop = threading.Event()

    def _on_button_event(self, bdaddr, click_type):
        if not bdaddr: return
        swapped = (self.state.game % 2 == 0) if self.cfg.sets_to_win > 0 else False
        is_mac_a = bdaddr.lower() == (self.cfg.flic_mac_a or "").lower()
        is_mac_b = bdaddr.lower() == (self.cfg.flic_mac_b or "").lower()
        if is_mac_a: who = "B" if swapped else "A"
        elif is_mac_b: who = "A" if swapped else "B"
        else: who = None
        if not who: return
        self.state.banner = ""
        if click_type == "ButtonSingleClick": self.state.add_point(who)
        elif click_type == "ButtonDoubleClick":
            if self.state.a == 0 and self.state.b == 0: self.state.toggle_server()
            else: self.state.remove_point(who)
        elif click_type == "ButtonHold": self.state.next_game()

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
                    for b in info["bd_addr_of_verified_buttons"]: got_button(b)
                self.client.get_info(got_info)
                self.client.on_new_verified_button = lambda b: got_button(b)
                while not self._stop.is_set():
                    self.client.handle_events()
                    time.sleep(0.1)
            except Exception as e:
                print("flicd error, retrying in 5s:", e)
                time.sleep(5)

    def start(self):
        if fliclib is None: print("fliclib not available"); return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("FLIC backend started.")

    def stop(self): self._stop.set()

# ======================================================================
# Match System
# ======================================================================

MATCH_ORDER = [
    (1,2),(3,1),(2,3),(1,1),(2,2),(3,3),(2,1),(1,3),(3,2),(0,0)
]

class MatchSystem:
    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.home_team = ""
        self.away_team = ""
        self.home_players = ["","",""]
        self.away_players = ["","",""]
        self.home_end = "clock"
        self.away_end = "window"
        self.doubles_home = ["",""]
        self.doubles_away = ["",""]
        self.current_match = 0
        self.home_score = 0
        self.away_score = 0
        self.match_results = []
        self.state = "idle"
        self.division = ""
        self.date = ""
        self.venue = ""
        self.current_ends = []

    def get_current_players(self):
        if self.current_match >= len(MATCH_ORDER): return ("","")
        h, a = MATCH_ORDER[self.current_match]
        if h == 0:
            return (
                f"{self.doubles_home[0]} & {self.doubles_home[1]}",
                f"{self.doubles_away[0]} & {self.doubles_away[1]}"
            )
        return (self.home_players[h-1], self.away_players[a-1])

match_system = MatchSystem()

# ======================================================================
# Flask App
# ======================================================================

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = "tabletennis-scoreboard-secret"

cfg = load_config()
state_obj = MatchState(cfg)
backend = None

def parse_sets_to_win(raw):
    raw = (raw or "").strip()
    if raw == "": return 0
    try: v = int(raw); return max(0, v)
    except: return 0

# -------------------- Routes --------------------

@app.route("/")
def root(): return redirect(url_for("score"))

@app.route("/score")
def score():
    return render_template("score.html", cfg=cfg)

@app.route("/state")
def get_state():
    with state_obj.lock:
        out = dict(
            a=state_obj.a, b=state_obj.b, game=state_obj.game,
            sets_a=state_obj.sets_a, sets_b=state_obj.sets_b,
            swapped=False if cfg.sets_to_win == 0 else state_obj.game % 2 == 0,
            mid_end_display_swapped=getattr(state_obj, 'mid_end_display_swapped', False),
            start_swapped=getattr(state_obj, 'start_swapped', False),
            sets_to_win=cfg.sets_to_win,
            match_active=match_system.active,
            match_home_team=match_system.home_team,
            match_away_team=match_system.away_team,
            match_home_score=match_system.home_score,
            match_away_score=match_system.away_score,
            name_a=cfg.player_a_name,
            name_b=cfg.player_b_name,
            status=state_obj.last_action,
            server=state_obj.server,
            banner=state_obj.banner
        )
    return jsonify(out)

@app.route("/quick", methods=["POST"])
def quick_update():
    a = (request.form.get("player_a_name") or "").strip() or cfg.player_a_name
    b = (request.form.get("player_b_name") or "").strip() or cfg.player_b_name
    stw = parse_sets_to_win(request.form.get("sets_to_win","0"))
    serv = (request.form.get("server_now") or state_obj.server).strip().upper()
    if serv not in ("A","B"): serv = state_obj.server
    cfg.player_a_name = a; cfg.player_b_name = b; cfg.sets_to_win = stw
    save_config(cfg)
    with state_obj.lock:
        state_obj.cfg = cfg; state_obj.server = serv
    return ("", 204)

@app.route("/admin", methods=["GET"])
def admin():
    if session.get("admin", False): return redirect(url_for("setup"))
    return render_template("login.html", error=None)

@app.route("/admin/login", methods=["POST"])
def admin_login():
    pin = request.form.get("pin","")
    if sha256(pin) == cfg.admin_pin_hash:
        session["admin"] = True
        return redirect(url_for("setup"))
    return render_template("login.html", error="Incorrect PIN.")

@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("score"))

@app.route("/setup", methods=["GET"])
def setup():
    if not session.get("admin", False): return redirect(url_for("admin"))
    return render_template("admin.html", cfg=cfg)

@app.route("/apply", methods=["POST"])
def apply_setup():
    if not session.get("admin", False): abort(403)
    cfg.player_a_name = request.form.get("player_a_name", cfg.player_a_name).strip() or "Player A"
    cfg.player_b_name = request.form.get("player_b_name", cfg.player_b_name).strip() or "Player B"
    cfg.points_per_game = int(request.form.get("points_per_game", cfg.points_per_game))
    cfg.sets_to_win = parse_sets_to_win(request.form.get("sets_to_win","0"))
    cfg.win_by_two = True if request.form.get("win_by_two") == "on" else False
    cfg.initial_server = request.form.get("initial_server", cfg.initial_server)
    cfg.serve_every_points = max(1, int(request.form.get("serve_every_points", cfg.serve_every_points)))
    cfg.flic_mac_a = request.form.get("flic_mac_a", cfg.flic_mac_a).strip()
    cfg.flic_mac_b = request.form.get("flic_mac_b", cfg.flic_mac_b).strip()
    cfg.double_click_ms = int(request.form.get("double_click_ms", cfg.double_click_ms))
    cfg.long_press_ms = int(request.form.get("long_press_ms", cfg.long_press_ms))
    new_pin = (request.form.get("admin_pin") or "").strip()
    if new_pin: cfg.admin_pin_hash = sha256(new_pin)
    save_config(cfg)
    state_obj.cfg = cfg
    return redirect(url_for("setup"))

@app.route("/key")
def key_action():
    cmd = request.args.get("cmd","")
    state_obj.banner = ""
    end_swapped = (state_obj.game % 2 == 0) if cfg.sets_to_win > 0 else False
    mid_swapped = getattr(state_obj, 'mid_end_display_swapped', False)
    swapped = end_swapped != mid_swapped
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
    if match_system.active and state_obj.match_over:
        return jsonify({"redirect": "/match/result"})
    return ("", 204)

# -------------------- Match Routes --------------------

@app.route("/match/setup", methods=["GET","POST"])
def match_setup():
    if request.method == "POST":
        match_system.division = request.form.get("division","")
        match_system.date = request.form.get("date","")
        match_system.venue = request.form.get("venue","")
        match_system.home_team = request.form.get("home_team","Home")
        match_system.away_team = request.form.get("away_team","Away")
        match_system.home_players = [
            request.form.get("home_p1","Player 1"),
            request.form.get("home_p2","Player 2"),
            request.form.get("home_p3","Player 3"),
        ]
        match_system.away_players = [
            request.form.get("away_p1","Player 1"),
            request.form.get("away_p2","Player 2"),
            request.form.get("away_p3","Player 3"),
        ]
        match_system.home_end = request.form.get("home_end","clock")
        match_system.away_end = request.form.get("away_end","window")
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
    return render_template("match_setup.html", ms=match_system)

@app.route("/match/upnext")
def match_upnext():
    hp, ap = match_system.get_current_players()
    match_num = match_system.current_match + 1
    return render_template("match_upnext.html", ms=match_system,
        home_player=hp, away_player=ap, match_num=match_num)

@app.route("/match/startgame", methods=["GET","POST"])
def match_startgame():
    hp, ap = match_system.get_current_players()
    server = request.form.get("server","A") if request.method == "POST" else "A"
    home_end = request.form.get("home_end","clock") if request.method == "POST" else "clock"
    # If home player starts at window end, they appear on the right (swapped)
    start_swapped = (home_end == "window")
    with state_obj.lock:
        state_obj.a = 0; state_obj.b = 0
        state_obj.sets_a = 0; state_obj.sets_b = 0
        state_obj.game = 1
        state_obj.game_over = False; state_obj.match_over = False
        state_obj.winner_text = ""; state_obj.banner = ""
        state_obj.last_action = "Ready"
        state_obj.server = server
        state_obj.end_history = []
        state_obj.mid_end_swapped = False
        state_obj.mid_end_display_swapped = False
        state_obj.start_swapped = start_swapped
    cfg.player_a_name = hp
    cfg.player_b_name = ap
    cfg.sets_to_win = 3
    match_system.state = "playing"
    return redirect(url_for("score"))

@app.route("/match/result")
def match_result():
    ends_home = state_obj.sets_a
    ends_away = state_obj.sets_b
    hp, ap = match_system.get_current_players()
    if ends_home > ends_away:
        winner = hp; match_system.home_score += 1
    else:
        winner = ap; match_system.away_score += 1
    if state_obj.a > 0 or state_obj.b > 0:
        match_system.current_ends.append((state_obj.a, state_obj.b))
    match_system.match_results.append({
        "home_player": hp, "away_player": ap,
        "home_ends": ends_home, "away_ends": ends_away,
        "winner": winner, "ends": list(match_system.current_ends)
    })
    match_system.current_ends = []
    return render_template("match_result.html", ms=match_system,
        home_player=hp, away_player=ap,
        ends_home=ends_home, ends_away=ends_away,
        winner=winner, match_num=match_system.current_match+1)

@app.route("/match/next")
def match_next():
    match_system.current_match += 1
    if match_system.current_match >= len(MATCH_ORDER):
        return redirect(url_for("match_final"))
    if MATCH_ORDER[match_system.current_match] == (0,0):
        return redirect(url_for("match_doubles"))
    return redirect(url_for("match_upnext"))

@app.route("/match/doubles", methods=["GET","POST"])
def match_doubles():
    if request.method == "POST":
        match_system.doubles_home = [request.form.get("home_d1",""), request.form.get("home_d2","")]
        match_system.doubles_away = [request.form.get("away_d1",""), request.form.get("away_d2","")]
        return redirect(url_for("match_upnext"))
    return render_template("match_doubles.html", ms=match_system)

@app.route("/match/abandon")
def match_abandon():
    match_system.reset()
    with state_obj.lock:
        state_obj.game_over = False; state_obj.match_over = False; state_obj.banner = ""
    return redirect(url_for("score"))

@app.route("/match/final")
def match_final():
    match_system.active = False; match_system.state = "finished"
    if match_system.home_score > match_system.away_score: winner = match_system.home_team
    elif match_system.away_score > match_system.home_score: winner = match_system.away_team
    else: winner = "It's a draw -"
    return render_template("match_final.html", ms=match_system, winner=winner)

# -------------------- Secret / Logs --------------------

@app.route("/secret")
def secret():
    log_path = os.path.expanduser("~/pingpong_usage.log")
    entries = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            entries = [l.strip() for l in f if l.strip()]
    lights_path = os.path.expanduser("~/pingpong_lights.log")
    lights = []
    if os.path.exists(lights_path):
        with open(lights_path) as f:
            lights = [l.strip() for l in f if l.strip()]
    threshold = get_lux_threshold()
    return render_template("secret.html",
        entries=list(enumerate(entries)), count=len(entries),
        lights=list(enumerate(lights)), lights_count=len(lights),
        threshold=threshold)

@app.route("/secret/setlux", methods=["POST"])
def secret_setlux():
    val = float(request.form.get("threshold", 30))
    set_lux_threshold(val)
    return redirect(url_for("secret"))

@app.route("/secret/deletelights", methods=["POST"])
def secret_deletelights():
    p = os.path.expanduser("~/pingpong_lights.log")
    if os.path.exists(p): open(p,"w").close()
    return redirect(url_for("secret"))

@app.route("/secret/delete", methods=["POST"])
def secret_delete():
    idx = int(request.form.get("index",-1))
    p = os.path.expanduser("~/pingpong_usage.log")
    if os.path.exists(p):
        with open(p) as f: entries = [l for l in f if l.strip()]
        if 0 <= idx < len(entries): entries.pop(idx)
        with open(p,"w") as f: f.writelines(entries)
    return redirect(url_for("secret"))

@app.route("/secret/deleteall", methods=["POST"])
def secret_deleteall():
    p = os.path.expanduser("~/pingpong_usage.log")
    if os.path.exists(p): open(p,"w").close()
    return redirect(url_for("secret"))

# ======================================================================
# Light Monitor
# ======================================================================

LUX_THRESHOLD_FILE = os.path.expanduser("~/pingpong_lux_threshold.txt")

def get_lux_threshold():
    try:
        with open(LUX_THRESHOLD_FILE) as f: return float(f.read().strip())
    except: return 30.0

def set_lux_threshold(val):
    with open(LUX_THRESHOLD_FILE,"w") as f: f.write(str(val))

def light_monitor():
    import datetime
    last_state = None
    bus = None
    while True:
        try:
            import smbus2
            if bus is None: bus = smbus2.SMBus(1)
            bus.write_byte(0x23, 0x01); time.sleep(0.1)
            bus.write_byte(0x23, 0x10); time.sleep(0.5)
            data = bus.read_i2c_block_data(0x23, 0x10, 2)
            lux = (data[0] << 8 | data[1]) / 1.2
            threshold = get_lux_threshold()
            state = "ON" if lux >= threshold else "OFF"
            if state != last_state:
                last_state = state
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                with open(os.path.expanduser("~/pingpong_lights.log"),"a") as f:
                    f.write(f"{ts} - Lights {state} ({lux:.0f} lux)\n")
                print(f"Lights {state} - {lux:.0f} lux")
        except Exception as e:
            pass
        time.sleep(10)

threading.Thread(target=light_monitor, daemon=True).start()

# ======================================================================
# Email & WiFi Monitor
# ======================================================================

def send_usage_report():
    import smtplib, datetime
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    cfg_path = os.path.expanduser("~/pingpong_email.json")
    if not os.path.exists(cfg_path): print("No email config"); return
    ecfg = json.load(open(cfg_path))
    lights_path = os.path.expanduser("~/pingpong_lights.log")
    lights = []
    if os.path.exists(lights_path):
        with open(lights_path) as f: lights = [l.strip() for l in f if l.strip()]
    if not lights: print("Nothing to report"); return
    now = datetime.datetime.now().strftime("%d/%m/%y %H:%M")
    body = f"Table Tennis Hut - Lights Report\nGenerated: {now}\n{'='*40}\n\n"
    body += f"LIGHTS LOG ({len(lights)} entries)\n" + "-"*30 + "\n"
    for l in lights: body += f"  {l}\n"
    msg = MIMEMultipart()
    msg['From'] = ecfg['email_from']; msg['To'] = ecfg['email_to']
    msg['Subject'] = f"TT Hut Lights Report - {now}"
    msg.attach(MIMEText(body,'plain'))
    try:
        s = smtplib.SMTP('smtp.gmail.com', 587); s.starttls()
        s.login(ecfg['email_from'], ecfg['email_pass'])
        s.sendmail(ecfg['email_from'], ecfg['email_to'], msg.as_string())
        s.quit(); print("Report email sent!")
    except Exception as e: print("Email error:", e)

def check_for_updates():
    import urllib.request, hashlib
    url = "https://raw.githubusercontent.com/silkjamie009/tabletennis-scoreboard-/main/pingpong.py"
    local_path = os.path.join(os.path.dirname(__file__), "pingpong.py")
    try:
        print("Checking for updates...")
        with urllib.request.urlopen(url, timeout=10) as r: new_code = r.read()
        with open(local_path,'rb') as f: current_code = f.read()
        if hashlib.md5(new_code).hexdigest() != hashlib.md5(current_code).hexdigest():
            print("Update found! Downloading...")
            with open(local_path,'wb') as f: f.write(new_code)
            print("Update downloaded - restarting...")
            import subprocess
            subprocess.Popen(['bash','-c','sleep 2 && python3 ' + local_path + ' &'])
            os._exit(0)
        else:
            print("Already up to date!")
    except Exception as e: print("Update check failed:", e)

def wifi_monitor():
    import subprocess
    last_connected = False; first_check = True
    while True:
        try:
            result = subprocess.run(['nmcli','-t','-f','ACTIVE,SSID','dev','wifi'],
                capture_output=True, text=True)
            connected = 'yes:Jamiehotspot' in result.stdout
            if connected and (not last_connected or first_check):
                print("Connected to Jamiehotspot - checking updates and sending report...")
                time.sleep(5)
                check_for_updates()
                send_usage_report()
            last_connected = connected; first_check = False
        except Exception as e: print("WiFi monitor error:", e)
        time.sleep(30)

threading.Thread(target=wifi_monitor, daemon=True).start()

# ======================================================================
# Lifecycle
# ======================================================================

def start_backend():
    global backend
    if fliclib and (cfg.flic_mac_a.strip() or cfg.flic_mac_b.strip()):
        backend = FlicBackend(cfg, state_obj)
        try: backend.start()
        except Exception as e: print("Backend start error:", e)

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
