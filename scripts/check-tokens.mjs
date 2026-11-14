#!/usr/bin/env node
/**
 * A block names a role, never a value.
 *
 * `var(--warn)` is a claim about *what the colour is for*; `#fdba74` is a claim
 * about what it is. The difference is the whole reason the registry works: a
 * consumer can re-skin the seven roles in one `:root` block and every installed
 * block follows, because no block ever wrote a colour down. The moment one does,
 * the theme becomes a suggestion and that block silently stops participating.
 *
 * This is not hypothetical. data-download hand-edited the *footer* out of a
 * vendored AppShell after installing it -- a change that lived through three
 * registry releases because nothing compared the bytes. A literal colour is the
 * same defect one step quieter: it is a local decision baked into a shared
 * component, and it survives every future theme change while looking installed
 * and correct.
 *
 * So there is exactly one file allowed to hold a colour value: the theme. In
 * this repo that is `src/app.css`; in a consumer it is wherever the CLI merged
 * `cssVars` into the consumer's own stylesheet, because a table of tokens is
 * made of values by definition.
 *
 *   node scripts/check-tokens.mjs                              # this repo
 *   node scripts/check-tokens.mjs web/src                       # a consumer
 *   node scripts/check-tokens.mjs web/src --theme web/src/app.css
 *
 * Comments are stripped before scanning: a comment that explains `#0f172a, not
 * #000` is documentation of the rule, not a violation of it. So is the
 * `theme-color` meta in `app.html` -- browser chrome has no cascade to read a
 * variable from, so that value has to be written down; it is stripped for the
 * same reason, and it is the only such case.
 *
 * Named colours (`white`, `red`) are deliberately not caught. They are rare in
 * this house style, and word-boundary matching on English prose produces false
 * positives in comments and copy that no allowlist survives.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

const ROOT = process.cwd();

/** Not a colour, and not a decision about one. */
const EXEMPT_LINE =
  /^\s*--[\w-]+\s*:\s*(?:none|inherit|currentColor|transparent)\s*;?\s*$/;

const SCAN_EXTENSIONS = new Set([".svelte", ".css", ".html", ".js", ".ts"]);
const SKIP_DIRS = new Set([
  "node_modules",
  "build",
  ".svelte-kit",
  ".git",
  "dist",
  "coverage",
  "registry",
  "tests",
]);

const COLOUR = [
  {
    what: "hex colour",
    re: /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3}(?:[0-9a-fA-F]{2})?)?\b/g,
  },
  {
    what: "colour function",
    re: /\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(/g,
  },
];

const argv = process.argv.slice(2);
const themePaths = [];
const targets = [];
for (let i = 0; i < argv.length; i += 1) {
  if (argv[i] === "--theme") themePaths.push(argv[i + 1]);
  else if (argv[i].startsWith("--theme="))
    themePaths.push(argv[i].slice("--theme=".length));
  else targets.push(argv[i]);
}

if (targets.length === 0) targets.push("blocks", "src");
if (themePaths.length === 0) themePaths.push("src/app.css");

const VALUE_FILES = new Set(
  themePaths.map((p) => relative(ROOT, resolve(ROOT, p))),
);

function blank(match) {
  return match.replace(/[^\n]/g, " ");
}

function stripComments(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, blank)
    .replace(/<meta[^>]*name=["']theme-color["'][^>]*>/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/(^|[^:])\/\/[^\n]*/g, (m, lead) => lead);
}

function walk(path, out) {
  let info;
  try {
    info = statSync(path);
  } catch {
    console.error(`✗ no such path: ${path}`);
    process.exit(2);
  }
  if (info.isDirectory()) {
    for (const entry of readdirSync(path)) {
      if (SKIP_DIRS.has(entry)) continue;
      walk(join(path, entry), out);
    }
    return;
  }
  if (SCAN_EXTENSIONS.has(path.slice(path.lastIndexOf(".")))) out.push(path);
}

const files = [];
for (const target of targets) walk(resolve(ROOT, target), files);

const findings = [];
for (const file of files.sort()) {
  const rel = relative(ROOT, file);
  if (VALUE_FILES.has(rel)) continue;

  stripComments(readFileSync(file, "utf8"))
    .split("\n")
    .forEach((line, index) => {
      if (EXEMPT_LINE.test(line)) return;
      for (const { what, re } of COLOUR) {
        re.lastIndex = 0;
        let match;
        while ((match = re.exec(line))) {
          findings.push({
            file: rel,
            line: index + 1,
            what,
            text: line.trim(),
          });
        }
      }
    });
}

const theme = [...VALUE_FILES].join(", ");

if (findings.length === 0) {
  console.log(
    `✓ ${files.length} files name roles, not values (${theme} is the theme, so it may)`,
  );
  process.exit(0);
}

console.error(
  `✗ ${findings.length} raw colour literal(s) outside the theme:\n`,
);
for (const finding of findings) {
  console.error(`  ${finding.file}:${finding.line}  ${finding.what}`);
  console.error(`    ${finding.text}`);
}
console.error(
  `\nA block names a role. Use one of the theme's tokens -- ` +
    `--bg, --bg-raised, --line, --ink, --ink-muted, --accent, --ok, --warn, --bad, --mark -- ` +
    `as var(--token).\n` +
    `If the role does not exist yet, the role is what is missing, not the token: add it to the ` +
    `theme (${theme}) so every consumer inherits it.`,
);
process.exit(1);
