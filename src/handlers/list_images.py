"""GET /images - list images with filters and cursor pagination."""

from src.common import responses, validation
from src.common.middleware import api_handler, query_params
from src.services import image_service


@api_handler
def handler(event, context):
    params = query_params(event)
    filters = {
        "userId": validation.require_string(params, "userId", required=False),
        "tag": _first_tag(params),
        "contentType": _content_type(params),
        "uploadedFrom": validation.parse_timestamp(
            params.get("uploadedFrom"), "uploadedFrom"
        ),
        "uploadedTo": validation.parse_timestamp(params.get("uploadedTo"), "uploadedTo"),
        "filename": validation.require_string(params, "filename", required=False),
        "limit": validation.parse_limit(params.get("limit")),
        "nextToken": params.get("nextToken"),
    }
    return responses.ok(image_service.list_images(filters))


def _first_tag(params):
    tags = validation.normalise_tags(params.get("tag"))
    return tags[0] if tags else None


def _content_type(params):
    value = validation.require_string(params, "contentType", required=False)
    return validation.validate_content_type(value) if value else None
