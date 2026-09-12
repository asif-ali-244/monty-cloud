"""The OpenAPI document is a contract, so it is tested like one.

Hand-written API docs rot the moment a limit or an enum changes in code. These
tests fail when docs/openapi.yaml and the implementation disagree: every real
handler response is validated against its documented schema, and every documented
constant is checked against the constant the code actually enforces.
"""

import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate as validate_spec
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from src.common import config, validation
from src.handlers import download_image, get_image, list_images, upload_image
from tests.conftest import api_event, body_of

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"


@pytest.fixture(scope="module")
def spec():
    return yaml.safe_load(SPEC_PATH.read_text())


@pytest.fixture(scope="module")
def schema_validator(spec):
    """Validate instances against the spec's components, honouring OpenAPI nullable."""
    document = _jsonschema_flavoured(copy.deepcopy(spec))
    base = "urn:image-service-openapi"
    registry = Registry().with_resource(
        base, Resource.from_contents(document, default_specification=DRAFT202012)
    )

    def _validate(schema_name, instance):
        Draft202012Validator(
            {"$ref": f"{base}#/components/schemas/{schema_name}"},
            registry=registry,
        ).validate(instance)

    return _validate


def _jsonschema_flavoured(node):
    """OpenAPI 3.0's `nullable: true` has no JSON Schema equivalent; translate it."""
    if isinstance(node, dict):
        node = {key: _jsonschema_flavoured(value) for key, value in node.items()}
        if node.pop("nullable", False) and "type" in node:
            node["type"] = [node["type"], "null"]
        return node
    if isinstance(node, list):
        return [_jsonschema_flavoured(item) for item in node]
    return node


class TestSpecItself:
    def test_is_a_valid_openapi_document(self, spec):
        validate_spec(spec)

    def test_documents_every_endpoint_the_service_exposes(self, spec):
        documented = {
            (method.upper(), path)
            for path, item in spec["paths"].items()
            for method in item
            if method in ("get", "post", "put", "patch", "delete")
        }
        assert documented == {
            ("POST", "/images"),
            ("GET", "/images"),
            ("GET", "/images/{imageId}"),
            ("DELETE", "/images/{imageId}"),
            ("GET", "/images/{imageId}/content"),
        }

    def test_every_example_matches_its_own_schema(self, spec, schema_validator):
        """A wrong example is worse than no example."""
        for name, schema in spec["components"]["schemas"].items():
            if "example" in schema:
                schema_validator(name, schema["example"])

        upload = spec["paths"]["/images"]["post"]
        for example in upload["requestBody"]["content"]["application/json"]["examples"].values():
            schema_validator("UploadRequest", example["value"])

        created = upload["responses"]["201"]["content"]["application/json"]["example"]
        schema_validator("Image", created)

        listing = spec["paths"]["/images"]["get"]["responses"]["200"]
        for example in listing["content"]["application/json"]["examples"].values():
            schema_validator("ImagePage", example["value"])

    def test_every_error_response_example_matches_the_error_schema(self, spec, schema_validator):
        for response in spec["components"]["responses"].values():
            content = response.get("content", {}).get("application/json", {})
            if "example" in content:
                schema_validator("Error", content["example"])
            for example in content.get("examples", {}).values():
                schema_validator("Error", example["value"])


class TestDocumentedConstantsMatchTheCode:
    def test_content_type_enum(self, spec):
        documented = set(spec["components"]["schemas"]["ContentType"]["enum"])
        assert documented == set(config.ALLOWED_CONTENT_TYPES)

    def test_error_code_enum(self, spec):
        from src.common import errors

        documented = set(spec["components"]["schemas"]["Error"]["properties"]["code"]["enum"])
        implemented = {
            cls.code
            for cls in vars(errors).values()
            if isinstance(cls, type) and issubclass(cls, errors.AppError)
        }
        assert documented == implemented

    def test_page_size_bounds(self, spec):
        limit = spec["components"]["parameters"]["Limit"]["schema"]
        assert limit["maximum"] == config.MAX_PAGE_SIZE
        assert limit["default"] == config.DEFAULT_PAGE_SIZE

    def test_presigned_url_ttl_bounds(self, spec):
        expires = spec["components"]["parameters"]["ExpiresIn"]["schema"]
        assert expires["maximum"] == download_image.MAX_URL_TTL_SECONDS
        assert expires["default"] == config.DEFAULT_URL_TTL_SECONDS

    def test_upload_field_limits(self, spec):
        upload = spec["components"]["schemas"]["UploadRequest"]["properties"]
        assert upload["filename"]["maxLength"] == validation.MAX_FILENAME_LENGTH
        assert upload["description"]["maxLength"] == validation.MAX_DESCRIPTION_LENGTH
        assert upload["tags"]["maxItems"] == validation.MAX_TAGS
        assert upload["tags"]["items"]["maxLength"] == validation.MAX_TAG_LENGTH
        assert upload["tags"]["items"]["pattern"] == validation._TAG_RE.pattern

    def test_documented_max_image_size_matches_the_default(self, spec):
        assert config.DEFAULT_MAX_IMAGE_BYTES == 5 * 1024 * 1024
        assert "5 MB" in spec["info"]["description"]


