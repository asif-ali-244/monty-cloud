# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Serverless AWS Application

A cloud-native backend application deployed using AWS Lambda, API Gateway, DynamoDB, and S3, with LocalStack for local development.

## Tech Stack
- AWS Lambda Python3.7+

## Requirements

You are developing the service layer of an application like Instagram. You are working on a module which is responsible for supporting image upload and storage in the Cloud. Along with the image, its metadata must be persisted in a NoSQL storage. Multiple users are going to use this service at the same time. For this reason, the service should be scalable.
The team currently uses API Gateway, Lambda Functions, S3 and DynamoDB services.
Language: Python3.7+
Tasks:
1. Create APIs for:
  - Uploading image with metadata
  - List all images, support at least two filters to search
  - View/download image
  - Delete an image
2. Write unit tests to cover all scenarios
3. The repository should follow the structure used by serverless applications. Use proper data validation and type checks.
3. API documentation and usage instructions

## Code Style & Architecture

* **Lambda Handlers**: Keep the handler file clean. Handlers must only parse the API Gateway event, call a core business logic function from `/src/services/`, and return an API Gateway-compatible proxy response format (statusCode, headers, body).
* **Environment Variables**: Never hardcode table names or bucket names. Access them strictly via environment variables.
* **Error Handling**: Wrap handler logic in a global try/catch block. Return a structured JSON error response (`{ "error": "ErrorMessage" }`) with appropriate HTTP status codes rather than letting the Lambda crash.
* Write proper unit tests

## Development Environment and Infrastructure

We will use LocalStack image to emulate AWS services. Use Terraform with localstack to create the aws infrastructure locally.

### Local Emulation (LocalStack)
* Start local infrastructure: `docker-compose up -d`
* Verify local AWS status: `awslocal status services`/pl
* Clear local DynamoDB data: `awslocal dynamodb delete-table --table-name LocalTable && npm run seed:local`

