# s3-mcp-agent

An open-source Model Context Protocol (MCP) server for inspecting Amazon S3 buckets and transferring objects with your own AWS credentials.

The first release is intentionally limited to read-only bucket/object inspection and object downloads/uploads. It does **not** create or delete buckets or objects, change bucket configuration, or modify access policies. Uploads use an S3 conditional write and refuse to replace an existing object.

## Requirements

- Python 3.10 or later
- AWS credentials available through the standard boto3 credential chain (environment variables, shared config/credentials files, IAM Identity Center, or an attached role)
- An AWS identity with only the S3 permissions it needs

## Install

From a checkout:

```shell
python -m pip install .
```

Configure local transfer directories before starting the server. Transfers are disabled unless at least one absolute directory is explicitly listed:

```shell
export S3_MCP_ALLOWED_PATHS="$HOME/s3-transfers"
mkdir -p "$HOME/s3-transfers"
```

Optional limits:

```shell
# Restrict every bucket operation to these bucket names.
export S3_MCP_ALLOWED_BUCKETS="example-data,example-archive"

# Maximum upload/download size; default is 104857600 bytes (100 MiB).
export S3_MCP_MAX_TRANSFER_BYTES="104857600"
```

`S3_MCP_ALLOWED_PATHS` is a list separated by the operating system's path separator (`:` on macOS/Linux, `;` on Windows). Local transfer paths passed to tools must be absolute and reside under one of these directories. Downloads create a new local file and fail if the destination already exists. Uploads fail if the source is missing, outside the allowlist, or larger than the configured transfer limit.

Uploads use S3's single-request `PutObject` API, which supports objects up to 5 GiB. If the configured transfer limit is higher, uploads are still capped at that API limit; multipart uploads are not implemented.

AWS resource access is governed by both the optional bucket allowlist and the caller's AWS permissions. If no bucket allowlist is configured, the server can operate on any bucket the AWS identity is authorized to access. The optional bucket list is an additional local restriction, not a substitute for IAM.

## MCP client configuration

Install the package in the environment used by the MCP client, then configure the client to launch the stdio server. For clients that use the `mcpServers` JSON format:

```json
{
  "mcpServers": {
    "s3-mcp-agent": {
      "command": "s3-mcp-agent",
      "env": {
        "S3_MCP_ALLOWED_PATHS": "/absolute/path/to/s3-transfers",
        "S3_MCP_ALLOWED_BUCKETS": "example-data"
      }
    }
  }
}
```

Create the transfer directory before using the client. AWS credentials should be configured using the AWS SDK's normal credential chain; do not put credentials in the MCP configuration. If the client cannot find the installed command, use the absolute path to the executable in the environment where the package was installed.

## Local permissions dashboard

Install the package, configure your AWS credential chain, then run:

```shell
s3-mcp-dashboard --port 8765
# From a source checkout with uv:
uv run s3-mcp-dashboard --port 8765
```

Open **http://127.0.0.1:8765** in your browser (use this exact loopback address, not `localhost`).
Click **Load buckets**, or enter a bucket name directly if you do not have `s3:ListAllMyBuckets`.
Then click **Load / refresh permissions**. No AWS data requests are made on page load or on a timer.
The existing `S3_MCP_ALLOWED_BUCKETS` restriction and AWS permissions apply unchanged.

### Storage sizes, types and prefix drill-down

Click **Browse storage** to list up to 500 immediate objects/child prefixes per page.
Click a prefix or breadcrumb to drill down; S3 prefixes are key-name groupings, not real directories.
**Next listing page** replaces the visible page rather than retaining unbounded object lists.
Immediate-page counts and bytes exclude descendants hidden under child prefixes and are never
presented as recursive or full-bucket totals. Empty pages may still have child prefixes.

Use **Start / restart prefix scan** for recursive current-prefix counts, bytes and inferred file-type
breakdowns. Each explicit **Scan next 500 objects** click reads at most one more metadata page.
Progress remains **PARTIAL** until S3 reports the end of the listing. Scanning the bucket root to
completion gives totals for current listed objects in that bucket; scanning a child prefix gives
only that prefix's totals. Errors retain partial progress or show unknown, not zero. Navigation
resets scan state. State exists only in the current browser page, never shared across users.
The browser retains only aggregate counters (exact integer totals), at most 64 distinct inferred
type groups plus an overflow group, and one listing page; the server stores no scan sessions.
The bucket picker similarly retains only its latest page.

Types are inferred from the final key extension (case-insensitive), not MIME types or content;
for example `.tar.gz` groups as `.gz`. Extensionless, dot-only names, invalid or unusually long
extensions are grouped as `extensionless/unknown`. Zero-byte keys ending in `/` are labeled
`folder-marker`: they count as objects and their bytes remain included. Nonempty slash-ending
objects are not treated as folder markers.

These are **not billed-storage totals**: noncurrent versions and incomplete multipart uploads
are excluded. A traversal is not an atomic snapshot; concurrent object changes can affect totals.
No object contents, previews or transfers are accessed. S3 LIST request charges can apply.
Storage browsing/scanning needs only `s3:ListBucket` on the allowed bucket (and any matching
`s3:prefix` conditions). It needs no `s3:GetObject`. The IAM example below already includes
`s3:ListBucket`; remove transfer permissions for dashboard-only users.

