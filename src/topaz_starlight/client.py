"""Topaz Labs Video API client targeting the Starlight Precise 2.5 model.

Workflow implemented (per https://developer.topazlabs.com):
    1. POST   /video/                            -> create request, get requestId
    2. PATCH  /video/{id}/accept                 -> reserve credits, get signed upload URLs
    3. PUT    {uploadUrl}                        -> upload each part to S3
    4. PATCH  /video/{id}/complete-upload        -> finalize multipart upload, queue job
    5. GET    /video/{id}/status                 -> poll until Completed, returns downloadUrl
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

import requests

DEFAULT_BASE_URL = "https://api.topazlabs.com"
STARLIGHT_PRECISE_25 = "slp-2.5"

# S3 multipart minimum part size is 5 MiB except for the last part. Topaz's
# accept response returns one signed URL per part, so we have to chunk the
# file with the same size used to compute partCount on create.
DEFAULT_PART_SIZE = 16 * 1024 * 1024  # 16 MiB


class TopazAPIError(RuntimeError):
    """Raised when the Topaz API returns a non-success response."""

    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


Creativity = Literal["low", "middle", "high"]


_FILTER_FIELD_MAP = {
    "input_frame_count": "inputFrameCount",
    "is_optimized_mode": "isOptimizedMode",
    "enable_clc": "enableClc",
    "video_profile": "videoProfile",
    "video_bit_depth": "videoBitDepth",
    "video_codec": "videoCodec",
}


@dataclass
class StarlightPreciseFilter:
    """Filter config for the Starlight Precise 2.5 (slp-2.5) model.

    Optional fields are omitted from the JSON body when left as None so the
    server applies its documented defaults.
    """

    model: str = STARLIGHT_PRECISE_25
    creativity: Optional[Creativity] = None
    input_frame_count: Optional[int] = None
    is_optimized_mode: Optional[bool] = None
    enable_clc: Optional[bool] = None
    video_profile: Optional[str] = None
    video_bit_depth: Optional[int] = None
    video_codec: Optional[str] = None
    slowmo: Optional[float] = None
    fps: Optional[float] = None

    def to_payload(self) -> dict[str, Any]:
        raw = {k: v for k, v in asdict(self).items() if v is not None}
        return {_FILTER_FIELD_MAP.get(k, k): v for k, v in raw.items()}


@dataclass
class SourceVideo:
    width: int
    height: int
    container: str
    size: int
    duration: float
    frame_rate: float
    frame_count: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "resolution": {"width": self.width, "height": self.height},
            "container": self.container,
            "size": self.size,
            "duration": self.duration,
            "frameRate": self.frame_rate,
            "frameCount": self.frame_count,
        }

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "SourceVideo":
        """Probe a local file with ffprobe to populate source metadata."""
        import json
        import shutil
        import subprocess

        if shutil.which("ffprobe") is None:
            raise RuntimeError(
                "ffprobe not found on PATH. Install ffmpeg or build SourceVideo manually."
            )

        path = Path(path)
        cmd = [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
        out = subprocess.check_output(cmd)
        data = json.loads(out)

        video_stream = next(
            (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
            None,
        )
        if video_stream is None:
            raise RuntimeError(f"No video stream found in {path}")

        num, den = (video_stream.get("avg_frame_rate") or "0/1").split("/")
        frame_rate = float(num) / float(den) if float(den) else 0.0

        duration = float(data["format"].get("duration", 0.0))
        frame_count = int(video_stream.get("nb_frames") or round(frame_rate * duration))

        return cls(
            width=int(video_stream["width"]),
            height=int(video_stream["height"]),
            container=path.suffix.lstrip(".") or "mp4",
            size=path.stat().st_size,
            duration=duration,
            frame_rate=frame_rate,
            frame_count=frame_count,
        )


@dataclass
class OutputSpec:
    width: int
    height: int
    frame_rate: float
    container: str = "mp4"
    audio_codec: str = "AAC"
    audio_transfer: str = "Copy"
    dynamic_compression_level: str = "High"

    def to_payload(self) -> dict[str, Any]:
        return {
            "resolution": {"width": self.width, "height": self.height},
            "container": self.container,
            "audioCodec": self.audio_codec,
            "audioTransfer": self.audio_transfer,
            "frameRate": self.frame_rate,
            "dynamicCompressionLevel": self.dynamic_compression_level,
        }


@dataclass
class JobStatus:
    status: str
    download_url: Optional[str] = None
    progress: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in {"completed", "failed", "canceled", "cancelled", "error"}

    @property
    def is_success(self) -> bool:
        return self.status.lower() == "completed"


class TopazClient:
    """Minimal HTTP client for the Topaz Video API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        session: Optional[requests.Session] = None,
        timeout: float = 60.0,
    ):
        api_key = api_key or os.environ.get("TOPAZ_API_KEY")
        if not api_key:
            raise ValueError(
                "Topaz API key required. Pass api_key=... or set TOPAZ_API_KEY."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        h = {"X-API-Key": self.api_key, "accept": "application/json"}
        if json_body:
            h["content-type"] = "application/json"
        return h

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
        if not resp.ok:
            raise TopazAPIError(
                f"{method} {path} failed: {resp.status_code} {resp.text}",
                status_code=resp.status_code,
                body=resp.text,
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    # ------------------------------------------------------------------ steps

    def create_request(
        self,
        source: SourceVideo,
        output: OutputSpec,
        filters: Iterable[StarlightPreciseFilter | dict[str, Any]],
    ) -> str:
        body = {
            "source": source.to_payload(),
            "output": output.to_payload(),
            "filters": [
                f.to_payload() if isinstance(f, StarlightPreciseFilter) else f
                for f in filters
            ],
        }
        data = self._request("POST", "/video/", headers=self._headers(json_body=True), json=body)
        request_id = data.get("requestId") or data.get("id")
        if not request_id:
            raise TopazAPIError(f"Create response missing requestId: {data}", body=data)
        return request_id

    def accept_request(self, request_id: str) -> list[str]:
        data = self._request(
            "PATCH",
            f"/video/{request_id}/accept",
            headers=self._headers(),
        )
        urls = data.get("urls") or data.get("uploadUrls") or data.get("upload_urls")
        if not urls:
            raise TopazAPIError(f"Accept response missing urls: {data}", body=data)
        return list(urls)

    def upload_parts(
        self,
        file_path: str | os.PathLike[str],
        upload_urls: list[str],
        part_size: int = DEFAULT_PART_SIZE,
        content_type: str = "video/mp4",
    ) -> list[dict[str, Any]]:
        # The server returns N signed URLs and the file must be split into
        # exactly N equal byte-ranges. `part_size` is kept for API
        # compatibility but is ignored — chunk size is derived from
        # file_size / len(upload_urls).
        file_size = Path(file_path).stat().st_size
        n_parts = len(upload_urls)
        if n_parts == 0:
            raise TopazAPIError("accept_request returned zero upload URLs")
        chunk_size = math.ceil(file_size / n_parts)

        results: list[dict[str, Any]] = []
        with open(file_path, "rb") as f:
            for idx, url in enumerate(upload_urls, start=1):
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                resp = self._session.put(
                    url,
                    data=chunk,
                    headers={"Content-Type": content_type},
                    timeout=self.timeout,
                )
                if not resp.ok:
                    raise TopazAPIError(
                        f"Part {idx} upload failed: {resp.status_code} {resp.text}",
                        status_code=resp.status_code,
                        body=resp.text,
                    )
                etag = resp.headers.get("ETag") or resp.headers.get("etag")
                if not etag:
                    raise TopazAPIError(f"Part {idx} response missing ETag header")
                results.append({"partNum": idx, "eTag": etag.strip('"')})
        return results

    def complete_upload(self, request_id: str, upload_results: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/video/{request_id}/complete-upload/",
            headers=self._headers(json_body=True),
            json={"uploadResults": upload_results},
        )

    def get_status(self, request_id: str) -> JobStatus:
        data = self._request(
            "GET",
            f"/video/{request_id}/status",
            headers=self._headers(),
        )
        return JobStatus(
            status=str(data.get("status", "Unknown")),
            download_url=data.get("downloadUrl") or data.get("download_url"),
            progress=data.get("progress"),
            raw=data,
        )

    def wait_for_completion(
        self,
        request_id: str,
        poll_interval: float = 15.0,
        timeout: Optional[float] = None,
        on_progress=None,
    ) -> JobStatus:
        deadline = time.time() + timeout if timeout else None
        while True:
            status = self.get_status(request_id)
            if on_progress:
                on_progress(status)
            if status.is_terminal:
                if not status.is_success:
                    raise TopazAPIError(
                        f"Job {request_id} ended with status={status.status}",
                        body=status.raw,
                    )
                return status
            if deadline and time.time() > deadline:
                raise TimeoutError(f"Timed out waiting for job {request_id}")
            time.sleep(poll_interval)

    def download_result(self, status: JobStatus, output_path: str | os.PathLike[str]) -> Path:
        if not status.download_url:
            raise ValueError("JobStatus has no downloadUrl; cannot download.")
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with self._session.get(status.download_url, stream=True, timeout=self.timeout) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return out

    # ----------------------------------------------------------- convenience

    def enhance_with_starlight_precise(
        self,
        input_path: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        *,
        output: Optional[OutputSpec] = None,
        filter_config: Optional[StarlightPreciseFilter] = None,
        part_size: int = DEFAULT_PART_SIZE,
        poll_interval: float = 15.0,
        timeout: Optional[float] = None,
        on_progress=None,
    ) -> Path:
        """End-to-end helper: probe, create, upload, wait, download."""
        source = SourceVideo.from_file(input_path)
        output = output or OutputSpec(
            width=source.width,
            height=source.height,
            frame_rate=source.frame_rate,
            container=source.container,
        )
        filter_config = filter_config or StarlightPreciseFilter()

        request_id = self.create_request(source, output, [filter_config])
        upload_urls = self.accept_request(request_id)
        upload_results = self.upload_parts(input_path, upload_urls, part_size=part_size)
        self.complete_upload(request_id, upload_results)
        status = self.wait_for_completion(
            request_id,
            poll_interval=poll_interval,
            timeout=timeout,
            on_progress=on_progress,
        )
        return self.download_result(status, output_path)
