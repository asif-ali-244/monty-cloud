"""Unit tests for the shared plumbing: validation, responses, middleware, config."""

import datetime as dt
import json

import pytest

from src.common import config, responses, validation
from src.common.errors import (
    AppError,
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from src.common.middleware import api_handler, caller_id, parse_json_body, path_param
from src.services import image_service
from src.services import metadata_repository as repo
from tests.conftest import GIF_BYTES, JPEG_BYTES, PNG_BYTES, WEBP_BYTES, FakeContext, api_event


class TestTimestamps:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("2024-05-01T00:00:00Z", "2024-05-01T00:00:00.000Z"),
            ("2024-05-01T00:00:00+00:00", "2024-05-01T00:00:00.000Z"),
            ("2024-05-01T05:30:00+05:30", "2024-05-01T00:00:00.000Z"),
            ("2024-05-01T00:00:00", "2024-05-01T00:00:00.000Z"),
        ],
    )
    def test_normalises_to_utc(self, given, expected):
        assert validation.parse_timestamp(given, "uploadedFrom") == expected

    def test_blank_is_no_filter(self):
        assert validation.parse_timestamp(None, "uploadedFrom") is None
        assert validation.parse_timestamp("", "uploadedFrom") is None

    def test_rejects_garbage(self):
        with pytest.raises(ValidationError):
            validation.parse_timestamp("not-a-date", "uploadedFrom")

    def test_canonical_form_sorts_chronologically(self):
        """The GSI sort key relies on lexicographic order matching time order."""
        moments = [
            dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc),
            dt.datetime(2024, 1, 10, tzinfo=dt.timezone.utc),
            dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2024, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc),
        ]
        rendered = [validation.to_iso(m) for m in moments]
        assert rendered == sorted(rendered)


class TestFilenames:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("photo.png", "photo.png"),
            ("../../../etc/passwd", "passwd"),
            ("dir/sub/photo.png", "photo.png"),
            (r"C:\Users\me\photo.png", "photo.png"),
            ("  spaced.png  ", "spaced.png"),
        ],
    )
    def test_reduces_to_a_bare_name(self, given, expected):
        assert validation.clean_filename(given) == expected

    @pytest.mark.parametrize("given", ["", "   ", "..", "/", "///"])
    def test_rejects_names_with_nothing_left(self, given):
        with pytest.raises(ValidationError):
            validation.clean_filename(given)

    def test_rejects_overlong_name(self):
        with pytest.raises(ValidationError):
            validation.clean_filename("a" * 300 + ".png")


class TestContentTypes:
    def test_ignores_charset_parameters(self):
        assert validation.validate_content_type("image/PNG; charset=binary") == "image/png"

    @pytest.mark.parametrize("given", ["text/html", "application/pdf", "image/svg+xml"])
    def test_rejects_unsupported(self, given):
        with pytest.raises(UnsupportedMediaTypeError):
            validation.validate_content_type(given)

    def test_every_allowed_type_has_an_extension(self):
        assert all(ext.startswith(".") for ext in config.ALLOWED_CONTENT_TYPES.values())


class TestTags:
    @pytest.mark.parametrize(
        "given,expected",
        [
            (None, []),
            ([], []),
            ("a,b,c", ["a", "b", "c"]),
            (["A", "a", " a "], ["a"]),
            (["keep-me", "keep_me2"], ["keep-me", "keep_me2"]),
            (["", "  ", "ok"], ["ok"]),
        ],
    )
    def test_normalises(self, given, expected):
        assert validation.normalise_tags(given) == expected

    @pytest.mark.parametrize(
        "given", [["-leading"], ["has space"], ["UPPER!"], [123], 42, ["x" * 51]]
    )
    def test_rejects_invalid(self, given):
        with pytest.raises(ValidationError):
            validation.normalise_tags(given)


class TestSizeBytes:
    @pytest.mark.parametrize("given", [1, 70, config.DEFAULT_MAX_IMAGE_BYTES])
    def test_accepts_positive_integers_up_to_the_limit(self, given):
        assert validation.parse_size_bytes(given) == given

    @pytest.mark.parametrize("given", ["70", 70.0, 70.5, True, False, [70], {"n": 70}])
    def test_rejects_anything_that_is_not_a_json_integer(self, given):
        """bool is an int subclass in Python; a JSON `true` must not pass as 1 byte."""
        with pytest.raises(ValidationError, match="must be an integer"):
            validation.parse_size_bytes(given)

    def test_missing_is_required_not_a_type_error(self):
        with pytest.raises(ValidationError, match="is required"):
            validation.parse_size_bytes(None)

    @pytest.mark.parametrize("given", [0, -1])
    def test_rejects_non_positive(self, given):
        with pytest.raises(ValidationError, match="at least 1"):
            validation.parse_size_bytes(given)

    def test_over_the_limit_is_413_not_400(self):
        with pytest.raises(PayloadTooLargeError):
            validation.parse_size_bytes(config.DEFAULT_MAX_IMAGE_BYTES + 1)


