"""OpenTelemetry Collector Contrib exporter catalog and YAML builders.

Component names match the collector config keys (see opentelemetry-collector-contrib/exporter).
https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import yaml

CONTRIB_EXPORTERS_REPO = (
    "https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter"
)

# GitHub folder name -> collector YAML component type
_FOLDER_OVERRIDES: dict[str, str] = {
    "alibabacloudlogserviceexporter": "alibabacloud_logservice",
    "awscloudwatchlogsexporter": "awscloudwatchlogs",
    "splunkhecexporter": "splunk_hec",
    "tencentcloudlogserviceexporter": "tencentcloud_logservice",
    "googlemanagedprometheusexporter": "googlemanagedprometheus",
    "prometheusremotewriteexporter": "prometheusremotewrite",
    "fileexporter": "file",
    "prometheusexporter": "prometheus",
    "otelarrowexporter": "otelarrow",
}


def folder_to_component(folder: str) -> str:
    if folder in _FOLDER_OVERRIDES:
        return _FOLDER_OVERRIDES[folder]
    name = folder
    if name.endswith("exporter"):
        name = name[: -len("exporter")]
    return name


CONTRIB_EXPORTER_FOLDERS: tuple[str, ...] = (
    "alertmanagerexporter",
    "alibabacloudlogserviceexporter",
    "awscloudwatchlogsexporter",
    "awsemfexporter",
    "awskinesisexporter",
    "awss3exporter",
    "awsxrayexporter",
    "azureblobexporter",
    "azuredataexplorerexporter",
    "azuremonitorexporter",
    "bmchelixexporter",
    "cassandraexporter",
    "clickhouseexporter",
    "coralogixexporter",
    "datadogexporter",
    "datasetexporter",
    "dorisexporter",
    "elasticsearchexporter",
    "faroexporter",
    "fileexporter",
    "googlecloudexporter",
    "googlecloudpubsubexporter",
    "googlecloudstorageexporter",
    "googlemanagedprometheusexporter",
    "googlesecopsexporter",
    "honeycombmarkerexporter",
    "influxdbexporter",
    "kafkaexporter",
    "loadbalancingexporter",
    "logicmonitorexporter",
    "logzioexporter",
    "mezmoexporter",
    "opensearchexporter",
    "otelarrowexporter",
    "prometheusexporter",
    "prometheusremotewriteexporter",
    "pulsarexporter",
    "rabbitmqexporter",
    "sematextexporter",
    "sentryexporter",
    "signalfxexporter",
    "splunkhecexporter",
    "stefexporter",
    "sumologicexporter",
    "syslogexporter",
    "tencentcloudlogserviceexporter",
    "tinybirdexporter",
    "zipkinexporter",
)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    field_type: str = "text"  # text | password | number | bool | textarea | select
    default: Any = ""
    required: bool = False
    placeholder: str = ""
    options: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d["options"]:
            d.pop("options")
        return d


_SIGNAL_BOTH = ("metrics", "logs")


@dataclass(frozen=True)
class ExporterSpec:
    type: str
    component: str
    label: str
    description: str
    category: str
    fields: tuple[FieldSpec, ...] = ()
    doc_folder: str = ""
    signals: tuple[str, ...] = ("metrics",)
    build: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def default_params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in self.fields:
            if f.default != "" and f.default is not None:
                out[f.name] = f.default
        return out

    def to_catalog_entry(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "component": self.component,
            "label": self.label,
            "description": self.description,
            "category": self.category,
            "signals": list(self.signals),
            "doc_folder": self.doc_folder or f"{self.component}exporter",
            "doc_url": f"{CONTRIB_EXPORTERS_REPO}/{self.doc_folder or self.component + 'exporter'}",
            "fields": [f.to_dict() for f in self.fields],
        }


def _p(params: dict[str, Any], name: str, default: Any = "") -> Any:
    v = params.get(name, default)
    return default if v is None or v == "" else v


def _split_csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


# Shared field for exporters that use the standard collector TLS client settings.
_SKIP_VERIFY_FIELD = FieldSpec(
    "insecure_skip_verify",
    "Skip TLS certificate verification",
    field_type="bool",
    default=False,
)


def _with_tls_skip_verify(cfg: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Merge tls.insecure_skip_verify into an exporter config when requested."""
    if bool(params.get("insecure_skip_verify", False)):
        tls = cfg.setdefault("tls", {})
        tls["insecure"] = False
        tls["insecure_skip_verify"] = True
    return cfg


