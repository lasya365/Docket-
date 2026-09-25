@echo off
REM Docket telemetry for Claude Code — cmd.exe (Windows).
REM   docket-env.cmd
REM   claude
REM Claude Code reads these once, at startup.

set DOCKET_URL=http://localhost:8000
set DOCKET_INGEST_TOKEN=change-me

set CLAUDE_CODE_ENABLE_TELEMETRY=1
set OTEL_LOGS_EXPORTER=otlp
set OTEL_METRICS_EXPORTER=otlp
set OTEL_EXPORTER_OTLP_PROTOCOL=http/json
set OTEL_EXPORTER_OTLP_ENDPOINT=%DOCKET_URL%
set OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer %DOCKET_INGEST_TOKEN%
set OTEL_LOG_USER_PROMPTS=1
set OTEL_LOG_TOOL_DETAILS=1
set OTEL_LOGS_EXPORT_INTERVAL=2000
set OTEL_METRICS_INCLUDE_REPOSITORY=true

echo Docket telemetry set for this shell -^> %DOCKET_URL%
echo Now start the agent in this same window:  claude
