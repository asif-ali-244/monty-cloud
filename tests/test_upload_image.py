"""POST /images - register metadata and receive a presigned upload form."""

import base64
import datetime as dt
import json

import pytest

from src.common import config
from src.handlers import upload_image
from tests.conftest import (
    api_event,
    body_of,
    s3_keys,
    table_item,
)


def post(context, payload, **kwargs):
    return upload_image.handler(api_event("POST", body=payload, **kwargs), context)


def policy_of(body):
    return json.loads(base64.b64decode(body["upload"]["fields"]["policy"]))


class TestRegistration:
    def test_returns_a_pending_image_and_an_upload_form(self, aws, context, upload_payload):
        response = post(context, upload_payload())

        assert response["statusCode"] == 201
        body = body_of(response)
        assert set(body) == {"image", "upload"}
        image = body["image"]
        assert image["status"] == "pending"
        assert image["filename"] == "sunset.png"
        assert image["contentType"] == "image/png"
        assert image["userId"] == "user-alice"
        assert image["tags"] == ["beach", "sunset"]
        assert image["description"] == "Golden hour"
        assert image["createdAt"].endswith("Z")
        assert response["headers"]["Location"] == f"/images/{image['imageId']}"

    def test_verified_fields_are_absent_until_the_upload_is_processed(
        self, aws, context, upload_payload
    ):
        image = body_of(post(context, upload_payload()))["image"]
        for field in ("sizeBytes", "uploadedAt", "checksumSha256", "rejectionReason"):
            assert field not in image

    def test_upload_form_targets_the_server_chosen_key(self, aws, context, upload_payload):
        body = body_of(post(context, upload_payload()))
        upload = body["upload"]

        assert upload["method"] == "POST"
        assert upload["fileField"] == "file"
        assert "test-images-bucket" in upload["url"]
        assert upload["fields"]["key"] == f"images/user-alice/{body['image']['imageId']}.png"
        assert upload["fields"]["Content-Type"] == "image/png"
        assert {"policy", "x-amz-signature", "x-amz-algorithm"} <= set(upload["fields"])
        assert upload["maxSizeBytes"] == config.DEFAULT_MAX_IMAGE_BYTES

    def test_no_bytes_are_stored_at_registration(self, aws, context, upload_payload):
        post(context, upload_payload())
        assert s3_keys() == []

    def test_row_is_pending_with_a_ttl_that_outlives_the_form(self, aws, context, upload_payload):
        before = int(dt.datetime.now(dt.timezone.utc).timestamp())
        image = body_of(post(context, upload_payload()))["image"]

        row = table_item(image["imageId"])
        assert row["status"] == "pending"
        expected = before + config.DEFAULT_UPLOAD_URL_TTL_SECONDS + config.PENDING_GRACE_SECONDS
        assert expected <= int(row["expiresAt"]) <= expected + 5

    def test_pending_rows_stay_out_of_the_user_index(self, aws, context, upload_payload):
        """The GSI is sparse on uploadedAt, so a pending row must not carry it."""
        image = body_of(post(context, upload_payload()))["image"]
        assert "uploadedAt" not in table_item(image["imageId"])

    def test_storage_internals_are_not_exposed(self, aws, context, upload_payload):
        image = body_of(post(context, upload_payload()))["image"]
        for hidden in ("s3Key", "s3Bucket", "filenameLower", "expiresAt"):
            assert hidden not in image

    def test_upload_url_ttl_is_configurable(self, aws, context, upload_payload, monkeypatch):
        monkeypatch.setenv("UPLOAD_URL_TTL_SECONDS", "120")
        upload = body_of(post(context, upload_payload()))["upload"]
        assert upload["expiresInSeconds"] == 120

    def test_accepts_a_base64_encoded_request_body(self, aws, context, upload_payload):
        encoded = base64.b64encode(json.dumps(upload_payload()).encode()).decode()
        response = upload_image.handler(
            api_event("POST", body=encoded, is_base64=True), context
        )
        assert response["statusCode"] == 201

    def test_consecutive_registrations_get_distinct_ids_and_keys(
        self, aws, context, upload_payload
    ):
        first = body_of(post(context, upload_payload()))
        second = body_of(post(context, upload_payload()))
        assert first["image"]["imageId"] != second["image"]["imageId"]
        assert first["upload"]["fields"]["key"] != second["upload"]["fields"]["key"]


