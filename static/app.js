// ════════════════════════════════════════════════════════════════════════════════
// DATOS DE INICIALIZACIÓN (embebidos por Python en el HTML como JSON)
// El árbol D3, las fechas por defecto y los límites de los date pickers
// vienen de <script type="application/json" id="init-data"> en index.html.
// ════════════════════════════════════════════════════════════════════════════════
const _init       = JSON.parse(document.getElementById("init-data").textContent);
const rawData      = _init.tree;
const defaultStart = _init.defaultStart;
const defaultEnd   = _init.defaultEnd;

let currentData = null;

// useAdjusted: si true usa adjPrices en lugar de prices para calcular pct
let useAdjusted = false;

// Celda activa en touch (para toggle: primer tap muestra, segundo tap oculta)
let _touchCell = null;

// Rango de fechas activo; usado por downloadPNG para el nombre del archivo
let _activeDateRange = { start: defaultStart, end: defaultEnd };


// ════════════════════════════════════════════════════════════════════════════════
// ESCALA DE COLOR POR PASOS (discreta)
// Replica los colores del diseño de referencia con 7 niveles saturados.
// ════════════════════════════════════════════════════════════════════════════════
function discreteColor(pct) {
  if (pct >= 4)   return "#00c897";
  if (pct >= 2)   return "#00a87e";
  if (pct >= 0.5) return "#007a5a";
  if (pct > -0.5) return "#2a3548";
  if (pct > -2)   return "#a02040";
  if (pct > -4)   return "#d93050";
  return "#ff4d6d";
}


// ════════════════════════════════════════════════════════════════════════════════
// BOTONES DE PERÍODO RÁPIDO
// data-days="0": HOY → start = end = today → cambio diario real (base = cierre anterior)
// data-days="N": start = today − N días, end = today
// ════════════════════════════════════════════════════════════════════════════════
function formatDateISO(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

document.getElementById("period-btns").addEventListener("click", e => {
  const btn = e.target.closest(".period-btn");
  if (!btn) return;
  document.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  const days = parseInt(btn.dataset.days);
  const endDate   = new Date();
  const startDate = new Date();
  if (days > 0) startDate.setDate(endDate.getDate() - days);
  document.getElementById("date-end").value   = formatDateISO(endDate);
  document.getElementById("date-start").value = formatDateISO(startDate);
  applyFilter();
});

// Auto-filter al cambiar manualmente los date pickers
["date-start", "date-end"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => {
    document.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
    applyFilter();
  });
});


// ════════════════════════════════════════════════════════════════════════════════
// MOVERS BAR — top 3 gainers y losers del período actual
// ════════════════════════════════════════════════════════════════════════════════
function updateMovers(data) {
  const leaves = [];
  data.children.forEach(s => s.children.forEach(t => leaves.push(t)));
  if (!leaves.length) return;

  leaves.sort((a, b) => b.pct - a.pct);
  const topN    = Math.min(3, Math.floor(leaves.length / 2));
  const gainers = leaves.slice(0, topN);
  const losers  = leaves.slice(-topN).reverse();

  function buildChip(t, cls) {
    const chip = document.createElement("span");
    chip.className = `mover-chip ${cls}`;
    const ticker = document.createElement("span");
    ticker.className = "m-ticker";
    ticker.textContent = t.name;
    const pct = document.createElement("span");
    pct.className = "m-pct";
    pct.textContent = cls === "up"
      ? ` +${t.pct.toFixed(2)}%`
      : ` ${t.pct.toFixed(2)}%`;
    chip.appendChild(ticker);
    chip.appendChild(pct);
    return chip;
  }

  document.getElementById("movers-up").replaceChildren(
    ...gainers.map(t => buildChip(t, "up"))
  );
  document.getElementById("movers-down").replaceChildren(
    ...losers.map(t => buildChip(t, "down"))
  );
}


