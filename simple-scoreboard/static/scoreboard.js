async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return response.json();
}

function render(state) {
  const values = {
    "left-score": state.left,
    "right-score": state.right,
    "control-left-score": state.left,
    "control-right-score": state.right,
  };

  for (const [id, value] of Object.entries(values)) {
    const element = document.getElementById(id);
    if (element) element.textContent = value;
  }

  const leftServer = document.getElementById("left-server");
  const rightServer = document.getElementById("right-server");
  if (leftServer) leftServer.classList.toggle("active", state.server === "left");
  if (rightServer) rightServer.classList.toggle("active", state.server === "right");

  const winner = document.getElementById("winner");
  if (winner) {
    winner.hidden = !state.game_over;
    winner.textContent = state.game_over ? `${state.winner} wins` : "";
  }
}

async function refresh() {
  try {
    render(await api("/api/state"));
  } catch (error) {
    console.error(error);
  }
}

document.querySelectorAll("[data-side]").forEach((button) => {
  button.addEventListener("click", async () => {
    render(await api("/api/score", {
      method: "POST",
      body: JSON.stringify({
        side: button.dataset.side,
        operation: button.dataset.operation,
      }),
    }));
  });
});

const newGame = document.getElementById("new-game");
if (newGame) {
  newGame.addEventListener("click", async () => {
    if (!window.confirm("Start a new game and reset both scores?")) return;
    render(await api("/api/new-game", { method: "POST", body: "{}" }));
  });
}

const changeServer = document.getElementById("change-server");
if (changeServer) {
  changeServer.addEventListener("click", async () => {
    render(await api("/api/server", { method: "POST", body: "{}" }));
  });
}

refresh();
setInterval(refresh, 500);
