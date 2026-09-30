// Exercises the remote-URL -> "owner/name" regex used by the Git panel to
// decide whether a repo is on GitHub. The regex is pulled straight out of
// static/git.js so this test can never drift from the real one.
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("../static/git.js", import.meta.url), "utf8");
const found = src.match(/const re = (\/.*\/i);/);
if (!found) {
  console.error("could not locate the remote regex in static/git.js");
  process.exit(1);
}
const re = eval(found[1]);

const cases = [
  ["https://github.com/acme/widget.git", "acme/widget"],
  ["https://github.com/acme/widget", "acme/widget"],
  ["https://github.com/acme/widget/", "acme/widget"],
  ["http://github.com/acme/widget.git", "acme/widget"],
  ["git@github.com:acme/widget.git", "acme/widget"],
  ["git@github.com:acme/widget", "acme/widget"],
  ["https://***@github.com/acme/widget.git", "acme/widget"],
  ["https://token@github.com/acme/my.repo-name.git", "acme/my.repo-name"],
  ["ssh://git@github.com/acme/widget.git", "acme/widget"],
  ["https://gitlab.com/acme/widget.git", ""],
  ["https://github.com/acme/widget/extra", ""],
  ["/home/me/repos/thing", ""],
  ["", ""],
];

let bad = 0;
for (const [url, want] of cases) {
  const m = re.exec(String(url).trim());
  const got = m ? `${m[1]}/${m[2]}` : "";
  const ok = got === want;
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${JSON.stringify(url).padEnd(46)} -> ${JSON.stringify(got)}${ok ? "" : "   want " + JSON.stringify(want)}`);
}
console.log(bad ? `\n${bad} FAILED` : "\nall remote-parse cases passed");
process.exit(bad ? 1 : 0);
