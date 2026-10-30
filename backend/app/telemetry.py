import logging
import os
from collections.abc import Generator
from contextlib import contextmanager

from opentelemetry import baggage, context, trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.trace import Span, Status, StatusCode

from app.config import Settings

TRACER_NAME = "recipe-agents"
CORRELATION_KEYS = ("gen_ai.conversation.id", "session.id", "recipe.turn.id")


class ConversationCorrelationProcessor(SpanProcessor):
    def on_start(self, span: SDKSpan, parent_context: Context | None = None) -> None:
        for key in CORRELATION_KEYS:
            value = baggage.get_baggage(key, context=parent_context)
            if isinstance(value, str):
                span.set_attribute(key, value)

    def on_end(self, span: ReadableSpan) -> None:
        pass


def configure_telemetry(settings: Settings) -> None:
    logging.basicConfig(level=logging.INFO)
    os.environ.setdefault("OTEL_SERVICE_NAME", "recipe-agents")
    os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"
    os.environ["AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING"] = "true"
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = (
        "SPAN_AND_EVENT" if settings.capture_message_content else "NO_CONTENT"
    )
    if settings.applicationinsights_connection_string:
        from microsoft.opentelemetry import use_microsoft_opentelemetry

        os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = (
            settings.applicationinsights_connection_string
        )
        use_microsoft_opentelemetry(
            enable_azure_monitor=True,
            sampling_ratio=1.0,
            enable_sensitive_data=settings.capture_message_content,
            instrumentation_options={
                "langchain": {"enabled": True},
                "fastapi": {"enabled": False},
            },
        )
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            raise RuntimeError("The telemetry distro did not configure a tracer provider.")
        provider.add_span_processor(ConversationCorrelationProcessor())
    else:
        logging.getLogger(TRACER_NAME).warning(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is unset; Azure telemetry export is disabled."
        )


@contextmanager
def operation_span(name: str, conversation_id: str, turn_id: str) -> Generator[Span]:
    attributes = {
        "gen_ai.conversation.id": conversation_id,
        "session.id": conversation_id,
        "recipe.turn.id": turn_id,
    }
    correlation = context.get_current()
    for key in CORRELATION_KEYS:
        correlation = baggage.set_baggage(key, attributes[key], context=correlation)
    token = context.attach(correlation)
    try:
        with trace.get_tracer(TRACER_NAME).start_as_current_span(
            name, attributes=attributes
        ) as span:
            yield span
    finally:
        context.detach(token)


def trace_id() -> str:
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


def record_error(span: Span, error: BaseException) -> None:
    span.set_status(Status(StatusCode.ERROR, type(error).__name__))
    span.record_exception(error)


def flush_telemetry() -> None:
    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    if flush is not None:
        flush(timeout_millis=5000)
