"""POST /images - upload an image together with its metadata."""

from src.common import responses
from src.common.middleware import api_handler, caller_id, parse_json_body
from src.services import image_service


@api_handler
def handler(event, context):
    user_id = caller_id(event)
    payload = parse_json_body(event)
    image = image_service.create_image(user_id, payload)
    return responses.created(image, location="/images/{}".format(image["imageId"]))
