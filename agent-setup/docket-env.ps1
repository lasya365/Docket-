# Docket telemetry for Claude Code — PowerShell (Windows).
#
#   . .\docket-env.ps1     <-- note the leading dot: it must run IN your shell,
#   claude                     not in a child process, or nothing is exported.
#
# Claude Code reads these once, at startup. Setting them after `claude` is running
# has no effect. `export VAR=value` is bash syntax and does nothing in PowerShell.

$env:DOCKET_URL          = "http://localhost:8000"
$env:DOCKET_INGEST_TOKEN = "change-me"      # must equal DOCKET_INGEST_TOKEN in Docket's .env

$env:CLAUDE_CODE_ENABLE_TELEMETRY = "1"
$env:OTEL_LOGS_EXPORTER           = "otlp"
$env:OTEL_METRICS_EXPORTER        = "otlp"
$env:OTEL_EXPORTER_OTLP_PROTOCOL  = "http/json"
$env:OTEL_EXPORTER_OTLP_ENDPOINT  = $env:DOCKET_URL
$env:OTEL_EXPORTER_OTLP_HEADERS   = "Authorization=Bearer $($env:DOCKET_INGEST_TOKEN)"
$env:OTEL_LOG_USER_PROMPTS        = "1"     # without this, prompts arrive redacted
$env:OTEL_LOG_TOOL_DETAILS        = "1"     # without this, MCP tools report only as `mcp_tool`
$env:OTEL_LOGS_EXPORT_INTERVAL    = "2000"
$env:OTEL_METRICS_INCLUDE_REPOSITORY = "true"

Write-Host "Docket telemetry set for this shell -> $($env:DOCKET_URL)"
Write-Host "Now start the agent in this same window:  claude"
