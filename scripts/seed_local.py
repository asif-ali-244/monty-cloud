#!/usr/bin/env python3
"""Seed LocalStack with a few images so the list filters have something to show.

Talks to the deployed API rather than to DynamoDB directly, so seeding exercises
the same validation path as a real client.
"""

import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request

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


def post_image(base_url, user, filename, tags, description):
    payload = json.dumps(
        {
            "filename": filename,
            "contentType": "image/png",
            "imageBase64": base64.b64encode(PNG_1X1).decode(),
            "tags": tags,
            "description": description,
        }
    ).encode()
    request = urllib.request.Request(
        "{}/images".format(base_url.rstrip("/")),
        data=payload,
        headers={"Content-Type": "application/json", "X-User-Id": user},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


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
            created = post_image(base_url, user, filename, tags, description)
        except urllib.error.HTTPError as exc:
            print(f"  FAILED {filename} for {user}: {exc.code} {exc.read()}")
            return 1
        print("  {:<8} {:<20} {}".format(user, filename, created["imageId"]))

    print(f"done: {len(SAMPLES)} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
