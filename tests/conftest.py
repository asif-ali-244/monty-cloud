"""Shared fixtures. Every test runs against moto, so no AWS account is needed.

Moto signs presigned POST forms but does not enforce their policies, so unit
tests simulate the client's direct-to-S3 upload with a plain put_object and then
deliver the S3 event to the processor by hand. Policy enforcement itself is
exercised against LocalStack by scripts/smoke_test.py.
"""

import base64
import json
import urllib.parse

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "test-images"
BUCKET_NAME = "test-images-bucket"
USER_INDEX = "userId-uploadedAt-index"
REGION = "us-east-1"

# A real 1x1 PNG: the processor checks magic bytes, so placeholder data will not do.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
GIF_BYTES = b"GIF89a" + b"\x00" * 32
WEBP_BYTES = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 16
PDF_BYTES = b"%PDF-1.7 definitely not an image"


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
    for key in (
        "AWS_ENDPOINT_URL",
        "S3_PUBLIC_ENDPOINT",
        "MAX_IMAGE_BYTES",
        "UPLOAD_URL_TTL_SECONDS",
        "DOWNLOAD_URL_TTL_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
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


def s3_event(*keys, event_name="ObjectCreated:Post", bucket=BUCKET_NAME):
    """Build an S3 notification, URL-encoding keys the way S3 does."""
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": event_name,
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": urllib.parse.quote_plus(key, safe="/")},
                },
            }
            for key in keys
        ]
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
            "tags": ["beach", "sunset"],
            "description": "Golden hour",
        }
        payload.update(overrides)
        return payload

    return _build


def body_of(response):
    return json.loads(response["body"]) if response["body"] else None


def register(context, payload, user_id="user-alice"):
    """POST /images through the real handler; returns the {image, upload} body."""
    from src.handlers import upload_image

    response = upload_image.handler(api_event("POST", body=payload, user_id=user_id), context)
    assert response["statusCode"] == 201, response["body"]
    return body_of(response)


def client_upload(registered, data, content_type=None):
    """Do what the client does with the form: put the bytes at the signed key.

    Moto does not enforce POST policies, so the content type is taken from the
    signed fields unless a test deliberately overrides it to simulate an object
    that reached the key some other way.
    """
    fields = registered["upload"]["fields"]
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=BUCKET_NAME,
        Key=fields["key"],
        Body=data,
        ContentType=content_type or fields["Content-Type"],
    )
    return fields["key"]


def process(context, *keys):
    """Deliver S3 ObjectCreated events to the real processor handler."""
    from src.handlers import process_upload

    return process_upload.handler(s3_event(*keys), context)


def fetch_image(context, image_id):
    from src.handlers import get_image

    event = api_event("GET", path_parameters={"imageId": image_id})
    return body_of(get_image.handler(event, context))


def complete_upload(context, payload, data, user_id="user-alice"):
    """Register, upload and process: the whole happy path. Returns the final image."""
    registered = register(context, payload, user_id=user_id)
    key = client_upload(registered, data)
    process(context, key)
    return fetch_image(context, registered["image"]["imageId"])


@pytest.fixture
def pending_image(aws, context, upload_payload):
    """Registered, but the client has not uploaded yet."""
    return register(context, upload_payload())


@pytest.fixture
def stored_image(aws, context, upload_payload):
    """A fully uploaded, verified, ready image."""
    image = complete_upload(context, upload_payload(), PNG_BYTES)
    assert image["status"] == "ready", image
    return image


def s3_keys():
    return [
        obj["Key"]
        for obj in boto3.client("s3", region_name=REGION)
        .list_objects_v2(Bucket=BUCKET_NAME)
        .get("Contents", [])
    ]


def table_item(image_id):
    return (
        boto3.resource("dynamodb", region_name=REGION)
        .Table(TABLE_NAME)
        .get_item(Key={"imageId": image_id})
        .get("Item")
    )
