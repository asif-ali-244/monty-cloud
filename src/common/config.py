"""Runtime configuration and cached AWS clients.

Config is read lazily on first access rather than at import time so that tests
can set the environment per-case, and clients are memoised at module level so
warm Lambda invocations reuse the underlying HTTPS connections.
"""

import os

import boto3
from botocore.config import Config as BotoConfig

DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
DEFAULT_URL_TTL_SECONDS = 900

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

_CLIENTS = {}

# Retries are bounded and adaptive: under concurrent load we would rather shed a
# request than let a Lambda sit burning its timeout inside botocore.
_BOTO_CONFIG = BotoConfig(
    retries={"max_attempts": 3, "mode": "adaptive"},
    connect_timeout=2,
    read_timeout=5,
)

# S3 must sign with SigV4: it is the only version accepted in regions launched
# after 2014, and it is what makes the response-header overrides on a presigned
# download URL tamper-proof.
_S3_CONFIG = _BOTO_CONFIG.merge(BotoConfig(signature_version="s3v4"))


def _env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def table_name():
    from src.common.errors import AppError

    name = _env("IMAGES_TABLE")
    if not name:
        raise AppError("IMAGES_TABLE is not configured")
    return name


def bucket_name():
    from src.common.errors import AppError

    name = _env("IMAGES_BUCKET")
    if not name:
        raise AppError("IMAGES_BUCKET is not configured")
    return name


def user_index_name():
    return _env("IMAGES_USER_INDEX", "userId-uploadedAt-index")


def max_image_bytes():
    return int(_env("MAX_IMAGE_BYTES", DEFAULT_MAX_IMAGE_BYTES))


def url_ttl_seconds():
    return int(_env("DOWNLOAD_URL_TTL_SECONDS", DEFAULT_URL_TTL_SECONDS))


def endpoint_url():
    """LocalStack endpoint; unset (None) means real AWS."""
    return _env("AWS_ENDPOINT_URL")


def s3_public_endpoint():
    """Endpoint used to sign download URLs when it differs from the internal one.

    Inside LocalStack the Lambda container reaches S3 on a hostname the caller's
    browser cannot resolve, so the signing endpoint has to be overridable.
    """
    return _env("S3_PUBLIC_ENDPOINT") or endpoint_url()


def dynamodb_table():
    if "table" not in _CLIENTS:
        resource = boto3.resource(
            "dynamodb", endpoint_url=endpoint_url(), config=_BOTO_CONFIG
        )
        _CLIENTS["table"] = resource.Table(table_name())
    return _CLIENTS["table"]


def s3_client():
    if "s3" not in _CLIENTS:
        _CLIENTS["s3"] = boto3.client(
            "s3", endpoint_url=endpoint_url(), config=_S3_CONFIG
        )
    return _CLIENTS["s3"]


def s3_signing_client():
    """Separate client bound to the publicly reachable endpoint for presigning."""
    if "s3_signing" not in _CLIENTS:
        public = s3_public_endpoint()
        if public == endpoint_url():
            _CLIENTS["s3_signing"] = s3_client()
        else:
            _CLIENTS["s3_signing"] = boto3.client(
                "s3", endpoint_url=public, config=_S3_CONFIG
            )
    return _CLIENTS["s3_signing"]


def reset_clients():
    """Drop memoised clients. Used by tests between cases."""
    _CLIENTS.clear()
