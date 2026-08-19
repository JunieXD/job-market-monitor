import gzip
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_market.schemas import Channel, RawSnapshotRecord

PARTITION_PREFIX_MAX_LENGTH = 80


def _safe_partition_name(partition: str) -> str:
    prefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", partition).strip("-_")
    prefix = prefix[:PARTITION_PREFIX_MAX_LENGTH].rstrip("-_") or "partition"
    digest = hashlib.sha256(partition.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


class RawStore:
    def __init__(self, root: Path, run_id: str, source_key: str):
        self.root = root
        self.run_id = run_id
        self.source_key = source_key

    def save(
        self,
        *,
        channel: Channel,
        partition: str,
        offset: int,
        payload: Any,
    ) -> RawSnapshotRecord:
        captured_at = datetime.now(UTC)
        safe_partition = _safe_partition_name(partition)
        relative = Path(
            self.source_key,
            captured_at.strftime("%Y-%m-%d"),
            self.run_id,
            channel.value,
            f"{safe_partition}-{offset:07d}.json.gz",
        )
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            with gzip.open(temporary, "wb", compresslevel=6) as handle:
                handle.write(encoded)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

        return RawSnapshotRecord(
            path=str(relative),
            sha256=hashlib.sha256(encoded).hexdigest(),
            byte_size=len(encoded),
            channel=channel,
            partition=partition,
            offset=offset,
            captured_at=captured_at,
        )
