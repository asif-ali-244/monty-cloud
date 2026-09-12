"""The global try/catch every handler is wrapped in.

Handlers stay declarative: parse the event, call a service, shape a response.
Error-to-status mapping, logging and the never-crash guarantee live here so
that the five handlers cannot drift from each other.
"""

import functools
import json
import logging
import os
import uuid

from src.common import responses
from src.common.errors import AppError, ValidationError

logging.basicConfig()
logger = logging.getLogger("images")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def api_handler(func):
    @functools.wraps(func)
    def wrapper(event, context):
        request_id = getattr(context, "aws_request_id", None) or str(uuid.uuid4())
        try:
            return func(event, context)
        except AppError as exc:
            # Expected, caller-facing failure: log at warning, no stack trace.
            logger.warning(
                "handled_error request_id=%s code=%s message=%s",
                request_id,
                exc.code,
                exc.message,
            )
            return responses.error(exc.status_code, exc.message, exc.code)
        except Exception:  # noqa: BLE001 - the Lambda must never crash
            logger.exception("unhandled_error request_id=%s", request_id)
            return responses.error(
                500,
                f"Internal server error. Reference: {request_id}",
                "InternalError",
            )

    return wrapper


def parse_json_body(event):
    """Decode the proxy body, transparently handling base64-encoded payloads."""
    import base64

    body = event.get("body")
    if body is None or body == "":
        raise ValidationError("Request body is required")
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise ValidationError("Request body is not valid UTF-8") from exc
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Request body must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("Request body must be a JSON object")
    return parsed


def path_param(event, name):
    value = (event.get("pathParameters") or {}).get(name)
    if not value:
        raise ValidationError(f"Path parameter '{name}' is required")
    return value


def query_params(event):
    return event.get("queryStringParameters") or {}


def caller_id(event, required=True):
    """Resolve the calling user.

    In a deployed stack this comes from the API Gateway authorizer, which the
    client cannot forge. The X-User-Id header is a local-development fallback
    only and must not be trusted once a real authorizer is attached.
    """
    ctx = event.get("requestContext") or {}
    claims = ((ctx.get("authorizer") or {}).get("claims")) or {}
    user_id = claims.get("sub") or claims.get("cognito:username")
    if not user_id:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        user_id = headers.get("x-user-id")
    if not user_id and required:
        raise ValidationError("Caller identity missing: supply the X-User-Id header")
    return user_id