class TestRealResponsesMatchTheirDocumentedSchema:
    def test_upload_201(self, aws, context, upload_payload, schema_validator):
        response = upload_image.handler(api_event("POST", body=upload_payload()), context)
        assert response["statusCode"] == 201
        schema_validator("Image", body_of(response))

    def test_upload_without_optional_fields(self, aws, context, upload_payload, schema_validator):
        payload = upload_payload()
        del payload["tags"], payload["description"]
        response = upload_image.handler(api_event("POST", body=payload), context)
        schema_validator("Image", body_of(response))

    def test_list_200(self, aws, context, stored_image, schema_validator):
        response = list_images.handler(api_event("GET"), context)
        schema_validator("ImagePage", body_of(response))

    def test_list_200_when_empty(self, aws, context, schema_validator):
        """nextToken is null on the last page - the schema has to permit that."""
        body = body_of(list_images.handler(api_event("GET"), context))
        assert body["nextToken"] is None
        schema_validator("ImagePage", body)

    def test_list_200_with_a_cursor(self, aws, context, upload_payload, schema_validator):
        for index in range(3):
            upload_image.handler(
                api_event("POST", body=upload_payload(filename=f"f{index}.png")), context
            )
        body = body_of(
            list_images.handler(
                api_event("GET", query={"userId": "user-alice", "limit": "1"}), context
            )
        )
        assert body["nextToken"] is not None
        schema_validator("ImagePage", body)

    def test_get_200(self, aws, context, stored_image, schema_validator):
        response = get_image.handler(
            api_event("GET", path_parameters={"imageId": stored_image["imageId"]}), context
        )
        schema_validator("Image", body_of(response))

    def test_download_200(self, aws, context, stored_image, schema_validator):
        response = download_image.handler(
            api_event(
                "GET",
                path_parameters={"imageId": stored_image["imageId"]},
                query={"redirect": "false"},
            ),
            context,
        )
        schema_validator("DownloadUrl", body_of(response))

    @pytest.mark.parametrize(
        "handler_call,expected_status",
        [
            ("missing_identity", 400),
            ("not_found", 404),
            ("unsupported_type", 415),
        ],
    )
    def test_error_responses(
        self, aws, context, upload_payload, schema_validator, handler_call, expected_status
    ):
        if handler_call == "missing_identity":
            response = upload_image.handler(
                api_event("POST", body=upload_payload(), user_id=None), context
            )
        elif handler_call == "not_found":
            response = get_image.handler(
                api_event("GET", path_parameters={"imageId": "missing"}), context
            )
        else:
            response = upload_image.handler(
                api_event("POST", body=upload_payload(contentType="application/pdf")), context
            )

        assert response["statusCode"] == expected_status
        schema_validator("Error", body_of(response))


class TestProxyResponseShape:
    """Every handler must return the API Gateway proxy contract, not just a body."""

    @pytest.mark.parametrize("status,builder", [(204, "delete"), (302, "download")])
    def test_bodyless_responses(self, aws, context, stored_image, status, builder):
        from src.handlers import delete_image

        if builder == "download":
            response = download_image.handler(
                api_event("GET", path_parameters={"imageId": stored_image["imageId"]}), context
            )
            assert response["headers"]["Location"].startswith("http")
        else:
            response = delete_image.handler(
                api_event("DELETE", path_parameters={"imageId": stored_image["imageId"]}), context
            )
            assert response["body"] == ""
        assert response["statusCode"] == status

    def test_every_handler_returns_a_serialisable_proxy_response(
        self, aws, context, stored_image
    ):
        responses = [
            list_images.handler(api_event("GET"), context),
            get_image.handler(
                api_event("GET", path_parameters={"imageId": stored_image["imageId"]}), context
            ),
            download_image.handler(
                api_event("GET", path_parameters={"imageId": stored_image["imageId"]}), context
            ),
        ]
        for response in responses:
            assert set(response) == {"statusCode", "headers", "body"}
            assert isinstance(response["statusCode"], int)
            assert isinstance(response["body"], str)
            json.dumps(response)
