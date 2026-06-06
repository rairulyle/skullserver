#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");
const YAML = require("yaml");

const SERVICE_PREFIX_KEY_ORDER = [
  "container_name",
  "image",
  "user",
  "hostname",
  "network_mode",
  "devices",
];

const SERVICE_TAIL_KEY_ORDER = [
  "ports",
  "environment",
  "volumes",
  "depends_on",
  "env_file",
  "healthcheck",
  "restart",
  "labels",
];

const ENV_KEY_ORDER = ["UMASK", "UMASK_SET", "PUID", "PGID", "TZ"];
const LABEL_KEY_ORDER = [
  "npm.proxy.domain",
  "npm.proxy.port",
  "net.unraid.docker.icon",
  "net.unraid.docker.webui",
];

function parseComposeYaml(source) {
  return parseComposeDocument(source).toJS({ mapAsMap: false });
}

function parseComposeDocument(source) {
  const document = YAML.parseDocument(source, {
    prettyErrors: false,
    version: "1.2",
  });

  if (document.errors.length > 0) {
    throw document.errors[0];
  }

  return document;
}

function orderedKeys(object, preferredOrder, sortRemaining = false) {
  if (!isPlainObject(object)) return [];

  const keys = Object.keys(object);
  const preferred = preferredOrder.filter((key) =>
    Object.prototype.hasOwnProperty.call(object, key)
  );
  const remaining = keys.filter((key) => !preferredOrder.includes(key));

  if (sortRemaining) {
    remaining.sort((a, b) => a.localeCompare(b, "en", { sensitivity: "accent" }));
  }

  return [...preferred, ...remaining];
}

function orderedLabelKeys(labels) {
  const keys = Object.keys(labels);
  const known = LABEL_KEY_ORDER.filter((key) =>
    Object.prototype.hasOwnProperty.call(labels, key)
  );
  const npmExtra = keys
    .filter((key) => key.startsWith("npm.proxy.") && !LABEL_KEY_ORDER.includes(key))
    .sort((a, b) => a.localeCompare(b, "en", { sensitivity: "accent" }));
  const rest = keys
    .filter(
      (key) =>
        !LABEL_KEY_ORDER.includes(key) &&
        !key.startsWith("npm.proxy.")
    )
    .sort((a, b) => a.localeCompare(b, "en", { sensitivity: "accent" }));

  const npmPortIndex = known.indexOf("npm.proxy.port");
  if (npmPortIndex === -1) return [...known, ...npmExtra, ...rest];

  return [
    ...known.slice(0, npmPortIndex + 1),
    ...npmExtra,
    ...known.slice(npmPortIndex + 1),
    ...rest,
  ];
}

function formatComposeYaml(source) {
  if (isEmptyOverrideFile(source)) return source;

  const document = parseComposeDocument(source);
  reorderComposeDocument(document);

  return String(document);
}

function checkComposeYaml(source, filePath) {
  try {
    const formatted = formatComposeYaml(source);
    if (formatted !== source) {
      return {
        ok: false,
        errors: [
          `${filePath}: YAML is valid but convention order/formatting differs. Run npm run yaml:fix.`,
        ],
      };
    }

    return { ok: true, errors: [] };
  } catch (error) {
    return {
      ok: false,
      errors: [formatYamlError(source, filePath, error)],
    };
  }
}

function formatYamlError(source, filePath, error) {
  const location = getErrorLocation(source, error);
  const pathWithLocation = location
    ? `${filePath}:${location.line}:${location.column}`
    : filePath;

  return `${pathWithLocation}: invalid YAML: ${error.message}`;
}

function getErrorLocation(source, error) {
  if (Array.isArray(error.linePos) && error.linePos[0]) {
    return {
      line: error.linePos[0].line,
      column: error.linePos[0].col,
    };
  }

  if (!Array.isArray(error.pos) || typeof error.pos[0] !== "number") {
    return null;
  }

  return offsetToLocation(source, nearestTokenOffset(source, error.pos[0]));
}

function nearestTokenOffset(source, offset) {
  let index = Math.min(offset, source.length - 1);

  while (index > 0 && /\s/.test(source[index])) {
    index -= 1;
  }

  return index;
}