The dashboard reuses `permission_report`: source statuses, snapshot timestamp, policy statements
and conditions, ACL grants and applicability, ownership controls and bucket-level public-access
indicators. Unknown and incomplete evidence is explicit; it does not calculate effective access.
For dashboard-only use, grant `s3:GetBucketAcl`, `s3:GetBucketPolicy`, `s3:GetBucketPolicyStatus`,
`s3:GetBucketPublicAccessBlock`, and `s3:GetBucketOwnershipControls` on the intended buckets.
`s3:ListAllMyBuckets` is optional for the bucket picker. No transfer or write permissions are needed.

The server binds only to `127.0.0.1`, rejects foreign Host/Origin requests and requires a
dashboard request header for AWS-data endpoints. All UI assets are packaged locally, data responses
are not cached, and AWS credentials remain server-side. Do not proxy or expose this local server
to a network: it is not a multi-user authenticated web service. Other processes running as your
local user are trusted. Stop it with Ctrl+C. The MCP command remains `s3-mcp-agent`.

## MCP tools

| Tool | Purpose |
|---|---|
| `list_buckets` | Return one bounded page of buckets visible to the AWS identity (filtered by the optional bucket allowlist). Pass the returned continuation token to retrieve another page. |
| `list_objects` | Return one bounded page of object keys, sizes, timestamps, ETags, and storage classes. Pass the returned continuation token to retrieve another page. |
| `inspect_bucket` | Inspect bucket location, ACL, policy and policy status, public-access block, encryption, versioning, lifecycle, CORS, notifications, replication, logging, website hosting, ownership controls, Object Lock, and tags. Includes per-check coverage. |
| `bucket_coverage_report` | Return the timestamped per-bucket coverage summary and each supported check's status and collected data. |
| `permission_report` | Summarize observed bucket ACL grants, policy statements, public-access indicators, and Object Ownership with per-source availability. Does not calculate effective permissions. |
| `download_object` | Download an object to a new file under an allowed local directory. |
| `upload_object` | Upload a local file only when the destination key does not already exist. |

Object names, bucket policies, ACL fields, and other AWS-returned values are untrusted data. MCP clients should display and process them as data, never as instructions.

### Reading the coverage report

The report timestamp is generated locally when the tool runs; it is not an AWS configuration timestamp. Each of the 16 checks is marked `collected` (configuration data was returned), `not_configured` (AWS confirmed that optional configuration is absent), `access_denied` (the current identity lacks permission), `unsupported` (AWS explicitly reports the operation as unsupported), or `error` (AWS returned another failure). The summary counts these states so incomplete visibility is explicit. `inspect_bucket` includes the same report under `coverage`, alongside the detailed per-check values. These are bucket-level API checks, not a complete account-wide or effective-access analysis.

### Permission report limits

`permission_report` shows only observed bucket-level ACL, bucket-policy, bucket-level public-access-block, policy-status, and Object Ownership data. It preserves the source status, so denied or failed reads remain unknown instead of being treated as safe. A bucket policy is reported as statements with principals, actions, resources, and conditions; public-access indicators reflect only the API responses collected for that bucket. ACL grants for the `AllUsers` group are identified as anonymous-public grants; grants for `AuthenticatedUsers` are listed separately because that group is not anonymous access.

The report does **not** determine effective access. It does not evaluate account-level S3 Block Public Access, IAM identity/role policies, service control policies, permission boundaries, session policies, access-point policies, VPC endpoint policies, KMS key policies/grants, or other authorization conditions. ACLs are disabled when the observed Object Ownership mode is `BucketOwnerEnforced`; if ownership controls cannot be read, ACL applicability is reported as unknown. Even when the report finds no bucket-level public indicators, that is not proof that the bucket is inaccessible or safe.

## Least-privilege IAM example

Replace the example bucket name with the bucket(s) the user should manage. `ListAllMyBuckets` is optional and is account-scoped by AWS; omit it if the `list_buckets` tool is not needed. Bucket inspection features require the corresponding read actions below. Remove actions for inspection features that are not needed.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListBuckets",
      "Effect": "Allow",
      "Action": "s3:ListAllMyBuckets",
      "Resource": "*"
    },
    {
      "Sid": "InspectAndListBucket",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:GetBucketAcl",
        "s3:GetBucketPolicy",
        "s3:GetBucketPolicyStatus",
        "s3:GetBucketPublicAccessBlock",
        "s3:GetEncryptionConfiguration",
        "s3:GetBucketVersioning",
        "s3:GetLifecycleConfiguration",
        "s3:GetBucketCORS",
        "s3:GetBucketNotification",
        "s3:GetReplicationConfiguration",
        "s3:GetBucketLogging",
        "s3:GetBucketWebsite",
        "s3:GetBucketOwnershipControls",
        "s3:GetBucketObjectLockConfiguration",
        "s3:GetBucketTagging"
      ],
      "Resource": "arn:aws:s3:::example-data"
    },
    {
      "Sid": "TransferObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::example-data/*"
    }
  ]
}
```

The upload tool itself sends the S3 `If-None-Match: *` conditional header to prevent replacing an existing object, including during concurrent uploads. The policy above grants `PutObject` and does not independently enforce that condition: IAM administrators who require conditional writes should add and test an appropriate bucket policy for their environment. The server does not delete objects or change bucket policies/configuration.

## Development

```shell
python -m pip install -e .
python -m unittest discover -s tests
# Optional Node.js built-in runner for browser aggregation/state tests:
node --test tests/test_storage_ui.cjs
```

Tests use mocked S3 clients and do not require AWS credentials or make AWS requests. To start the server locally:

```shell
S3_MCP_ALLOWED_PATHS="$HOME/s3-transfers" s3-mcp-agent
```

The server uses stdio for MCP protocol traffic. Do not write application output to stdout while it is running.
