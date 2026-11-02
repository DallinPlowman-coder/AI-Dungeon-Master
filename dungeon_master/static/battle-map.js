"use strict";

// This view owns only temporary drag visuals. Campaign state is supplied by the controller.
class BattleMapView {
  constructor(container, handlers) {
    this.container = container;
    this.handlers = handlers;
    this.cellPixels = 38;
    this.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.svg.classList.add("tactical-svg");
    this.svg.setAttribute("role", "group");
    container.append(this.svg);
    this.svg.addEventListener("pointerdown", (event) => this.pointerDown(event));
    this.svg.addEventListener("pointermove", (event) => this.pointerMove(event));
    this.svg.addEventListener("pointerup", (event) => this.pointerUp(event));
    this.svg.addEventListener("pointercancel", () => this.cancelDrag());
    this.svg.addEventListener("keydown", (event) => this.keyDown(event));
  }

  render(state, selectedId, busy) {
    this.state = state; this.selectedId = selectedId; this.busy = busy;
    if (!state) { this.svg.replaceChildren(); return; }
    const focused = this.svg.contains(document.activeElement) ? document.activeElement.dataset.tokenId : null;
    const {width, height, terrain, walls, doors} = state.map, margin = 36;
    const w = width * 100, h = height * 100, suffix = this.container.id;
    this.svg.setAttribute("viewBox", `0 0 ${w + 72} ${h + 72}`);
    this.svg.setAttribute("width", (w + 72) * this.cellPixels / 100);
    this.svg.setAttribute("height", (h + 72) * this.cellPixels / 100);
    this.svg.setAttribute("aria-label", `${state.title}, ${width} by ${height} squares, 5 feet per square`);
    this.svg.setAttribute("aria-busy", String(busy));
    const terrainMarkup = terrain.map((tile) => {
      let mark = "";
      if (tile.kind === "difficult") mark = '<path d="m10 75 65-65m-45 80 60-60m-80 20 40-40"/>';
      if (tile.kind === "water") mark = '<path d="M10 30q12-12 24 0t24 0t24 0M10 65q12-12 24 0t24 0t24 0"/>';
      if (tile.kind === "hazard") mark = '<path d="m32 30 36 40m0-40-36 40"/>';
      if (tile.kind === "obstacle") mark = '<rect x="15" y="15" width="70" height="70" rx="3"/><path d="m15 15 70 70m0-70-70 70"/>';
      return `<g class="terrain-tile terrain-${tile.kind}" transform="translate(${tile.x * 100},${tile.y * 100})"><title>${title(tile.kind)} at column ${tile.x + 1}, row ${tile.y + 1}</title><rect class="terrain-fill" width="100" height="100"/>${mark}</g>`;
    }).join("");
    const lines = walls.map((wall) => `<line x1="${wall.x1 * 100}" y1="${wall.y1 * 100}" x2="${wall.x2 * 100}" y2="${wall.y2 * 100}"/>`).join("");
    const doorMarkup = doors.map((door) => `<g class="map-door ${door.open ? "door-open" : "door-closed"}"><title>${esc(door.id)} · ${door.open ? "open" : "closed"}</title><line class="door-gap" x1="${door.x1 * 100}" y1="${door.y1 * 100}" x2="${door.x2 * 100}" y2="${door.y2 * 100}"/><line class="door-leaf" x1="${door.x1 * 100}" y1="${door.y1 * 100}" x2="${door.x2 * 100}" y2="${door.y2 * 100}"/></g>`).join("");
    const labels = Array.from({length:width}, (_, x) => `<text x="${x * 100 + 50}" y="-12">${x + 1}</text>`).join("") +
      Array.from({length:height}, (_, y) => `<text x="-19" y="${y * 100 + 59}">${y + 1}</text>`).join("");
    const tokens = state.tokens.map((token) => {
      const words = token.name.split(/\s+/), chunks = words.length === 2 ? words : [token.name];
      const minimum = Math.max(18, 800 / this.cellPixels), longest = Math.max(...chunks.map((word) => [...word].length));
      const font = Math.max(minimum, Math.min(24, 72 / (longest * .56)));
      const limit = Math.max(3, Math.floor(72 / (font * .56)));
      const display = chunks.map((word) => [...word].length > limit ? [...word].slice(0, limit - 1).join("") + "…" : word);
      const text = display.map((line, index) => `<tspan x="0" y="${display.length === 1 ? font * .35 : index === 0 ? -4 : font}">${esc(line)}</tspan>`).join("");
      return `<g class="battle-token faction-${token.faction}${token.id === selectedId ? " token-selected" : ""}${state.combat.active && state.combat.active_token_id === token.id ? " token-active" : ""}" data-token-id="${token.id}" transform="translate(${token.x * 100 + 50},${token.y * 100 + 50})" role="button" tabindex="0" aria-pressed="${token.id === selectedId}" aria-label="${esc(token.name)}, ${token.faction}, column ${token.x + 1}, row ${token.y + 1}"><title>${esc(token.name)} · ${token.faction}</title><circle class="token-turn-ring" r="46"/><circle class="token-circle" r="40"/><text font-size="${font}" text-anchor="middle">${text}</text></g>`;
    }).join("");
    this.svg.innerHTML = `<defs><pattern id="grid-${suffix}" width="100" height="100" patternUnits="userSpaceOnUse"><path d="M100 0H0V100"/></pattern></defs><g transform="translate(${margin},${margin})"><rect class="board-paper" width="${w}" height="${h}"/><g class="terrain-layer">${terrainMarkup}</g><rect class="grid-layer" width="${w}" height="${h}" fill="url(#grid-${suffix})"/><g class="wall-layer">${lines}</g><g class="door-layer">${doorMarkup}</g><g class="coordinate-labels">${labels}</g><g class="token-layer">${tokens}</g></g>`;
    if (focused) this.svg.querySelector(`[data-token-id="${focused}"]`)?.focus({preventScroll:true});
  }

