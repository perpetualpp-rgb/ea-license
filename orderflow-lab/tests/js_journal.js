// Node harness for tests/test_journal.py: reads {fn, args} as JSON on stdin, calls
// desk/journal.js (the same file the desk page inlines) and prints the result as JSON.
"use strict";
const path = require("path");
const OFJ = require(path.join(__dirname, "..", "desk", "journal.js"));

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", d => { input += d; });
process.stdin.on("end", () => {
  const req = JSON.parse(input);
  const out = req.fn === "RULES" ? OFJ.RULES : OFJ[req.fn](...req.args);
  process.stdout.write(JSON.stringify(out === undefined ? null : out));
});
