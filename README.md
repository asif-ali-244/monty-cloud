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
- **Tests**: 201 unit tests against [moto](https://github.com/getmoto/moto), 100% line coverage,
  plus OpenAPI contract tests and a live end-to-end smoke test

---

## Quick start

Requires Docker, Terraform ≥ 1.5 and Python 3.9+.

```bash
make install     # virtualenv + dev dependencies
make test        # 201 unit tests, no AWS or Docker needed
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

## API

Full reference: **[docs/API.md](docs/API.md)**. Machine-readable spec:
**[docs/openapi.yaml](docs/openapi.yaml)** (OpenAPI 3.0.3). Postman collection:
**[docs/postman_collection.json](docs/postman_collection.json)**.

| Method | Path | Purpose | Success |
|---|---|---|---|
| `POST` | `/images` | Upload an image with metadata | `201` |
| `GET` | `/images` | List and search | `200` |
| `GET` | `/images/{imageId}` | View metadata | `200` |
| `GET` | `/images/{imageId}/content` | View or download the file | `302` |
| `DELETE` | `/images/{imageId}` | Delete an image | `204` |

Search filters — combined with AND, all optional: `userId` (indexed), `tag`,
`contentType`, `filename`, `uploadedFrom`/`uploadedTo`, plus `limit` and
`nextToken` for cursor pagination.

The caller is identified by the `X-User-Id` header, a development stand-in for
an authorizer. Errors are uniform — `{"error": "…", "code": "…"}` — with the
status codes listed in [the error reference](docs/API.md#errors).

```bash
export API=$(terraform -chdir=terraform output -raw api_base_url)

curl -sS -X POST "$API/images" -H 'Content-Type: application/json' -H 'X-User-Id: alice' \
  -d "{\"filename\":\"sunset.png\",\"contentType\":\"image/png\",
       \"imageBase64\":\"$(base64 < sunset.png | tr -d '\n')\",\"tags\":[\"beach\"]}"

curl -sS "$API/images?userId=alice&tag=beach&limit=10"
curl -sSL "$API/images/$ID/content" -o downloaded.png
curl -sS -X DELETE "$API/images/$ID" -H 'X-User-Id: alice'
```

Browse the spec in Swagger UI, with the LocalStack server preselected so requests
can be fired from the page:

```bash
make docs   # http://localhost:8080
```

The spec is not decoration: `tests/test_openapi_contract.py` validates every real
handler response against its documented schema and checks every documented limit
and enum against the constant the code enforces, so the docs cannot drift from
the implementation without a test failing.
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
make test    # 201 tests, ~18s
make cov     # with a coverage report
make lint    # ruff + terraform fmt
```

Unit tests run entirely against moto — no Docker, no credentials, no network.
They cover the five handlers end to end, the service and repository layers, all
validation rules, pagination, every filter combination, multi-user concurrency,
and the AWS-failure paths (each `ClientError` mapped to its HTTP status,
including the compensating delete and the double-failure case).

`tests/test_openapi_contract.py` keeps the documentation honest: it validates
`docs/openapi.yaml` as an OpenAPI document, validates every example in it against
its own schema, validates real handler responses against their documented
schemas, and asserts that every documented limit, default and enum matches the
constant the code actually enforces.

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
tests/          201 unit tests, including the OpenAPI contract suite
scripts/        package.sh, seed_local.py, smoke_test.py
docs/           API.md (reference), openapi.yaml (OpenAPI 3.0.3), postman_collection.json
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
