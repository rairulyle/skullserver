#!/usr/bin/env node
/**
 * Manage all Docker Compose stacks under the stacks directory.
 * Discovery: every immediate subdirectory that contains docker-compose.yml.
 *
 * Stacks root (first match wins):
 *   1) --root PATH
 *   2) SKULLSERVER_STACKS
 *   3) ../stacks relative to this script
 *
 * Usage: node scripts/stacks.js [--root PATH] COMMAND [STACK]
 *   COMMAND: list | up | down | pull | update | ps
 */

const { spawnSync } = require("node:child_process");
const { existsSync, readdirSync, statSync, realpathSync } = require("node:fs");
const { basename, join, resolve } = require("node:path");

function usage() {
  console.log(`Usage: node scripts/stacks.js [--root PATH] COMMAND [STACK]

  COMMAND   list | up | down | pull | update | ps
  STACK     Optional; if set, only that stack (folder name under stacks/).

Stacks are auto-discovered: each subdirectory of the stacks root that
contains docker-compose.yml is a stack.

Environment:
  SKULLSERVER_STACKS   Stacks directory (default: ../stacks from this script).

Examples:
  npm run stacks -- list
  npm run stacks -- update
  npm run stacks -- up infra
`);
}

function die(msg) {
  console.error(`stacks: ${msg}`);
  process.exit(1);
}

function resolveStacksRoot(rootFlag) {
  let raw;
  if (rootFlag) raw = resolve(rootFlag);
  else if (process.env.SKULLSERVER_STACKS)
    raw = resolve(process.env.SKULLSERVER_STACKS);
  else raw = resolve(__dirname, "../stacks");

  try {
    return realpathSync(raw);
  } catch {
    die(`could not resolve stacks root: ${raw} (set SKULLSERVER_STACKS or use --root)`);
  }
}

function discoverStacks(root) {
  let st;
  try {
    st = statSync(root);
  } catch {
    die(`stacks root is not a directory: ${root}`);
  }
  if (!st.isDirectory()) die(`stacks root is not a directory: ${root}`);

  const stacks = [];
  for (const name of readdirSync(root)) {
    if (name.startsWith(".")) continue;
    const dir = join(root, name);
    let s;
    try {
      s = statSync(dir);
    } catch {
      continue;
    }
    if (!s.isDirectory()) continue;
    if (existsSync(join(dir, "docker-compose.yml"))) stacks.push(dir);
  }
  stacks.sort((a, b) =>
    basename(a).localeCompare(basename(b), "en", { sensitivity: "accent" })
  );
  return stacks;
}

function selectStacks(root, want) {
  const all = discoverStacks(root);
  if (all.length === 0)
    die(`no stacks found under ${root} (need */docker-compose.yml)`);
  if (!want) return all;
  const hit = all.find((d) => basename(d) === want);
  if (!hit)
    die(
      `unknown stack '${want}' (not under ${root} with docker-compose.yml)`
    );
  return [hit];
}

function compose(cwd, args) {
  const r = spawnSync("docker", ["compose", ...args], {
    cwd,
    stdio: "inherit",
  });
  return r.status === 0;
}

function parseArgs(argv) {
  const flags = { root: null };
  const pos = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--root") {
      if (i + 1 >= argv.length) die("--root requires a path");
      flags.root = argv[++i];
      continue;
    }
    if (a === "-h" || a === "--help") flags.help = true;
    else pos.push(a);
  }
  return { flags, pos };
}

const { flags, pos } = parseArgs(process.argv.slice(2));
if (flags.help) {
  usage();
  process.exit(0);
}
if (pos.length === 0) {
  usage();
  process.exit(1);
}

const cmd = pos[0];
const stackFilter = pos[1] ?? "";

const stacksRoot = resolveStacksRoot(flags.root);
const stackDirs = selectStacks(stacksRoot, stackFilter);

if (cmd === "list") {
  for (const d of stackDirs) console.log(basename(d));
  process.exit(0);
}

const composeArgsByCmd = {
  up: ["up", "-d"],
  down: ["down"],
  pull: ["pull"],
  ps: ["ps"],
};

if (
  !Object.prototype.hasOwnProperty.call(composeArgsByCmd, cmd) &&
  cmd !== "update"
)
  die(`unknown command '${cmd}' (try: list, up, down, pull, update, ps)`);

let fail = false;
for (const dir of stackDirs) {
  const name = basename(dir);
  console.log(`=== [${name}] ===`);
  if (cmd === "update") {
    if (!compose(dir, ["pull"])) {
      console.error(`stacks: [${name}] pull failed`);
      fail = true;
      continue;
    }
    if (!compose(dir, ["up", "-d"])) {
      console.error(`stacks: [${name}] up -d failed`);
      fail = true;
    }
  } else {
    const args = composeArgsByCmd[cmd];
    if (!compose(dir, args)) {
      console.error(
        `stacks: [${name}] docker compose ${args.join(" ")} failed`
      );
      fail = true;
    }
  }
}

process.exit(fail ? 1 : 0);
