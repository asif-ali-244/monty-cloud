# monty-cloud — Image Service

Service layer for image upload and storage: API Gateway → Lambda → S3 (bytes) +
DynamoDB (metadata), with Terraform and LocalStack for a full local stack.

```
                 ┌──────────────────────────────────────────────┐
   client ──────▶│  API Gateway (REST, regional)                │
                 └───┬────────┬────────┬─────────┬──────────┬───┘
                     │        │        │         │          │
                POST /images  │   GET /images/{id}      DELETE /images/{id}
                     │   GET /images    │    GET /images/{id}/content
                     ▼        ▼         ▼         ▼          ▼
                 ┌────────┐┌──────┐┌────────┐┌──────────┐┌────────┐
                 │ upload ││ list ││  get   ││ download ││ delete │  5 Lambdas,
                 └───┬────┘└──┬───┘└───┬────┘└────┬─────┘└───┬────┘  one IAM role
                     │        │        │          │          │       each
        ┌────────────┴────────┴────────┴──────────┴──────────┴───┐
        │                                                        │
        ▼                                                        ▼
  ┌───────────────┐                                     ┌─────────────────┐
  │ S3            │◀── presigned GET (302, direct) ──── │ DynamoDB        │
  │ image bytes   │                                     │ metadata        │
  └───────────────┘                                     │ + userId GSI    │
                                                        └─────────────────┘
```

