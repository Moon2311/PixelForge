from django.conf import settings
from elasticsearch import Elasticsearch


_es_client = None


def get_elasticsearch_client() -> Elasticsearch:
    global _es_client
    if _es_client is not None:
        return _es_client

    hosts = getattr(settings, "ELASTICSEARCH_HOSTS", ["http://localhost:9200"])
    kwargs = {
        "hosts": hosts,
        "timeout": getattr(settings, "ELASTICSEARCH_TIMEOUT", 30),
    }

    # ponytail: skip cert verification for self-signed/local dev certs
    if hosts and hosts[0].startswith("https://"):
        kwargs["verify_certs"] = False
        kwargs["ssl_show_warn"] = False

    if hasattr(settings, "ELASTICSEARCH_USER") and hasattr(settings, "ELASTICSEARCH_PASSWORD"):
        kwargs["basic_auth"] = (settings.ELASTICSEARCH_USER, settings.ELASTICSEARCH_PASSWORD)

    _es_client = Elasticsearch(**kwargs)
    return _es_client


def close_elasticsearch_client() -> None:
    global _es_client
    if _es_client is not None:
        _es_client.close()
        _es_client = None