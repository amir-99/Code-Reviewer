import structlog
from prometheus_client import Counter

REVIEWS = Counter("reviewer_runs_total", "Completed review jobs", ["state"])


def configure():
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


DISCARDED = Counter(
    "reviewer_findings_discarded_total",
    "Mechanical fabrication discards",
    ["agent", "prompt_version", "reason"],
)


def configure_traces(endpoint):
    if not endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