class TestSignatures:
    @pytest.mark.parametrize(
        "content_type,data",
        [
            ("image/png", PNG_BYTES),
            ("image/jpeg", JPEG_BYTES),
            ("image/gif", GIF_BYTES),
            ("image/gif", b"GIF87a" + b"\x00" * 10),
            ("image/webp", WEBP_BYTES),
        ],
    )
    def test_accepts_matching_magic_bytes(self, content_type, data):
        validation.verify_signature(data[: validation.SIGNATURE_PREFIX_BYTES], content_type)

    @pytest.mark.parametrize(
        "content_type,data",
        [
            ("image/png", JPEG_BYTES),
            ("image/jpeg", PNG_BYTES),
            ("image/gif", b"%PDF-1.7 not a gif"),
            ("image/webp", b"RIFF\x00\x00\x00\x00WAVEfmt "),
        ],
    )
    def test_rejects_mismatched_magic_bytes(self, content_type, data):
        with pytest.raises(ValidationError, match="does not match"):
            validation.verify_signature(data[: validation.SIGNATURE_PREFIX_BYTES], content_type)

    def test_prefix_is_long_enough_for_every_signature(self):
        """WEBP's marker sits at bytes 8-12, the deepest check."""
        assert validation.SIGNATURE_PREFIX_BYTES >= 12


class TestUserIds:
    @pytest.mark.parametrize(
        "given", ["alice", "user-42", "a.b_c@example.com", "0f8fad5b-d9cb-469f-a165-70867728950e"]
    )
    def test_accepts_usernames_emails_and_cognito_subs(self, given):
        assert validation.validate_user_id(given) == given

    @pytest.mark.parametrize(
        "given", ["", None, 42, "a/b", "../x", "with space", "tab\there", "x" * 129, ".hidden"]
    )
    def test_rejects_anything_unsafe_in_an_s3_key(self, given):
        with pytest.raises(ValidationError):
            validation.validate_user_id(given)


class TestUnknownFields:
    def test_allows_the_documented_fields(self):
        validation.reject_unknown_fields({"filename": "a", "tags": []}, ("filename", "tags"))

    def test_names_every_unknown_field(self):
        with pytest.raises(ValidationError, match="imageBase64, legacy"):
            validation.reject_unknown_fields(
                {"filename": "a", "legacy": 1, "imageBase64": "x"}, ("filename",)
            )


VALID_ID = "0123456789abcdef0123456789abcdef"


class TestUploadKeys:
    @pytest.mark.parametrize(
        "key,expected",
        [
            (f"images/alice/{VALID_ID}.png", VALID_ID),
            (f"images/a.b@x.com/{VALID_ID}.webp", VALID_ID),
            ("images/alice/0123456789ABCDEF0123456789ABCDEF.png", None),
            ("images/alice/short.png", None),
            ("uploads/alice/0123456789abcdef0123456789abcdef.png", None),
            ("images/", None),
        ],
    )
    def test_recovers_the_image_id_only_from_our_own_keys(self, key, expected):
        assert image_service.image_id_from_key(key) == expected


class TestPagination:
    def test_token_round_trips(self):
        key = {"imageId": "abc", "userId": "alice", "uploadedAt": "2024-01-01T00:00:00.000Z"}
        assert repo.decode_token(repo.encode_token(key)) == key

    def test_absent_key_yields_no_token(self):
        assert repo.encode_token(None) is None
        assert repo.decode_token(None) is None

    def test_token_is_url_safe(self):
        token = repo.encode_token({"imageId": "a" * 200})
        assert "+" not in token and "/" not in token

    def test_malformed_token_is_a_client_error(self):
        with pytest.raises(ValidationError):
            repo.decode_token("%%%not-base64%%%")


