import shutil
from pathlib import Path


class RuntimeChecker:
    """Validate local runtime resources before a crawl batch starts."""

    def __init__(self, raw_data_dir: Path, *, minimum_free_gib: float):
        if minimum_free_gib <= 0:
            raise ValueError("minimum_free_gib must be positive")
        self.raw_data_dir = raw_data_dir
        self.minimum_free_gib = minimum_free_gib

    def run(self) -> dict[str, object]:
        if not self.raw_data_dir.is_dir():
            return {
                "ok": False,
                "raw_data_dir": str(self.raw_data_dir),
                "minimum_free_gib": self.minimum_free_gib,
                "violations": ["raw_data_dir_missing"],
            }

        usage = shutil.disk_usage(self.raw_data_dir)
        gib = 1024**3
        free_gib = usage.free / gib
        violations = []
        if free_gib < self.minimum_free_gib:
            violations.append("raw_data_disk_space_low")
        return {
            "ok": not violations,
            "raw_data_dir": str(self.raw_data_dir),
            "minimum_free_gib": self.minimum_free_gib,
            "total_gib": round(usage.total / gib, 3),
            "used_gib": round(usage.used / gib, 3),
            "free_gib": round(free_gib, 3),
            "free_percent": round(usage.free / usage.total * 100, 3),
            "violations": violations,
        }