// ════════════════════════════════════════════════════════════════════════════════
// RECALCULAR DATOS PARA UN RANGO DE FECHAS (operación local, sin red)
// ════════════════════════════════════════════════════════════════════════════════
function computeDataForRange(startStr, endStr) {
  const newSectors = rawData.children.map(sector => {
    const newTickers = sector.children.map(ticker => {
      const priceData = useAdjusted ? ticker.adjPrices : ticker.prices;
      const dates = Object.keys(priceData).sort();

      const beforeStart = dates.filter(d => d < startStr);
      const toEnd       = dates.filter(d => d <= endStr);

      if (!beforeStart.length || !toEnd.length) return null;

      const dIni = beforeStart[beforeStart.length - 1];
      const dFin = toEnd[toEnd.length - 1];
      const pIni = priceData[dIni];
      const pFin = priceData[dFin];

      const pct = +((pFin / pIni - 1) * 100).toFixed(2);
      // Market cap siempre con precio nominal para que el área refleje el valor de mercado real
      const nominalFin = ticker.prices[dFin] ?? pFin;
      const mcap = ticker.shares > 0 ? ticker.shares * nominalFin : nominalFin * 1e6;

      return { ...ticker, pct, price: nominalFin, value: mcap };
    }).filter(Boolean);

    return { name: sector.name, children: newTickers };
  }).filter(s => s.children.length > 0);

  return { name: "IPSA", children: newSectors };
}


// ════════════════════════════════════════════════════════════════════════════════
// UTILIDADES DE UI
// ════════════════════════════════════════════════════════════════════════════════
function showProgress() { document.getElementById("progress-bar-wrap").style.display = "block"; }
function hideProgress() { document.getElementById("progress-bar-wrap").style.display = "none"; }

function setStatus(type, msg) {
  const el = document.getElementById("filter-status");
  el.textContent = msg;
  el.className   = type;
}

function updateBadge(startStr, endStr) {
  _activeDateRange = { start: startStr, end: endStr };
}

function validateDates() {
  const startVal = document.getElementById("date-start").value;
  const endVal   = document.getElementById("date-end").value;
  if (!startVal || !endVal)  { setStatus("error", "⚠ Fechas inválidas");    return null; }
  if (startVal > endVal)     { setStatus("error", "⚠ Rango inválido");       return null; }
  return { startVal, endVal };
}

function countLeaves(tree) {
  return tree.children.reduce((acc, s) => acc + s.children.length, 0);
}


// ════════════════════════════════════════════════════════════════════════════════
// TOGGLE NOMINAL / AJUSTADO POR DIVIDENDOS
// ════════════════════════════════════════════════════════════════════════════════
function toggleAdjusted() {
  useAdjusted = !useAdjusted;
  document.getElementById("adj-toggle").classList.toggle("active", useAdjusted);
  document.getElementById("lbl-nominal").classList.toggle("on", !useAdjusted);
  document.getElementById("lbl-adj").classList.toggle("on",  useAdjusted);
  const dates = validateDates();
  if (!dates) return;
  currentData = computeDataForRange(dates.startVal, dates.endVal);
  if (countLeaves(currentData) > 0) render(currentData);
}


// ════════════════════════════════════════════════════════════════════════════════
// APLICAR FILTRO LOCAL — sin botón; llamado por presets y por cambio de fecha
// ════════════════════════════════════════════════════════════════════════════════
function applyFilter() {
  const dates = validateDates();
  if (!dates) return;
  const { startVal, endVal } = dates;

  currentData = computeDataForRange(startVal, endVal);

  if (countLeaves(currentData) === 0) {
    setStatus("error", "⚠ Sin datos en ese rango");
    return;
  }

  updateBadge(startVal, endVal);
  setStatus("ok", `✓ ${countLeaves(currentData)} tickers`);
  render(currentData);
}


