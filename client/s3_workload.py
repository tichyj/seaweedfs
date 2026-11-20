#!/usr/bin/env python3
"""Exercises the SeaweedFS S3 API strictly from the client perspective.

This script only ever speaks S3 to the published endpoint. It has no notion of
masters, volume servers or filers - which is the point: a tenant application
must be able to do its whole job through the one protocol we expose at the
edge.

Subcommands:
  setup    create the bucket (admin identity)
  write    write an object of a given size, print its sha256
  verify   read an object back and compare against an expected sha256
  rw-check quick end-to-end write+read+delete, used as a liveness assertion
  authz    negative tests: read-only key, anonymous access, foreign bucket
  full     setup + a representative workload + authz
  cleanup  remove objects and the bucket
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import urllib.error
import urllib.request

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config
from botocore.exceptions import ClientError

ENDPOINT = os.environ.get("S3_ENDPOINT", "http://s3-lb:8333")
BUCKET = os.environ.get("S3_BUCKET", "poc-bucket")

RW_KEY = os.environ.get("S3_ACCESS_KEY", "poc-app-rw")
RW_SECRET = os.environ.get("S3_SECRET_KEY", "poc-app-rw-secret-change-me")
RO_KEY = os.environ.get("S3_RO_ACCESS_KEY", "poc-app-ro")
RO_SECRET = os.environ.get("S3_RO_SECRET_KEY", "poc-app-ro-secret-change-me")
ADMIN_KEY = os.environ.get("S3_ADMIN_ACCESS_KEY", "poc-admin")
ADMIN_SECRET = os.environ.get("S3_ADMIN_SECRET_KEY", "poc-admin-secret-change-me")

# 5 MiB is the S3 minimum part size, so an 8 MiB object is guaranteed to be
# split into several parts and therefore spread over several volumes.
MULTIPART_CHUNK = 5 * 1024 * 1024
TRANSFER = TransferConfig(
    multipart_threshold=MULTIPART_CHUNK,
    multipart_chunksize=MULTIPART_CHUNK,
    max_concurrency=4,
)

_failures: list[str] = []


def log(status: str, message: str) -> None:
    print(f"  [{status:^6}] {message}", flush=True)


def check(condition: bool, message: str) -> bool:
    if condition:
        log("ok", message)
    else:
        log("FAIL", message)
        _failures.append(message)
    return condition


def client(access_key: str, secret_key: str):
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            # No wildcard DNS in front of the gateway, so bucket names go in
            # the path, not the hostname.
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=60,
            # Lowered by the quorum test, which asserts that a request FAILS
            # and should not spend a minute retrying to find that out.
            retries={
                "max_attempts": int(os.environ.get("S3_MAX_ATTEMPTS", "5")),
                "mode": "standard",
            },
        ),
    )


def deterministic_body(size_bytes: int, seed: bytes) -> bytes:
    """Reproducible pseudo-random payload, so checksums are meaningful."""
    out = bytearray()
    block = seed
    while len(out) < size_bytes:
        block = hashlib.sha256(block).digest()
        out.extend(block)
    return bytes(out[:size_bytes])


def error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def status_code(exc: ClientError) -> int:
    return int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_setup(_args) -> None:
    s3 = client(ADMIN_KEY, ADMIN_SECRET)
    try:
        s3.create_bucket(Bucket=BUCKET)
        log("ok", f"bucket {BUCKET} created")
    except ClientError as exc:
        if error_code(exc) in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            log("ok", f"bucket {BUCKET} already exists")
        else:
            raise
    s3.head_bucket(Bucket=BUCKET)
    log("ok", f"bucket {BUCKET} reachable via {ENDPOINT}")


def cmd_write(args) -> None:
    size = int(args.size_mb * 1024 * 1024)
    body = deterministic_body(size, args.key.encode())
    digest = hashlib.sha256(body).hexdigest()

    s3 = client(RW_KEY, RW_SECRET)
    s3.upload_fileobj(io.BytesIO(body), BUCKET, args.key, Config=TRANSFER)

    head = s3.head_object(Bucket=BUCKET, Key=args.key)
    check(head["ContentLength"] == size, f"{args.key}: stored length {size} B")
    log("ok", f"{args.key}: wrote {args.size_mb} MiB, sha256={digest[:16]}...")
    # Printed on its own line so shell callers can capture it.
    print(digest)


def cmd_verify(args) -> None:
    s3 = client(RW_KEY, RW_SECRET)
    body = s3.get_object(Bucket=BUCKET, Key=args.key)["Body"].read()
    digest = hashlib.sha256(body).hexdigest()
    check(
        digest == args.sha256,
        f"{args.key}: read back {len(body)} B, sha256 matches "
        f"(expected {args.sha256[:16]}..., got {digest[:16]}...)",
    )


def cmd_rw_check(args) -> None:
    key = args.key
    body = deterministic_body(64 * 1024, key.encode())
    digest = hashlib.sha256(body).hexdigest()

    s3 = client(RW_KEY, RW_SECRET)
    s3.put_object(Bucket=BUCKET, Key=key, Body=body)
    got = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    check(hashlib.sha256(got).hexdigest() == digest, f"{key}: write+read round trip")
    s3.delete_object(Bucket=BUCKET, Key=key)


def cmd_workload(_args) -> None:
    s3 = client(RW_KEY, RW_SECRET)

    small = deterministic_body(4096, b"small")
    s3.put_object(Bucket=BUCKET, Key="objects/small.bin", Body=small)
    got = s3.get_object(Bucket=BUCKET, Key="objects/small.bin")["Body"].read()
    check(got == small, "single-part PUT/GET round trip")

    large = deterministic_body(8 * 1024 * 1024, b"large")
    s3.upload_fileobj(io.BytesIO(large), BUCKET, "objects/large.bin", Config=TRANSFER)
    got = s3.get_object(Bucket=BUCKET, Key="objects/large.bin")["Body"].read()
    check(got == large, "multipart upload (8 MiB / 5 MiB parts) round trip")

    ranged = s3.get_object(
        Bucket=BUCKET, Key="objects/large.bin", Range="bytes=1048576-1048585"
    )["Body"].read()
    check(ranged == large[1048576:1048586], "ranged GET across a chunk boundary")

    listed = s3.list_objects_v2(Bucket=BUCKET, Prefix="objects/")
    names = sorted(o["Key"] for o in listed.get("Contents", []))
    check(
        names == ["objects/large.bin", "objects/small.bin"],
        f"ListObjectsV2 returned {names}",
    )

    s3.delete_object(Bucket=BUCKET, Key="objects/small.bin")
    try:
        s3.head_object(Bucket=BUCKET, Key="objects/small.bin")
        check(False, "DELETE removed the object")
    except ClientError as exc:
        check(status_code(exc) == 404, "DELETE removed the object (HEAD now 404)")


def cmd_authz(_args) -> None:
    """The edge must enforce identity, not just reachability."""
    ro = client(RO_KEY, RO_SECRET)

    ro.list_objects_v2(Bucket=BUCKET)
    log("ok", "read-only identity can LIST (positive control)")

    try:
        ro.put_object(Bucket=BUCKET, Key="objects/should-not-exist", Body=b"nope")
        check(False, "read-only identity is refused on PUT")
    except ClientError as exc:
        check(
            status_code(exc) == 403,
            f"read-only identity is refused on PUT ({error_code(exc)})",
        )

    rw = client(RW_KEY, RW_SECRET)
    try:
        rw.list_objects_v2(Bucket="some-other-tenant")
        check(False, "app identity is refused outside its own bucket")
    except ClientError as exc:
        check(
            status_code(exc) in (403, 404),
            f"app identity is refused outside its own bucket ({error_code(exc)})",
        )

    try:
        bad = client(RW_KEY, "completely-wrong-secret")
        bad.list_objects_v2(Bucket=BUCKET)
        check(False, "a bad secret is rejected")
    except ClientError as exc:
        check(status_code(exc) == 403, f"a bad secret is rejected ({error_code(exc)})")

    # Unsigned request: there is no `anonymous` identity in s3-identities.json,
    # so the gateway must refuse this outright.
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/{BUCKET}/", timeout=10) as resp:
            check(False, f"anonymous GET is refused (got HTTP {resp.status})")
    except urllib.error.HTTPError as exc:
        check(exc.code in (401, 403), f"anonymous GET is refused (HTTP {exc.code})")


def cmd_cleanup(_args) -> None:
    s3 = client(ADMIN_KEY, ADMIN_SECRET)
    try:
        while True:
            listed = s3.list_objects_v2(Bucket=BUCKET)
            contents = listed.get("Contents", [])
            if not contents:
                break
            s3.delete_objects(
                Bucket=BUCKET,
                Delete={"Objects": [{"Key": o["Key"]} for o in contents]},
            )
        s3.delete_bucket(Bucket=BUCKET)
        log("ok", f"bucket {BUCKET} removed")
    except ClientError as exc:
        if error_code(exc) in ("NoSuchBucket", "404"):
            log("ok", f"bucket {BUCKET} already gone")
        else:
            raise


def cmd_full(args) -> None:
    cmd_setup(args)
    cmd_workload(args)
    cmd_authz(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup")
    sub.add_parser("workload")
    sub.add_parser("authz")
    sub.add_parser("full")
    sub.add_parser("cleanup")

    p_write = sub.add_parser("write")
    p_write.add_argument("--key", required=True)
    p_write.add_argument("--size-mb", type=float, default=8)

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--key", required=True)
    p_verify.add_argument("--sha256", required=True)

    p_rw = sub.add_parser("rw-check")
    p_rw.add_argument("--key", default="healthcheck/probe.bin")

    args = parser.parse_args()
    handler = globals()[f"cmd_{args.command.replace('-', '_')}"]
    handler(args)

    if _failures:
        print(f"\n{len(_failures)} assertion(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
