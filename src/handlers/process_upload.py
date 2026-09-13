"""S3 ObjectCreated -> verify the upload and mark the image ready or rejected.

This is the one handler not behind API Gateway, and it deliberately does not use
the API handlers' swallow-and-respond error handling. Nobody reads its return
value; what matters is whether Lambda retries. So:

* Permanent problems with an upload (wrong magic bytes, size mismatch, no row)
  are settled inside the service and never raised - retrying cannot fix them.
* Transient failures (S3 or DynamoDB unavailable) are allowed to propagate, so
  Lambda's asynchronous retries run and, if they are exhausted, the event lands
  in the failure destination instead of the upload being silently lost.
"""

import logging
import urllib.parse

from src.services import image_service

logger = logging.getLogger("images")


def handler(event, context):
    outcomes = []
    for record in event.get("Records") or []:
        if not str(record.get("eventName", "")).startswith("ObjectCreated"):
            continue
        # Keys in S3 notifications are URL-encoded, with spaces as '+'.
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        try:
            outcome = image_service.process_uploaded_object(key)
        except Exception:
            logger.exception("upload_processing_failed key=%s", key)
            raise
        outcomes.append({"key": key, "outcome": outcome})
    return {"processed": outcomes}
