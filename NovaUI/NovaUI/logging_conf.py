import logging
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk.resources import Resource

# 1) Create a Resource to identify this service
resource = Resource.create({
    "service.name": "novaui",  # change to your app name
    "service.namespace": "backend",
    "deployment.environment": "dev",  # dev / staging / prod etc.
})

# 2) Create LoggerProvider with the resource
logger_provider = LoggerProvider(resource=resource)

# 3) Configure OTLP exporter (HTTP example; you can use gRPC exporter if you prefer)
otlp_log_exporter = OTLPLogExporter(
    # Change endpoint to your OTel collector / backend
    endpoint="http://otel-collector:4318/v1/logs",
    # headers={"Authorization": "Bearer <token>"}  # if needed
)

# 4) Attach processor
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(otlp_log_exporter)
)

# 5) Create logging handler that sends Python logs to OTel
otel_handler = LoggingHandler(
    level=logging.INFO,  # minimum log level to export
    logger_provider=logger_provider,
)
