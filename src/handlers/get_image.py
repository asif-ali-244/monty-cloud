"""GET /images/{imageId} - fetch a single image's metadata."""

from src.common import responses
from src.common.middleware import api_handler, path_param
from src.services import image_service


@api_handler
def handler(event, context):
    image_id = path_param(event, "imageId")
    return responses.ok(image_service.get_image(image_id))
