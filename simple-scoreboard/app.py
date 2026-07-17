from flask import Flask, jsonify, render_template, request
from threading import Lock

app = Flask(__name__)
lock = Lock()

state = {
    "left": 0,
    "right": 0,
    "server": "left",
    "game_over": False,
    "winner": None,
}


def recalculate():
    left = state["left"]
    right = state["right"]
    winner = None
    if max(left, right) >= 11 and abs(left - right) >= 2:
        winner = "left" if left > right else "right"
    state["winner"] = winner
    state["game_over"] = winner is not None

    total = left + right
    interval = 1 if left >= 10 and right >= 10 else 2
    state["server"] = "left" if (total // interval) % 2 == 0 else "right"


@app.get("/")
def scoreboard():
    return render_template("scoreboard.html")


@app.get("/control")
def control():
    return render_template("control.html")


@app.get("/api/state")
def get_state():
    with lock:
        return jsonify(state)


@app.post("/api/score")
def score():
    data = request.get_json(silent=True) or {}
    side = data.get("side")
    operation = data.get("operation")

    if side not in {"left", "right"} or operation not in {"add", "subtract"}:
        return jsonify({"error": "Invalid request"}), 400

    with lock:
        if operation == "add":
            state[side] += 1
        elif state[side] > 0:
            state[side] -= 1
        recalculate()
        return jsonify(state)


@app.post("/api/new-game")
def new_game():
    with lock:
        state.update({
            "left": 0,
            "right": 0,
            "server": "left",
            "game_over": False,
            "winner": None,
        })
        return jsonify(state)


@app.post("/api/server")
def change_server():
    with lock:
        state["server"] = "right" if state["server"] == "left" else "left"
        return jsonify(state)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5052, debug=False)
