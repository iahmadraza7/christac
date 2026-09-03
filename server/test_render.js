// Pulls withEmphasis() straight out of the shipped chat.html and exercises it,
// so what is tested is what the page actually runs.
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");

const start = html.indexOf("  var ESCAPES");
const end = html.indexOf("  function add(text, kind)");
if (start < 0 || end < 0 || end <= start) {
  console.error("could not find the renderer in chat.html");
  process.exit(2);
}
const withEmphasis = new Function(html.slice(start, end) + "; return withEmphasis;")();

let pass = 0, fail = 0;
function check(name, got, want) {
  if (got === want) { pass++; console.log("  pass  " + name); }
  else {
    fail++;
    console.log("  FAIL  " + name);
    console.log("        got  " + JSON.stringify(got));
    console.log("        want " + JSON.stringify(want));
  }
}
function truthy(name, cond, detail) {
  if (cond) { pass++; console.log("  pass  " + name); }
  else { fail++; console.log("  FAIL  " + name + (detail ? "  " + detail : "")); }
}

console.log("\nEmphasis renders");
check("bold becomes bold",
  withEmphasis("**My Queen... this isn't looking good.**"),
  "<strong>My Queen... this isn&#39;t looking good.</strong>");
check("italic becomes italic",
  withEmphasis("Not *will be* a wife."),
  "Not <em>will be</em> a wife.");
check("bold inside a sentence",
  withEmphasis("Six dates in. **Queen... this isn't looking good.** By this stage."),
  "Six dates in. <strong>Queen... this isn&#39;t looking good.</strong> By this stage.");
check("two bold runs in one reply",
  withEmphasis("**one** middle **two**"),
  "<strong>one</strong> middle <strong>two</strong>");

console.log("\nLine breaks and blank lines survive untouched");
check("a blank line is still a blank line",
  withEmphasis("First line.\n\nSecond line."),
  "First line.\n\nSecond line.");
check("the repeat-decode line keeps its gap",
  withEmphasis("We already read him, Queen. Going back over it won't change what he showed you.\n\nWant to decode another man?"),
  "We already read him, Queen. Going back over it won&#39;t change what he showed you.\n\nWant to decode another man?");
check("bold spanning a line break still closes",
  withEmphasis("**over\ntwo lines**"),
  "<strong>over\ntwo lines</strong>");

console.log("\nNothing can inject HTML");
check("angle brackets are shown, not run",
  withEmphasis("<script>alert(1)</script>"),
  "&lt;script&gt;alert(1)&lt;/script&gt;");
check("an image tag with a handler is inert",
  withEmphasis('<img src=x onerror="alert(1)">'),
  "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
check("ampersands cannot rebuild an entity",
  withEmphasis("&lt;script&gt;"),
  "&amp;lt;script&amp;gt;");
check("markup smuggled inside bold is still escaped",
  withEmphasis("**<b>x</b>**"),
  "<strong>&lt;b&gt;x&lt;/b&gt;</strong>");

console.log("\nStray markers are left alone rather than guessed at");
check("a lone opening marker stays literal",
  withEmphasis("**unclosed and then nothing"),
  "**unclosed and then nothing");
check("a single asterisk on its own stays literal",
  withEmphasis("5 * 3 = 15"),
  "5 * 3 = 15");
check("italic does not reach across a blank line",
  withEmphasis("*first paragraph\n\nsecond paragraph*"),
  "*first paragraph\n\nsecond paragraph*");
check("underscores are never touched",
  withEmphasis("no_change_here and _not italic_"),
  "no_change_here and _not italic_");

console.log("\nHer real DELIVER AS WRITTEN blocks");
const blocks = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
let changedWording = 0, gotBold = 0;
for (const b of blocks) {
  const out = withEmphasis(b);
  // strip the tags we added and unescape, and it must be her text exactly
  const back = out
    .replace(/<\/?strong>/g, "**").replace(/<\/?em>/g, "*")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  if (back !== b) { changedWording++; console.log("  CHANGED: " + JSON.stringify(b.slice(0, 70))); }
  if (out.includes("<strong>")) gotBold++;
  if (/[<>](?!\/?(strong|em)>)/.test(out.replace(/<\/?(strong|em)>/g, ""))) {
    fail++; console.log("  FAIL  raw angle bracket survived in a block");
  }
}
truthy("all " + blocks.length + " blocks round-trip to her exact wording",
  changedWording === 0, changedWording + " changed");
truthy("bold actually rendered in " + gotBold + " of them", gotBold > 0);

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
