// Build step: compute both canvas layouts ahead of time so the page never runs dagre.
// usage: node tools/prerender.mjs build/map.json build/layout.json
import fs from "node:fs";
import { createRequire } from "node:module";
import { prepare, layoutJourneys, layoutGraph } from "../src/layout.js";

const dagre = createRequire(import.meta.url)("dagre");
const [src, dst] = process.argv.slice(2);
const MAP = JSON.parse(fs.readFileSync(src, "utf8"));
const P = prepare(MAP);
const t0 = performance.now();
const out = { journeys: layoutJourneys(MAP, P), graph: layoutGraph(MAP, P, dagre) };
fs.writeFileSync(dst, JSON.stringify(out));
console.error(`layout: ${out.journeys.cards.length} + ${out.graph.cards.length} cards in ${Math.round(performance.now() - t0)}ms`);
