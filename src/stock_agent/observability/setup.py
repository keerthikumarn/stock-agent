"""One-call OpenTelemetry bootstrap shared by the agent and the MCP server."""

from __future__ import annotations

import logging
import socket
import uuid
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_INSTANCE_ID, SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from stock_agent.config import Settings
from stock_agent.observability.logging import PayloadSanitizer, configure_logging
from stock_agent.observability.metrics import Instruments, create_instruments

log = logging.getLogger(__name__)


@dataclass
class Telemetry:
    service_name: str
    tracer: trace.Tracer
    instruments: Instruments
    sanitize: PayloadSanitizer
    _tracer_provider: TracerProvider | None = None
    _meter_provider: MeterProvider | None = None
    _logger_provider: LoggerProvider | None = None

    def shutdown(self) -> None:
        """Flush and close exporters. Call on graceful shutdown."""
        for provider in (self._tracer_provider, self._meter_provider, self._logger_provider):
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:  # noqa: BLE001 - never fail shutdown on telemetry
                    log.warning("telemetry provider shutdown failed", exc_info=True)


_telemetry: Telemetry | None = None


def init_telemetry(service_name: str, settings: Settings) -> Telemetry:
    """Initialise tracing, metrics, logs and auto-instrumentation (idempotent).

    Exporter endpoints/headers come from the standard OTEL_EXPORTER_OTLP_* env
    vars; sampling from OTEL_TRACES_SAMPLER / OTEL_TRACES_SAMPLER_ARG.
    """
    global _telemetry
    if _telemetry is not None:
        return _telemetry

    configure_logging(service_name, settings.log_level, settings.log_format)

    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            SERVICE_VERSION: settings.service_version,
            SERVICE_INSTANCE_ID: f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}",
            "deployment.environment.name": settings.deployment_environment,
        }
    )

    tp = mp = lp = None
    if settings.otel_enabled:
        tp = TracerProvider(resource=resource)
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tp)

        mp = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(), export_interval_millis=settings.otel_metric_export_interval_ms
                )
            ],
        )
        metrics.set_meter_provider(mp)

        lp = LoggerProvider(resource=resource)
        lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(lp)
        otlp_handler = LoggingHandler(level=logging.INFO, logger_provider=lp)
        otlp_handler.set_name("otlp")
        logging.getLogger().addHandler(otlp_handler)

        # Outbound HTTP (Ollama, MCP) gets client spans + W3C traceparent injection.
        HTTPXClientInstrumentor().instrument()

    _telemetry = Telemetry(
        service_name=service_name,
        tracer=trace.get_tracer("stock_agent", settings.service_version),
        instruments=create_instruments(),
        sanitize=PayloadSanitizer(settings.log_payloads, settings.log_payload_max_chars),
        _tracer_provider=tp,
        _meter_provider=mp,
        _logger_provider=lp,
    )
    log.info(
        "telemetry initialised",
        extra={"event": "telemetry.init", "otel_enabled": settings.otel_enabled, "env": settings.deployment_environment},
    )
    return _telemetry


def get_telemetry() -> Telemetry:
    if _telemetry is None:
        raise RuntimeError("init_telemetry() must be called at process start-up")
    return _telemetry


def _reset_for_tests() -> None:
    global _telemetry
    _telemetry = None
