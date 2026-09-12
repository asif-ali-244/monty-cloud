"""Shared fixtures. Every test runs against moto, so no AWS account is needed."""

import base64

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "test-images"
BUCKET_NAME = "test-images-bucket"
USER_INDEX = "userId-uploadedAt-index"
REGION = "us-east-1"

# A real 1x1 PNG: the service checks magic bytes, so placeholder data will not do.
PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_BASE64)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
JPEG_BASE64 = base64.b64encode(JPEG_BYTES).decode()
GIF_BYTES = b"GIF89a" + b"\x00" * 32
GIF_BASE64 = base64.b64encode(GIF_BYTES).decode()


@pytest.fixture(autouse=True)
def aws_environment(monkeypatch):
    """Isolate every test from real credentials and from cached clients."""
    from src.common import config

    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
        "IMAGES_TABLE": TABLE_NAME,
        "IMAGES_BUCKET": BUCKET_NAME,
        "IMAGES_USER_INDEX": USER_INDEX,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("S3_PUBLIC_ENDPOINT", raising=False)
    monkeypatch.delenv("MAX_IMAGE_BYTES", raising=False)
    config.reset_clients()
    yield
    config.reset_clients()


@pytest.fixture
def aws(aws_environment):
    """Live moto-backed S3 bucket and DynamoDB table, shaped like Terraform's."""
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "imageId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "imageId", "AttributeType": "S"},
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "uploadedAt", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": USER_INDEX,
                    "KeySchema": [
                        {"AttributeName": "userId", "KeyType": "HASH"},
                        {"AttributeName": "uploadedAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET_NAME)
        yield boto3


def api_event(
    method="GET",
    path="/images",
    body=None,
    path_parameters=None,
    query=None,
    user_id="user-alice",
    headers=None,
    is_base64=False,
):
    """Build an API Gateway REST proxy event."""
    import json

    request_headers = {"Content-Type": "application/json"}
    if user_id:
        request_headers["X-User-Id"] = user_id
    request_headers.update(headers or {})
    return {
        "httpMethod": method,
        "path": path,
        "headers": request_headers,
        "pathParameters": path_parameters,
        "queryStringParameters": query,
        "requestContext": {"requestId": "test-request", "authorizer": None},
        "body": body if isinstance(body, str) or body is None else json.dumps(body),
        "isBase64Encoded": is_base64,
    }


class FakeContext:
    aws_request_id = "test-request-id"
    function_name = "test-function"
    memory_limit_in_mb = 512


@pytest.fixture
def context():
    return FakeContext()


@pytest.fixture
def upload_payload():
    def _build(**overrides):
        payload = {
            "filename": "sunset.png",
            "contentType": "image/png",
            "imageBase64": PNG_BASE64,
            "tags": ["beach", "sunset"],
            "description": "Golden hour",
        }
        payload.update(overrides)
        return payload

    return _build


@pytest.fixture
def stored_image(aws, context, upload_payload):
    """Upload one image through the real handler and return its metadata."""
    import json

    from src.handlers import upload_image

    response = upload_image.handler(
        api_event("POST", "/images", body=upload_payload()), context
    )
    assert response["statusCode"] == 201, response["body"]
    return json.loads(response["body"])


def body_of(response):
    import json

    return json.loads(response["body"]) if response["body"] else None


def s3_keys():
    return [
        obj["Key"]
        for obj in boto3.client("s3", region_name=REGION)
        .list_objects_v2(Bucket=BUCKET_NAME)
        .get("Contents", [])
    ]