def _build_otlp_http(params: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(_p(params, "endpoint", "http://localhost:4318"))
    base = endpoint.rstrip("/")
    cfg: dict[str, Any] = {"compression": _p(params, "compression", "gzip")}
    if params.get("_pipeline") == "logs":
        cfg["logs_endpoint"] = str(_p(params, "logs_endpoint", f"{base}/v1/logs"))
    else:
        cfg["metrics_endpoint"] = str(_p(params, "metrics_endpoint", f"{base}/v1/metrics"))
    return _with_tls_skip_verify(cfg, params)


def _build_otlp_grpc(params: dict[str, Any]) -> dict[str, Any]:
    cfg = {
        "endpoint": str(_p(params, "endpoint", "localhost:4317")),
        "compression": _p(params, "compression", "gzip"),
        "tls": {"insecure": bool(_p(params, "insecure", True))},
    }
    return _with_tls_skip_verify(cfg, params)


def _build_prometheus(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "endpoint": str(_p(params, "endpoint", "0.0.0.0:8889")),
        "const_labels": {"source": "bigip-telemetry-exporter"},
    }


def _build_debug(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "verbosity": _p(params, "verbosity", "basic"),
        "sampling_initial": 5,
        "sampling_thereafter": 200,
    }


def _build_file(params: dict[str, Any]) -> dict[str, Any]:
    default_path = (
        "/tmp/bigip-logs.json"
        if params.get("_pipeline") == "logs"
        else "/tmp/bigip-telemetry.json"
    )
    return {
        "path": str(_p(params, "path", default_path)),
        "rotation": {"max_megabytes": 10, "max_days": 1, "max_backups": 2},
    }


def _build_prometheusremotewrite(params: dict[str, Any]) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "endpoint": str(_p(params, "endpoint", "http://localhost:9090/api/v1/write")),
    }
    if _p(params, "headers", ""):
        cfg["headers"] = {
            line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
            for line in str(params["headers"]).splitlines()
            if ":" in line
        }
    return _with_tls_skip_verify(cfg, params)


def _build_kafka(params: dict[str, Any]) -> dict[str, Any]:
    brokers = _split_csv(str(_p(params, "brokers", "localhost:9092")))
    cfg = {
        "brokers": brokers,
        "topic": str(_p(params, "topic", "otel-metrics")),
        "encoding": str(_p(params, "encoding", "otlp_proto")),
    }
    if bool(params.get("insecure_skip_verify", False)):
        cfg["auth"] = {
            "tls": {
                "insecure": False,
                "insecure_skip_verify": True,
            },
        }
    return cfg


def _build_datadog(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "api": {"key": str(_p(params, "api_key", ""))},
        "host_metadata": {"enabled": bool(_p(params, "host_metadata", True))},
    }


def _build_signalfx(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "access_token": str(_p(params, "access_token", "")),
        "realm": str(_p(params, "realm", "us0")),
    }


def _build_splunk_hec(params: dict[str, Any]) -> dict[str, Any]:
    cfg = {
        "token": str(_p(params, "token", "")),
        "endpoint": str(_p(params, "endpoint", "https://localhost:8088/services/collector")),
    }
    return _with_tls_skip_verify(cfg, params)


def _build_coralogix(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "domain": str(_p(params, "domain", "coralogix.com")),
        "private_key": str(_p(params, "private_key", "")),
        "application_name": str(_p(params, "application_name", "bigip-telemetry")),
        "subsystem_name": str(_p(params, "subsystem_name", "exporter")),
    }


def _build_elasticsearch(params: dict[str, Any]) -> dict[str, Any]:
    endpoints = _split_csv(str(_p(params, "endpoints", "http://localhost:9200")))
    return _with_tls_skip_verify({"endpoints": endpoints}, params)


def _build_influxdb(params: dict[str, Any]) -> dict[str, Any]:
    cfg = {
        "endpoint": str(_p(params, "endpoint", "http://localhost:8086")),
        "org": str(_p(params, "org", "")),
        "bucket": str(_p(params, "bucket", "")),
        "token": str(_p(params, "token", "")),
    }
    return _with_tls_skip_verify(cfg, params)