function offsetToLocation(source, offset) {
  let line = 1;
  let column = 1;

  for (let index = 0; index < offset && index < source.length; index++) {
    if (source[index] === "\n") {
      line += 1;
      column = 1;
    } else {
      column += 1;
    }
  }

  return { line, column };
}

function reorderComposeDocument(document) {
  const services = document.get("services", true);
  if (!YAML.isMap(services)) return;

  for (const servicePair of services.items) {
    if (YAML.isMap(servicePair.value)) {
      reorderService(servicePair.value);
    }
  }
}

function reorderService(service) {
  reorderServicePairs(service);

  const environment = service.get("environment", true);
  if (YAML.isMap(environment)) {
    reorderEnvironmentPairs(environment);
  }

  const labels = service.get("labels", true);
  if (YAML.isMap(labels)) {
    reorderLabelPairs(labels);
  }
}

function reorderServicePairs(service) {
  service.items.sort((left, right) => {
    const leftRank = serviceKeyRank(pairKey(left));
    const rightRank = serviceKeyRank(pairKey(right));

    if (leftRank.group !== rightRank.group) return leftRank.group - rightRank.group;
    if (leftRank.rank !== rightRank.rank) return leftRank.rank - rightRank.rank;
    return leftRank.key.localeCompare(rightRank.key, "en", { sensitivity: "accent" });
  });
}

function serviceKeyRank(key) {
  const prefixIndex = SERVICE_PREFIX_KEY_ORDER.indexOf(key);
  if (prefixIndex !== -1) return { group: 0, rank: prefixIndex, key };

  const tailIndex = SERVICE_TAIL_KEY_ORDER.indexOf(key);
  if (tailIndex !== -1) return { group: 2, rank: tailIndex, key };

  return { group: 1, rank: 0, key };
}

function reorderEnvironmentPairs(environment) {
  environment.items.sort((left, right) => {
    const leftKey = pairKey(left);
    const rightKey = pairKey(right);
    const leftRank = environmentKeyRank(leftKey);
    const rightRank = environmentKeyRank(rightKey);

    if (leftRank.group !== rightRank.group) return leftRank.group - rightRank.group;
    if (leftRank.rank !== rightRank.rank) return leftRank.rank - rightRank.rank;
    return compareEnvironmentKeys(leftKey, rightKey);
  });
}

function environmentKeyRank(key) {
  const standardIndex = ENV_KEY_ORDER.indexOf(key);
  if (standardIndex !== -1) return { group: 0, rank: standardIndex, key };

  return { group: 1, rank: 0, key };
}

function compareEnvironmentKeys(leftKey, rightKey) {
  const leftIsUser = /USER/i.test(leftKey);
  const rightIsUser = /USER/i.test(rightKey);
  const leftIsPass = /PASS/i.test(leftKey);
  const rightIsPass = /PASS/i.test(rightKey);

  if (leftIsUser && rightIsPass) return -1;
  if (leftIsPass && rightIsUser) return 1;

  return leftKey.localeCompare(rightKey, "en", { sensitivity: "accent" });
}

function reorderMapPairs(map, preferredOrder, options = {}) {
  const originalIndexes = new Map(
    map.items.map((pair, index) => [pair, index])
  );

  map.items.sort((left, right) => {
    const leftKey = pairKey(left);
    const rightKey = pairKey(right);
    const leftPreferred = preferredOrder.indexOf(leftKey);
    const rightPreferred = preferredOrder.indexOf(rightKey);

    if (leftPreferred !== -1 || rightPreferred !== -1) {
      if (leftPreferred === -1) return 1;
      if (rightPreferred === -1) return -1;
      return leftPreferred - rightPreferred;
    }

    if (options.sortRemaining) {
      return leftKey.localeCompare(rightKey, "en", { sensitivity: "accent" });
    }

    return originalIndexes.get(left) - originalIndexes.get(right);
  });
}

function reorderLabelPairs(labels) {
  labels.items.sort((left, right) => {
    const leftRank = labelRank(pairKey(left));
    const rightRank = labelRank(pairKey(right));

    if (leftRank.group !== rightRank.group) return leftRank.group - rightRank.group;
    if (leftRank.rank !== rightRank.rank) return leftRank.rank - rightRank.rank;
    return leftRank.key.localeCompare(rightRank.key, "en", { sensitivity: "accent" });
  });
}

