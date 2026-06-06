const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const {
  checkComposeYaml,
  formatComposeYaml,
  runCli,
} = require("../scripts/yaml-conventions");

test("reorders service, environment, and label keys without changing values", () => {
  const source = `name: sample

services:
  app:
    image: example/app:latest
    sysctls:
      net.ipv4.ip_unprivileged_port_start: 0
    labels:
      net.unraid.docker.webui: https://app.example.test
      npm.proxy.port: 8080
      npm.proxy.advanced.config: send_timeout 10m;
      npm.proxy.domain: app.\${DOMAIN_NAME}
      net.unraid.docker.icon: https://example.test/icon.png
    ports:
      - 8080:80 # web UI
    security_opt:
      - no-new-privileges:true
    container_name: app
    healthcheck:
      disable: false
    env_file:
      - .env
    devices:
      - /dev/dri:/dev/dri
    cap_add:
      - NET_ADMIN
    environment:
      EXTRA: value
      PASSWORD: secret
      USERNAME: app
      TZ: \${TZ}
      PGID: \${PGID}
      PUID: \${PUID}
      UMASK: \${UMASK}
    restart: unless-stopped
`;

  assert.equal(
    formatComposeYaml(source),
    `name: sample

services:
  app:
    container_name: app
    image: example/app:latest
    devices:
      - /dev/dri:/dev/dri
    cap_add:
      - NET_ADMIN
    security_opt:
      - no-new-privileges:true
    sysctls:
      net.ipv4.ip_unprivileged_port_start: 0
    ports:
      - 8080:80 # web UI
    environment:
      UMASK: \${UMASK}
      PUID: \${PUID}
      PGID: \${PGID}
      TZ: \${TZ}
      EXTRA: value
      USERNAME: app
      PASSWORD: secret
    env_file:
      - .env
    healthcheck:
      disable: false
    restart: unless-stopped
    labels:
      npm.proxy.domain: app.\${DOMAIN_NAME}
      npm.proxy.port: 8080
      npm.proxy.advanced.config: send_timeout 10m;
      net.unraid.docker.icon: https://example.test/icon.png
      net.unraid.docker.webui: https://app.example.test
`
  );
});

test("check mode reports files that would be reformatted", () => {
  const source = `services:
  app:
    image: example/app:latest
    container_name: app
`;

  assert.deepEqual(checkComposeYaml(source, "stacks/demo/docker-compose.yml"), {
    ok: false,
    errors: [
      "stacks/demo/docker-compose.yml: YAML is valid but convention order/formatting differs. Run npm run yaml:fix.",
    ],
  });
});

test("check mode reports invalid YAML", () => {
  const source = `services:
  app:
    image: [unterminated
`;

  const result = checkComposeYaml(source, "stacks/demo/docker-compose.yml");

  assert.equal(result.ok, false);
  assert.match(
    result.errors[0],
    /^stacks\/demo\/docker-compose.yml:3:\d+: invalid YAML:/
  );
});

test("fix mode reports invalid YAML with line and column", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "yaml-conventions-"));
  const filePath = path.join(dir, "bad.yml");
  fs.writeFileSync(
    filePath,
    `services:
  app:
    image: [unterminated
`,
    "utf8"
  );

  const errors = [];
  const originalError = console.error;
  console.error = (message) => errors.push(message);

  try {
    assert.equal(runCli(["--fix", filePath], dir), 1);
  } finally {
    console.error = originalError;
    fs.rmSync(dir, { recursive: true, force: true });
  }

  assert.match(errors[0], /^bad\.yml:3:\d+: invalid YAML:/);
});

test("empty override files stay compact", () => {
  const source = `# Override file for UI labels (icon, webui, shell)
# This file is managed by Compose Manager
services: {}
`;

  assert.equal(formatComposeYaml(source), source);
});
