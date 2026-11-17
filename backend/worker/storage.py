"""Asset storage: Cloudflare R2 (S3 API) or local directory fallback served by the API."""
from __future__ import annotations

import gzip
import shutil
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings

log = logging.getLogger("storage")
settings = get_settings()

CACHE_FRESH = "public, max-age=180"          # spec: 3 min edge cache
CACHE_IMMUTABLE = "public, max-age=86400, immutable"


class Storage:
    def __init__(self):
        self.r2 = None
        if settings.r2_enabled:
            import boto3
            from botocore.config import Config

            self.r2 = boto3.client(
                "s3",
                endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=settings.r2_access_key_id,
                aws_secret_access_key=settings.r2_secret_access_key,
                region_name="auto",
                config=Config(retries={"max_attempts": 4, "mode": "standard"}, signature_version="s3v4"),
            )
            self._ensure_cors()
        self.local = Path(settings.local_assets_dir)
        log.info("Asset storage: %s", "R2 bucket " + settings.r2_bucket_name if self.r2 else f"local {self.local}")

    def _ensure_cors(self) -> None:
        """MapLibre fetches images/GeoJSON with CORS: allow the PWA origin(s) on the bucket (idempotent)."""
        origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
        try:
            self.r2.put_bucket_cors(Bucket=settings.r2_bucket_name, CORSConfiguration={"CORSRules": [{
                "AllowedOrigins": origins, "AllowedMethods": ["GET", "HEAD"],
                "AllowedHeaders": ["*"], "MaxAgeSeconds": 86400}]})
        except Exception as exc:  # noqa: BLE001 - token may lack bucket-admin scope
            log.warning("Could not set R2 CORS policy (%s); configure it in the dashboard", exc)

    @property
    def base_url(self) -> str:
        return settings.assets_base_url

    def put(self, key: str, body: bytes, content_type: str, cache_control: str = CACHE_FRESH,
            compress: bool = False) -> str:
        extra = {}
        if compress:
            body = gzip.compress(body, compresslevel=6)
            extra["ContentEncoding"] = "gzip"
        if self.r2:
            self.r2.put_object(Bucket=settings.r2_bucket_name, Key=key, Body=body,
                               ContentType=content_type, CacheControl=cache_control, **extra)
        else:
            path = self.local / key
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            # Local static server can't set Content-Encoding: store uncompressed.
            tmp.write_bytes(gzip.decompress(body) if compress else body)
            tmp.replace(path)
        return f"{self.base_url}/{key}"

    def prune_runs(self, prefix: str = "frames/", keep_hours: int = 2) -> int:
        """Delete run folders frames/YYYYmmddHHMM/ older than keep_hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=keep_hours)
        removed = 0
        if self.r2:
            paginator = self.r2.get_paginator("list_objects_v2")
            batch: list[dict] = []
            for page in paginator.paginate(Bucket=settings.r2_bucket_name, Prefix=prefix):
                for obj in page.get("Contents", []):
                    if _run_older(obj["Key"], prefix, cutoff):
                        batch.append({"Key": obj["Key"]})
                    if len(batch) == 1000:
                        self.r2.delete_objects(Bucket=settings.r2_bucket_name, Delete={"Objects": batch})
                        removed += len(batch)
                        batch = []
            if batch:
                self.r2.delete_objects(Bucket=settings.r2_bucket_name, Delete={"Objects": batch})
                removed += len(batch)
        else:
            root = self.local / prefix
            if root.exists():
                for d in root.iterdir():
                    if _run_older(f"{prefix}{d.name}/x", prefix, cutoff):
                        shutil.rmtree(d, ignore_errors=True)
                        removed += 1
        return removed


def _run_older(key: str, prefix: str, cutoff: datetime) -> bool:
    try:
        ts = datetime.strptime(key[len(prefix):].split("/")[0], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return ts < cutoff