function labelRank(key) {
  if (key === "npm.proxy.domain") return { group: 0, rank: 0, key };
  if (key === "npm.proxy.port") return { group: 0, rank: 1, key };
  if (key.startsWith("npm.proxy.")) return { group: 0, rank: 2, key };
  if (key === "net.unraid.docker.icon") return { group: 1, rank: 0, key };
  if (key === "net.unraid.docker.webui") return { group: 1, rank: 1, key };
  return { group: 2, rank: 0, key };
}

function pairKey(pair) {
  if (YAML.isScalar(pair.key)) return String(pair.key.value);
  return String(pair.key);
}

function renderServices(lines, services) {
  lines.push("services:");

  const serviceNames = Object.keys(services);
  if (serviceNames.length === 0) {
    lines[lines.length - 1] = "services: {}";
    return;
  }

  serviceNames.forEach((serviceName, index) => {
    if (index > 0) lines.push("");
    lines.push(`${indent(1)}${serviceName}:`);

    const service = services[serviceName];
    if (!isPlainObject(service)) {
      renderValue(lines, service, 2);
      return;
    }

    for (const key of orderedKeys(service, SERVICE_KEY_ORDER)) {
      if (key === "environment" && isPlainObject(service.environment)) {
        renderMapping(lines, key, service.environment, 2, orderedKeys(service.environment, ENV_KEY_ORDER, true));
      } else if (key === "labels" && isPlainObject(service.labels)) {
        renderMapping(lines, key, service.labels, 2, orderedLabelKeys(service.labels), {
          quoteKeys: new Set(["npm.proxy.port"]),
        });
      } else if (key === "ports" && Array.isArray(service.ports)) {
        renderSequence(lines, key, service.ports, 2, { quotePortValues: true });
      } else {
        renderKeyValue(lines, key, service[key], 2);
      }
    }
  });
}

function renderMapping(lines, key, value, level, keys, options = {}) {
  if (keys.length === 0) {
    lines.push(`${indent(level)}${key}: {}`);
    return;
  }

  lines.push(`${indent(level)}${key}:`);
  for (const childKey of keys) {
    renderKeyValue(lines, childKey, value[childKey], level + 1, options);
  }
}

function renderSequence(lines, key, value, level, options = {}) {
  if (value.length === 0) {
    lines.push(`${indent(level)}${key}: []`);
    return;
  }

  lines.push(`${indent(level)}${key}:`);
  for (const item of value) {
    if (isScalar(item)) {
      lines.push(`${indent(level + 1)}- ${formatScalar(item, key, options)}`);
    } else if (isPlainObject(item)) {
      const entries = Object.entries(item);
      if (entries.length === 0) {
        lines.push(`${indent(level + 1)}- {}`);
      } else {
        const [firstKey, firstValue] = entries[0];
        lines.push(`${indent(level + 1)}- ${firstKey}: ${formatScalar(firstValue, firstKey, options)}`);
        for (const [childKey, childValue] of entries.slice(1)) {
          renderKeyValue(lines, childKey, childValue, level + 2, options);
        }
      }
    } else {
      lines.push(`${indent(level + 1)}-`);
      renderValue(lines, item, level + 2, options);
    }
  }
}

function renderKeyValue(lines, key, value, level, options = {}) {
  if (isScalar(value)) {
    if (typeof value === "string" && value.includes("\n")) {
      lines.push(`${indent(level)}${key}: |`);
      renderBlockScalar(lines, value, level + 1);
    } else {
      lines.push(`${indent(level)}${key}: ${formatScalar(value, key, options)}`);
    }
    return;
  }

  lines.push(`${indent(level)}${key}:`);
  renderValue(lines, value, level + 1, options);
}

function renderValue(lines, value, level, options = {}) {
  if (Array.isArray(value)) {
    for (const item of value) {
      if (isScalar(item)) {
        lines.push(`${indent(level)}- ${formatScalar(item, "", options)}`);
      } else {
        lines.push(`${indent(level)}-`);
        renderValue(lines, item, level + 1, options);
      }
    }
    return;
  }

  if (isPlainObject(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      lines[lines.length - 1] += " {}";
      return;
    }

    for (const [childKey, childValue] of entries) {
      renderKeyValue(lines, childKey, childValue, level, options);
    }
    return;
  }

  lines.push(`${indent(level)}${formatScalar(value, "", options)}`);
}