- **Language / runtime**: Python (Lambda `python3.12`; source targets 3.7+ syntax)
- **Infrastructure**: Terraform, one configuration for both LocalStack and AWS
- **Tests**: 177 unit tests against [moto](https://github.com/getmoto/moto), 100% line coverage,
  plus a live end-to-end smoke test

---

## Quick start

Requires Docker, Terraform ≥ 1.5 and Python 3.9+.

```bash
make install     # virtualenv + dev dependencies
make test        # 177 unit tests, no AWS or Docker needed
make up          # start LocalStack, wait for health
make deploy      # package the Lambdas and terraform apply
make seed        # load six sample images
make smoke       # end-to-end check of every endpoint
```

`make deploy` prints the base URL. Capture it for the examples below:

```bash
export API=$(terraform -chdir=terraform output -raw api_base_url)
# http://localhost:4566/restapis/<rest-api-id>/local/_user_request_
```

Tear down with `make destroy && make down`. `make help` lists every target.

---

## API reference

Base path: `{API}`. All request and response bodies are JSON.

The caller is identified by the **`X-User-Id`** header. In a deployed
environment this is replaced by an API Gateway authorizer — the handlers already
read `requestContext.authorizer.claims.sub` in preference to the header, so
attaching Cognito requires no application change.

### `POST /images` — upload an image with metadata

| Field | Type | Required | Notes |
|---|---|---|---|
| `filename` | string | yes | ≤ 255 chars; any directory component is stripped |
| `contentType` | string | yes | `image/jpeg`, `image/png`, `image/gif`, `image/webp` |
| `imageBase64` | string | yes | Base64, or a `data:image/png;base64,…` URL. ≤ 5 MB decoded |
| `tags` | string[] | no | ≤ 20 tags, `[a-z0-9][a-z0-9_-]*`; lowercased and de-duplicated |
| `description` | string | no | ≤ 1024 chars |

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

`201 Created`, with `Location: /images/{imageId}`:

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

The declared `contentType` is checked against the file's magic bytes, so a
renamed PDF is rejected with `400`, not stored.

### `GET /images` — list and search

| Query parameter | Example | Behaviour |
|---|---|---|
| `userId` | `alice` | **Indexed.** Queries the GSI; results are newest-first |
| `uploadedFrom` / `uploadedTo` | `2024-05-01T00:00:00Z` | ISO-8601. A key condition when combined with `userId`, otherwise a filter |
| `tag` | `beach` | Matches one tag, case-insensitive |
| `contentType` | `image/png` | Exact match |
| `filename` | `sun` | Case-insensitive substring |
| `limit` | `25` | 1–100, default 25 |
| `nextToken` | *(opaque)* | Cursor from the previous page |

Filters combine with AND.

```bash
curl -sS "$API/images?userId=alice&tag=beach&limit=10"
curl -sS "$API/images?uploadedFrom=2026-01-01T00:00:00Z&contentType=image/png"
```

```json
{
  "items": [ { "imageId": "…", "filename": "beach-sunset.png", "…": "…" } ],
  "count": 1,
  "nextToken": "eyJpbWFnZUlkIjogIjMzNGQzYjQ3…"
}
```

`nextToken` is `null` on the last page. Pass it back verbatim to continue:

```bash
curl -sS "$API/images?userId=alice&limit=2&nextToken=$TOKEN"
```

### `GET /images/{imageId}` — view metadata

```bash
curl -sS "$API/images/334d3b476eba4fbfa2b00e2aa4792a8a"
```

`200` with the same object as the upload response, or `404`.

### `GET /images/{imageId}/content` — view or download the file

Returns `302` to a presigned S3 URL, so the bytes never pass through Lambda or
API Gateway.

| Query parameter | Default | Behaviour |
|---|---|---|
| `disposition` | `inline` | `attachment` makes the browser save rather than render |
| `expiresIn` | `900` | URL lifetime in seconds, capped at 3600 |
| `redirect` | `true` | `false` returns the URL as JSON instead of redirecting |

```bash
curl -sSL "$API/images/$ID/content" -o downloaded.png          # follow the redirect
curl -sS  "$API/images/$ID/content?disposition=attachment" -D - # inspect the 302
curl -sS  "$API/images/$ID/content?redirect=false"              # get the URL as JSON
```

```json
{
  "downloadUrl": "http://localhost:4566/monty-images-local-bucket/images/alice/…",
  "expiresInSeconds": 900,
  "image": { "imageId": "…", "…": "…" }
}
```

### `DELETE /images/{imageId}` — delete

```bash
curl -sS -X DELETE "$API/images/$ID" -o /dev/null -w '%{http_code}\n'
```

`204` on success, `404` if it was already gone. Deleting twice is deliberately
**not** treated as success — a client that deletes the wrong id should hear about it.

### Errors

Every failure returns the same shape with an appropriate status:

```json
{ "error": "'contentType' must be one of: image/gif, image/jpeg, image/png, image/webp",
  "code": "UnsupportedMediaType" }
```

| Status | `code` | Cause |
|---|---|---|
| 400 | `ValidationError` | Malformed body, bad filter, bad `nextToken`, missing caller id |
| 404 | `NotFound` | No such image |
| 409 | `Conflict` | Image id collision |
| 413 | `PayloadTooLarge` | Image over `MAX_IMAGE_BYTES` |
| 415 | `UnsupportedMediaType` | `contentType` not an accepted image type |
| 502 | `StorageError` | S3 or DynamoDB call failed |
| 500 | `InternalError` | Unexpected — the message carries a request id for the logs, nothing else |

---

## Data model

**S3** — `images/{userId}/{imageId}.{ext}`. The id is server-generated, so
concurrent uploads of the same filename by the same user never collide, and a
client cannot influence the key.

**DynamoDB** — one item per image:

| Attribute | | |
|---|---|---|
| `imageId` | string | **Partition key.** Every single-image read is by id |
| `userId` | string | GSI partition key |
| `uploadedAt` | string | GSI sort key; ISO-8601, millisecond precision, always UTC |
| `filename`, `filenameLower` | string | The second is the case-insensitive search field |
| `contentType`, `sizeBytes`, `checksumSha256` | | |
| `tags` | list | Normalised, de-duplicated |
| `description` | string | Present only when supplied |
| `s3Key`, `s3Bucket` | string | Internal; stripped before anything is returned |

The single GSI, `userId-uploadedAt-index`, exists for the dominant access
pattern of a photo service: *this user's images, newest first, optionally within
a date window*. Because `uploadedAt` sorts lexicographically in the canonical
form, the date range is a **key condition** rather than a post-read filter, so
that query stays proportional to the number of results returned.

---

## Design decisions

**Bytes go to S3 before metadata goes to DynamoDB.** The failure window has to
land somewhere. This way an interruption leaves an object nobody references —
invisible to every API, logged as `orphaned_object`, reclaimable by a
reconciliation sweep. The reverse order would leave a metadata row pointing at
bytes that were never written, and every subsequent read of that image would
fail. The upload path also issues a compensating delete when the metadata write
fails, so the orphan case is rare rather than routine.

**Deletes reverse that order.** The metadata row is the source of truth for
whether an image exists, so it goes first: once it is gone the image is gone as
far as the API is concerned. If the S3 delete then fails the object is orphaned
and logged — strictly better than a live row whose bytes have already vanished.

**Downloads are a 302 to a presigned URL, not a proxied body.** Streaming a 5 MB
image back through Lambda would pay for the transfer twice, burn compute time
proportional to file size, and run into API Gateway's 6 MB response cap. The
presigned URL also carries signed `Content-Type` and `Content-Disposition`, so
the browser still gets the original filename.

**Uploads are base64 in the request body.** This keeps the required "upload
image with metadata" a single atomic call, which is the right shape at this
size. It does inherit API Gateway's 10 MB request limit, and base64 inflates
payloads by a third — hence the 5 MB ceiling. The scaling path is a presigned
`PUT`: the client registers metadata, uploads straight to S3, and an S3 event
marks the row complete. That removes the size ceiling and the Lambda from the
data path entirely, at the cost of a two-phase client and pending-state cleanup.
Worth it above a few MB or a few hundred uploads a second; not worth it here.

**Listing without a `userId` is a Scan, and says so.** With no partition key
there is nothing to query. A Scan is bounded by `limit` and paginated, which is
fine for an admin view or a small corpus, but it reads the table. Two ways out
at scale: a sharded GSI (`bucket = hash(imageId) % N` as the partition, `uploadedAt`
as the sort key) for a global reverse-chronological feed, or a search index
(OpenSearch) if tag and filename search need to be first-class. Both are
significant additions; neither is justified by the current requirement.

**Tag filtering is a `FilterExpression`, not an index.** Filters are applied
after the read, so they cut payload but not consumed capacity. Making tags
indexed means one item per image-tag pair in a `tag → imageId` adjacency
structure, which doubles write cost and adds a fan-out read. The current corpus
does not justify it; the model would extend cleanly if it did.

**One IAM role per function.** `list-images` cannot delete; `download-image`
cannot write. A bug in any single handler cannot reach past what that endpoint
legitimately does. The five roles cost nothing and are derived from one map in
`terraform/lambda.tf`, so adding an endpoint means adding one entry.

**On-demand DynamoDB billing.** Many users uploading at once is exactly the
bursty, hard-to-forecast pattern that provisioned capacity handles badly.

**One shared deployment zip.** The package holds no third-party code — boto3 is
in the runtime — so it is 16 KB. Splitting it per function would make deploys
five times slower and buy nothing. `scripts/package.sh` normalises timestamps so
an unchanged tree hashes identically and Terraform skips the redeploy.

**Clients are created lazily and memoised.** Warm invocations reuse the
connection; tests reset the cache between cases. Configuration is read per call
rather than at import so tests can vary it without reimporting modules.

---

## Testing

```bash
make test    # 177 tests, ~17s
make cov     # with a coverage report
make lint    # ruff + terraform fmt
```

Unit tests run entirely against moto — no Docker, no credentials, no network.
They cover the five handlers end to end, the service and repository layers, all
validation rules, pagination, every filter combination, multi-user concurrency,
and the AWS-failure paths (each `ClientError` mapped to its HTTP status,
including the compensating delete and the double-failure case).

Run a single test or a subset:

```bash
.venv/bin/python -m pytest tests/test_upload_image.py
.venv/bin/python -m pytest tests/test_list_images.py::test_filters_by_tag
.venv/bin/python -m pytest -k "concurrent or lifecycle" -v
```

`make smoke` is the live counterpart: it drives the deployed API over HTTP,
including following the presigned redirect and byte-comparing the downloaded
file against what was uploaded. It passes against LocalStack (26 checks).

---

## Deploying to real AWS

The same configuration targets an AWS account:

```bash
make package
terraform -chdir=terraform apply -var use_localstack=false -var environment=dev
```

`use_localstack=false` drops the endpoint overrides and the credential-check
skips, enables point-in-time recovery and CloudWatch metrics, restores the S3
lifecycle rule, and turns off `force_destroy` on the bucket. Before doing this
for real: attach an authorizer to the API Gateway methods (see
`terraform/apigateway.tf`), and move Terraform state to a remote backend.

---

## Layout

```
src/
  handlers/     one module per endpoint - parse the event, call a service, shape a response
  services/     image_service (business logic), metadata_repository (DynamoDB), object_store (S3)
  common/       config, errors, validation, response builders, the handler decorator
terraform/      storage.tf, lambda.tf (functions + IAM), apigateway.tf, variables, outputs
tests/          177 unit tests
scripts/        package.sh, seed_local.py, smoke_test.py
```

Handlers are 5–20 lines. The `@api_handler` decorator in
`src/common/middleware.py` is the global try/catch: it maps `AppError` subclasses
to their status codes, logs unexpected exceptions with a stack trace, and returns
an opaque 500 carrying only a request id — so a Lambda cannot crash, and an
internal message cannot leak.

---

## Known limitations

- **No authentication.** `X-User-Id` is trusted, which is fine for an assessment
  and not for anything else. The handler-side plumbing for a real authorizer is
  already in place.
- **No authorisation.** Any caller can read or delete any image by id. Ownership
  is recorded but not enforced; enforcing it is a conditional expression on the
  delete and an ownership check on the reads.
- **Global listing scans.** See the design note above.
- **No thumbnails.** A production service would fan out from an S3 event to a
  resize function and store derivative keys on the metadata row.
- **Orphan reconciliation is not implemented.** Orphans are logged, not swept.
- **LocalStack skips two resources** (S3 lifecycle, PITR) that apply on AWS.
