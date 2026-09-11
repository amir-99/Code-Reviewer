import hashlib
import os
import time
from pathlib import Path

from reviewer.store.models import LLMCall
from reviewer.telemetry.activity import activity


class BlobStore:
    """Content addressed local object store; references are never public URLs."""

    def __init__(self, path, retention_days=30):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.retention = retention_days * 86400

    def put(self, text):
        key = hashlib.sha256(text.encode()).hexdigest()
        path = self.path / key
        if not path.exists():
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return key
            with os.fdopen(fd, "w") as file:
                file.write(text)
        return key

    def reap(self):
        for path in self.path.iterdir():
            if path.is_file() and path.stat().st_mtime < time.time() - self.retention:
                path.unlink()


class Audit:
    def __init__(self, store, blobs):
        self.store, self.blobs = store, blobs

    @activity("storage", "LLM audit persistence")
    async def write(self, *, prompt, response, **fields):
        import inspect

        prompt_ref = self.blobs.put(prompt)
        response_ref = self.blobs.put(response)
        if inspect.isawaitable(prompt_ref):
            prompt_ref = await prompt_ref
        if inspect.isawaitable(response_ref):
            response_ref = await response_ref
        async with self.store.transaction() as session:
            session.add(
                LLMCall(
                    prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                    prompt_blob_ref=prompt_ref,
                    response_blob_ref=response_ref,
                    **fields,
                )
            )


class S3BlobStore:
    def __init__(self, settings):
        import boto3
        from botocore.config import Config

        if not settings.s3_endpoint or not settings.s3_bucket:
            raise ValueError("Internal S3 endpoint and bucket required")
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
            config=Config(
                connect_timeout=5,
                read_timeout=15,
                retries={"max_attempts": 2},
                s3={"addressing_style": "path"},
            ),
        )

    async def put(self, text):
        import asyncio

        key = hashlib.sha256(text.encode()).hexdigest()
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key="review-audit/" + key,
            Body=text.encode(),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        return "s3://" + self.bucket + "/review-audit/" + key

    def reap(self):
        # Retention is configured as a bucket lifecycle policy by the operator.
        pass


def blob_store(settings):
    return (
        S3BlobStore(settings)
        if settings.s3_endpoint
        else BlobStore(settings.audit_path, settings.audit_retention_days)
    )