function renderBlockScalar(lines, value, level) {
  const blockLines = value.endsWith("\n") ? value.slice(0, -1).split("\n") : value.split("\n");
  for (const line of blockLines) {
    lines.push(`${indent(level)}${line}`);
  }
}

function formatScalar(value, key, options = {}) {
  if (value === null) return "null";
  if (typeof value === "number" || typeof value === "boolean") {
    if (options.quoteKeys?.has(key)) return quoteString(String(value));
    return String(value);
  }

  const stringValue = String(value);
  if (
    options.quotePortValues ||
    options.quoteKeys?.has(key) ||
    isPortMapping(stringValue) ||
    needsQuotes(stringValue)
  ) {
    return quoteString(stringValue);
  }

  return stringValue;
}

function isPortMapping(value) {
  return /^\d+(?::\d+){1,2}(?:\/(?:tcp|udp))?$/.test(value);
}

function needsQuotes(value) {
  return (
    value === "" ||
    /^[\d.]+$/.test(value) ||
    /:\s/.test(value) ||
    /^[,[\]{}#&*!?|>'"%@`-]/.test(value)
  );
}

function quoteString(value) {
  return JSON.stringify(value);
}

function isEmptyOverrideFile(source) {
  return /^(\s*#.*\n)*services:\s*\{\}\s*$/.test(source);
}

function isScalar(value) {
  return value === null || typeof value !== "object";
}

function isPlainObject(value) {
  return value !== null && !Array.isArray(value) && typeof value === "object";
}

function indent(level) {
  return "  ".repeat(level);
}

function discoverYamlFiles(rootDir) {
  const stacksDir = path.join(rootDir, "stacks");
  const files = [];

  walk(stacksDir, files);
  return files.sort((a, b) => a.localeCompare(b, "en", { sensitivity: "accent" }));
}

function walk(dir, files) {
  if (!fs.existsSync(dir)) return;

  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(fullPath, files);
    } else if (/\.ya?ml$/i.test(entry.name)) {
      files.push(fullPath);
    }
  }
}

function parseArgs(argv) {
  const flags = { mode: "check", files: [] };

  for (const arg of argv) {
    if (arg === "--check") {
      flags.mode = "check";
    } else if (arg === "--fix") {
      flags.mode = "fix";
    } else if (arg === "-h" || arg === "--help") {
      flags.help = true;
    } else {
      flags.files.push(path.resolve(arg));
    }
  }

  return flags;
}

function usage() {
  console.log(`Usage: node scripts/yaml-conventions.js [--check|--fix] [files...]

Checks or fixes Docker Compose YAML files under stacks/.

Examples:
  npm run yaml:check
  npm run yaml:fix
  node scripts/yaml-conventions.js --check stacks/arr/docker-compose.yml
`);
}

function runCli(argv = process.argv.slice(2), rootDir = path.resolve(__dirname, "..")) {
  const flags = parseArgs(argv);
  if (flags.help) {
    usage();
    return 0;
  }

  const files = flags.files.length > 0 ? flags.files : discoverYamlFiles(rootDir);
  const errors = [];
  let changed = 0;

  for (const file of files) {
    const source = fs.readFileSync(file, "utf8");

    if (flags.mode === "fix") {
      try {
        const formatted = formatComposeYaml(source);
        if (formatted !== source) {
          fs.writeFileSync(file, formatted, "utf8");
          changed += 1;
          console.log(`fixed ${path.relative(rootDir, file)}`);
        }
      } catch (error) {
        errors.push(formatYamlError(source, path.relative(rootDir, file), error));
      }
    } else {
      const result = checkComposeYaml(source, path.relative(rootDir, file));
      errors.push(...result.errors);
    }
  }

  if (errors.length > 0) {
    for (const error of errors) console.error(error);
    return 1;
  }

  if (flags.mode === "fix") {
    console.log(changed === 0 ? "YAML conventions already satisfied." : `Fixed ${changed} file(s).`);
  } else {
    console.log("YAML conventions check passed.");
  }

  return 0;
}

if (require.main === module) {
  process.exit(runCli());
}

module.exports = {
  checkComposeYaml,
  formatComposeYaml,
  runCli,
};