class TestUploadPolicy:
    """The policy is what S3 enforces, so its conditions are the real contract."""

    def test_limits_the_body_to_between_one_byte_and_the_size_limit(
        self, aws, context, upload_payload
    ):
        policy = policy_of(body_of(post(context, upload_payload())))
        assert ["content-length-range", 1, config.DEFAULT_MAX_IMAGE_BYTES] in policy["conditions"]

    def test_size_limit_is_configurable(self, aws, context, upload_payload, monkeypatch):
        monkeypatch.setenv("MAX_IMAGE_BYTES", "1024")
        body = body_of(post(context, upload_payload()))
        assert ["content-length-range", 1, 1024] in policy_of(body)["conditions"]
        assert body["upload"]["maxSizeBytes"] == 1024

    def test_pins_the_content_type(self, aws, context, upload_payload):
        policy = policy_of(body_of(post(context, upload_payload())))
        assert {"Content-Type": "image/png"} in policy["conditions"]

    def test_pins_the_bucket_and_key(self, aws, context, upload_payload):
        body = body_of(post(context, upload_payload()))
        conditions = policy_of(body)["conditions"]
        assert {"bucket": "test-images-bucket"} in conditions
        assert {"key": body["upload"]["fields"]["key"]} in conditions

    def test_expires_with_the_form(self, aws, context, upload_payload):
        body = body_of(post(context, upload_payload()))
        expiration = dt.datetime.strptime(
            policy_of(body)["expiration"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=dt.timezone.utc)
        advertised = dt.datetime.fromisoformat(body["upload"]["expiresAt"].replace("Z", "+00:00"))
        assert abs((expiration - advertised).total_seconds()) < 2


@pytest.mark.parametrize(
    "content_type,extension",
    [
        ("image/jpeg", ".jpg"),
        ("image/png", ".png"),
        ("image/gif", ".gif"),
        ("image/webp", ".webp"),
    ],
)
def test_key_extension_follows_the_content_type(
    aws, context, upload_payload, content_type, extension
):
    body = body_of(post(context, upload_payload(contentType=content_type, filename="a.bin")))
    assert body["upload"]["fields"]["key"].endswith(extension)


def test_strips_directory_traversal_from_filename(aws, context, upload_payload):
    body = body_of(post(context, upload_payload(filename="../../etc/passwd.png")))
    assert body["image"]["filename"] == "passwd.png"
    assert body["upload"]["fields"]["key"].startswith("images/user-alice/")


def test_tags_are_normalised_and_deduplicated(aws, context, upload_payload):
    body = body_of(post(context, upload_payload(tags=["Beach", " beach ", "SUNSET"])))
    assert body["image"]["tags"] == ["beach", "sunset"]


class TestIdentity:
    def test_missing_identity_is_rejected(self, aws, context, upload_payload):
        response = post(context, upload_payload(), user_id=None)
        assert response["statusCode"] == 400
        assert "X-User-Id" in body_of(response)["error"]

    def test_prefers_authorizer_claims_over_the_header(self, aws, context, upload_payload):
        event = api_event("POST", body=upload_payload(), user_id="spoofed")
        event["requestContext"]["authorizer"] = {"claims": {"sub": "user-from-token"}}
        body = body_of(upload_image.handler(event, context))
        assert body["image"]["userId"] == "user-from-token"

    @pytest.mark.parametrize(
        "user_id",
        ["../escape", "a/b", "has space", "x" * 129, "-leading-dash", "émile"],
    )
    def test_rejects_ids_that_cannot_be_an_s3_key_segment(
        self, aws, context, upload_payload, user_id
    ):
        """The id becomes part of the object key, so '/' must never get through."""
        response = post(context, upload_payload(), user_id=user_id)
        assert response["statusCode"] == 400
        assert s3_keys() == []

    @pytest.mark.parametrize("user_id", ["alice", "user-42", "a.b_c@example.com", "0a1b"])
    def test_accepts_ordinary_ids(self, aws, context, upload_payload, user_id):
        assert post(context, upload_payload(), user_id=user_id)["statusCode"] == 201


@pytest.mark.parametrize(
    "overrides,expected_status,fragment",
    [
        ({"filename": None}, 400, "'filename' is required"),
        ({"filename": "   "}, 400, "'filename'"),
        ({"filename": 42}, 400, "'filename' must be a string"),
        ({"contentType": None}, 400, "'contentType' is required"),
        ({"contentType": "application/pdf"}, 415, "contentType"),
        ({"tags": "not,a,list,but,ok"}, 201, None),
        ({"tags": [1, 2]}, 400, "array of strings"),
        ({"tags": ["bad tag!"]}, 400, "may contain only"),
        ({"tags": ["x"] * 21}, 400, "at most 20"),
        ({"description": "x" * 2000}, 400, "at most 1024"),
        ({"imageBase64": "iVBORw0KGgo="}, 400, "Unknown field(s): imageBase64"),
    ],
)
def test_rejects_invalid_payloads(
    aws, context, upload_payload, overrides, expected_status, fragment
):
    payload = upload_payload()
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    response = post(context, payload)
    assert response["statusCode"] == expected_status, response["body"]
    if fragment:
        assert fragment in body_of(response)["error"]



@pytest.mark.parametrize("body", [None, "", "not json", "[1,2,3]"])
def test_rejects_malformed_body(aws, context, body):
    assert upload_image.handler(api_event("POST", body=body), context)["statusCode"] == 400


def test_metadata_write_failure_returns_502_and_no_form(aws, context, upload_payload, monkeypatch):
    from src.common.errors import StorageError
    from src.services import metadata_repository

    def explode(_item):
        raise StorageError("Could not persist image metadata")

    monkeypatch.setattr(metadata_repository, "put_new", explode)
    response = post(context, upload_payload())

    assert response["statusCode"] == 502
    assert "upload" not in body_of(response)


def test_unexpected_error_returns_opaque_500(aws, context, upload_payload, monkeypatch):
    from src.services import object_store

    def explode(*_args, **_kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(object_store, "presigned_post", explode)
    response = post(context, upload_payload())

    assert response["statusCode"] == 500
    body = body_of(response)
    assert "secret internal detail" not in body["error"]
    assert context.aws_request_id in body["error"]
