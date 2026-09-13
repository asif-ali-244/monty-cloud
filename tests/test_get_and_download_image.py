"""GET /images/{imageId} and GET /images/{imageId}/content"""

import urllib.parse

import pytest

from src.handlers import download_image, get_image
from tests.conftest import PNG_BYTES, api_event, body_of


def path(image_id):
    return {"imageId": image_id}


def test_returns_metadata_for_existing_image(aws, context, stored_image):
    response = get_image.handler(
        api_event("GET", path_parameters=path(stored_image["imageId"])), context
    )
    assert response["statusCode"] == 200
    body = body_of(response)
    assert body["imageId"] == stored_image["imageId"]
    assert body["filename"] == "sunset.png"
    assert body["sizeBytes"] == len(PNG_BYTES)


def test_metadata_omits_storage_internals(aws, context, stored_image):
    body = body_of(
        get_image.handler(
            api_event("GET", path_parameters=path(stored_image["imageId"])), context
        )
    )
    assert "s3Key" not in body and "s3Bucket" not in body


def test_missing_image_returns_404(aws, context):
    response = get_image.handler(api_event("GET", path_parameters=path("nope")), context)
    assert response["statusCode"] == 404
    assert body_of(response)["code"] == "NotFound"


def test_missing_path_parameter_returns_400(aws, context):
    response = get_image.handler(api_event("GET", path_parameters=None), context)
    assert response["statusCode"] == 400


def test_download_redirects_to_presigned_url(aws, context, stored_image):
    response = download_image.handler(
        api_event("GET", path_parameters=path(stored_image["imageId"])), context
    )
    assert response["statusCode"] == 302
    location = response["headers"]["Location"]
    assert stored_image["imageId"] in location
    assert "X-Amz-Signature" in location
    assert response["headers"]["Cache-Control"] == "no-store"


def test_presigned_url_serves_the_original_bytes(aws, context, stored_image):
    """Follow the signed URL against moto and confirm the payload round-trips."""
    import boto3

    response = download_image.handler(
        api_event("GET", path_parameters=path(stored_image["imageId"])), context
    )
    key = urllib.parse.urlparse(response["headers"]["Location"]).path.lstrip("/")
    fetched = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket="test-images-bucket", Key=key
    )
    assert fetched["Body"].read() == PNG_BYTES


def test_inline_disposition_is_the_default(aws, context, stored_image):
    response = download_image.handler(
        api_event("GET", path_parameters=path(stored_image["imageId"])), context
    )
    location = urllib.parse.unquote_plus(response["headers"]["Location"])
    assert 'inline; filename="sunset.png"' in location


def test_attachment_disposition_is_honoured(aws, context, stored_image):
    response = download_image.handler(
        api_event(
            "GET",
            path_parameters=path(stored_image["imageId"]),
            query={"disposition": "attachment"},
        ),
        context,
    )
    location = urllib.parse.unquote_plus(response["headers"]["Location"])
    assert 'attachment; filename="sunset.png"' in location


def test_json_mode_returns_the_url_instead_of_redirecting(aws, context, stored_image):
    response = download_image.handler(
        api_event(
            "GET",
            path_parameters=path(stored_image["imageId"]),
            query={"redirect": "false"},
        ),
        context,
    )
    assert response["statusCode"] == 200
    body = body_of(response)
    assert body["downloadUrl"].startswith("https://")
    assert body["expiresInSeconds"] == 900
    assert body["image"]["imageId"] == stored_image["imageId"]


def test_custom_expiry_is_applied(aws, context, stored_image):
    response = download_image.handler(
        api_event(
            "GET",
            path_parameters=path(stored_image["imageId"]),
            query={"redirect": "false", "expiresIn": "60"},
        ),
        context,
    )
    body = body_of(response)
    assert body["expiresInSeconds"] == 60
    assert "X-Amz-Expires=60" in body["downloadUrl"]


def test_expiry_is_capped(aws, context, stored_image):
    response = download_image.handler(
        api_event(
            "GET",
            path_parameters=path(stored_image["imageId"]),
            query={"redirect": "false", "expiresIn": "999999"},
        ),
        context,
    )
    assert body_of(response)["expiresInSeconds"] == download_image.MAX_URL_TTL_SECONDS


@pytest.mark.parametrize("value", ["0", "abc"])
def test_rejects_invalid_expiry(aws, context, stored_image, value):
    response = download_image.handler(
        api_event(
            "GET", path_parameters=path(stored_image["imageId"]), query={"expiresIn": value}
        ),
        context,
    )
    assert response["statusCode"] == 400


def test_download_of_missing_image_returns_404(aws, context):
    response = download_image.handler(
        api_event("GET", path_parameters=path("does-not-exist")), context
    )
    assert response["statusCode"] == 404


def test_signing_uses_the_public_endpoint_when_set(aws, context, stored_image, monkeypatch):
    """LocalStack's internal hostname is unreachable from the caller's browser."""
    from src.common import config

    monkeypatch.setenv("S3_PUBLIC_ENDPOINT", "http://localhost:4566")
    config.reset_clients()

    response = download_image.handler(
        api_event(
            "GET", path_parameters=path(stored_image["imageId"]), query={"redirect": "false"}
        ),
        context,
    )
    assert body_of(response)["downloadUrl"].startswith("http://localhost:4566")


def test_pending_image_metadata_is_readable_for_polling(aws, context, pending_image):
    image_id = pending_image["image"]["imageId"]
    response = get_image.handler(api_event("GET", path_parameters=path(image_id)), context)
    assert response["statusCode"] == 200
    body = body_of(response)
    assert body["status"] == "pending"
    assert "expiresAt" not in body


def test_downloading_a_pending_image_is_409(aws, context, pending_image):
    response = download_image.handler(
        api_event("GET", path_parameters=path(pending_image["image"]["imageId"])), context
    )
    assert response["statusCode"] == 409
    body = body_of(response)
    assert body["code"] == "ImageNotReady"
    assert "has not finished uploading" in body["error"]


def test_downloading_a_rejected_image_is_409_with_the_reason(aws, context, upload_payload):
    from tests.conftest import PDF_BYTES, client_upload, process, register

    registered = register(context, upload_payload(sizeBytes=len(PDF_BYTES)))
    process(context, client_upload(registered, PDF_BYTES))

    response = download_image.handler(
        api_event("GET", path_parameters=path(registered["image"]["imageId"])), context
    )
    assert response["statusCode"] == 409
    assert "was rejected: File content does not match" in body_of(response)["error"]


def test_ready_image_metadata_carries_verified_fields(aws, context, stored_image):
    for field in ("uploadedAt", "checksumSha256", "sizeBytes", "createdAt"):
        assert field in stored_image
    assert stored_image["status"] == "ready"
