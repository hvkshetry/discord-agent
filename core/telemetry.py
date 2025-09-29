"""Telemetry and observability layer"""
import logging
import structlog
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any, Dict, Optional


# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)


logger = structlog.get_logger()


class Telemetry:
    """Telemetry and observability"""

    def __init__(self):
        self.logger = logger

    @asynccontextmanager
    async def trace_task(self, name: str, **context):
        """Time async operations with context"""
        start = perf_counter()
        self.logger.info(f"{name}.start", **context)

        try:
            yield
            duration = perf_counter() - start
            self.logger.info(
                f"{name}.complete",
                duration_ms=round(duration * 1000, 2),
                **context
            )
        except Exception as e:
            duration = perf_counter() - start
            self.logger.error(
                f"{name}.failed",
                error=str(e),
                error_type=type(e).__name__,
                duration_ms=round(duration * 1000, 2),
                **context
            )
            raise

    def log_event(self, event_type: str, level: str = "info", **context):
        """Log structured event"""
        log_fn = getattr(self.logger, level, self.logger.info)
        log_fn(event_type, **context)

    def log_metric(self, metric_name: str, value: float, **context):
        """Log metric with context"""
        self.logger.info(
            "metric",
            metric_name=metric_name,
            value=value,
            **context
        )


# Global telemetry instance
telemetry = Telemetry()