// ════════════════════════════════════════════════════════════════════════════════
// ACTUALIZAR CON DATOS FRESCOS — descarga 13 meses nuevos desde Yahoo Finance
// Llamado automáticamente al cargar la página; no hay botón manual.
// ════════════════════════════════════════════════════════════════════════════════
async function freshUpdate() {
  const dates = validateDates();
  if (!dates) return;
  const { startVal, endVal } = dates;

  setStatus("loading", "⟳ Descargando...");
  showProgress();

  try {
    const response = await fetch("/refresh");

    if (!response.ok) {
      const err = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
      throw new Error(err.error || `HTTP ${response.status}`);
    }

    const freshTree = await response.json();
    if (freshTree.error) throw new Error(freshTree.error);

    rawData.children = freshTree.children;

    currentData = computeDataForRange(startVal, endVal);

    if (countLeaves(currentData) === 0) {
      setStatus("error", "⚠ Sin datos");
      return;
    }

    updateBadge(startVal, endVal);
    setStatus("ok", `✓ ${countLeaves(currentData)} tickers`);
    render(currentData);

  } catch (err) {
    setStatus("error", `⚠ ${err.message}`);
  } finally {
    hideProgress();
  }
}


// ════════════════════════════════════════════════════════════════════════════════
// RENDERIZADO DEL TREEMAP
//
// Escala de color: discreteColor() — 7 pasos saturados (mismo esquema del diseño ref.)
// Sombra inferior: gradiente SVG objectBoundingBox → transparent→rgba(0,0,0,0.3)
//   replica el efecto ::before { linear-gradient(to bottom, transparent 40%, rgba(0,0,0,0.3)) }
// En móvil (≤640px) se usa renderMobile(): sectores apilados verticalmente.
// En escritorio se usa renderDesktop(): treemap 2D clásico.
// ════════════════════════════════════════════════════════════════════════════════
const svg       = d3.select("#treemap");
const container = document.getElementById("chart-container");
const tooltip   = document.getElementById("tooltip");

function isMobile() { return window.innerWidth <= 640; }

function render(data) {
  if (isMobile()) { renderMobile(data); } else { renderDesktop(data); }
}

