#!/usr/bin/env python3
"""End-to-end check against a deployed stack.

Exercises every endpoint over real HTTP: upload, list (with each filter), get,
download (following the presigned redirect and comparing bytes), delete, and
the 404 that must follow it.
"""

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print("  {} {}{}".format("PASS" if condition else "FAIL", name, detail and " - " + detail))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def call(base_url, method, path, body=None, user="smoke-user", follow=False):
    url = "{}{}".format(base_url.rstrip("/"), path)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "X-User-Id": user},
        method=method,
    )
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=60) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def as_json(payload):
    return json.loads(payload) if payload else None


def terraform_output(name):
    result = subprocess.run(
        ["terraform", "-chdir=terraform", "output", "-raw", name],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Defaults to the api_base_url Terraform output.")
    args = parser.parse_args()
    base_url = args.base_url or terraform_output("api_base_url")
    print(f"smoke testing {base_url}\n")

    print("upload")
    status, headers, payload = call(
        base_url,
        "POST",
        "/images",
        {
            "filename": "smoke.png",
            "contentType": "image/png",
            "imageBase64": base64.b64encode(PNG_1X1).decode(),
            "tags": ["smoke", "test"],
            "description": "smoke test image",
        },
    )
    check("returns 201", status == 201, f"got {status} {payload[:200]}")
    if status != 201:
        return 1
    created = as_json(payload)
    image_id = created["imageId"]
    check("assigns an id", bool(image_id))
    check("records the size", created["sizeBytes"] == len(PNG_1X1))
    check(
        "records the checksum",
        created["checksumSha256"] == hashlib.sha256(PNG_1X1).hexdigest(),
    )
    check("normalises tags", created["tags"] == ["smoke", "test"])
    check("hides the s3 key", "s3Key" not in created)
    check("sets Location", headers.get("Location") == f"/images/{image_id}")

    print("\nvalidation")
    status, _, payload = call(
        base_url,
        "POST",
        "/images",
        {"filename": "x.png", "contentType": "application/pdf", "imageBase64": "AAAA"},
    )
    check("rejects unsupported type with 415", status == 415, f"got {status}")
    status, _, _ = call(base_url, "POST", "/images", {"filename": "x.png"})
    check("rejects an incomplete body with 400", status == 400)

    print("\nlist")
    for label, query in [
        ("unfiltered", "/images"),
        ("by user", "/images?userId=smoke-user"),
        ("by tag", "/images?tag=smoke"),
        ("by contentType", "/images?contentType=image/png"),
        ("by user and tag", "/images?userId=smoke-user&tag=smoke"),
        ("by filename", "/images?filename=smoke"),
    ]:
        status, _, payload = call(base_url, "GET", query)
        body = as_json(payload) or {}
        found = any(item["imageId"] == image_id for item in body.get("items", []))
        check(f"finds the image {label}", status == 200 and found, f"got {status}")

    status, _, payload = call(base_url, "GET", "/images?tag=definitely-not-present")
    check("empty result set is a 200", status == 200 and as_json(payload)["items"] == [])

    status, _, payload = call(base_url, "GET", "/images?limit=1")
    check("honours limit", status == 200 and len(as_json(payload)["items"]) <= 1)

    print("\nget")
    status, _, payload = call(base_url, "GET", f"/images/{image_id}")
    check("returns the metadata", status == 200 and as_json(payload)["imageId"] == image_id)
    status, _, _ = call(base_url, "GET", "/images/does-not-exist")
    check("unknown id is a 404", status == 404, f"got {status}")

    print("\ndownload")
    status, headers, _ = call(base_url, "GET", f"/images/{image_id}/content")
    location = headers.get("Location")
    check("redirects with 302", status == 302, f"got {status}")
    check("points at a signed URL", bool(location) and "X-Amz-Signature" in (location or ""))
    if location:
        with urllib.request.urlopen(location, timeout=60) as response:
            fetched = response.read()
        check("bytes round-trip intact", fetched == PNG_1X1)
    status, _, payload = call(
        base_url, "GET", f"/images/{image_id}/content?redirect=false"
    )
    check("json mode returns the url", status == 200 and "downloadUrl" in as_json(payload))

    print("\ndelete")
    status, _, _ = call(base_url, "DELETE", f"/images/{image_id}")
    check("returns 204", status == 204, f"got {status}")
    status, _, _ = call(base_url, "GET", f"/images/{image_id}")
    check("the image is gone", status == 404)
    status, _, _ = call(base_url, "DELETE", f"/images/{image_id}")
    check("deleting twice is a 404", status == 404)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed: {}".format(", ".join(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
