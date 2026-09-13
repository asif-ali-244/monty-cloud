#!/usr/bin/env python3
"""End-to-end check against a deployed stack.

Drives every endpoint and the full upload lifecycle over real HTTP: register,
upload straight to S3 on the presigned POST, wait for the S3-triggered processor,
then list, get, download (byte-comparing the file), and delete.

It also exercises what the unit tests cannot, because moto does not enforce POST
policies: S3 itself refusing an upload that is empty, over the size limit, or
has a tampered type or key, and the processor rejecting content that lies about
its type.
"""

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)
PDF_BYTES = b"%PDF-1.7 renamed to look like a png"

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print("  {} {}{}".format("PASS" if condition else "FAIL", name, detail and " - " + detail))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def call(base_url, method, path, body=None, user="smoke-user"):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-User-Id": user},
        method=method,
    )
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=60) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def as_json(payload):
    return json.loads(payload) if payload else None


def multipart(fields, file_field, data, content_type):
    """Encode a browser-style form: signed fields first, the file last."""
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        'filename="upload"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_to_s3(upload, data, content_type, fields=None):
    body, header = multipart(fields or upload["fields"], upload["fileField"], data, content_type)
    request = urllib.request.Request(
        upload["url"], data=body, method="POST", headers={"Content-Type": header}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def register(base_url, **overrides):
    payload = {
        "filename": "smoke.png",
        "contentType": "image/png",
        "tags": ["smoke", "test"],
        "description": "smoke test image",
    }
    payload.update(overrides)
    status, headers, body = call(base_url, "POST", "/images", payload)
    return status, headers, as_json(body)


def wait_until_settled(base_url, image_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, _, body = call(base_url, "GET", f"/images/{image_id}")
        image = as_json(body) or {}
        if image.get("status") != "pending":
            return image
        time.sleep(1)
    return image


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

    print("register")
    status, headers, registered = register(base_url)
    check("returns 201", status == 201, f"got {status} {str(registered)[:200]}")
    if status != 201:
        return 1
    image = registered["image"]
    upload = registered["upload"]
    image_id = image["imageId"]
    check("image starts pending", image["status"] == "pending")
    check("size is not known until the upload is verified", "sizeBytes" not in image)
    check("normalises tags", image["tags"] == ["smoke", "test"])
    check("hides the s3 key", "s3Key" not in image)
    check("sets Location", headers.get("Location") == f"/images/{image_id}")
    check("returns a POST form", upload["method"] == "POST" and "policy" in upload["fields"])
    max_bytes = upload.get("maxSizeBytes")
    check("advertises the size limit", isinstance(max_bytes, int) and max_bytes > 0)

    print("\nbefore the upload")
    status, _, _ = call(base_url, "GET", f"/images/{image_id}/content")
    check("download is 409 while pending", status == 409, f"got {status}")
    _, _, body = call(base_url, "GET", "/images?userId=smoke-user")
    listed = [i["imageId"] for i in as_json(body)["items"]]
    check("pending image is not listed", image_id not in listed)

    print("\nS3 enforces the upload policy")
    oversized = PNG_1X1 + b"\x00" * (max_bytes + 1 - len(PNG_1X1))
    status, body = upload_to_s3(upload, oversized, "image/png")
    check("refuses a file one byte over the limit", status == 400, f"got {status} {body[:120]}")
    status, body = upload_to_s3(upload, b"", "image/png")
    check("refuses an empty file", status == 400, f"got {status} {body[:120]}")
    tampered = dict(upload["fields"], **{"Content-Type": "text/html"})
    status, body = upload_to_s3(upload, PNG_1X1, "text/html", fields=tampered)
    check("refuses a tampered Content-Type", status == 403, f"got {status}")
    tampered = dict(upload["fields"], key=upload["fields"]["key"].replace(image_id, "f" * 32))
    status, body = upload_to_s3(upload, PNG_1X1, "image/png", fields=tampered)
    check("refuses a tampered key", status == 403, f"got {status}")

    print("\nupload")
    status, body = upload_to_s3(upload, PNG_1X1, "image/png")
    check("S3 accepts the exact file", status in (200, 201, 204), f"got {status} {body[:200]}")
    settled = wait_until_settled(base_url, image_id)
    status = settled.get("status")
    check("processor marks it ready", status == "ready", f"got {status}")
    check("records the measured size", settled.get("sizeBytes") == len(PNG_1X1))
    check(
        "records the verified checksum",
        settled.get("checksumSha256") == hashlib.sha256(PNG_1X1).hexdigest(),
    )
    check("records uploadedAt", bool(settled.get("uploadedAt")))

    print("\nvalidation")
    status, _, body = register(base_url, contentType="application/pdf")
    check("rejects unsupported type with 415", status == 415, f"got {status}")
    status, _, body = register(base_url, imageBase64="aGVsbG8=")
    check("rejects the retired imageBase64 field with 400", status == 400, f"got {status}")

    print("\ncontent that lies about its type")
    status, _, lying = register(base_url, filename="not-really.png")
    upload_to_s3(lying["upload"], PDF_BYTES, "image/png")
    rejected = wait_until_settled(base_url, lying["image"]["imageId"])
    status = rejected.get("status")
    check("processor rejects it", status == "rejected", f"got {status}")
    check("explains why", "does not match" in (rejected.get("rejectionReason") or ""))
    status, _, _ = call(base_url, "GET", f"/images/{lying['image']['imageId']}/content")
    check("download of a rejected image is 409", status == 409, f"got {status}")

    print("\nlist")
    for label, query in [
        ("unfiltered", "/images"),
        ("by user", "/images?userId=smoke-user"),
        ("by tag", "/images?tag=smoke"),
        ("by contentType", "/images?contentType=image/png"),
        ("by user and tag", "/images?userId=smoke-user&tag=smoke"),
        ("by filename", "/images?filename=smoke"),
    ]:
        # Follow the cursor: a filtered page can be empty while matches remain.
        found, token, pages = False, None, 0
        while pages < 20:
            separator = "&" if "?" in query else "?"
            path = f"{query}{separator}nextToken={token}" if token else query
            status, _, body = call(base_url, "GET", path)
            page = as_json(body) or {}
            found = found or any(i["imageId"] == image_id for i in page.get("items", []))
            token, pages = page.get("nextToken"), pages + 1
            if found or not token:
                break
        check(f"finds the image {label}", status == 200 and found, f"got {status}")

    status, _, body = call(base_url, "GET", "/images?tag=definitely-not-present")
    check("empty result set is a 200", status == 200 and as_json(body)["items"] == [])

    print("\nget")
    status, _, body = call(base_url, "GET", f"/images/{image_id}")
    check("returns the metadata", status == 200 and as_json(body)["imageId"] == image_id)
    status, _, _ = call(base_url, "GET", "/images/does-not-exist")
    check("unknown id is a 404", status == 404, f"got {status}")

    print("\ndownload")
    status, headers, _ = call(base_url, "GET", f"/images/{image_id}/content")
    location = headers.get("Location")
    check("redirects with 302", status == 302, f"got {status}")
    check("points at a signed URL", bool(location) and "X-Amz-Signature" in (location or ""))
    if location:
        with urllib.request.urlopen(location, timeout=60) as response:
            check("bytes round-trip intact", response.read() == PNG_1X1)
    status, _, body = call(base_url, "GET", f"/images/{image_id}/content?redirect=false")
    check("json mode returns the url", status == 200 and "downloadUrl" in as_json(body))

    print("\ndelete")
    status, _, _ = call(base_url, "DELETE", f"/images/{image_id}")
    check("returns 204", status == 204, f"got {status}")
    status, _, _ = call(base_url, "GET", f"/images/{image_id}")
    check("the image is gone", status == 404)
    status, _, _ = call(base_url, "DELETE", f"/images/{image_id}")
    check("deleting twice is a 404", status == 404)
    call(base_url, "DELETE", f"/images/{lying['image']['imageId']}")

    print("\nupload after delete")
    status, _, late = register(base_url, filename="late.png")
    late_id = late["image"]["imageId"]
    call(base_url, "DELETE", f"/images/{late_id}")
    upload_to_s3(late["upload"], PNG_1X1, "image/png")
    time.sleep(3)
    status, _, _ = call(base_url, "GET", f"/images/{late_id}")
    check("a late upload does not resurrect a deleted image", status == 404, f"got {status}")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed: {}".format(", ".join(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
