// Fails when connector code leaks into the production bundle. Run after `npm run build:single`
// (or after `npm run build` alone: then only the production files are checked).
// Production = dist/assets/*.js, dist/index.html, dist/flow-lab-single.html. The artifact file must
// contain the connector, otherwise this check would pass for the wrong reason.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const dist = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "dist");
const FORBIDDEN = ["tool_execute", "callTool", "futures_usds.", "spot.aggTrades"];
let failed = 0;
const fail = (m) => { failed += 1; console.log(`FAIL ${m}`); };

const prodFiles = [
  ...fs.readdirSync(path.join(dist, "assets")).filter((f) => /\.(js|html)$/.test(f)).map((f) => path.join(dist, "assets", f)),
  path.join(dist, "index.html"),
  path.join(dist, "flow-lab-single.html"),
].filter((f) => fs.existsSync(f));
if (!prodFiles.some((f) => f.endsWith(".js"))) fail("no production JS found in dist/assets (build first)");
for (const f of prodFiles) {
  const text = fs.readFileSync(f, "utf8");
  for (const word of FORBIDDEN) if (text.includes(word)) fail(`${path.relative(dist, f)} contains "${word}" (connector code in the production bundle)`);
}
console.log(`checked ${prodFiles.length} production file(s) for ${FORBIDDEN.map((w) => `"${w}"`).join(", ")}`);

const art = path.join(dist, "flow-lab-artifact.html");
if (fs.existsSync(art)) {
  const text = fs.readFileSync(art, "utf8");
  for (const word of ["tool_execute", "callTool"]) if (!text.includes(word)) fail(`flow-lab-artifact.html lacks "${word}" (artifact build did not include the connector)`);
  console.log("checked dist/flow-lab-artifact.html includes the connector");
}
if (failed) { console.log(`\n${failed} bundle check(s) failed`); process.exit(1); }
console.log("bundle check passed: production bundle is connector-free");