def _build_googlecloud(params: dict[str, Any]) -> dict[str, Any]:
    cfg: dict[str, Any] = {"project": str(_p(params, "project", ""))}
    if _p(params, "user_agent", ""):
        cfg["user_agent"] = str(params["user_agent"])
    return cfg


def _build_googlemanagedprometheus(params: dict[str, Any]) -> dict[str, Any]:
    return {"project": str(_p(params, "project", ""))}


def _build_awss3(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "region": str(_p(params, "region", "us-east-1")),
        "s3uploader": {
            "bucket": str(_p(params, "bucket", "")),
            "prefix": str(_p(params, "prefix", "otel-metrics/")),
        },
    }


def _build_awsemf(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "region": str(_p(params, "region", "us-east-1")),
        "namespace": str(_p(params, "namespace", "BIGIP/Metrics")),
        "log_group_name": str(_p(params, "log_group_name", "")),
    }


def _build_azuremonitor(params: dict[str, Any]) -> dict[str, Any]:
    return {"instrumentation_key": str(_p(params, "instrumentation_key", ""))}


def _build_opensearch(params: dict[str, Any]) -> dict[str, Any]:
    endpoints = _split_csv(str(_p(params, "endpoints", "https://localhost:9200")))
    http: dict[str, Any] = {"endpoint": endpoints[0] if endpoints else ""}
    if bool(params.get("insecure_skip_verify", False)):
        http["tls"] = {
            "insecure": False,
            "insecure_skip_verify": True,
        }
    return {"http": http}


def _build_logzio(params: dict[str, Any]) -> dict[str, Any]:
    cfg = {
        "token": str(_p(params, "token", "")),
        "region": str(_p(params, "region", "us")),
    }
    return _with_tls_skip_verify(cfg, params)