function renderDesktop(data) {
  svg.style("height", null).attr("height", null);
  svg.selectAll("*").remove();

  const W = container.clientWidth;
  const H = container.clientHeight;
  svg.attr("viewBox", `0 0 ${W} ${H}`);

  // Gradiente para sombra inferior en cada celda (objectBoundingBox = relativo a cada rect)
  const defs = svg.append("defs");
  const grad = defs.append("linearGradient")
    .attr("id", "cell-shadow")
    .attr("x1", "0").attr("y1", "0")
    .attr("x2", "0").attr("y2", "1");
  grad.append("stop").attr("offset", "0%")   .attr("stop-color", "#000").attr("stop-opacity", "0");
  grad.append("stop").attr("offset", "40%")  .attr("stop-color", "#000").attr("stop-opacity", "0");
  grad.append("stop").attr("offset", "100%") .attr("stop-color", "#000").attr("stop-opacity", "0.32");

  const root = d3.hierarchy(data)
    .sum(d => d.value || 0)
    .sort((a, b) => b.value - a.value);

  d3.treemap()
    .tile(d3.treemapSquarify.ratio(1.2))
    .size([W, H])
    .paddingOuter(5)
    .paddingTop(20)
    .paddingInner(2)
    (root);

  // ── Grupos de sector ────────────────────────────────────────────────────────
  const groups = svg.selectAll(".ig").data(root.children).join("g").attr("class", "ig");

  groups.append("rect")
    .attr("x", d => d.x0).attr("y", d => d.y0)
    .attr("width",  d => Math.max(0, d.x1 - d.x0))
    .attr("height", d => Math.max(0, d.y1 - d.y0))
    .attr("fill", "#0c1628").attr("rx", 6);

  groups.append("text")
    .attr("class", "industry-label")
    .attr("x", d => d.x0 + 8).attr("y", d => d.y0 + 13)
    .text(d => {
      const maxChars = Math.floor((d.x1 - d.x0) / 6.5);
      return d.data.name.length > maxChars
        ? d.data.name.substring(0, maxChars - 1) + "…"
        : d.data.name;
    });

  // ── Celdas individuales (tickers) ──────────────────────────────────────────
  const cells = svg.selectAll(".cell").data(root.leaves()).join("g")
    .attr("class", "cell").style("cursor", "pointer");

  // 1. Fondo coloreado por variación %
  cells.append("rect")
    .attr("class", "cell-fill")
    .attr("x", d => d.x0 + 1).attr("y", d => d.y0 + 1)
    .attr("width",  d => Math.max(0, d.x1 - d.x0 - 2))
    .attr("height", d => Math.max(0, d.y1 - d.y0 - 2))
    .attr("fill", d => discreteColor(d.data.pct))
    .attr("rx", 5)
    .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave)
    .on("touchstart", onTouchStart);

  // 2. Sombra inferior (gradient overlay, no recibe eventos de mouse)
  cells.append("rect")
    .attr("x", d => d.x0 + 1).attr("y", d => d.y0 + 1)
    .attr("width",  d => Math.max(0, d.x1 - d.x0 - 2))
    .attr("height", d => Math.max(0, d.y1 - d.y0 - 2))
    .attr("fill", "url(#cell-shadow)")
    .attr("rx", 5)
    .attr("pointer-events", "none");

  // 3. Etiquetas ancladas al fondo-izquierda de cada celda
  // Layout de arriba a abajo: ticker → nombre empresa → pct
  cells.each(function(d) {
    const g  = d3.select(this);
    const cw = d.x1 - d.x0;
    const ch = d.y1 - d.y0;

    if (cw < 28 || ch < 18) return;

    const xl       = d.x0 + 10;
    const fsTicker = Math.min(13, Math.max(7, cw / 5.5));
    const fsPct    = Math.min(14, Math.max(7, cw / 5.5));
    const fsName   = 9;
    const pctSign  = d.data.pct >= 0 ? "+" : "";
    const pctText  = `${pctSign}${d.data.pct.toFixed(2)}%`;

    const pctY    = d.y1 - 10;
    const nameY   = pctY - fsPct - 4;
    const tickerY = nameY - fsName - 3;

    if (ch >= 55 && tickerY > d.y0 + 4) {
      const maxChars = Math.max(0, Math.floor((cw - 20) / 5.5));
      const fullName = d.data.fullName || "";
      const nameTrunc = fullName.length > maxChars
        ? fullName.substring(0, Math.max(0, maxChars - 1)) + "…"
        : fullName;

      g.append("text").attr("class", "cell-ticker")
        .attr("x", xl).attr("y", tickerY)
        .attr("font-size", fsTicker + "px").text(d.data.name)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      g.append("text").attr("class", "cell-name")
        .attr("x", xl).attr("y", nameY)
        .attr("font-size", fsName + "px").text(nameTrunc)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      g.append("text").attr("class", "cell-pct")
        .attr("x", xl).attr("y", pctY)
        .attr("font-size", fsPct + "px").text(pctText)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);

    } else if (ch >= 36) {
      const ty = pctY - fsPct - 5;
      g.append("text").attr("class", "cell-ticker")
        .attr("x", xl).attr("y", ty > d.y0 + 4 ? ty : pctY - fsTicker - 3)
        .attr("font-size", fsTicker + "px").text(d.data.name)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      g.append("text").attr("class", "cell-pct")
        .attr("x", xl).attr("y", pctY)
        .attr("font-size", fsPct + "px").text(pctText)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);

    } else {
      g.append("text").attr("class", "cell-ticker")
        .attr("x", xl).attr("y", pctY)
        .attr("font-size", fsTicker + "px").text(d.data.name)
        .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
    }
  });

  updateMovers(data);
}


