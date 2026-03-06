"""Database backup — backs up SQLite/PostgreSQL to Google Cloud Storage.

Supports both SQLite (using sqlite3.backup) and PostgreSQL (using pg_dump).
Manages retention by deleting old backups beyond the configured number of days.
"""

import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone

from src.logger import log


def backup_db_to_gcs(bucket_name: str, retention_days: int = 7) -> dict | None:
    """Back up the database to a GCS bucket.

    Args:
        bucket_name: GCS bucket name (e.g., 'my-project-db-backups').
        retention_days: Delete backups older than this many days.

    Returns:
        dict with backup details, or None if backup failed / not configured.
    """
    try:
        from google.cloud import storage
    except ImportError:
        log.info("google-cloud-storage not available — skipping DB backup.")
        return None

    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        # Create bucket if it doesn't exist
        if not bucket.exists():
            bucket.storage_class = "STANDARD"
            client.create_bucket(bucket, location="europe-west3")
            log.info(f"Created backup bucket: {bucket_name}")
    except Exception as e:
        log.warning(f"GCS bucket access failed — skipping backup: {e}")
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Determine database type
    db_url = os.environ.get('DATABASE_URL', '')
    is_postgres = db_url.startswith('postgresql')

    try:
        if is_postgres:
            backup_path = _backup_postgres(db_url)
            blob_name = f"backups/postgres_{timestamp}.sql.gz"
        else:
            backup_path = _backup_sqlite()
            blob_name = f"backups/sqlite_{timestamp}.db"

        if not backup_path:
            return None

        # Upload to GCS
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(backup_path)
        file_size = os.path.getsize(backup_path)
        log.info(f"DB backup uploaded: gs://{bucket_name}/{blob_name} ({file_size} bytes)")

        # Clean up temp file
        os.unlink(backup_path)

        # Delete old backups
        _cleanup_old_backups(bucket, retention_days)

        return {
            "bucket": bucket_name,
            "blob": blob_name,
            "size_bytes": file_size,
            "timestamp": timestamp,
        }

    except Exception as e:
        log.error(f"DB backup failed: {e}", exc_info=True)
        return None


def _backup_sqlite() -> str | None:
    """Back up SQLite database using sqlite3.backup()."""
    db_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'bot.db')
    db_path = os.path.abspath(db_path)

    if not os.path.exists(db_path):
        log.warning(f"SQLite database not found at {db_path}")
        return None

    fd, tmp_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    try:
        source = sqlite3.connect(db_path)
        dest = sqlite3.connect(tmp_path)
        source.backup(dest)
        dest.close()
        source.close()
        return tmp_path
    except Exception as e:
        log.error(f"SQLite backup failed: {e}")
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return None


def _backup_postgres(db_url: str) -> str | None:
    """Back up PostgreSQL using pg_dump."""
    fd, tmp_path = tempfile.mkstemp(suffix='.sql.gz')
    os.close(fd)

    try:
        result = subprocess.run(
            ['pg_dump', db_url, '--no-owner', '--no-acl'],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            log.error(f"pg_dump failed: {result.stderr.decode()[:500]}")
            os.unlink(tmp_path)
            return None

        import gzip
        with gzip.open(tmp_path, 'wb') as f:
            f.write(result.stdout)
        return tmp_path
    except FileNotFoundError:
        log.warning("pg_dump not found — PostgreSQL backup not available")
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return None
    except Exception as e:
        log.error(f"PostgreSQL backup failed: {e}")
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return None


def _cleanup_old_backups(bucket, retention_days: int):
    """Delete backup blobs older than retention_days."""
    cutoff = datetime.now(timezone.utc).timestamp() - (retention_days * 86400)
    try:
        blobs = list(bucket.list_blobs(prefix="backups/"))
        deleted = 0
        for blob in blobs:
            if blob.time_created and blob.time_created.timestamp() < cutoff:
                blob.delete()
                deleted += 1
        if deleted:
            log.info(f"Cleaned up {deleted} old backups (retention={retention_days}d)")
    except Exception as e:
        log.warning(f"Backup cleanup failed: {e}")
