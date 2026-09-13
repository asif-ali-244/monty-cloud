"""POST /images - register an image's metadata and get a direct-to-S3 upload form.

The image bytes are not part of this request. The response carries a presigned
POST that the client uses to send the file straight to S3, so uploads are not
bound by API Gateway or Lambda payload limits and no compute is spent moving bytes.
"""

from src.common import responses
from src.common.middleware import api_handler, caller_id, parse_json_body
from src.services import image_service


@api_handler
def handler(event, context):
    user_id = caller_id(event)
    payload = parse_json_body(event)
    result = image_service.register_upload(user_id, payload)
    return responses.created(result, location=f"/images/{result['image']['imageId']}")