def _build_sumologic(params: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(_p(params, "endpoint", "")).strip()
    if not endpoint:
        raise ValueError(
            "Sumo Logic exporter requires an HTTP Source URL "
            "(Sumo Logic → Manage Data → Collection → Sources → HTTP Logs). "
            "Paste the full https://collectors.../receiver/v1/http/... URL."
        )
    return _with_tls_skip_verify(
        {
            "endpoint": endpoint,
            "log_format": "otlp",
        },
        params,
    )


def _build_mezmo(params: dict[str, Any]) -> dict[str, Any]:
    cfg = {
        "ingest_url": str(_p(params, "ingest_url", "https://api.mezmo.com/v1/ingest")),
        "ingest_key": str(_p(params, "ingest_key", "")),
    }
    return _with_tls_skip_verify(cfg, params)


def _build_pulsar(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "endpoint": str(_p(params, "endpoint", "pulsar://localhost:6650")),
        "topic": str(_p(params, "topic", "persistent://public/default/otel-metrics")),
    }


def _build_rabbitmq(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection": {"endpoint": str(_p(params, "endpoint", "amqp://localhost:5672"))},
        "routing_key": str(_p(params, "routing_key", "otel-metrics")),
    }


def _build_otelarrow(params: dict[str, Any]) -> dict[str, Any]:
    cfg = {
        "endpoint": str(_p(params, "endpoint", "localhost:4317")),
        "tls": {"insecure": bool(_p(params, "insecure", True))},
    }
    return _with_tls_skip_verify(cfg, params)


def _build_sematext(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "token": str(_p(params, "token", "")),
        "region": str(_p(params, "region", "US")),
    }


def _build_syslog(params: dict[str, Any]) -> dict[str, Any]:
    cfg = {
        "endpoint": str(_p(params, "endpoint", "udp://localhost:514")),
        "network": str(_p(params, "network", "udp")),
    }
    return _with_tls_skip_verify(cfg, params)


def _build_clickhouse(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "endpoint": str(_p(params, "endpoint", "tcp://localhost:9000")),
        "database": str(_p(params, "database", "default")),
        "table_name": str(_p(params, "table_name", "otel_metrics")),
    }


def _build_cassandra(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "dsn": str(_p(params, "dsn", "127.0.0.1")),
        "keyspace": str(_p(params, "keyspace", "otel")),
    }


def _build_logicmonitor(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "api_token_access_id": str(_p(params, "api_token_access_id", "")),
        "api_token_access_key": str(_p(params, "api_token_access_key", "")),
    }


def _build_contrib_custom(params: dict[str, Any]) -> dict[str, Any]:
    raw = str(_p(params, "config_yaml", "")).strip()
    if not raw:
        return {}
    parsed = yaml.safe_load(raw)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("config_yaml must be a YAML mapping (exporter settings only)")
    return parsed


def _curated_specs() -> tuple[ExporterSpec, ...]:
    return (
        ExporterSpec(
            type="prometheus",
            component="prometheus",
            label="Prometheus (scrape)",
            description="Expose metrics for Prometheus or compatible scrapers.",
            category="Core",
            doc_folder="prometheusexporter",
            fields=(
                FieldSpec("endpoint", "Listen endpoint", default="0.0.0.0:8889"),
            ),
            build=_build_prometheus,
        ),
        ExporterSpec(
            type="otlp_http",
            component="otlphttp",
            label="OTLP HTTP",
            description="Forward telemetry to a remote OTLP/HTTP endpoint.",
            category="Core",
            doc_folder="otlphttpexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec("endpoint", "Base URL", default="http://localhost:4318"),
                FieldSpec(
                    "metrics_endpoint",
                    "Metrics URL (optional)",
                    placeholder="Defaults to {base}/v1/metrics",
                ),
                FieldSpec(
                    "logs_endpoint",
                    "Logs URL (optional)",
                    placeholder="Defaults to {base}/v1/logs",
                ),
                FieldSpec(
                    "compression",
                    "Compression",
                    field_type="select",
                    default="gzip",
                    options=("gzip", "none"),
                ),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_otlp_http,
        ),
        ExporterSpec(
            type="otlp_grpc",
            component="otlp",
            label="OTLP gRPC",
            description="Forward telemetry to a remote OTLP/gRPC endpoint.",
            category="Core",
            doc_folder="otlpexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec("endpoint", "host:port", default="localhost:4317"),
                FieldSpec(
                    "compression",
                    "Compression",
                    field_type="select",
                    default="gzip",
                    options=("gzip", "none"),
                ),
                FieldSpec("insecure", "TLS insecure", field_type="bool", default=True),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_otlp_grpc,
        ),
        ExporterSpec(
            type="debug",
            component="debug",
            label="Debug",
            description="Log telemetry to collector stdout.",
            category="Core",
            doc_folder="debugexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec(
                    "verbosity",
                    "Verbosity",
                    field_type="select",
                    default="basic",
                    options=("basic", "normal", "detailed"),
                ),
            ),
            build=_build_debug,
        ),
        ExporterSpec(
            type="file",
            component="file",
            label="File (JSON)",
            description="Write telemetry to a JSON file in the collector container.",
            category="Core",
            doc_folder="fileexporter",
            signals=_SIGNAL_BOTH,
            fields=(FieldSpec("path", "File path", default="/tmp/bigip-telemetry.json"),),
            build=_build_file,
        ),
        ExporterSpec(
            type="prometheusremotewrite",
            component="prometheusremotewrite",
            label="Prometheus remote write",
            description="Push metrics to Prometheus, Grafana Mimir, or Grafana Cloud.",
            category="Observability",
            doc_folder="prometheusremotewriteexporter",
            fields=(
                FieldSpec(
                    "endpoint",
                    "Remote write URL",
                    default="http://localhost:9090/api/v1/write",
                    required=True,
                ),
                FieldSpec(
                    "headers",
                    "Headers (one per line, Key: Value)",
                    field_type="textarea",
                    placeholder="Authorization: Bearer token",
                ),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_prometheusremotewrite,
        ),
        ExporterSpec(
            type="datadog",
            component="datadog",
            label="Datadog",
            description="Send metrics to Datadog.",
            category="Observability",
            doc_folder="datadogexporter",
            fields=(
                FieldSpec("api_key", "API key", field_type="password", required=True),
                FieldSpec("host_metadata", "Host metadata", field_type="bool", default=True),
            ),
            build=_build_datadog,
        ),
        ExporterSpec(
            type="signalfx",
            component="signalfx",
            label="Splunk Observability (SignalFx)",
            description="Send metrics to Splunk Observability Cloud.",
            category="Observability",
            doc_folder="signalfxexporter",
            fields=(
                FieldSpec("access_token", "Access token", field_type="password", required=True),
                FieldSpec("realm", "Realm", default="us0"),
            ),
            build=_build_signalfx,
        ),
        ExporterSpec(
            type="splunk_hec",
            component="splunk_hec",
            label="Splunk HEC",
            description="Send telemetry via Splunk HTTP Event Collector.",
            category="Observability",
            doc_folder="splunkhecexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec("token", "HEC token", field_type="password", required=True),
                FieldSpec(
                    "endpoint",
                    "HEC endpoint",
                    default="https://localhost:8088/services/collector",
                ),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_splunk_hec,
        ),
        ExporterSpec(
            type="coralogix",
            component="coralogix",
            label="Coralogix",
            description="Send telemetry to Coralogix.",
            category="Observability",
            doc_folder="coralogixexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec("private_key", "Private key", field_type="password", required=True),
                FieldSpec("domain", "Domain", default="coralogix.com"),
                FieldSpec("application_name", "Application", default="bigip-telemetry"),
                FieldSpec("subsystem_name", "Subsystem", default="exporter"),
            ),
            build=_build_coralogix,
        ),
        ExporterSpec(
            type="elasticsearch",
            component="elasticsearch",
            label="Elasticsearch",
            description="Export telemetry to Elasticsearch.",
            category="Storage",
            doc_folder="elasticsearchexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec(
                    "endpoints",
                    "Endpoints (comma-separated)",
                    default="http://localhost:9200",
                    required=True,
                ),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_elasticsearch,
        ),
        ExporterSpec(
            type="influxdb",
            component="influxdb",
            label="InfluxDB",
            description="Write metrics to InfluxDB v2.",
            category="Storage",
            doc_folder="influxdbexporter",
            fields=(
                FieldSpec("endpoint", "URL", default="http://localhost:8086"),
                FieldSpec("org", "Organization"),
                FieldSpec("bucket", "Bucket"),
                FieldSpec("token", "Token", field_type="password"),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_influxdb,
        ),
        ExporterSpec(
            type="kafka",
            component="kafka",
            label="Kafka",
            description="Export telemetry to a Kafka topic (OTLP encoding).",
            category="Messaging",
            doc_folder="kafkaexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec("brokers", "Brokers (comma-separated)", default="localhost:9092"),
                FieldSpec("topic", "Topic", default="otel-metrics"),
                FieldSpec(
                    "encoding",
                    "Encoding",
                    field_type="select",
                    default="otlp_proto",
                    options=("otlp_proto", "otlp_json"),
                ),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_kafka,
        ),
        ExporterSpec(
            type="googlecloud",
            component="googlecloud",
            label="Google Cloud Monitoring",
            description="Send metrics to Google Cloud.",
            category="Cloud",
            doc_folder="googlecloudexporter",
            fields=(FieldSpec("project", "GCP project ID", required=True),),
            build=_build_googlecloud,
        ),
        ExporterSpec(
            type="googlemanagedprometheus",
            component="googlemanagedprometheus",
            label="Google Managed Prometheus",
            description="Send metrics to Google Managed Prometheus.",
            category="Cloud",
            doc_folder="googlemanagedprometheusexporter",
            fields=(FieldSpec("project", "GCP project ID", required=True),),
            build=_build_googlemanagedprometheus,
        ),
        ExporterSpec(
            type="awss3",
            component="awss3",
            label="AWS S3",
            description="Archive metrics to Amazon S3.",
            category="Cloud",
            doc_folder="awss3exporter",
            fields=(
                FieldSpec("region", "AWS region", default="us-east-1"),
                FieldSpec("bucket", "S3 bucket", required=True),
                FieldSpec("prefix", "Key prefix", default="otel-metrics/"),
            ),
            build=_build_awss3,
        ),
        ExporterSpec(
            type="awsemf",
            component="awsemf",
            label="AWS CloudWatch EMF",
            description="Send metrics as Embedded Metric Format to CloudWatch.",
            category="Cloud",
            doc_folder="awsemfexporter",
            fields=(
                FieldSpec("region", "AWS region", default="us-east-1"),
                FieldSpec("namespace", "Namespace", default="BIGIP/Metrics"),
                FieldSpec("log_group_name", "Log group (optional)"),
            ),
            build=_build_awsemf,
        ),
        ExporterSpec(
            type="azuremonitor",
            component="azuremonitor",
            label="Azure Monitor",
            description="Send metrics to Azure Monitor.",
            category="Cloud",
            doc_folder="azuremonitorexporter",
            fields=(
                FieldSpec(
                    "instrumentation_key",
                    "Instrumentation key",
                    field_type="password",
                    required=True,
                ),
            ),
            build=_build_azuremonitor,
        ),
        ExporterSpec(
            type="opensearch",
            component="opensearch",
            label="OpenSearch",
            description="Export telemetry to OpenSearch.",
            category="Storage",
            doc_folder="opensearchexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec(
                    "endpoints",
                    "Endpoints (comma-separated)",
                    default="https://localhost:9200",
                ),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_opensearch,
        ),
        ExporterSpec(
            type="logzio",
            component="logzio",
            label="Logz.io",
            description="Send telemetry to Logz.io.",
            category="Observability",
            doc_folder="logzioexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec("token", "Token", field_type="password", required=True),
                FieldSpec(
                    "region",
                    "Region",
                    field_type="select",
                    default="us",
                    options=("us", "eu", "au", "ca", "uk"),
                ),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_logzio,
        ),
        ExporterSpec(
            type="sumologic",
            component="sumologic",
            label="Sumo Logic",
            description="Send telemetry to Sumo Logic via an HTTP Source URL.",
            category="Observability",
            doc_folder="sumologicexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec(
                    "endpoint",
                    "HTTP Source URL",
                    required=True,
                    placeholder="https://collectors.sumologic.com/receiver/v1/http/...",
                ),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_sumologic,
        ),
        ExporterSpec(
            type="mezmo",
            component="mezmo",
            label="Mezmo",
            description="Send telemetry to Mezmo (LogDNA).",
            category="Observability",
            doc_folder="mezmoexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec("ingest_key", "Ingest key", field_type="password", required=True),
                FieldSpec("ingest_url", "Ingest URL", default="https://api.mezmo.com/v1/ingest"),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_mezmo,
        ),
        ExporterSpec(
            type="pulsar",
            component="pulsar",
            label="Apache Pulsar",
            description="Export metrics to a Pulsar topic.",
            category="Messaging",
            doc_folder="pulsarexporter",
            fields=(
                FieldSpec("endpoint", "Broker URL", default="pulsar://localhost:6650"),
                FieldSpec(
                    "topic",
                    "Topic",
                    default="persistent://public/default/otel-metrics",
                ),
            ),
            build=_build_pulsar,
        ),
        ExporterSpec(
            type="rabbitmq",
            component="rabbitmq",
            label="RabbitMQ",
            description="Publish metrics to RabbitMQ.",
            category="Messaging",
            doc_folder="rabbitmqexporter",
            fields=(
                FieldSpec("endpoint", "AMQP URL", default="amqp://localhost:5672"),
                FieldSpec("routing_key", "Routing key", default="otel-metrics"),
            ),
            build=_build_rabbitmq,
        ),
        ExporterSpec(
            type="otelarrow",
            component="otelarrow",
            label="OTel Arrow",
            description="Export via OpenTelemetry Arrow (high-throughput OTLP).",
            category="Core",
            doc_folder="otelarrowexporter",
            fields=(
                FieldSpec("endpoint", "Endpoint", default="localhost:4317"),
                FieldSpec("insecure", "TLS insecure", field_type="bool", default=True),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_otelarrow,
        ),
        ExporterSpec(
            type="sematext",
            component="sematext",
            label="Sematext",
            description="Send metrics to Sematext.",
            category="Observability",
            doc_folder="sematextexporter",
            fields=(
                FieldSpec("token", "Token", field_type="password", required=True),
                FieldSpec(
                    "region",
                    "Region",
                    field_type="select",
                    default="US",
                    options=("US", "EU"),
                ),
            ),
            build=_build_sematext,
        ),
        ExporterSpec(
            type="syslog",
            component="syslog",
            label="Syslog",
            description="Export telemetry via syslog.",
            category="Messaging",
            doc_folder="syslogexporter",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec("endpoint", "Endpoint", default="udp://localhost:514"),
                FieldSpec(
                    "network",
                    "Network",
                    field_type="select",
                    default="udp",
                    options=("udp", "tcp"),
                ),
                _SKIP_VERIFY_FIELD,
            ),
            build=_build_syslog,
        ),
        ExporterSpec(
            type="clickhouse",
            component="clickhouse",
            label="ClickHouse",
            description="Write metrics to ClickHouse.",
            category="Storage",
            doc_folder="clickhouseexporter",
            fields=(
                FieldSpec("endpoint", "Endpoint", default="tcp://localhost:9000"),
                FieldSpec("database", "Database", default="default"),
                FieldSpec("table_name", "Table", default="otel_metrics"),
            ),
            build=_build_clickhouse,
        ),
        ExporterSpec(
            type="cassandra",
            component="cassandra",
            label="Cassandra",
            description="Write metrics to Cassandra.",
            category="Storage",
            doc_folder="cassandraexporter",
            fields=(
                FieldSpec("dsn", "DSN / hosts", default="127.0.0.1"),
                FieldSpec("keyspace", "Keyspace", default="otel"),
            ),
            build=_build_cassandra,
        ),
        ExporterSpec(
            type="logicmonitor",
            component="logicmonitor",
            label="LogicMonitor",
            description="Send metrics to LogicMonitor.",
            category="Observability",
            doc_folder="logicmonitorexporter",
            fields=(
                FieldSpec("api_token_access_id", "API token access ID", required=True),
                FieldSpec(
                    "api_token_access_key",
                    "API token access key",
                    field_type="password",
                    required=True,
                ),
            ),
            build=_build_logicmonitor,
        ),
        ExporterSpec(
            type="contrib",
            component="",
            label="Contrib exporter (custom YAML)",
            description=(
                "Any other exporter from opentelemetry-collector-contrib. "
                "Paste settings from upstream docs."
            ),
            category="Advanced",
            doc_folder="",
            signals=_SIGNAL_BOTH,
            fields=(
                FieldSpec(
                    "component",
                    "Collector component name",
                    required=True,
                    placeholder="e.g. loadbalancing, sentry, zipkin",
                ),
                FieldSpec(
                    "config_yaml",
                    "Exporter configuration (YAML mapping only)",
                    field_type="textarea",
                    placeholder="token: secret\nendpoint: https://...",
                ),
            ),
            build=_build_contrib_custom,
        ),
    )


