#!/usr/bin/env node
/**
 * Are the vendored `@nb` blocks still the blocks the registry published?
 *
 * `shadcn-svelte add` copies bytes into this repo and then forgets them. The
 * only signal that a block fix had shipped used to be a human noticing a bug —
 * that is how app-shell 1.11.0 arrived (a Homepage card grew unbounded blank
 * space, which is not a thing CI can see). This script turns "is this repo on
 * published bytes?" into a red build.
 *
 * It answers exactly two questions:
 *
 *   1. Does every block in `web/src/lib/components/nb/` still match the release
 *      named in `.nb-registry.json`? (drift — someone edited a block here, so
 *      the next copy either loses that edit or silently reverts an upstream fix)
 *   2. Is every block published at that version? (a per-version index is a
 *      *snapshot* of the catalogue, and four versions in one day shipped first
 *      as an index holding one or two items)
 *
 * It deliberately does NOT fail when a newer version exists — an upgrade changes
 * a live UI and is a decision, not a safety invariant — but it says so on every
 * run, because "nothing tells the consumer" was the original complaint.
 *
 * Formatting is not drift. Both sides are compared after comments, whitespace
 * and prettier's trailing commas are removed, because this repo runs `prettier
 * --write` over its sources and the algorithm that unifies them is the same one
 * that reformatted the block. A changed prop, class, string or element is
 * still a change.
 *
 * Usage:
 *   node scripts/check-nb-blocks.mjs [--verbose]
 *
 * Environment:
 *   COMPONENTS_REGISTRY_URL  the registry HOST (no /v<version>), e.g.
 *                            http://ryzen:3012/r. Versionless on purpose: the
 *                            version is named once, in .nb-registry.json.
 */

import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { basename, join } from "node:path";

const BLOCKS_DIR = "web/src/lib/components/nb";
const PIN_FILE = ".nb-registry.json";
const VERBOSE = process.argv.includes("--verbose");

/**
 * Comments, whitespace and trailing commas are not behaviour, and a `prettier`
 * run here adds all three. Everything else is compared.
 */
function normalise(source) {
  return source
    .replace(/<!--[\s\S]*?-->/g, "") // Svelte comment
    .replace(/\/\*[\s\S]*?\*\//g, "") // JS block comment
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1") // JS line comment (not http://)
    .replace(/\s+/g, "")
    .replace(/,([)\]}])/g, "$1"); // trailing comma before a closer
}

const digest = (source) =>
  createHash("sha256").update(normalise(source)).digest("hex");

function fail(message) {
  console.error(`\n✗ ${message}\n`);
  process.exit(1);
}

const pin = existsSync(PIN_FILE)
  ? JSON.parse(readFileSync(PIN_FILE, "utf8"))
  : null;
if (!pin?.version) {
  fail(
    `${PIN_FILE} is missing or has no "version". It names the @nb release this repo's blocks come from — add {"version": "x.y.z"}.`,
  );
}

const host = (process.env.COMPONENTS_REGISTRY_URL ?? "").trim();
if (!host) {
  fail(
    "COMPONENTS_REGISTRY_URL is not set. It is the registry host, not a pin:\n" +
      "  export COMPONENTS_REGISTRY_URL=\"$(doppler secrets get COMPONENTS_REGISTRY_URL \\\n" +
      "    --project common --config dev --plain)\"",
  );
}

const versioned = host.match(/\/v(\d+\.\d+\.\d+)\/?$/);
if (versioned) {
  fail(
    `COMPONENTS_REGISTRY_URL must be the host, not a pin — drop the /v${versioned[1]}.\n` +
      `  got: ${host}\n` +
      `  The pin belongs in ${PIN_FILE}; appending it twice gives /v${versioned[1]}/v${versioned[1]}.`,
  );
}

const base = host.replace(/\/+$/, "");
const version = pin.version;
const pinnedBase = `${base}/v${version}`;

const get = async (url) => {
  const response = await fetch(url);
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`${url} -> ${response.status} ${body.slice(0, 200)}`);
  }
  return response;
};

