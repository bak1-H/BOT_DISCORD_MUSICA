# Fly.io — Referencia de comandos

Todos los comandos usados para deployar y administrar el bot en Fly.io.

## Instalación de flyctl

```powershell
# En PowerShell como administrador
iwr https://fly.io/install.ps1 -useb | iex
```

Cerrar y volver a abrir la terminal para que tome efecto el PATH.

## Autenticación

```bash
fly auth login
```

## Deploy

```bash
# Deploy normal (lee fly.toml del directorio actual)
fly deploy

# Ver historial de deploys
fly releases -a bot-discord-musica
```

## Máquinas

```bash
# Listar todas las máquinas
fly machine list -a bot-discord-musica

# Ver estado detallado de una máquina
fly machine status <machine-id>

# Escalar a N máquinas (usar 1 para este bot)
fly scale count 1 -a bot-discord-musica

# Destruir una máquina específica
fly machine destroy <machine-id> --force -a bot-discord-musica
```

## Logs

```bash
# Ver logs en tiempo real
fly logs -a bot-discord-musica
```

## Secrets (variables de entorno)

```bash
# Setear uno o varios secrets
fly secrets set DISCORD_TOKEN="tu_token" -a bot-discord-musica
fly secrets set DISCORD_TOKEN="tu_token" GENIUS_TOKEN="tu_token" -a bot-discord-musica

# Listar secrets configurados (solo nombres, no valores)
fly secrets list -a bot-discord-musica

# Eliminar un secret
fly secrets unset NOMBRE_VARIABLE -a bot-discord-musica
```

## Cookies de YouTube

```bash
# Subir cookies exportadas del navegador
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt"))
fly secrets set YOUTUBE_COOKIES_B64="$b64" -a bot-discord-musica

# O usar el script incluido en el proyecto
.\refresh_cookies.ps1
```

## Regiones

```bash
# Ver regiones disponibles
fly platform regions

# Cambiar región: editar fly.toml y redesployar
# primary_region = 'gru'   <- São Paulo, Brasil (recomendado para Sudamérica)
# primary_region = 'eze'   <- Buenos Aires
fly deploy
```

## Métricas y monitoreo

```bash
# Estado de la máquina
fly machine status <machine-id>

# Métricas gráficas (CPU, RAM, red) — solo disponible en el dashboard web
# https://fly.io/apps/bot-discord-musica/metrics
```

## Escalar recursos

Editar la sección `[[vm]]` en `fly.toml` y redesployar:

```toml
[[vm]]
  memory = '512mb'   # 256mb | 512mb | 1gb | 2gb
  cpu_kind = 'shared'
  cpus = 1
```

```bash
fly deploy
```

## fly.toml de referencia

```toml
app = 'bot-discord-musica'
primary_region = 'gru'

[build]

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = 'off'
  auto_start_machines = true
  min_machines_running = 0
  processes = ['app']

[[vm]]
  memory = '512mb'
  cpu_kind = 'shared'
  cpus = 1
```

## Flujo completo desde cero

```bash
# 1. Login
fly auth login

# 2. Deploy inicial
fly deploy

# 3. Configurar secrets
fly secrets set DISCORD_TOKEN="..." GENIUS_TOKEN="..." -a bot-discord-musica

# 4. Asegurarse de tener solo 1 máquina
fly scale count 1 -a bot-discord-musica

# 5. Ver que todo esté corriendo
fly machine list -a bot-discord-musica
fly logs -a bot-discord-musica
```
