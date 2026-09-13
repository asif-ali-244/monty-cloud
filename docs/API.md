# Image Service — API Reference

Complete reference for the image upload and catalogue API. The machine-readable
version is [`openapi.yaml`](openapi.yaml); it is validated and contract-tested
against the implementation on every test run, so the two cannot silently drift.

- [Conventions](#conventions)
- [Authentication](#authentication)
- [Quick reference](#quick-reference)
- [Uploading an image](#uploading-an-image)
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

**Image bytes never pass through the API.** Uploads go straight to S3 on a
presigned POST, and downloads redirect to a presigned S3 GET. Every API request
and response body is `application/json`; the only bodyless responses are the
`302` download redirect and the `204` delete.

**Timestamps.** Always UTC, ISO-8601, millisecond precision, `Z`-suffixed:
`2026-09-13T10:15:03.874Z`. Inputs are more forgiving — any ISO-8601 instant is
accepted and normalised, and a timestamp with no zone is read as UTC.

**Identifiers.** `imageId` is a server-generated 32-character hex string. Clients
never choose it or the S3 key, which is what makes concurrent uploads
collision-free.

**CORS.** All API responses carry `Access-Control-Allow-Origin: *`. `OPTIONS` on
`/images` and `/images/{imageId}` is answered by an API Gateway mock integration,
so a browser preflight never invokes a Lambda. The bucket has its own CORS rule
allowing browsers to `POST` uploads and `GET` downloads directly.

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

The value becomes part of the image's S3 key, so it must be 1–128 characters of
letters, digits, `.`, `_`, `@` or `-`, starting with a letter or digit. Anything
else — notably a `/` — is a `400`.

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
| `POST` | `/images` | Register an image; get a direct-to-S3 upload form | `201` |
| `POST` | *`upload.url`* (S3) | Upload the file itself — S3, not this API | `204` |
| `GET` | `/images` | List and search ready images | `200` |
| `GET` | `/images/{imageId}` | View metadata, in any state — poll this after uploading | `200` |
| `GET` | `/images/{imageId}/content` | View or download the file | `302` |
| `DELETE` | `/images/{imageId}` | Delete an image | `204` |

---

## Uploading an image

An upload is three steps: register the metadata with the API, send the file
straight to S3, then wait for it to be verified.

```
 client                          API                       S3                 processor
   │  POST /images {metadata}     │                         │                      │
   │─────────────────────────────▶│ row: status=pending     │                      │
   │◀──── 201 {image, upload} ────│                         │                      │
   │                              │                         │                      │
   │  POST upload.url  (fields + file)                      │                      │
   │───────────────────────────────────────────────────────▶│ policy checked       │
   │◀──────────────────────────────────────────────── 204 ──│ ObjectCreated ──────▶│
   │                              │                         │                      │ magic bytes,
   │  GET /images/{id}  (poll)    │                         │                      │ size, SHA-256
   │─────────────────────────────▶│◀──────────────── status = ready | rejected ────│
   │◀─── 200 {status: "ready"} ───│                         │                      │
```

### Why this shape

The file never touches API Gateway or Lambda, so uploads are not bound by their
payload limits (10 MB request, 6 MB Lambda invocation), cost no compute to move,
and scale with S3 rather than with Lambda concurrency. The size ceiling is a
product limit — 20 MB by default — and S3 enforces it.

### Image status

| `status` | Meaning | Listed? | `/content` |
|---|---|---|---|
| `pending` | Registered; file not yet uploaded or not yet verified | No | `409` |
| `ready` | Verified: `sizeBytes`, `checksumSha256` and `uploadedAt` are set | Yes | `302` |
| `rejected` | The uploaded file failed verification; see `rejectionReason`. The object has been deleted | No | `409` |

A `pending` image whose file never arrives expires on its own about an hour after
its upload form does. A `rejected` image stays readable for 24 hours so the
client can see why, then expires.

### Walkthrough

Verified against LocalStack with macOS's bash 3.2 and `jq`:

```bash
FILE=sunset.png

# 1. Register: metadata only, no bytes.
RESP=$(curl -sS -X POST "$API/images" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: alice' \
  -d "{\"filename\": \"$FILE\", \"contentType\": \"image/png\", \"tags\": [\"beach\", \"sunset\"]}")
ID=$(jq -r .image.imageId <<<"$RESP")

# 2. Upload straight to S3: every signed field as-is, then the file, last.
FORM=()
while IFS=$'\t' read -r name value; do
  FORM+=(--form-string "$name=$value")
done < <(jq -r '.upload.fields | to_entries[] | [.key, .value] | @tsv' <<<"$RESP")

curl -sS -o /dev/null -w 'S3 answered %{http_code}\n' "$(jq -r .upload.url <<<"$RESP")" \
  "${FORM[@]}" -F "file=@$FILE;type=image/png"

# 3. Poll until the processor has verified it.
until [ "$(curl -sS "$API/images/$ID" | jq -r .status)" != "pending" ]; do sleep 1; done
curl -sS "$API/images/$ID" | jq '{status, sizeBytes, checksumSha256, rejectionReason}'
```

```json
{
  "status": "ready",
  "sizeBytes": 70,
  "checksumSha256": "c414cd0e204de974f73753c7e28d7638e7b3691bb8b1a2bab6b25bb7fed7ce77",
  "rejectionReason": null
}
```

Three details that trip up hand-written clients:

- **Send every field in `upload.fields`, unchanged.** The set varies with the
  credentials that signed it — `x-amz-security-token` appears only for temporary
  credentials — so copy them all rather than naming them. Changing any value
  invalidates the signature.
- **The file must be the last form field.** S3 ignores fields after the file.
- **Use `--form-string`, not `-F`, for the signed fields.** `-F` treats a value
  beginning with `@` or `<` as a file reference.

### What S3 refuses

The upload policy is signed, so S3 checks every condition before storing a
byte. These failures come from S3, as XML, not from this API:

| Attempt | S3 response |
|---|---|
| File larger than `upload.maxSizeBytes` | `400 EntityTooLarge` |
| Empty file | `400 EntityTooSmall` |
| `Content-Type` field changed | `403 AccessDenied` (policy condition failed) |
| `key` field changed | `403 AccessDenied` (policy condition failed) |
| Form used after `upload.expiresAt` | `403 AccessDenied` (policy expired) |

### What the processor rejects

Things a policy cannot express are checked after the upload lands. A rejected
image's object is deleted and the reason is recorded:

| Problem | `rejectionReason` |
|---|---|
| Leading bytes are not the declared type — a PDF sent as `image/png` | `File content does not match the declared contentType 'image/png'` |
| Stored object over the size limit | `Uploaded 20971521 bytes; the limit is 20971520 bytes` |
| Stored object is empty | `Uploaded file is empty` |
| Stored content type differs from the declared one | `Uploaded as 'text/html' but 'image/png' was declared` |

The last three cannot happen through the signed form; they are defence in depth
against an object reaching the key some other way. The size recorded on a ready
image is the one the processor measured while hashing.

---

## Endpoints

### Register an image

```
POST /images
```

Records the metadata with status `pending` and returns a presigned S3 POST for
the file. No bytes are sent in this request.

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
| `tags` | string[] | no | ≤ 20 items, each ≤ 50 chars matching `[a-z0-9][a-z0-9_-]*`. Lowercased and de-duplicated on write. A comma-separated string is also accepted |
| `description` | string | no | ≤ 1024 chars |

Any other field is a `400`. That includes `imageBase64`, which earlier versions
of this API accepted — a client still sending it hears so, instead of receiving a
`201` for an image that will never arrive.

The size limit is enforced by S3, not by this request. The upload form accepts 1 byte up to
`upload.maxSizeBytes`, and S3 refuses anything outside that range when the file is
sent. A client that wants to fail before uploading should compare its file
against `maxSizeBytes` itself.

**Request**

```bash
curl -sS -X POST "$API/images" \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: alice' \
  -d '{"filename": "sunset.png", "contentType": "image/png",
       "tags": ["beach", "sunset"], "description": "Golden hour"}'
```

**`201 Created`** · `Location: /images/{imageId}`

```json
{
  "image": {
    "imageId": "334d3b476eba4fbfa2b00e2aa4792a8a",
    "userId": "alice",
    "filename": "sunset.png",
    "contentType": "image/png",
    "tags": ["beach", "sunset"],
    "description": "Golden hour",
    "status": "pending",
    "createdAt": "2026-09-13T10:15:02.118Z"
  },
  "upload": {
    "method": "POST",
    "url": "http://localhost:4566/monty-images-local-bucket",
    "fileField": "file",
    "maxSizeBytes": 20971520,
    "fields": {
      "Content-Type": "image/png",
      "key": "images/alice/334d3b476eba4fbfa2b00e2aa4792a8a.png",
      "x-amz-algorithm": "AWS4-HMAC-SHA256",
      "x-amz-credential": "…/20260913/us-east-1/s3/aws4_request",
      "x-amz-date": "20260913T101502Z",
      "policy": "eyJleHBpcmF0aW9uIjog…",
      "x-amz-signature": "5f1c1b3f…"
    },
    "expiresAt": "2026-09-13T10:30:02.118Z",
    "expiresInSeconds": 900
  }
}
```

**Failures** — `400` malformed, missing, mistyped or unknown field, or bad
`X-User-Id` · `415` unsupported `contentType` · `502` DynamoDB unavailable.

---

### List and search

```
GET /images
```

Returns a page of **ready** images, newest first. Pending and rejected images are
never listed.

**Query parameters** — all optional, combined with AND.

| Parameter | Example | Notes |
|---|---|---|
| `userId` | `alice` | **Indexed.** The only filter that turns the read into a Query |
| `tag` | `beach` | Matches one tag; case-insensitive |
| `contentType` | `image/png` | Exact match. An unsupported value is `415`, not an empty page |
| `filename` | `sun` | Case-insensitive substring |
| `uploadedFrom` | `2026-01-01T00:00:00Z` | Inclusive lower bound on `uploadedAt` |
| `uploadedTo` | `2026-12-31T23:59:59Z` | Inclusive upper bound on `uploadedAt` |
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
      "imageId": "946affab1178454d8127a89b932c4a12",
      "userId": "alice",
      "filename": "beach-sunset.png",
      "contentType": "image/png",
      "sizeBytes": 70,
      "checksumSha256": "c414cd0e204de974f73753c7e28d7638e7b3691bb8b1a2bab6b25bb7fed7ce77",
      "tags": ["beach", "sunset", "summer"],
      "description": "Golden hour at the beach",
      "status": "ready",
      "createdAt": "2026-09-13T10:15:07.402Z",
      "uploadedAt": "2026-09-13T10:15:08.960Z"
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

**`200 OK`** — the image in whatever state it is in. This is the endpoint to poll
after uploading: wait until `status` leaves `pending`.

Fields present only in some states:

| Field | Present when |
|---|---|
| `sizeBytes`, `checksumSha256`, `uploadedAt` | `ready` |
| `rejectionReason` | `rejected` |
| `description` | it was supplied at registration |

`404` if there is no such image. Storage-internal attributes (`s3Key`,
`s3Bucket`, `filenameLower`) and the expiry timestamp are never exposed.

---

### View or download the file

```
GET /images/{imageId}/content
```

Answers `302` with a `Location` pointing at a short-lived presigned S3 URL. The
bytes never pass through Lambda or API Gateway, so downloads are not subject to
Lambda's 6 MB response payload cap and cost no compute time proportional to file
size. The presigned URL carries a signed `Content-Type` and `Content-Disposition`,
so the browser still sees the original filename.

Only `ready` images have content. A `pending` or `rejected` image is a `409`
`ImageNotReady`; for a rejected image the message includes the reason.

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
`409` image pending or rejected · `502` S3 or DynamoDB unavailable.

---

### Delete an image

```
DELETE /images/{imageId}
```

```bash
curl -sS -X DELETE "$API/images/$ID" -H 'X-User-Id: alice' -o /dev/null -w '%{http_code}\n'
```

**`204 No Content`**, with no body. Works in any state; a pending image may have
no object yet, which is fine.

Deleting an image that is already gone returns **`404`, not `204`**. This is
deliberate: a client deleting the wrong id should hear about it rather than be
told the call succeeded. Callers that want idempotent semantics should treat
`404` on delete as success.

Delete wins against an in-flight upload. If the file for a deleted image lands
afterwards, the processor finds no metadata row and deletes the object, so a late
upload cannot bring the image back.

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

The index is also **sparse**: `uploadedAt` is written only when an upload is
verified, so pending and rejected images are simply absent from it. Listing by
user never reads them and needs no status filter.

```bash
# Query: reads only January's images for alice.
curl -sS "$API/images?userId=alice&uploadedFrom=2026-01-01T00:00:00Z&uploadedTo=2026-01-31T23:59:59Z"
```

**Everything else is a filter expression**, applied after the read. `tag`,
`contentType` and `filename` reduce the response payload but not the capacity
consumed. And with no `userId` there is no partition key to anchor on, so the
request falls back to a bounded, paginated Scan of the base table, which also
filters out images that are not `ready`:

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

Every API failure returns the same shape:

```json
{
  "error": "'contentType' must be one of: image/gif, image/jpeg, image/png, image/webp",
  "code": "UnsupportedMediaType"
}
```

Branch on `code`; `error` is for humans and its wording may change.

| Status | `code` | Meaning | Retry? |
|---|---|---|---|
| `400` | `ValidationError` | Malformed, missing, mistyped or unknown field; bad filter or `nextToken`; missing or malformed caller identity | No — fix the request |
| `404` | `NotFound` | No image with that id | No |
| `409` | `ImageNotReady` | Content requested for an image that is pending or rejected | Yes, if pending — poll `GET /images/{id}` first |
| `409` | `Conflict` | Image id already exists (not reachable in normal use) | No |
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

Errors from the direct S3 upload are S3's own XML responses; see
[What S3 refuses](#what-s3-refuses). An upload that S3 accepts but the processor
refuses is not an HTTP error at all: the image moves to `rejected`, with the
reason in `rejectionReason`.

---

## Client examples

Both examples were run verbatim against the LocalStack deployment — the
JavaScript one from a separate browser origin, so the bucket's CORS rule is
exercised too.

### Python

```python
import mimetypes, pathlib, time

import requests

API = "http://localhost:4566/restapis/<restApiId>/local/_user_request_"
SESSION = requests.Session()
SESSION.headers["X-User-Id"] = "alice"


def upload(path, tags=(), description=None, timeout=60):
    """Register, upload straight to S3, and wait for verification."""
    path = pathlib.Path(path)
    body = {
        "filename": path.name,
        "contentType": mimetypes.guess_type(path.name)[0],
        "tags": list(tags),
    }
    if description:
        body["description"] = description
    registered = SESSION.post(f"{API}/images", json=body, timeout=30)
    registered.raise_for_status()
    image, form = registered.json()["image"], registered.json()["upload"]
    if path.stat().st_size > form["maxSizeBytes"]:
        raise ValueError(f"{path} is larger than {form['maxSizeBytes']} bytes")

    # requests sends `data` fields before `files`, which is the order S3 needs.
    with path.open("rb") as handle:
        stored = requests.post(
            form["url"],
            data=form["fields"],
            files={form["fileField"]: (path.name, handle, body["contentType"])},
            timeout=60,
        )
    stored.raise_for_status()

    deadline = time.monotonic() + timeout
    while image["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(1)
        image = SESSION.get(f"{API}/images/{image['imageId']}", timeout=30).json()
    if image["status"] != "ready":
        raise RuntimeError(f"upload {image['status']}: {image.get('rejectionReason')}")
    return image


def search(**filters):
    """Yield every match, following the cursor until nextToken is null."""
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

Note the upload goes through plain `requests.post`, not `SESSION`: S3 does not
expect the `X-User-Id` header, and the form's signature is the only credential it
needs.

### Browser / JavaScript

```js
const API = "http://localhost:4566/restapis/<restApiId>/local/_user_request_";

async function upload(file, { tags = [], userId = "alice" } = {}) {
  const registered = await fetch(`${API}/images`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-User-Id": userId },
    body: JSON.stringify({
      filename: file.name,
      contentType: file.type,
      tags,
    }),
  });
  if (!registered.ok) {
    const { error, code } = await registered.json();
    throw new Error(`${code}: ${error}`);
  }
  const { image, upload } = await registered.json();
  if (file.size > upload.maxSizeBytes) {
    throw new Error(`File is larger than ${upload.maxSizeBytes} bytes`);
  }

  // Signed fields first, file last. Let the browser set the multipart boundary.
  const form = new FormData();
  for (const [name, value] of Object.entries(upload.fields)) form.append(name, value);
  form.append(upload.fileField, file);

  const stored = await fetch(upload.url, { method: "POST", body: form });
  if (!stored.ok) throw new Error(`S3 refused the upload: ${stored.status}`);
  return image;
}

async function waitUntilSettled(imageId, { timeoutMs = 60000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const image = await (await fetch(`${API}/images/${imageId}`)).json();
    if (image.status !== "pending") return image;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error("timed out waiting for the upload to be verified");
}

// From an <input type="file">:
//   const image = await upload(input.files[0], { tags: ["beach"] });
//   const settled = await waitUntilSettled(image.imageId);

// Render straight from the API: the browser follows the 302 to S3.
function thumbnail(imageId) {
  const img = document.createElement("img");
  img.src = `${API}/images/${imageId}/content`;
  return img;
}
```

`File.type` supplies `contentType` directly, and comparing `File.size` against
`upload.maxSizeBytes` fails an oversized file before any bytes are sent. Do not
set a `Content-Type` header on the S3 request yourself — the browser has to
generate the multipart boundary.

---

## Limits

| | Value | Configured by |
|---|---|---|
| Max image size | 20 MB (20 971 520 bytes) | `MAX_IMAGE_BYTES` / `var.max_image_bytes` |
| Upload form lifetime | 900 s | `UPLOAD_URL_TTL_SECONDS` / `var.upload_url_ttl_seconds` |
| Unfinished (`pending`) image lifetime | Form lifetime + 1 hour | `PENDING_GRACE_SECONDS` |
| Rejected image lifetime | 24 hours | `REJECTED_RETENTION_SECONDS` |
| Accepted types | jpeg, png, gif, webp | `ALLOWED_CONTENT_TYPES` |
| Filename length | 255 chars | — |
| Tags per image | 20, each ≤ 50 chars | — |
| Description length | 1024 chars | — |
| Page size | 25 default, 100 max | — |
| Download URL lifetime | 900 s default, 3600 s max | `DOWNLOAD_URL_TTL_SECONDS` / `var.download_url_ttl_seconds` |
| API request rate | 200/s sustained, 400 burst | `aws_api_gateway_method_settings` |

The image size limit is a product decision, not a transport constraint. Uploads
bypass API Gateway and Lambda entirely, so it can be raised to S3's 5 GB
single-request maximum without architectural change; the practical cost is the
processor's SHA-256 pass, which streams in 1 MiB chunks with flat memory.

Expiry uses DynamoDB TTL, which deletes expired rows within a few days rather than
at the exact second. An expired-but-not-yet-deleted `pending` row is harmless: it
is never listed, and its upload form has long since stopped working.

---

## Trying it out

**Swagger UI** — render `openapi.yaml` locally:

```bash
make docs
```

Serves the spec at <http://localhost:8080> with the LocalStack server
preselected. Every API endpoint can be tried from the page, including
`POST /images`. The step between — sending the file to S3 — is a request to a
different host with a form built from the response, which Swagger UI cannot
express; use the [walkthrough](#walkthrough) or a client example for that part.

**Postman / Insomnia** — import [`postman_collection.json`](postman_collection.json).
Set the `baseUrl` and `userId` collection variables and choose a PNG for the
upload request's `file` field. The collection registers the image, uploads the
file to S3 with the signed fields it captured, polls until the image is ready,
and then runs the read and delete requests top to bottom.

Two things trip up a first run:

- **Set the variable's *Current value*.** Postman sends the Current value, not
  the Initial value, so editing only the Initial column leaves requests pointed at
  the `REPLACE_ME` placeholder.
- **Refresh `baseUrl` after LocalStack restarts.** The REST API id in the URL is
  regenerated on every fresh deploy. An unknown id gets an empty `404` from
  LocalStack, which the collection reports as "baseUrl is wrong or stale".

**Command line** — `make seed` loads six sample images across three users through
the full upload flow, and `make smoke` runs 41 end-to-end assertions over the
deployed stack. They include S3 refusing uploads that are empty, over the size
limit, or carry a tampered type or key,
the processor rejecting a PDF sent as a PNG, and a byte-for-byte comparison of the
downloaded file.
