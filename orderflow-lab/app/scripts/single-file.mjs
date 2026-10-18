// Inline the built JS and CSS into one HTML file (dist/flow-lab-single.html) so the
// prototype can be opened from a single file or a host that serves one page.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const dist = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "dist");
let html = fs.readFileSync(path.join(dist, "index.html"), "utf8");
html = html.replace(/<script type="module" crossorigin src="\.\/(assets\/[^"]+\.js)"><\/script>/, (_, f) =>
  "").replace(/<link rel="stylesheet" crossorigin href="\.\/(assets\/[^"]+\.css)">/, (_, f) => `<style>${fs.readFileSync(path.join(dist, f), "utf8")}</style>`);
const js = fs.readdirSync(path.join(dist, "assets")).find((f) => f.endsWith(".js"));
// the module goes at the end of <body> so #root exists when it runs
// a function replacement: the bundle contains "$&"-style sequences a string replacement would expand
const code = fs.readFileSync(path.join(dist, "assets", js), "utf8").replace(/<\/script/g, () => "<\\/script");
html = html.replace("</body>", () => `<script type="module">${code}</script>\n</body>`)
  .replace(/<link rel="manifest"[^>]*>\s*/, "").replace(/<link rel="icon"[^>]*>\s*/, "");
fs.writeFileSync(path.join(dist, "flow-lab-single.html"), html);
console.log(`dist/flow-lab-single.html ${(html.length / 1024).toFixed(0)} KB`);

// claude.ai artifact variant: the host adds <!doctype>/<head>/<body>, so ship only the content
const css = html.match(/<style>[\s\S]*?<\/style>/)[0];
const artifact = [
  "<title>Flow Lab</title>",
  '<meta name="claude:viewport-fit-sides" content="cover">',
  '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
  '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+Thai:wght@400;500;600;700;800&display=swap">',
  css,
  '<div id="root"></div>',
  `<script type="module">${code}</script>`,
].join("\n");
fs.writeFileSync(path.join(dist, "flow-lab-artifact.html"), artifact);
console.log(`dist/flow-lab-artifact.html ${(artifact.length / 1024).toFixed(0)} KB`);
