// Node harness for tests/test_js_parity.py: reads a JSON request on stdin,
// runs the JS engine, prints the summary as JSON. Not used by the browser.
"use strict";
const path = require("path");
const OF = require(path.join(__dirname, "..", "web", "engine.js"));

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", d => { input += d; });
process.stdin.on("end", () => {
  const req = JSON.parse(input);
  let out;
  if (req.mode === "bars") {
    out = OF.summarizeBars(req.bars, req.tick, req.round_step);
  } else {
    out = OF.summarizeTrades(req.trades, req.books, req.tick, req.round_step, req.interval_ms,
                             0.70, 0.30, 0.99, 0, 4.0, 200, 60000, 1.0);
  }
  // drop browser-only extras so the JSON stays small and comparable
  delete out.profile; delete out.vwap_series; delete out.bar_list;
  if (out.cvd) { delete out.cvd.series; delete out.cvd.buckets; }
  if (out.big_trades) delete out.big_trades.list;
  process.stdout.write(JSON.stringify(out));
});
