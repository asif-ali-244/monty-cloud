"""DELETE /images/{imageId} - delete an image and its metadata."""

from src.common import responses
from src.common.middleware import api_handler, path_param
from src.services import image_service


@api_handler
def handler(event, context):
    image_id = path_param(event, "imageId")
    image_service.delete_image(image_id)
    return responses.no_content()
