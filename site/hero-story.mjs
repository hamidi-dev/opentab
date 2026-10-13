// A close-up of one synthetic session. tools/build-story.py rebuilds this small
// snapshot from the report; the hero never needs to download the full report.
const story = document.querySelector("[data-story]");
const money = n => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n);
const compact = n => new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);
const date = value => new Date(value + "T12:00:00Z").toLocaleDateString("en-US", {
  month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
});
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function stats(items) {
  const row = el("div", "story-stats");
  for (const [value, label] of items) {
    const item = el("span");
    item.append(el("b", "", value), document.createTextNode(" " + label));
    row.append(item);
  }
  return row;
}

async function init() {
  const response = await fetch("media/hero-story.json");
  if (!response.ok) throw new Error("Story snapshot unavailable");
  const data = await response.json();
  const session = data.session;
  const selectedDate = session.date.slice(0, 10);
  const day = data.days.find(d => d.date === selectedDate);
  const panels = [...story.querySelectorAll('[role="tabpanel"]')];
  const tabs = [...story.querySelectorAll("[data-story-step]")];
  const next = story.querySelector("[data-story-next]");
  const open = story.querySelector(".story-bottom [data-story-open]");
  const captions = ["Follow the highlighted session", "Own recorded cost per agent", "Same session, down to the work"];
  let step = 0;
  function select(index, focus = false) {
    step = index;
    panels.forEach((panel, i) => { panel.hidden = i !== index; });
    tabs.forEach((tab, i) => {
      tab.setAttribute("aria-selected", String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
    });
    next.hidden = index === 2;
    open.hidden = index !== 2;
    next.textContent = index === 0 ? "Where did it go? →" : "What did they do? →";
    story.querySelector("[data-story-caption]").textContent = `0${index + 1} / 03 · ${captions[index]}`;
    if (focus) tabs[index].focus({ preventScroll: true });
  }
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(i));
    tab.addEventListener("keydown", event => {
      const keys = { ArrowRight: (i + 1) % 3, ArrowLeft: (i + 2) % 3, Home: 0, End: 2 };
      if (!(event.key in keys)) return;
      event.preventDefault();
      select(keys[event.key], true);
    });
  });
  next.addEventListener("click", () => select(step + 1, true));

  // The whole year, then the selected day's largest sessions.
  const overview = panels[0];
  overview.append(el("p", "story-label", `${date(selectedDate)} · ${data.daySessions.length} sessions`));
  const headline = el("div", "story-headline");
  headline.append(el("span", "story-number", money(day.cost)), el("small", "", "recorded + estimated"));
  overview.append(headline);
  const heatmap = el("div", "story-heatmap");
  heatmap.setAttribute("role", "img");
  heatmap.setAttribute("aria-label", `A year of daily spend; ${date(selectedDate)} highlighted at ${money(day.cost)}.`);
  const max = Math.max(...data.days.map(d => d.cost));
  const offset = (new Date(data.days[0].date + "T12:00:00Z").getUTCDay() + 6) % 7;
  for (let i = 0; i < offset; i++) {
    const cell = el("i");
    cell.style.visibility = "hidden";
    heatmap.append(cell);
  }
  data.days.forEach(d => {
    const cell = el("i", d.date === selectedDate ? "selected" : "");
    cell.style.setProperty("--heat", d.cost ? `${18 + 82 * Math.sqrt(d.cost / max)}%` : "0%");
    cell.title = `${date(d.date)} · ${money(d.cost)}`;
    heatmap.append(cell);
  });
  const axis = el("div", "story-axis");
  axis.append(el("span", "", date(data.days[0].date)), el("span", "selected-day", `${date(selectedDate)} ↑`));
  overview.append(heatmap, axis);
  data.daySessions.slice(0, 3).forEach(s => {
    const featured = s.id === session.id;
    const row = el(featured ? "button" : "div", "story-session" + (featured ? " featured" : ""));
    row.append(el("span", "", s.title), el("strong", "", money(s.api)));
    if (featured) {
      row.type = "button";
      row.title = "Explore this session's agents";
      row.addEventListener("click", () => select(1, true));
    }
    overview.append(row);
  });
  overview.append(el("p", "story-caption", `${Math.round(session.api / day.cost * 100)}% of this day's total came from one session. Follow it in →`));

  // Per-node self costs: nested agents aren't counted twice.
  const agents = panels[1];
  agents.append(el("p", "story-label", `${session.source} · one session`), el("h3", "", session.title));
  agents.append(stats([[money(session.real), "recorded"], [String(Math.round(session.dur / 60)) + "m", "elapsed"], [String(session.subagents), "subagents"]]));
  const colors = ["--lav", "--t-input", "--t-cache-write", "--t-reasoning", "--t-cache-read"];
  for (const [i, node] of data.nodes.entries()) {
    const row = el("div", "story-node" + (i === 0 ? " root-node" : ""));
    row.style.setProperty("--depth", Math.min(node.depth, 2));
    row.style.setProperty("--share", `${node.real / session.real * 100}%`);
    row.style.setProperty("--node-color", `var(${colors[i % colors.length]})`);
    const title = i === 0 ? "Main agent" : node.title.replace(/ \(@.* subagent\)$/, "");
    const label = el("span", "node-title", title);
    label.title = `${node.title}${node.depth > 1 ? " · nested subagent" : ""}`;
    row.append(label, el("span", "node-cost", money(node.real)));
    agents.append(row);
  }
  agents.append(el("p", "story-caption", `${Math.round((session.real - data.nodes[0].real) / session.real * 100)}% of the recorded cost is in delegated work.`));

  // Root-agent actions and measured context from the same session.
  const work = panels[2];
  work.append(el("p", "story-label", "It started with this prompt"), el("blockquote", "story-prompt", data.prompt));
  const turns = el("div", "story-turns");
  for (const turn of data.firstTurns) {
    const item = el("div", "story-turn");
    item.append(el("small", "", turn.time.slice(11)), el("code", "", turn.tools.join(" + ")));
    turns.append(item);
  }
  work.append(turns, el("p", "story-label", "Main agent · context across the session"));
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.classList.add("story-context");
  svg.setAttribute("viewBox", "0 0 520 70");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  const peak = Math.max(...data.context);
  svg.setAttribute("aria-label", `Root agent context over ${data.context.length} measurements, peaking at ${peak.toLocaleString("en-US")} tokens.`);
  const points = data.context.map((value, i) => `${(i / (data.context.length - 1) * 520).toFixed(1)},${(65 - value / peak * 60).toFixed(1)}`).join(" ");
  const fill = document.createElementNS(ns, "polygon");
  fill.setAttribute("points", `0,70 ${points} 520,70`);
  fill.classList.add("context-fill");
  const line = document.createElementNS(ns, "polyline");
  line.setAttribute("points", points);
  svg.append(fill, line);
  const contextAxis = el("div", "story-axis");
  contextAxis.append(el("span", "", "Session start"), el("span", "", `${compact(peak)} tokens at peak`));
  work.append(svg, contextAxis, el("p", "story-caption", `${data.turns} turns · ${data.calls} tool calls across the session. Open the report to inspect them.`));

  story.querySelector(".story-loading").hidden = true;
  story.querySelector(".story-tabs").hidden = false;
  story.querySelector(".story-bottom").hidden = false;
  select(0);
}

init().catch(() => {
  // Keep the plain report link available if a snapshot is missing or offline.
  story.querySelector(".story-loading").hidden = false;
});