// ════════════════════════════════════════════════════════════════════════════════
// RENDERIZADO MÓVIL — sectores apilados verticalmente, scroll nativo
// Cada sector recibe altura proporcional a su market cap (mínimo 90px).
// Dentro de cada sector se aplica d3.treemap() igual que en escritorio.
// ════════════════════════════════════════════════════════════════════════════════
function renderMobile(data) {
  svg.selectAll("*").remove();

  const W          = container.clientWidth || window.innerWidth;
  const sectorGap  = 4;
  const headerH    = 18;
  const minSectorH = 90;

  const totalValue = data.children.reduce((sum, s) =>
    sum + s.children.reduce((a, t) => a + (t.value || 0), 0), 0);

  // Altura proporcional al market cap de cada sector, con mínimo garantizado
  const targetTotal = Math.max(data.children.length * 130, 1400);
  const sectorHeights = data.children.map(s => {
    const sv = s.children.reduce((a, t) => a + (t.value || 0), 0);
    return Math.max(minSectorH, (sv / totalValue) * targetTotal);
  });
  const totalH = sectorHeights.reduce((sum, h) => sum + h, 0)
               + (data.children.length + 1) * sectorGap;

  svg.attr("viewBox", `0 0 ${W} ${totalH}`)
     .attr("height", totalH)
     .style("height", totalH + "px");

  // Gradiente de sombra inferior (igual que en escritorio)
  const defs = svg.append("defs");
  const grad = defs.append("linearGradient")
    .attr("id", "cell-shadow")
    .attr("x1", "0").attr("y1", "0")
    .attr("x2", "0").attr("y2", "1");
  grad.append("stop").attr("offset", "0%")   .attr("stop-color", "#000").attr("stop-opacity", "0");
  grad.append("stop").attr("offset", "40%")  .attr("stop-color", "#000").attr("stop-opacity", "0");
  grad.append("stop").attr("offset", "100%") .attr("stop-color", "#000").attr("stop-opacity", "0.32");

  let yOffset = sectorGap;

  data.children.forEach((sector, si) => {
    const sH = sectorHeights[si];
    const sX = sectorGap;
    const sW = W - sectorGap * 2;

    // Fondo del sector
    svg.append("rect")
      .attr("x", sX).attr("y", yOffset)
      .attr("width", sW).attr("height", sH)
      .attr("fill", "#0c1628").attr("rx", 6);

    // Etiqueta del sector
    svg.append("text")
      .attr("class", "industry-label")
      .attr("x", sX + 8).attr("y", yOffset + 13)
      .text(sector.name);

    // Área interior para los tickers
    const innerX = sX + 2;
    const innerY = yOffset + headerH + 2;
    const innerW = sW - 4;
    const innerH = sH - headerH - 4;

    // Treemap D3 para los tickers de este sector
    const sectorRoot = d3.hierarchy({ name: sector.name, children: sector.children })
      .sum(d => d.value || 0)
      .sort((a, b) => b.value - a.value);

    d3.treemap()
      .tile(d3.treemapSquarify.ratio(1.2))
      .size([innerW, innerH])
      .paddingInner(2)
      (sectorRoot);

    // Grupo desplazado al origen del área interior del sector
    const sectorGroup = svg.append("g")
      .attr("transform", `translate(${innerX}, ${innerY})`);

    const cells = sectorGroup.selectAll(".cell")
      .data(sectorRoot.leaves())
      .join("g")
      .attr("class", "cell")
      .style("cursor", "pointer");

    // Fondo de cada celda coloreado por variación %
    cells.append("rect")
      .attr("class", "cell-fill")
      .attr("x", d => d.x0 + 1).attr("y", d => d.y0 + 1)
      .attr("width",  d => Math.max(0, d.x1 - d.x0 - 2))
      .attr("height", d => Math.max(0, d.y1 - d.y0 - 2))
      .attr("fill", d => discreteColor(d.data.pct))
      .attr("rx", 5)
      .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave)
      .on("touchstart", onTouchStart);

    // Sombra inferior (gradient overlay, sin eventos de puntero)
    cells.append("rect")
      .attr("x", d => d.x0 + 1).attr("y", d => d.y0 + 1)
      .attr("width",  d => Math.max(0, d.x1 - d.x0 - 2))
      .attr("height", d => Math.max(0, d.y1 - d.y0 - 2))
      .attr("fill", "url(#cell-shadow)")
      .attr("rx", 5)
      .attr("pointer-events", "none");

    // Etiquetas ancladas al fondo-izquierda de cada celda (mismo criterio que escritorio)
    cells.each(function(d) {
      const g  = d3.select(this);
      const cw = d.x1 - d.x0;
      const ch = d.y1 - d.y0;

      if (cw < 28 || ch < 18) return;

      const xl       = d.x0 + 10;
      const fsTicker = Math.min(13, Math.max(7, cw / 5.5));
      const fsPct    = Math.min(14, Math.max(7, cw / 5.5));
      const fsName   = 9;
      const pctSign  = d.data.pct >= 0 ? "+" : "";
      const pctText  = `${pctSign}${d.data.pct.toFixed(2)}%`;

      const pctY    = d.y1 - 10;
      const nameY   = pctY - fsPct - 4;
      const tickerY = nameY - fsName - 3;

      if (ch >= 55 && tickerY > d.y0 + 4) {
        const maxChars  = Math.max(0, Math.floor((cw - 20) / 5.5));
        const fullName  = d.data.fullName || "";
        const nameTrunc = fullName.length > maxChars
          ? fullName.substring(0, Math.max(0, maxChars - 1)) + "…"
          : fullName;
        g.append("text").attr("class", "cell-ticker")
          .attr("x", xl).attr("y", tickerY)
          .attr("font-size", fsTicker + "px").text(d.data.name)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
        g.append("text").attr("class", "cell-name")
          .attr("x", xl).attr("y", nameY)
          .attr("font-size", fsName + "px").text(nameTrunc)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
        g.append("text").attr("class", "cell-pct")
          .attr("x", xl).attr("y", pctY)
          .attr("font-size", fsPct + "px").text(pctText)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      } else if (ch >= 36) {
        const ty = pctY - fsPct - 5;
        g.append("text").attr("class", "cell-ticker")
          .attr("x", xl).attr("y", ty > d.y0 + 4 ? ty : pctY - fsTicker - 3)
          .attr("font-size", fsTicker + "px").text(d.data.name)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
        g.append("text").attr("class", "cell-pct")
          .attr("x", xl).attr("y", pctY)
          .attr("font-size", fsPct + "px").text(pctText)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      } else {
        g.append("text").attr("class", "cell-ticker")
          .attr("x", xl).attr("y", pctY)
          .attr("font-size", fsTicker + "px").text(d.data.name)
          .on("mousemove", onMouseMove).on("mouseleave", onMouseLeave);
      }
    });

    yOffset += sH + sectorGap;
  });

  updateMovers(data);
}