  point(event) {
    const matrix = this.svg.getScreenCTM();
    if (!matrix) return null;
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    return {x:point.x - 36, y:point.y - 36};
  }

  pointerDown(event) {
    if (!this.state || this.busy || event.button !== 0) return;
    const element = event.target.closest("[data-token-id]");
    const token = this.state.tokens.find((item) => item.id === element?.dataset.tokenId);
    const start = this.point(event);
    if (!start) return;
    if (token) {
      event.preventDefault();
      element.focus({preventScroll:true});
      this.handlers.select(token.id);
      this.svg.setPointerCapture(event.pointerId);
    }
    this.drag = {id:token?.id, token, start, clientX:event.clientX, clientY:event.clientY, moved:false};
  }

  pointerMove(event) {
    if (!this.drag?.id) return;
    const point = this.point(event);
    if (!point) return;
    this.drag.moved ||= Math.hypot(event.clientX - this.drag.clientX, event.clientY - this.drag.clientY) > 5;
    if (!this.drag.moved) return;
    const token = this.drag.token;
    const group = this.svg.querySelector(`[data-token-id="${token.id}"]`);
    group?.setAttribute("transform", `translate(${token.x * 100 + 50 + point.x - this.drag.start.x},${token.y * 100 + 50 + point.y - this.drag.start.y})`);
  }

  pointerUp(event) {
    const drag = this.drag, point = this.point(event);
    this.drag = null;
    if (this.svg.hasPointerCapture(event.pointerId)) this.svg.releasePointerCapture(event.pointerId);
    if (!drag || !point) return;
    const id = drag.id || this.selectedId;
    const x = Math.floor(point.x / 100), y = Math.floor(point.y / 100);
    const moved = Math.hypot(event.clientX - drag.clientX, event.clientY - drag.clientY) > 5;
    this.render(this.state, this.selectedId, this.busy);
    if (!id || (drag.id && !drag.moved) || (!drag.id && moved)) return;
    if (x < 0 || y < 0 || x >= this.state.map.width || y >= this.state.map.height) return this.handlers.feedback("Drop the token inside the grid.");
    const token = this.state.tokens.find((item) => item.id === id);
    if (token && (token.x !== x || token.y !== y)) this.handlers.move(id, x, y);
  }

  cancelDrag() {
    this.drag = null;
    this.render(this.state, this.selectedId, this.busy);
  }

  keyDown(event) {
    const id = event.target.closest("[data-token-id]")?.dataset.tokenId;
    const token = this.state?.tokens.find((item) => item.id === id);
    if (!token || this.busy) return;
    if (["Enter", " "].includes(event.key)) { event.preventDefault(); this.handlers.select(id); return; }
    const delta = {ArrowLeft:[-1,0], ArrowRight:[1,0], ArrowUp:[0,-1], ArrowDown:[0,1]}[event.key];
    if (delta) { event.preventDefault(); this.handlers.select(id); this.handlers.move(id, token.x + delta[0], token.y + delta[1]); }
  }

  fit() {
    if (!this.state || !this.container.clientWidth || !this.container.clientHeight) return;
    this.cellPixels = Math.max(28, Math.min(64, Math.floor(Math.min(this.container.clientWidth / (this.state.map.width + .72), this.container.clientHeight / (this.state.map.height + .72)))));
    this.render(this.state, this.selectedId, this.busy);
  }

  zoom(direction) {
    this.cellPixels = Math.max(28, Math.min(90, this.cellPixels + direction * 8));
    this.render(this.state, this.selectedId, this.busy);
  }
}
