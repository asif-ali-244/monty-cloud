"""GET /images/{imageId}/content - view or download the image bytes.

Answers with a 302 to a short-lived presigned S3 URL by default so the payload
never crosses Lambda. ``?redirect=false`` returns that URL as JSON instead, for
clients that would rather not follow redirects.
"""

from src.common import config, responses, validation
from src.common.middleware import api_handler, path_param, query_params
from src.services import image_service

MAX_URL_TTL_SECONDS = 3600


@api_handler
def handler(event, context):
    image_id = path_param(event, "imageId")
    params = query_params(event)
    disposition = (
        "attachment" if params.get("disposition") == "attachment" else "inline"
    )
    expires_in = validation.parse_positive_int(
        params.get("expiresIn"), "expiresIn", config.url_ttl_seconds(), MAX_URL_TTL_SECONDS
    )

    result = image_service.build_download(image_id, disposition, expires_in)
    if str(params.get("redirect", "true")).lower() == "false":
        return responses.ok(
            {
                "downloadUrl": result["url"],
                "expiresInSeconds": expires_in,
                "image": result["image"],
            }
        )
    return responses.redirect(result["url"])
