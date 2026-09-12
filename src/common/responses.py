"""API Gateway proxy response builders."""

import decimal
import json

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-User-Id",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
}


class _DynamoJSONEncoder(json.JSONEncoder):
    """DynamoDB hands back Decimal for every number; JSON does not know it."""

    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return int(o) if o % 1 == 0 else float(o)
        if isinstance(o, set):
            return sorted(o)
        return super().default(o)


def _response(status_code, body=None, headers=None):
    resp = {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS, **(headers or {})},
        "body": "" if body is None else json.dumps(body, cls=_DynamoJSONEncoder),
    }
    return resp


def ok(body, headers=None):
    return _response(200, body, headers)


def created(body, location=None):
    headers = {"Location": location} if location else None
    return _response(201, body, headers)


def no_content():
    return _response(204, None)


def redirect(url):
    """302 to a presigned S3 URL.

    Bytes never pass through Lambda or API Gateway, so downloads are not bound
    by the 6 MB proxy payload limit and cost nothing in compute time.
    """
    return _response(302, None, {"Location": url, "Cache-Control": "no-store"})


def error(status_code, message, code):
    return _response(status_code, {"error": message, "code": code})
