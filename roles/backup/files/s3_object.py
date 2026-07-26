#!/usr/bin/env python3
"""S3 / MinIO object helper for Galera backups.

Kernel-independent off-cluster transport (the OrbStack lab kernel lacks the
cifs module, so SMB cannot be mounted here — S3 is the sanctioned alternative).

Credentials and endpoint come from the environment, never argv/repo:
  S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_SECURE (true/false)

Usage:
  s3_object.py ensure-bucket <bucket>
  s3_object.py put <bucket> <object> <file>
  s3_object.py get <bucket> <object> <file>
  s3_object.py list <bucket> [prefix]
  s3_object.py delete <bucket> <object>
  s3_object.py prune <bucket> <prefix> <retention_days>   # delete objects older than N days
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from minio import Minio


def client():
    return Minio(
        os.environ["S3_ENDPOINT"],
        access_key=os.environ["S3_ACCESS_KEY"],
        secret_key=os.environ["S3_SECRET_KEY"],
        secure=os.environ.get("S3_SECURE", "false").lower() == "true",
    )


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    c = client()
    cmd, bucket = sys.argv[1], sys.argv[2]

    if cmd == "ensure-bucket":
        if not c.bucket_exists(bucket):
            c.make_bucket(bucket)
        print(f"bucket-ready:{bucket}")
    elif cmd == "put":
        obj, path = sys.argv[3], sys.argv[4]
        try:
            c.fput_object(bucket, obj, path)
        except Exception as exc:
            # Nie zostawiaj częściowego obiektu — zanieczyszcza bucket i prunowanie.
            try:
                c.remove_object(bucket, obj)
            except Exception:
                pass
            print(f"put-failed:{obj} {exc}", file=sys.stderr)
            return 1
        print(f"put:{obj}")
    elif cmd == "get":
        obj, path = sys.argv[3], sys.argv[4]
        c.fget_object(bucket, obj, path)
        print(f"get:{obj}")
    elif cmd == "list":
        prefix = sys.argv[3] if len(sys.argv) > 3 else None
        for o in c.list_objects(bucket, prefix=prefix, recursive=True):
            print(o.object_name)
    elif cmd == "delete":
        obj = sys.argv[3]
        c.remove_object(bucket, obj)
        print(f"deleted:{obj}")
    elif cmd == "prune":
        prefix, retention_days = sys.argv[3], int(sys.argv[4])
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        removed = 0
        for o in c.list_objects(bucket, prefix=prefix, recursive=True):
            if o.last_modified and o.last_modified < cutoff:
                c.remove_object(bucket, o.object_name)
                removed += 1
        print(f"pruned:{removed}")
    else:
        print(f"unknown command: {cmd}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