// ════════════════════════════════════════════════════════════════════════════════
// TOOLTIP — XSS-safe: construido con DOM APIs, nunca innerHTML
// ════════════════════════════════════════════════════════════════════════════════
function onMouseMove(event, d) {
  if (d.data.pct == null) return;

  const pctCls = d.data.pct >= 0 ? "pos" : "neg";
  const sign   = d.data.pct >= 0 ? "+"  : "";
  const v      = d.value;
  const mcap   = v >= 1e12 ? `$${(v/1e12).toFixed(2)} T`
               : v >= 1e9  ? `$${(v/1e9).toFixed(2)} B`
               :              `$${(v/1e6).toFixed(0)} M`;

  function tipEl(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls)          e.className   = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function tipRow(label, value, cls) {
    const r = tipEl("div", "tip-row");
    r.appendChild(tipEl("span", null, label));
    r.appendChild(tipEl("span", cls ? `tip-val ${cls}` : "tip-val", value));
    return r;
  }
  tooltip.replaceChildren(
    tipEl("div", "tip-ticker", d.data.name),
    tipEl("div", "tip-name",   d.data.fullName),
    tipEl("div", "tip-sep"),
    tipRow("Industria",  d.parent.data.name),
    tipRow("Precio",     `$${Number(d.data.price).toLocaleString("es-CL",{minimumFractionDigits:2})}`),
    tipRow("Market Cap", mcap),
    tipRow("Variación",  `${sign}${d.data.pct.toFixed(2)}%`, pctCls),
  );

  const pad = 12, tw = 220, th = 170;
  let tx = event.clientX + pad;
  let ty = event.clientY - pad;
  tx = Math.max(pad, Math.min(tx, window.innerWidth  - tw - pad));
  ty = Math.max(pad, Math.min(ty, window.innerHeight - th - pad));
  tooltip.style.left = tx + "px";
  tooltip.style.top  = ty + "px";
  tooltip.classList.add("visible");
}

