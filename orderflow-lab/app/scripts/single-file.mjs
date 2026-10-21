// Inline the built JS and CSS into single HTML files:
//   dist/flow-lab-single.html    from dist/          (production build: demo / Flow Lab API, no connector code)
//   dist/flow-lab-artifact.html  from dist/artifact/ (`vite build --mode artifact`: adds the owner's Binance connector)
// The artifact file is content only: claude.ai adds <!doctype>/<head>/<body> around it.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const dist = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "dist");

function inline(dir) {
  let html = fs.readFileSync(path.join(dir, "index.html"), "utf8");
  html = html.replace(/<script type="module" crossorigin src="\.\/(assets\/[^"]+\.js)"><\/script>/, () => "")
    .replace(/<link rel="stylesheet" crossorigin href="\.\/(assets\/[^"]+\.css)">/, (_, f) => `<style>${fs.readFileSync(path.join(dir, f), "utf8")}</style>`);
  const js = fs.readdirSync(path.join(dir, "assets")).filter((f) => f.endsWith(".js"));
  // one module only: a second chunk would be fetched at run time and is not in the single file
  if (js.length !== 1) throw new Error(`${dir}: expected exactly one JS chunk, found ${js.join(", ") || "none"}`);
  // a function replacement: the bundle contains "$&"-style sequences a string replacement would expand
  const code = fs.readFileSync(path.join(dir, "assets", js[0]), "utf8").replace(/<\/script/g, () => "<\\/script");
  // the module goes at the end of <body> so #root exists when it runs
  html = html.replace("</body>", () => `<script type="module">${code}</script>\n</body>`)
    .replace(/<link rel="manifest"[^>]*>\s*/, "").replace(/<link rel="icon"[^>]*>\s*/, "");
  return { html, code, css: html.match(/<style>[\s\S]*?<\/style>/)[0] };
}

const prod = inline(dist);
fs.writeFileSync(path.join(dist, "flow-lab-single.html"), prod.html);
console.log(`dist/flow-lab-single.html ${(prod.html.length / 1024).toFixed(0)} KB (production, no connector)`);

const artDir = path.join(dist, "artifact");
if (!fs.existsSync(path.join(artDir, "index.html"))) throw new Error("dist/artifact missing: run `npm run build:artifact` first");
const art = inline(artDir);
const artifact = [
  "<title>Flow Lab</title>",
  '<meta name="claude:viewport-fit-sides" content="cover">',
  '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
  '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+Thai:wght@400;500;600;700;800&display=swap">',
  art.css,
  '<div id="root"></div>',
  `<script type="module">${art.code}</script>`,
].join("\n");
fs.writeFileSync(path.join(dist, "flow-lab-artifact.html"), artifact);
console.log(`dist/flow-lab-artifact.html ${(artifact.length / 1024).toFixed(0)} KB (artifact mode, owner's Binance connector included)`);