class TestResponses:
    def test_serialises_dynamodb_decimals(self):
        import decimal

        body = json.loads(
            responses.ok({"size": decimal.Decimal("42"), "ratio": decimal.Decimal("1.5")})[
                "body"
            ]
        )
        assert body == {"size": 42, "ratio": 1.5}

    def test_serialises_sets(self):
        body = json.loads(responses.ok({"tags": {"b", "a"}})["body"])
        assert body["tags"] == ["a", "b"]

    def test_no_content_has_an_empty_body(self):
        assert responses.no_content() == {
            "statusCode": 204,
            "headers": {"Content-Type": "application/json", **responses.CORS_HEADERS},
            "body": "",
        }

    def test_every_response_carries_cors_headers(self):
        for response in (
            responses.ok({}),
            responses.created({}),
            responses.no_content(),
            responses.redirect("https://example.test"),
            responses.error(400, "bad", "ValidationError"),
        ):
            assert response["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_error_shape_matches_the_documented_contract(self):
        body = json.loads(responses.error(404, "gone", "NotFound")["body"])
        assert body == {"error": "gone", "code": "NotFound"}


class TestMiddleware:
    def test_maps_app_errors_to_their_status(self):
        @api_handler
        def handler(event, context):
            raise NotFoundError("nope")

        response = handler({}, FakeContext())
        assert response["statusCode"] == 404
        assert json.loads(response["body"])["error"] == "nope"

    def test_default_message_comes_from_the_docstring(self):
        @api_handler
        def handler(event, context):
            raise NotFoundError()

        assert "does not exist" in json.loads(handler({}, FakeContext())["body"])["error"]

    def test_hides_internals_behind_a_traceable_reference(self):
        @api_handler
        def handler(event, context):
            raise KeyError("internal-table-name")

        response = handler({}, FakeContext())
        body = json.loads(response["body"])
        assert response["statusCode"] == 500
        assert "internal-table-name" not in body["error"]
        assert FakeContext.aws_request_id in body["error"]

    def test_survives_a_missing_lambda_context(self):
        @api_handler
        def handler(event, context):
            raise RuntimeError("boom")

        assert handler({}, None)["statusCode"] == 500

    def test_successful_handlers_pass_through_untouched(self):
        @api_handler
        def handler(event, context):
            return {"statusCode": 200, "body": "{}"}

        assert handler({}, FakeContext())["statusCode"] == 200


class TestEventParsing:
    def test_reads_headers_case_insensitively(self):
        event = api_event(headers={"x-USER-id": "user-bob"}, user_id=None)
        assert caller_id(event) == "user-bob"

    def test_identity_is_optional_when_asked(self):
        event = api_event(user_id=None)
        assert caller_id(event, required=False) is None

    def test_missing_path_parameters_map_to_400(self):
        with pytest.raises(ValidationError):
            path_param({"pathParameters": None}, "imageId")

    def test_body_must_be_a_json_object(self):
        with pytest.raises(ValidationError):
            parse_json_body({"body": '"a string"'})

    def test_undecodable_base64_body_is_a_400(self):
        with pytest.raises(ValidationError):
            parse_json_body({"body": "!!!", "isBase64Encoded": True})


class TestConfig:
    def test_missing_table_name_is_a_configuration_error(self, monkeypatch):
        monkeypatch.delenv("IMAGES_TABLE", raising=False)
        with pytest.raises(AppError):
            config.table_name()

    def test_missing_bucket_name_is_a_configuration_error(self, monkeypatch):
        monkeypatch.delenv("IMAGES_BUCKET", raising=False)
        with pytest.raises(AppError):
            config.bucket_name()

    def test_limits_are_overridable_by_environment(self, monkeypatch):
        monkeypatch.setenv("MAX_IMAGE_BYTES", "123")
        monkeypatch.setenv("DOWNLOAD_URL_TTL_SECONDS", "45")
        assert config.max_image_bytes() == 123
        assert config.url_ttl_seconds() == 45

    def test_clients_are_reused_across_warm_invocations(self, aws):
        assert config.s3_client() is config.s3_client()
        assert config.dynamodb_table() is config.dynamodb_table()

    def test_signing_client_is_shared_when_endpoints_match(self, aws):
        assert config.s3_signing_client() is config.s3_client()


class TestDefensiveGuards:
    """The remaining type guards; cheap to assert and easy to regress on."""

    def test_non_string_field_is_rejected(self):
        with pytest.raises(ValidationError):
            validation.require_string({"filename": 12345}, "filename")

    def test_non_string_timestamp_is_rejected(self):
        with pytest.raises(ValidationError):
            validation.parse_timestamp(12345, "uploadedFrom")

    def test_optional_identity_that_is_absent_returns_none(self):
        assert caller_id(api_event(user_id=None), required=False) is None

    def test_to_public_hides_expiry_and_storage_attributes(self):
        public = image_service.to_public(
            {"imageId": "x", "s3Key": "k", "s3Bucket": "b", "filenameLower": "f", "expiresAt": 1}
        )
        assert public == {"imageId": "x"}

    def test_encoder_still_raises_on_genuinely_unserialisable_values(self):
        with pytest.raises(TypeError):
            responses.ok({"handle": object()})

    def test_base64_body_that_is_not_utf8_is_a_400(self):
        import base64 as b64

        event = {"body": b64.b64encode(b"\xff\xfe\xff").decode(), "isBase64Encoded": True}
        with pytest.raises(ValidationError) as excinfo:
            parse_json_body(event)
        assert "UTF-8" in str(excinfo.value)
