"""Google Cloud Storage as the shared home of the lineage graph.

`codelineage publish gs://bucket/lineage` uploads:
    graph.json        the lineage graph
    lineage.html      the Lineage Terminal with graph and SQL embedded (open it anywhere)
    files/<path>      the SQL / DAG files the graph points to (for read_sql_file and the SQL tab)
    manifest.json     when, from which commit, by whom

Everyone else points CODELINEAGE_GRAPH at gs://bucket/lineage/graph.json: the tool downloads it
once, keeps a local copy and re-downloads only when the object changes. No one needs a clone of the
SQL repo. Access is plain GCS IAM on the bucket, with each user's own Google credentials.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)


def is_gcs(path) -> bool:
    return str(path).startswith("gs://")


def split_uri(uri: str) -> tuple[str, str]:
    bucket, _, prefix = uri[len("gs://"):].partition("/")
    return bucket, prefix.strip("/")


def cache_dir() -> Path:
    return Path(os.environ.get("CODELINEAGE_CACHE", Path.home() / ".cache" / "codelineage"))


class GcsStore:
    """A gs://bucket/prefix "folder"."""

    def __init__(self, uri: str, client=None):
        self.bucket_name, self.prefix = split_uri(uri.rstrip("/"))
        self._client = client

    @classmethod
    def for_object(cls, object_uri: str, client=None) -> tuple["GcsStore", str]:
        """Splits gs://bucket/prefix/graph.json into (store for the prefix, "graph.json")."""
        bucket, path = split_uri(object_uri)
        parent, name = PurePosixPath(path).parent, PurePosixPath(path).name
        return cls(f"gs://{bucket}/{parent}" if str(parent) != "." else f"gs://{bucket}", client), name

    @property
    def bucket(self):
        if self._client is None:
            from google.cloud import storage
            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def _name(self, rel: str) -> str:
        rel = str(PurePosixPath(rel))
        if rel.startswith("/") or ".." in PurePosixPath(rel).parts:
            raise ValueError(f"invalid path: {rel}")
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def uri(self, rel: str) -> str:
        return f"gs://{self.bucket_name}/{self._name(rel)}"

    def upload_text(self, rel: str, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        self.bucket.blob(self._name(rel)).upload_from_string(text, content_type=content_type)

    def read_text(self, rel: str) -> str | None:
        try:
            blob = self.bucket.blob(self._name(rel))
            return blob.download_as_text() if blob.exists() else None
        except ValueError:
            return None

    def download_cached(self, rel: str) -> Path:
        """Local copy of an object, re-downloaded only when its GCS generation changes. Falls back to
        the cached copy (with a warning) when GCS can't be reached."""
        target = cache_dir() / self.bucket_name / self._name(rel)
        stamp = target.with_name(target.name + ".generation")
        try:
            blob = self.bucket.get_blob(self._name(rel))
            if blob is None:
                raise FileNotFoundError(self.uri(rel))
            if not (target.exists() and stamp.exists() and stamp.read_text() == str(blob.generation)):
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_text(blob.download_as_text())
                tmp.replace(target)
                stamp.write_text(str(blob.generation))
        except FileNotFoundError:
            raise
        except Exception as e:  # noqa: BLE001
            if not target.exists():
                raise
            log.warning("using cached %s (GCS unreachable: %s)", target, e)
        return target


def publish(graph, repo_root: Path, uri: str, client=None, include_sql: bool = True) -> dict:
    """Uploads graph.json, lineage.html, the referenced files and a manifest to gs://… ."""
    from .explorer import read_repo_file, referenced_files, render_explorer
    from .telemetry import default_user

    store = GcsStore(uri, client)
    files = sorted(referenced_files(graph)) if include_sql else []
    uploaded = 0
    for f in files:
        text = read_repo_file(repo_root, f)
        if text is not None:
            store.upload_text(f"files/{f}", text)
            uploaded += 1
    store.upload_text("lineage.html", render_explorer(graph, repo_root, include_sql=include_sql),
                      "text/html; charset=utf-8")
    manifest = {
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "published_by": default_user(),
        "built_at": graph.data.get("built_at"),
        "git_commit": graph.data.get("git_commit"),
        "tables": len(graph.tables), "edges": len(graph.edges), "files": uploaded,
    }
    store.upload_text("manifest.json", json.dumps(manifest, indent=2), "application/json")
    # graph.json last: readers only ever see a graph whose files are already in place
    store.upload_text("graph.json", json.dumps(graph.data, default=list), "application/json")
    return {**manifest, "uri": store.uri("graph.json")}
