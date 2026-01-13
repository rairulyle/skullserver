# Docker Compose Conventions

This document defines the conventions and standards for all Docker Compose files in this project.

## File Organization

- `compose.networking.yml` - Network-related services (proxy, DNS, etc.)
- `compose.media.yml` - Media server and acquisition services
- `compose.utilities.yml` - Utility and monitoring services

## Service Definition Order

Services must follow this property order:

```yaml
services:
  {service-name}:
    container_name: {service-name}
    image: {image:tag}
    # hostname: {hostname}                  # Optional: only if needed
    # network_mode: {mode}                  # Optional: only if needed (e.g., host)
    # cap_add:                              # Optional: only if needed
    #   - {CAPABILITY}
    # security_opt:                         # Optional: only if needed
    #   - {option}
    # devices:                              # Optional: only if needed
    #   - {device}
    ports:
      - "{host}:{container}"
    environment:
      PUID: 1000
      PGID: 1000
      TZ: ${TZ}
      {OTHER_ENV_VARS}
    volumes:
      - ${APP_DATA}/{service-name}:{container-path}
      - ${DATA}:{container-data-path}
    restart: unless-stopped
    labels:
      npm.proxy.domain: {subdomain}.${DOMAIN_NAME}
      npm.proxy.port: "{port}"
      net.unraid.docker.icon: https://raw.githubusercontent.com/homarr-labs/dashboard-icons/refs/heads/main/png/{icon}.png
      net.unraid.docker.webui: http://{subdomain}.${DOMAIN_NAME}
```

## Formatting Rules

### 1. Ports
- **Format**: Array format with strings
- **Example**:
  ```yaml
  ports:
    - "8080:80"
    - "443:443"
  ```

### 2. Environment Variables
- **Format**: Non-array (key-value pairs)
- **Order**:
  1. `PUID: 1000` (if applicable)
  2. `PGID: 1000` (if applicable)
  3. `TZ: ${TZ}` (if applicable)
  4. Other variables in alphabetical order
- **Example**:
  ```yaml
  environment:
    PUID: 1000
    PGID: 1000
    TZ: ${TZ}
    APP_KEY: ${SOME_KEY}
    VERSION: docker
  ```

### 3. Labels
- **Format**: Non-array (key-value pairs)
- **No hyphens/dashes** before label keys
- **Label Order**:
  1. NPM proxy labels (for frequently accessed services)
  2. Unraid docker labels (icon and webui)
- **Example**:
  ```yaml
  labels:
    npm.proxy.domain: plex.${DOMAIN_NAME}
    npm.proxy.port: "32400"
    net.unraid.docker.icon: https://raw.githubusercontent.com/homarr-labs/dashboard-icons/refs/heads/main/png/plex.png
    net.unraid.docker.webui: http://plex.${DOMAIN_NAME}
  ```

### 4. Volumes
- **Format**: Array format
- **Convention**: `${APP_DATA}/{service-name}` should mount directly to the container's config/data path
- **NO extra subdirectories** under `${APP_DATA}/{service-name}/`
- **Examples**:
  ```yaml
  # ✅ CORRECT
  volumes:
    - ${APP_DATA}/plex:/config
    - ${APP_DATA}/radarr:/config
    - ${DATA}:/data

  # ❌ WRONG - Extra subdirectories
  volumes:
    - ${APP_DATA}/plex/config:/config
    - ${APP_DATA}/scrutiny/config:/opt/scrutiny/config
    - ${APP_DATA}/scrutiny/influxdb:/opt/scrutiny/influxdb
  ```

### 5. Service Names & Container Names
- Use lowercase with hyphens for multi-word names
- **Always include `container_name`** - it should match the service name for consistency
- **Why?** Without `container_name`, Docker Compose generates names like `skullpluggery-server-homepage-1` instead of just `homepage`
- This ensures explicit naming and avoids issues with Docker Compose automatic naming
- **Examples**:
  ```yaml
  # ✅ CORRECT - container_name explicitly set
  services:
    plex:
      container_name: plex
      image: lscr.io/linuxserver/plex:latest

  # ✅ CORRECT - container_name matches service name
  services:
    nginx-proxy-manager:
      container_name: nginx-proxy-manager
      image: jc21/nginx-proxy-manager:latest

  # ❌ WRONG - Missing container_name
  services:
    plex:
      image: lscr.io/linuxserver/plex:latest
  ```

## Environment Variables

### Standard Variables
- `${APP_DATA}` - Base directory for application data (`~/docker`)
- `${DATA}` - Base directory for media/data files
- `${TZ}` - Timezone
- `${IP_ADDRESS}` - Server IP address
- `${DOMAIN_NAME}` - Domain name for services

### Service-Specific Variables
- Use SCREAMING_SNAKE_CASE
- Prefix with service name when applicable
- **Examples**: `${PLEX_API_KEY}`, `${RADARR_API_KEY}`, `${DISCORD_TOKEN}`

## Common Patterns

### Standard User/Group
```yaml
environment:
  PUID: 1000
  PGID: 1000
```

### Unraid Docker Labels
```yaml
labels:
  net.unraid.docker.icon: https://raw.githubusercontent.com/homarr-labs/dashboard-icons/refs/heads/main/png/{icon}.png
  net.unraid.docker.webui: http://{subdomain}.${DOMAIN_NAME}
```

**Notes:**
- Icons are sourced from the homarr-labs dashboard-icons repository
- The webui URL should match the service's domain URL
- Use PNG format for icons (path: `.../png/{icon}.png`)

### Nginx Proxy Manager (NPM) Auto-Configuration
For services with web UIs that you frequently access, add npm labels to automatically configure reverse proxy via npm-docker-sync:

```yaml
labels:
  npm.proxy.domain: {subdomain}.${DOMAIN_NAME}
  npm.proxy.port: "{internal-port}"
```

**When to add NPM labels:**
- Services with web interfaces that you access regularly
- Services that need SSL/HTTPS access
- **Do NOT add** for internal-only services or services without web UIs

**Notes:**
- The `npm.proxy.domain` should match the subdomain used in `homepage.href`
- The `npm.proxy.port` should be the internal container port (not the host port)
- These labels work with the `npm-docker-sync` service to automatically create/update proxy hosts
- Default settings (Force SSL, Block Common Exploits, WebSockets, HTTP/2) are configured in the `npm-docker-sync` service and apply to all proxies automatically
- You can override defaults on a per-service basis by adding specific labels like `npm.proxy.ssl.force: "false"` if needed

### Restart Policy
- Always use: `restart: unless-stopped`

## Exceptions

Some services may require deviations from these conventions due to:
- Container-specific requirements
- Multiple data directories needed
- Special configuration needs

Document any exceptions with inline comments explaining why.

## Checklist for New Services

- [ ] Service name matches container name
- [ ] Properties in correct order
- [ ] Ports use array format with strings
- [ ] Environment variables use non-array format
- [ ] Labels use non-array format (no hyphens)
- [ ] Volumes follow `${APP_DATA}/{service-name}` convention
- [ ] `restart: unless-stopped` is set
- [ ] PUID/PGID set to 1000 (if applicable)
- [ ] TZ set to `${TZ}` (if applicable)
- [ ] Unraid docker labels added (icon and webui)
- [ ] NPM proxy labels added (if service is frequently accessed via web)
