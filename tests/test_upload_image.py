"""POST /images"""

import base64
import json

import pytest

from src.common import config
from src.handlers import upload_image
from tests.conftest import (
    GIF_BASE64,
    JPEG_BASE64,
    PNG_BASE64,
    PNG_BYTES,
    api_event,
    body_of,
    s3_keys,
)


def test_uploads_image_and_returns_metadata(aws, context, upload_payload):
    response = upload_image.handler(api_event("POST", body=upload_payload()), context)

    assert response["statusCode"] == 201
    body = body_of(response)
    assert body["filename"] == "sunset.png"
    assert body["contentType"] == "image/png"
    assert body["sizeBytes"] == len(PNG_BYTES)
    assert body["userId"] == "user-alice"
    assert body["tags"] == ["beach", "sunset"]
    assert body["description"] == "Golden hour"
    assert body["uploadedAt"].endswith("Z")
    assert response["headers"]["Location"] == "/images/{}".format(body["imageId"])


def test_persists_object_and_row(aws, context, upload_payload):
    body = body_of(upload_image.handler(api_event("POST", body=upload_payload()), context))

    keys = s3_keys()
    assert keys == ["images/user-alice/{}.png".format(body["imageId"])]

    stored = aws.resource("dynamodb", region_name="us-east-1").Table(
        config.table_name()
    ).get_item(Key={"imageId": body["imageId"]})["Item"]
    assert stored["s3Key"] == keys[0]
    assert stored["filenameLower"] == "sunset.png"


def test_storage_internals_are_not_exposed(aws, context, upload_payload):
    body = body_of(upload_image.handler(api_event("POST", body=upload_payload()), context))
    for hidden in ("s3Key", "s3Bucket", "filenameLower"):
        assert hidden not in body


def test_checksum_matches_uploaded_bytes(aws, context, upload_payload):
    import hashlib

    body = body_of(upload_image.handler(api_event("POST", body=upload_payload()), context))
    assert body["checksumSha256"] == hashlib.sha256(PNG_BYTES).hexdigest()


def test_accepts_data_url_form(aws, context, upload_payload):
    payload = upload_payload(imageBase64="data:image/png;base64," + PNG_BASE64)
    response = upload_image.handler(api_event("POST", body=payload), context)
    assert response["statusCode"] == 201


def test_accepts_base64_encoded_request_body(aws, context, upload_payload):
    encoded = base64.b64encode(json.dumps(upload_payload()).encode()).decode()
    event = api_event("POST", body=encoded, is_base64=True)
    assert upload_image.handler(event, context)["statusCode"] == 201


@pytest.mark.parametrize(
    "content_type,image",
    [("image/jpeg", JPEG_BASE64), ("image/gif", GIF_BASE64), ("image/png", PNG_BASE64)],
)
def test_accepts_every_supported_type(aws, context, upload_payload, content_type, image):
    payload = upload_payload(contentType=content_type, imageBase64=image, filename="a.bin")
    assert upload_image.handler(api_event("POST", body=payload), context)["statusCode"] == 201


def test_strips_directory_traversal_from_filename(aws, context, upload_payload):
    payload = upload_payload(filename="../../etc/passwd.png")
    body = body_of(upload_image.handler(api_event("POST", body=payload), context))
    assert body["filename"] == "passwd.png"
    assert s3_keys() == ["images/user-alice/{}.png".format(body["imageId"])]


def test_tags_are_normalised_and_deduplicated(aws, context, upload_payload):
    payload = upload_payload(tags=["Beach", " beach ", "SUNSET"])
    body = body_of(upload_image.handler(api_event("POST", body=payload), context))
    assert body["tags"] == ["beach", "sunset"]


def test_missing_identity_is_rejected(aws, context, upload_payload):
    response = upload_image.handler(
        api_event("POST", body=upload_payload(), user_id=None), context
    )
    assert response["statusCode"] == 400
    assert "X-User-Id" in body_of(response)["error"]


def test_identity_prefers_authorizer_claims_over_header(aws, context, upload_payload):
    event = api_event("POST", body=upload_payload(), user_id="spoofed")
    event["requestContext"]["authorizer"] = {"claims": {"sub": "user-from-token"}}
    body = body_of(upload_image.handler(event, context))
    assert body["userId"] == "user-from-token"


@pytest.mark.parametrize(
    "overrides,expected_status,fragment",
    [
        ({"filename": None}, 400, "'filename' is required"),
        ({"filename": "   "}, 400, "'filename'"),
        ({"contentType": None}, 400, "'contentType' is required"),
        ({"contentType": "application/pdf"}, 415, "contentType"),
        ({"imageBase64": None}, 400, "'imageBase64' is required"),
        ({"imageBase64": "!!!not-base64!!!"}, 400, "valid base64"),
        ({"imageBase64": ""}, 400, "'imageBase64' is required"),
        ({"tags": "not,a,list,but,ok"}, 201, None),
        ({"tags": [1, 2]}, 400, "array of strings"),
        ({"tags": ["bad tag!"]}, 400, "may contain only"),
        ({"tags": ["x"] * 21}, 400, "at most 20"),
        ({"description": "x" * 2000}, 400, "at most 1024"),
    ],
)
def test_rejects_invalid_payloads(
    aws, context, upload_payload, overrides, expected_status, fragment
):
    response = upload_image.handler(
        api_event("POST", body=upload_payload(**overrides)), context
    )
    assert response["statusCode"] == expected_status, response["body"]
    if fragment:
        assert fragment in body_of(response)["error"]


def test_rejects_content_that_does_not_match_declared_type(aws, context, upload_payload):
    """A PDF renamed to .png must not get through: magic bytes are checked."""
    payload = upload_payload(
        imageBase64=base64.b64encode(b"%PDF-1.7 not really a png").decode()
    )
    response = upload_image.handler(api_event("POST", body=payload), context)
    assert response["statusCode"] == 400
    assert "does not match the declared contentType" in body_of(response)["error"]


def test_rejects_oversized_image(aws, context, upload_payload, monkeypatch):
    monkeypatch.setenv("MAX_IMAGE_BYTES", "10")
    response = upload_image.handler(api_event("POST", body=upload_payload()), context)
    assert response["statusCode"] == 413
    assert body_of(response)["code"] == "PayloadTooLarge"


@pytest.mark.parametrize("body", [None, "", "not json", "[1,2,3]"])
def test_rejects_malformed_body(aws, context, body):
    response = upload_image.handler(api_event("POST", body=body), context)
    assert response["statusCode"] == 400


def test_no_orphan_object_when_metadata_write_fails(
    aws, context, upload_payload, monkeypatch
):
    """If DynamoDB rejects the row the S3 object must be cleaned up."""
    from src.services import metadata_repository

    def explode(_item):
        raise RuntimeError("dynamodb is down")

    monkeypatch.setattr(metadata_repository, "put_new", explode)
    response = upload_image.handler(api_event("POST", body=upload_payload()), context)

    assert response["statusCode"] == 500
    assert s3_keys() == []


def test_unexpected_error_returns_opaque_500(aws, context, upload_payload, monkeypatch):
    from src.services import object_store

    def explode(*_args, **_kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(object_store, "put_object", explode)
    response = upload_image.handler(api_event("POST", body=upload_payload()), context)

    assert response["statusCode"] == 500
    body = body_of(response)
    assert "secret internal detail" not in body["error"]
    assert context.aws_request_id in body["error"]
