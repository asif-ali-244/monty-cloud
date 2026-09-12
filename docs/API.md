# Image Service — API Reference

Complete reference for the image upload and catalogue API. The machine-readable
version is [`openapi.yaml`](openapi.yaml); it is validated and contract-tested
against the implementation on every test run, so the two cannot silently drift.

- [Conventions](#conventions)
- [Authentication](#authentication)
- [Quick reference](#quick-reference)
- [Endpoints](#endpoints)
- [Pagination](#pagination)
- [Filtering](#filtering)
- [Errors](#errors)
- [Client examples](#client-examples)
- [Limits](#limits)
- [Trying it out](#trying-it-out)

---

## Conventions

**Base URL.** Every path below is relative to the stage base URL.

```bash
export API=$(terraform -chdir=terraform output -raw api_base_url)
```

| Target | Shape |
|---|---|
| LocalStack | `http://localhost:4566/restapis/{restApiId}/local/_user_request_` |
| AWS | `https://{restApiId}.execute-api.{region}.amazonaws.com/{stage}` |

**Content type.** Requests and responses are `application/json; charset=utf-8`.
The single exception is `GET /images/{imageId}/content`, which answers `302` with
an empty body.

**Timestamps.** Always UTC, ISO-8601, millisecond precision, `Z`-suffixed:
`2026-09-12T00:43:34.500Z`. Inputs are more forgiving — any ISO-8601 instant is
accepted and normalised, and a timestamp with no zone is read as UTC.

**Identifiers.** `imageId` is a server-generated 32-character hex string. Clients
never choose it, which is what makes concurrent uploads collision-free.

**CORS.** All responses carry `Access-Control-Allow-Origin: *`. `OPTIONS` on
`/images` and `/images/{imageId}` is answered by an API Gateway mock integration,
so a browser preflight never invokes a Lambda.

---

## Authentication

The caller is identified by the **`X-User-Id`** request header.

```
X-User-Id: alice
```

| Endpoint | Identity |
|---|---|
| `POST /images` | **Required** — becomes the image's `userId` |
| `DELETE /images/{imageId}` | Required |
| `GET /images`, `GET /images/{imageId}`, `GET /images/{imageId}/content` | Not required |

> **This is a development stand-in, not authentication.** A client can claim to
> be anyone. In a deployed environment a Cognito or Lambda authorizer supplies
> the identity and the header is ignored: the handlers already read
> `requestContext.authorizer.claims.sub` in preference to the header, so
> attaching one requires no application change. See
> [Known limitations](../README.md#known-limitations).

---

## Quick reference

| Method | Path | Purpose | Success |
|---|---|---|---|
| `POST` | `/images` | Upload an image with metadata | `201` |
| `GET` | `/images` | List and search | `200` |
| `GET` | `/images/{imageId}` | View metadata | `200` |
| `GET` | `/images/{imageId}/content` | View or download the file | `302` |
| `DELETE` | `/images/{imageId}` | Delete an image | `204` |

---

## Endpoints

### Upload an image

```
POST /images
```

Stores the image in S3 and its metadata in DynamoDB.

**Headers**

| Header | | |
|---|---|---|
| `Content-Type` | required | `application/json` |
| `X-User-Id` | required | Becomes the image's `userId` |

**Body**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `filename` | string | yes | 1–255 chars. Any directory component is stripped: `../../etc/passwd.png` is stored as `passwd.png` |
| `contentType` | string | yes | `image/jpeg`, `image/png`, `image/gif`, `image/webp` |
| `imageBase64` | string | yes | Base64, or a `data:image/png;base64,…` URL. ≤ 5 MB decoded |
| `tags` | string[] | no | ≤ 20 items, each ≤ 50 chars matching `[a-z0-9][a-z0-9_-]*`. Lowercased and de-duplicated on write. A comma-separated string is also accepted |
| `description` | string | no | ≤ 1024 chars |

The declared `contentType` is checked against the file's leading magic bytes. A
PDF renamed to `.png` is rejected with `400` rather than stored.

**Request**

```bash
curl -sS -X POST "$API/images" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: alice' \
  -d "{
        \"filename\": \"sunset.png\",
        \"contentType\": \"image/png\",
        \"imageBase64\": \"$(base64 < sunset.png | tr -d '\n')\",
        \"tags\": [\"beach\", \"sunset\"],
        \"description\": \"Golden hour\"
      }"
```

**`201 Created`** · `Location: /images/{imageId}`

```json
{
  "imageId": "334d3b476eba4fbfa2b00e2aa4792a8a",
  "userId": "alice",
  "filename": "sunset.png",
  "contentType": "image/png",
  "sizeBytes": 70,
  "checksumSha256": "c414cd0e204de974f73753c7e28d7638e7b3691bb8b1a2bab6b25bb7fed7ce77",
  "tags": ["beach", "sunset"],
  "description": "Golden hour",
  "uploadedAt": "2026-09-12T00:43:34.500Z"
}
```

`description` is present only when it was supplied. `checksumSha256` is the
SHA-256 of the stored bytes, so a client can verify a later download.

**Failures** — `400` malformed body, missing field, content/type mismatch, or
missing `X-User-Id` · `413` image over the size limit · `415` unsupported
`contentType` · `502` S3 or DynamoDB unavailable.

---

### List and search

```
GET /images
```

Returns a page of images, newest first.

**Query parameters** — all optional, combined with AND.

| Parameter | Example | Notes |
|---|---|---|
| `userId` | `alice` | **Indexed.** The only filter that turns the read into a Query |
| `tag` | `beach` | Matches one tag; case-insensitive |
| `contentType` | `image/png` | Exact match. An unsupported value is `415`, not an empty page |
| `filename` | `sun` | Case-insensitive substring |
| `uploadedFrom` | `2026-01-01T00:00:00Z` | Inclusive lower bound |
| `uploadedTo` | `2026-12-31T23:59:59Z` | Inclusive upper bound |
| `limit` | `25` | 1–100, default 25. Larger values are capped, not rejected. Bounds rows **read**, not rows returned — see [Pagination](#pagination) |
| `nextToken` | *(opaque)* | Cursor from the previous page |

**Request**

```bash
curl -sS "$API/images?userId=alice&tag=beach&limit=10"
```

**`200 OK`**

```json
{
  "items": [
    {
      "imageId": "e324de3fe89447a798d9b7f95e4be5bb",
      "userId": "alice",
      "filename": "beach-sunset.png",
      "contentType": "image/png",
      "sizeBytes": 70,
      "checksumSha256": "c414cd0e204de974f73753c7e28d7638e7b3691bb8b1a2bab6b25bb7fed7ce77",
      "tags": ["beach", "sunset", "summer"],
      "description": "Golden hour at the beach",
      "uploadedAt": "2026-09-12T00:43:34.500Z"
    }
  ],
  "count": 1,
  "nextToken": null
}
```

`count` is the size of **this page**, not the total number of matches. A query
matching nothing is a `200` with `items: []`, never a `404`.

A page can be empty *and still have a `nextToken`* — see
[Pagination](#pagination) before writing a client loop.

**Failures** — `400` unparseable timestamp, non-integer `limit`, or malformed
`nextToken` · `415` unsupported `contentType` filter · `502` DynamoDB unavailable.

---

### View metadata

```
GET /images/{imageId}
```

```bash
curl -sS "$API/images/334d3b476eba4fbfa2b00e2aa4792a8a"
```

**`200 OK`** — the same object as the upload response. `404` if there is no such
image. Storage-internal attributes (`s3Key`, `s3Bucket`, `filenameLower`) are
never exposed.

---

### View or download the file

```
GET /images/{imageId}/content
```

Answers `302` with a `Location` pointing at a short-lived presigned S3 URL. The
bytes never pass through Lambda or API Gateway, so downloads are not subject to
the 6 MB proxy response cap and cost no compute time proportional to file size.
The presigned URL carries a signed `Content-Type` and `Content-Disposition`, so
the browser still sees the original filename.

| Parameter | Default | |
|---|---|---|
| `disposition` | `inline` | `attachment` makes the browser save rather than render |
| `expiresIn` | `900` | URL lifetime in seconds; 1–3600, larger values capped |
| `redirect` | `true` | `false` returns the URL as JSON instead of redirecting |

**Follow the redirect** — the normal case:

```bash
curl -sSL "$API/images/$ID/content" -o sunset.png
```

**Inspect the redirect**:

```bash
curl -sS "$API/images/$ID/content?disposition=attachment" -D - -o /dev/null
```

```
HTTP/1.1 302 FOUND
Location: http://localhost:4566/monty-images-local-bucket/images/alice/334d…png?…&X-Amz-Signature=…
Cache-Control: no-store
```

**Get the URL as JSON** — for clients that cannot follow redirects, or to hand a
URL to another system:

```bash
curl -sS "$API/images/$ID/content?redirect=false&expiresIn=60"
```

```json
{
  "downloadUrl": "http://localhost:4566/monty-images-local-bucket/images/alice/334d….png?…",
  "expiresInSeconds": 60,
  "image": { "imageId": "334d3b476eba4fbfa2b00e2aa4792a8a", "…": "…" }
}
```

The `Cache-Control: no-store` on the redirect matters: the URL expires, so a
cached `302` would hand out a dead link.

**Failures** — `400` `expiresIn` not a positive integer · `404` no such image ·
`502` S3 or DynamoDB unavailable.

---

### Delete an image

```
DELETE /images/{imageId}
```

```bash
curl -sS -X DELETE "$API/images/$ID" -H 'X-User-Id: alice' -o /dev/null -w '%{http_code}\n'
```

**`204 No Content`**, with no body.

Deleting an image that is already gone returns **`404`, not `204`**. This is
deliberate: a client deleting the wrong id should hear about it rather than be
told the call succeeded. Callers that want idempotent semantics should treat
`404` on delete as success.

---

## Pagination

Cursor-based. A response carrying a non-null `nextToken` has more pages; pass
that value back verbatim on the next request. The token is an opaque encoding of
the DynamoDB `LastEvaluatedKey` — do not parse or construct one. A malformed
token is a `400`.

> **`nextToken`, not `count`, is what tells you whether to stop.**
>
> `limit` bounds the rows the database **reads**, not the rows it returns.
> Filters other than `userId` are applied after that read, so a page whose rows
> all fail the filter comes back **empty with a non-null `nextToken`** while
> matches still wait behind the cursor:
>
> ```
> GET /images?userId=alice&tag=beach&limit=2   → { "items": [], "count": 0, "nextToken": "eyJ…" }
> GET /images?userId=alice&tag=beach&limit=2&nextToken=eyJ…
>                                              → { "items": [ … ], "count": 1, "nextToken": null }
> ```
>
> A client that stops at the first empty page silently loses results. Keep
> following the cursor until `nextToken` is `null`. Raising `limit` makes this
> less frequent but cannot remove it.

```bash
TOKEN=$(curl -sS "$API/images?userId=alice&limit=2" | jq -r .nextToken)
curl -sS "$API/images?userId=alice&limit=2&nextToken=$TOKEN"
```

Walk every page:

```bash
TOKEN=""
while :; do
  PAGE=$(curl -sS "$API/images?userId=alice&limit=25${TOKEN:+&nextToken=$TOKEN}")
  echo "$PAGE" | jq -r '.items[].filename'
  TOKEN=$(echo "$PAGE" | jq -r '.nextToken // empty')
  [ -z "$TOKEN" ] && break
done
```

Both loops above are correct because they stop on `nextToken`, not on an empty
page.

Keep the filters identical across pages — a cursor is only meaningful for the
query that produced it. Images uploaded mid-walk may or may not appear, but no
image is ever returned twice within one walk.

---

## Filtering

Two filters have very different costs, and it is worth knowing which is which.

**`userId` is indexed.** It is the partition key of the
`userId-uploadedAt-index` GSI, so supplying it makes the request a Query whose
cost is proportional to the number of results returned. Because `uploadedAt` is
the sort key and the canonical timestamp format sorts lexicographically,
`uploadedFrom`/`uploadedTo` become **key conditions** in that case — the date
range narrows the read itself rather than discarding rows afterwards.

```bash
# Query: reads only January's images for alice.
curl -sS "$API/images?userId=alice&uploadedFrom=2026-01-01T00:00:00Z&uploadedTo=2026-01-31T23:59:59Z"
```

**Everything else is a filter expression**, applied after the read. `tag`,
`contentType` and `filename` reduce the response payload but not the capacity
consumed. And with no `userId` there is no partition key to anchor on, so the
request falls back to a bounded, paginated Scan:

```bash
# Scan: bounded by limit and paginated, but it reads the table.
curl -sS "$API/images?tag=beach&limit=25"
```

That is fine for an admin view or a modest corpus and does not scale to a large
one. The paths out — a sharded GSI for a global reverse-chronological feed, or a
search index if tag and filename search need to be first-class — are discussed in
[the README's design notes](../README.md#design-decisions).

---

## Errors

Every failure returns the same shape:

```json
{
  "error": "'contentType' must be one of: image/gif, image/jpeg, image/png, image/webp",
  "code": "UnsupportedMediaType"
}
```

Branch on `code`; `error` is for humans and its wording may change.

| Status | `code` | Meaning | Retry? |
|---|---|---|---|
| `400` | `ValidationError` | Malformed body, bad filter, bad `nextToken`, missing caller identity | No — fix the request |
| `404` | `NotFound` | No image with that id | No |
| `409` | `Conflict` | Image id already exists (not reachable in normal use) | No |
| `413` | `PayloadTooLarge` | Decoded image over the size limit | No — shrink the image |
| `415` | `UnsupportedMediaType` | `contentType` is not an accepted image type | No |
| `502` | `StorageError` | S3 or DynamoDB call failed | Yes, with backoff |
| `500` | `InternalError` | Unexpected failure | Yes, with backoff |

A `500` body carries only a request id:

```json
{ "error": "Internal server error. Reference: 8f2c1a4e-6b9d-4c3a-9f11-2d7e5a0b4c88", "code": "InternalError" }
```

That reference correlates with the CloudWatch log entry holding the stack trace.
Internal details are never returned to the caller — quote the reference when
reporting a problem.

---

## Client examples

### Python

```python
import base64, pathlib, requests

API = "http://localhost:4566/restapis/<restApiId>/local/_user_request_"
SESSION = requests.Session()
SESSION.headers["X-User-Id"] = "alice"


def upload(path, tags=(), description=None):
    body = {
        "filename": pathlib.Path(path).name,
        "contentType": "image/png",
        "imageBase64": base64.b64encode(pathlib.Path(path).read_bytes()).decode(),
        "tags": list(tags),
    }
    if description:
        body["description"] = description
    response = SESSION.post(f"{API}/images", json=body, timeout=30)
    response.raise_for_status()
    return response.json()


def search(**filters):
    """Yield every match, following the cursor."""
    token = None
    while True:
        params = {**filters, "limit": 100}
        if token:
            params["nextToken"] = token
        page = SESSION.get(f"{API}/images", params=params, timeout=30).json()
        yield from page["items"]
        token = page["nextToken"]
        if not token:
            break


def download(image_id, destination):
    # requests follows the 302 to S3 by default.
    response = SESSION.get(f"{API}/images/{image_id}/content", timeout=60)
    response.raise_for_status()
    pathlib.Path(destination).write_bytes(response.content)


image = upload("sunset.png", tags=["beach", "sunset"], description="Golden hour")
print(image["imageId"], image["checksumSha256"])

for match in search(userId="alice", tag="beach"):
    print(match["filename"], match["uploadedAt"])

download(image["imageId"], "downloaded.png")
SESSION.delete(f"{API}/images/{image['imageId']}", timeout=30)
```

### Browser / JavaScript

```js
const API = "http://localhost:4566/restapis/<restApiId>/local/_user_request_";

async function upload(file, tags = []) {
  // FileReader gives a data URL, which the API accepts as-is.
  const imageBase64 = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });

  const response = await fetch(`${API}/images`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-User-Id": "alice" },
    body: JSON.stringify({
      filename: file.name,
      contentType: file.type,
      imageBase64,
      tags,
    }),
  });

  if (!response.ok) {
    const { error, code } = await response.json();
    throw new Error(`${code}: ${error}`);
  }
  return response.json();
}

// Render straight from the API: the browser follows the 302 to S3.
function thumbnail(imageId) {
  const img = document.createElement("img");
  img.src = `${API}/images/${imageId}/content`;
  return img;
}

// Or resolve the signed URL first, e.g. to hand it to another component.
async function downloadUrl(imageId, expiresIn = 900) {
  const params = new URLSearchParams({ redirect: "false", expiresIn });
  const response = await fetch(`${API}/images/${imageId}/content?${params}`);
  const { downloadUrl } = await response.json();
  return downloadUrl;
}
```

Because `imageBase64` accepts a data URL, `FileReader.readAsDataURL` output can
be posted unchanged — no stripping of the `data:` prefix.

---

## Limits

| | Value | Configured by |
|---|---|---|
| Max image size | 5 MB decoded | `MAX_IMAGE_BYTES` / `var.max_image_bytes` |
| Max request payload | 10 MB | API Gateway (fixed) |
| Accepted types | jpeg, png, gif, webp | `ALLOWED_CONTENT_TYPES` |
| Filename length | 255 chars | — |
| Tags per image | 20, each ≤ 50 chars | — |
| Description length | 1024 chars | — |
| Page size | 25 default, 100 max | — |
| Presigned URL lifetime | 900s default, 3600s max | `DOWNLOAD_URL_TTL_SECONDS` / `var.download_url_ttl_seconds` |
| Request rate | 200/s sustained, 400 burst | `aws_api_gateway_method_settings` |

Base64 inflates a payload by about a third, so the 10 MB request cap is what
sets the practical ceiling on the 5 MB image limit. Raising it much further means
moving to presigned uploads — see the README's design notes.

---

## Trying it out

**Swagger UI** — render `openapi.yaml` locally with no install:

```bash
make docs
```

Serves the spec at <http://localhost:8080> in Swagger UI, with the LocalStack
server preselected so requests can be fired from the browser.

**Postman / Insomnia** — import [`postman_collection.json`](postman_collection.json).
Set the `baseUrl` and `userId` collection variables; `imageId` is captured
automatically from the upload response, so the requests can be run top to bottom.

**Command line** — `make seed` loads six sample images across three users, and
`make smoke` runs 26 end-to-end assertions over every endpoint, including
following the presigned redirect and byte-comparing the download.
