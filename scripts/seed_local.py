#!/usr/bin/env python3
"""Seed LocalStack with a few images so the list filters have something to show.

Goes through the same path as a real client - register via the API, upload
straight to S3 on the presigned POST, wait for the processor - rather than
writing to DynamoDB directly, so seeding exercises validation end to end.
"""

import argparse
import base64
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

SAMPLES = [
    ("alice", "beach-sunset.png", ["beach", "sunset", "summer"], "Golden hour at the beach"),
    ("alice", "mountain-trail.png", ["mountains", "hiking"], "Trail above the treeline"),
    ("alice", "city-lights.png", ["city", "night"], "Downtown after dark"),
    ("bob", "puppy.png", ["pets", "dogs"], "New puppy"),
    ("bob", "coffee.png", ["food", "coffee"], "Morning flat white"),
    ("carol", "surfboard.png", ["beach", "sports"], "Board waxed and ready"),
]


def terraform_output(name):
    result = subprocess.run(
        ["terraform", "-chdir=terraform", "output", "-raw", name],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def api(base_url, method, path, user, body=None):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-User-Id": user},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def upload_to_s3(upload, data, content_type):
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in upload["fields"].items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{upload["fileField"]}"; '
        f'filename="upload"\r\nContent-Type: {content_type}\r\n\r\n'.encode() + data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        upload["url"],
        data=b"".join(parts),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=60):
        pass


def seed_one(base_url, user, filename, tags, description):
    registered = api(
        base_url,
        "POST",
        "/images",
        user,
        {
            "filename": filename,
            "contentType": "image/png",
            "tags": tags,
            "description": description,
        },
    )
    upload_to_s3(registered["upload"], PNG_1X1, "image/png")
    image_id = registered["image"]["imageId"]
    for _ in range(60):
        image = api(base_url, "GET", f"/images/{image_id}", user)
        if image["status"] != "pending":
            return image
        time.sleep(1)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        help="API base URL. Defaults to the api_base_url Terraform output.",
    )
    args = parser.parse_args()

    base_url = args.base_url or terraform_output("api_base_url")
    print(f"seeding {base_url}")

    for user, filename, tags, description in SAMPLES:
        try:
            image = seed_one(base_url, user, filename, tags, description)
        except urllib.error.HTTPError as exc:
            print(f"  FAILED {filename} for {user}: {exc.code} {exc.read()}")
            return 1
        print(f"  {user:<8} {filename:<20} {image['imageId']}  {image['status']}")
        if image["status"] != "ready":
            return 1

    print(f"done: {len(SAMPLES)} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
