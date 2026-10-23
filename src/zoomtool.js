// Zoom tool, as in Figma or Photoshop: hold Z and click to zoom in, add Option (Alt) to zoom out.
// Shared by the map canvas and the screenshot lightbox.
//
// Match the key by the character it types, not its position: `code` names the QWERTY position, so the
// key labelled Z sends KeyW on AZERTY and KeyY on QWERTZ. Option changes the character on macOS
// (Option+Z types "Ω"), so while Alt is down, match the physical key that last typed a plain "z".
const state = { on: false, out: false }, subs = new Set();
let zCode = "KeyZ";
function set(on, out) {
  out = on && out;
  if (on === state.on && out === state.out) return;
  state.on = on; state.out = out;
  subs.forEach((f) => f(state));
}
const typing = (t) => t instanceof Element && t.matches("input, textarea, select, [contenteditable]");
addEventListener("keydown", (e) => {
  // macOS never sends keyup for a key released while Cmd is held, so Cmd ends the tool rather than strand it on.
  if (e.key === "Meta") return set(false, false);
  if (e.key === "Alt") return set(state.on, true);
  if (e.repeat || e.metaKey || e.ctrlKey || typing(e.target)) return;
  if (e.altKey ? e.code !== zCode : e.key.toLowerCase() !== "z") return;
  if (!e.altKey) zCode = e.code;
  set(true, e.altKey);
}, true);
// Releases always count, wherever focus is: a Z let go inside the search box must still end the tool.
addEventListener("keyup", (e) => {
  if (e.key === "Meta" || e.code === zCode) set(false, false);
  else if (e.key === "Alt") set(state.on, false);
}, true);
addEventListener("blur", () => set(false, false));
document.addEventListener("visibilitychange", () => set(false, false));

export const zoomTool = {
  get on() { return state.on; },
  get out() { return state.out; },
  // Pointer events carry the live Alt state, which catches an Option press made while the page was unfocused.
  syncAlt(altKey) { if (state.on && altKey !== state.out) set(true, altKey); },
  subscribe(f) { subs.add(f); f(state); },
  // Show the zoom-in or zoom-out cursor on `el` while the tool is active (styled in app.css).
  bindCursor(el) { this.subscribe(({ on, out }) => { el.classList.toggle("zoom-in", on && !out); el.classList.toggle("zoom-out", out); }); },
};