if (!existsSync(BLOCKS_DIR)) {
  fail(`${BLOCKS_DIR} does not exist — is this the repo root?`);
}

const localFiles = readdirSync(BLOCKS_DIR).filter((name) =>
  name.endsWith(".svelte"),
);
if (localFiles.length === 0) {
  fail(`${BLOCKS_DIR} holds no blocks. Remove the check or the directory.`);
}

let index;
try {
  index = await (await get(`${pinnedBase}/index.json`)).json();
} catch (failure) {
  fail(
    `cannot read the catalogue at ${pinnedBase}/index.json: ${failure.message}\n` +
      `  A version index is a snapshot, so this usually means the release with the pin is not published.`,
  );
}

const blocks = index.filter((entry) => entry.type === "registry:block");
const names = new Set(blocks.map((entry) => entry.name));

const stale = [];
const unpublished = [];
const checked = [];
const publishedTargets = new Set();

for (const entry of blocks) {
  let item;
  try {
    item = await (await get(`${pinnedBase}/${entry.relativeUrl}`)).json();
  } catch (failure) {
    fail(`cannot read ${entry.name} at v${version}: ${failure.message}`);
  }

  for (const file of item.files ?? []) {
    if (!file.target) continue;
    publishedTargets.add(basename(file.target));
    if (!file.content) continue;

    const localPath = join(BLOCKS_DIR, basename(file.target));
    if (!existsSync(localPath)) continue; // published, not used here

    const expected = digest(file.content);
    const actual = digest(readFileSync(localPath, "utf8"));
    if (expected === actual) checked.push(entry.name);
    else stale.push({ name: entry.name, path: localPath });
  }
}

for (const file of localFiles) {
  if (!publishedTargets.has(file)) unpublished.push(file);
}

/** Semantic-ish ordering for `x.y.z`; the registry only publishes that shape. */
function compareVersions(a, b) {
  const left = a.split(".").map(Number);
  const right = b.split(".").map(Number);
  for (let i = 0; i < 3; i += 1) {
    if ((left[i] ?? 0) !== (right[i] ?? 0)) return (left[i] ?? 0) - (right[i] ?? 0);
  }
  return 0;
}

// Informational: is there a newer release to move to? Not a failure — see the
// header — but it is printed on every run so the answer is never a surprise.
let notice = "";
try {
  const catalogue = await get(`${base}/registry.json`);
  const published = (catalogue.headers.get("x-registry-versions") ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  const newest = published.sort(compareVersions).at(-1);
  if (newest && newest !== version) {
    notice = `newer release published: ${newest} (this repo pins ${version})`;
  }
} catch {
  notice = "could not read the registry's version list";
}

if (VERBOSE) {
  for (const name of checked) console.log(`  ok    ${name}`);
}

console.log(
  `\n@nb ${version}: ${checked.length} of ${names.size} published blocks match this repo.`,
);
if (notice) console.log(`note: ${notice}`);

if (stale.length) {
  console.error(
    `\n✗ ${stale.length} vendored block(s) do not match v${version}:`,
  );
  for (const { name, path } of stale) console.error(`    ${path}  (${name})`);
  console.error(
    `\n  Either re-install to get the published version:\n` +
      `    COMPONENTS_REGISTRY_URL="${pinnedBase}" npx shadcn-svelte@1.8.1 add <name> --yes --overwrite\n` +
      `  or, if the local change is deliberate, publish it upstream first — a block\n` +
      `  edited only here is a block the next install overwrites.`,
  );
}

if (unpublished.length) {
  console.error(
    `\n✗ ${unpublished.length} file(s) in ${BLOCKS_DIR} are not published blocks:`,
  );
  for (const file of unpublished) console.error(`    ${file}`);
  console.error(
    `\n  A file wearing the house name that the registry does not publish is\n` +
      `  either a renamed block or a local invention. Publish it, or move it out\n` +
      `  of ${BLOCKS_DIR}.`,
  );
}

if (stale.length || unpublished.length) process.exitCode = 1;
else console.log("✓ no drift\n");