function onMouseLeave() {
  tooltip.classList.remove("visible");
}

function onTouchStart(event, d) {
  if (_touchCell === d) {
    onMouseLeave();
    _touchCell = null;
    return;
  }
  _touchCell = d;
  const t = event.touches[0];
  onMouseMove({ clientX: t.clientX, clientY: t.clientY }, d);
}


// ════════════════════════════════════════════════════════════════════════════════
// DESCARGA COMO PNG
// ════════════════════════════════════════════════════════════════════════════════
async function downloadPNG() {
  const btn = document.getElementById("btn-download");
  btn.disabled    = true;
  btn.textContent = "Generando…";
  tooltip.classList.remove("visible");

  try {
    const canvas = await html2canvas(document.body, {
      backgroundColor: "#080c12",
      scale: Math.max(window.devicePixelRatio * 2, 4),
      useCORS: true,
      logging: false,
      ignoreElements: el => el.id === "tooltip",
    });

    const { start, end } = _activeDateRange;
    const fname = `IPSA_treemap_${start}_${end || "export"}.png`;
    const link  = document.createElement("a");
    link.download = fname;
    link.href     = canvas.toDataURL("image/png");
    link.click();
  } catch (err) {
    console.error("PNG error:", err);
    alert("Error al generar PNG. Intenta nuevamente.");
  } finally {
    btn.disabled    = false;
    btn.textContent = "⬇ PNG";
  }
}


// ════════════════════════════════════════════════════════════════════════════════
// INICIALIZACIÓN
// ════════════════════════════════════════════════════════════════════════════════

// Inicializar date pickers con los valores y límites del servidor
const _dateMin = _init.dateMin;
const _dateMax = _init.dateMax;
["date-start", "date-end"].forEach(id => {
  document.getElementById(id).min = _dateMin;
  document.getElementById(id).max = _dateMax;
});
document.getElementById("date-start").value = defaultStart;
document.getElementById("date-end").value   = defaultEnd;

currentData = computeDataForRange(defaultStart, defaultEnd);
updateBadge(defaultStart, defaultEnd);
render(currentData);

window.addEventListener("resize", () => {
  clearTimeout(window._rt);
  window._rt = setTimeout(() => render(currentData), 80);
});

// Ocultar tooltip al tocar fuera de una celda o al hacer scroll
document.addEventListener("touchstart", e => {
  if (!e.target.closest(".cell")) { onMouseLeave(); _touchCell = null; }
}, { passive: true });
window.addEventListener("scroll", () => { onMouseLeave(); _touchCell = null; }, { passive: true });

// Auto-refresh al entrar: muestra la data embebida de inmediato y luego descarga
// datos frescos en segundo plano; al terminar actualiza los filtros a hoy (7D).
(function autoRefreshOnLoad() {
  const today       = new Date();
  const todayStr    = formatDateISO(today);
  const sevenAgo    = new Date(); sevenAgo.setDate(today.getDate() - 7);
  const sevenAgoStr = formatDateISO(sevenAgo);

  // Corregir el max de los date pickers al día real de hoy (puede diferir del deploy)
  ["date-start", "date-end"].forEach(id => {
    document.getElementById(id).max = todayStr;
  });

  // Precargar los pickers con el rango 7D relativo a hoy
  document.getElementById("date-start").value = sevenAgoStr;
  document.getElementById("date-end").value   = todayStr;
  document.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
  document.querySelector('.period-btn[data-days="7"]').classList.add("active");

  freshUpdate();
})();
