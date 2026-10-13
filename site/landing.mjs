// Lower-page interactions deliberately do not depend on the report download.
const views = document.querySelector("[data-feature-views]");
const tabs = [...views.querySelectorAll('[role="tab"]')];
const tablist = views.querySelector('[role="tablist"]');
const narrow = matchMedia("(max-width: 760px)");
function orientation() {
  tablist.setAttribute("aria-orientation", narrow.matches ? "horizontal" : "vertical");
}
orientation();
narrow.addEventListener("change", orientation);
function selectView(tab) {
  tabs.forEach(item => {
    const selected = item === tab;
    item.setAttribute("aria-selected", String(selected));
    item.tabIndex = selected ? 0 : -1;
    document.getElementById(item.getAttribute("aria-controls")).hidden = !selected;
  });
}
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectView(tab));
  tab.addEventListener("keydown", event => {
    let next;
    if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else if (["ArrowRight", "ArrowDown"].includes(event.key)) next = (index + 1) % tabs.length;
    else if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = (index + tabs.length - 1) % tabs.length;
    else return;
    event.preventDefault();
    selectView(tabs[next]);
    tabs[next].focus();
  });
});

const colors = new Set(["cache-read", "output", "cache-write", "input", "reasoning"]);
const count = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 });
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 2 });
const econ = document.querySelector("[data-econ]");
const totals = econ.querySelector("[data-econ-totals]");
const readout = econ.querySelector("[data-readout]");
const charts = econ.querySelector("[data-econ-charts]");
const legend = econ.querySelector("[data-legend]");

function validate(data) {
  const validNumber = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
  if (!data || data.basis !== "API list-price estimate" || !validNumber(data.tokens) ||
      !validNumber(data.cost) || data.tokens === 0 || data.cost === 0 ||
      !Array.isArray(data.types) || data.types.length === 0) throw new Error("Missing economics snapshot");
  const seen = new Set();
  for (const type of data.types) {
    if (!colors.has(type.key) || seen.has(type.key) || typeof type.label !== "string" ||
        !type.label.trim() || !validNumber(type.tokens) || !validNumber(type.cost)) {
      throw new Error("Invalid economics bucket");
    }
    seen.add(type.key);
  }
  for (const key of ["tokens", "cost"]) {
    const sum = data.types.reduce((total, type) => total + type[key], 0);
    if (Math.abs(sum - data[key]) > Math.max(1e-8, data[key] * 1e-12)) {
      throw new Error("Economics totals do not match buckets");
    }
  }
  return data;
}

function renderEconomics(data) {
  totals.textContent = `${compact.format(data.tokens)} tokens · ${money.format(data.cost)} API list-price estimate`;
  totals.title = `${count.format(data.tokens)} tokens`;
  const cache = data.types.find(type => type.key === "cache-read");
  if (cache) document.querySelector("[data-econ-intro]").textContent = `In this showcase, cache reads account for ${Math.round(cache.tokens / data.tokens * 100)}% of tokens, but only ${Math.round(cache.cost / data.cost * 100)}% of their estimated cost. Pick a type to compare.`;
  const controls = [];
  function pick(index) {
    controls.forEach(({ button, bucket }) => {
      const selected = bucket === index;
      button.classList.toggle("hot", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    const type = data.types[index];
    readout.textContent = `${type.label}: ${percent.format(type.tokens / data.tokens)} of tokens → ${percent.format(type.cost / data.cost)} of estimated cost. ${compact.format(type.tokens)} tokens · ${money.format(type.cost)}.`;
  }
  function interactive(button, bucket) {
    button.type = "button";
    controls.push({ button, bucket });
    for (const event of ["click", "focus", "mouseenter"]) {
      button.addEventListener(event, () => pick(bucket));
    }
  }
  data.types.forEach((type, index) => {
    for (const [side, key] of [["tok", "tokens"], ["usd", "cost"]]) {
      const share = type[key] / data[key];
      const button = document.createElement("button");
      button.style.background = `var(--t-${type.key})`;
      button.style.flexBasis = `${share * 100}%`;
      // No minimum width: the chart must preserve the actual proportions.
      if (share >= 0.08) button.textContent = percent.format(share);
      const amount = key === "tokens" ? `${count.format(type.tokens)} tokens` : money.format(type.cost);
      button.setAttribute("aria-label", `${type.label}: ${amount}, ${percent.format(share)} of ${key === "tokens" ? "tokens" : "API list-price estimate"}`);
      button.title = button.getAttribute("aria-label");
      // Tiny and zero buckets remain available through the full-size legend.
      button.tabIndex = -1;
      interactive(button, index);
      econ.querySelector(`[data-stack="${side}"]`).append(button);
    }
    const button = document.createElement("button");
    const swatch = document.createElement("i");
    swatch.style.background = `var(--t-${type.key})`;
    swatch.setAttribute("aria-hidden", "true");
    button.append(swatch, document.createTextNode(type.label));
    interactive(button, index);
    legend.append(button);
  });
  charts.hidden = false;
  pick(0);
}

try {
  const response = await fetch("media/hero-story.json");
  if (!response.ok) throw new Error("Snapshot request failed");
  renderEconomics(validate((await response.json()).economics));
} catch {
  charts.hidden = true;
  totals.textContent = "The showcase breakdown is unavailable. See the pricing guide below for how OpenTab distinguishes recorded spend from estimates.";
}