_EXPORTER_SPECS: dict[str, ExporterSpec] = {s.type: s for s in _curated_specs()}
_EXPORTER_TYPES: dict[str, dict[str, str]] = {
    s.type: {"label": s.label, "description": s.description, "category": s.category}
    for s in _curated_specs()
}


def get_exporter_spec(exporter_type: str) -> ExporterSpec | None:
    return _EXPORTER_SPECS.get(exporter_type)


def list_exporter_catalog() -> list[dict[str, Any]]:
    return [s.to_catalog_entry() for s in _curated_specs()]


def list_contrib_components() -> list[dict[str, str]]:
    """All contrib exporter folders (for Advanced custom picker and docs)."""
    out: list[dict[str, str]] = []
    for folder in CONTRIB_EXPORTER_FOLDERS:
        component = folder_to_component(folder)
        out.append(
            {
                "component": component,
                "folder": folder,
                "doc_url": f"{CONTRIB_EXPORTERS_REPO}/{folder}",
            }
        )
    return sorted(out, key=lambda x: x["component"])


def resolve_exporter(
    exporter_type: str,
    params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """
    Return (collector_component_name, exporter_config_block).
    Raises ValueError for unknown types or invalid custom config.
    """
    spec = get_exporter_spec(exporter_type)
    if not spec:
        raise ValueError(f"Unsupported exporter type: {exporter_type}")

    if exporter_type == "contrib":
        component = str(_p(params, "component", "")).strip()
        if not component:
            raise ValueError("Collector component name is required for contrib exporter")
        if spec.build is None:
            raise ValueError("contrib exporter has no builder")
        return component, spec.build(params)

    if spec.build is None:
        raise ValueError(f"Exporter type {exporter_type} has no configuration builder")

    return spec.component, spec.build(params)


# Back-compat for app.py
EXPORTER_TYPES = _EXPORTER_TYPES
