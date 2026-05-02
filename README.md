# topaz-starlight

A small Python client + CLI for the [Topaz Labs Video API](https://developer.topazlabs.com/),
wired specifically for the **Starlight Precise 2.5** (`slp-2.5`) video enhancement model.

It implements the documented five-step flow:

1. `POST /video/` — create a request describing the source video, output spec, and filters
2. `PATCH /video/{id}/accept` — reserve credits and receive signed multipart upload URLs
3. `PUT {url}` — upload each part to S3
4. `PATCH /video/{id}/complete-upload` — finalize the upload and queue the job
5. `GET /video/{id}/status` — poll until `Completed`, then download the result

## Install

```bash
pip install -e .
```

`ffprobe` (from ffmpeg) is required for automatic source-video probing. If it's
not available, build a `SourceVideo` manually.

## Auth

Set your API key (created from the Topaz developer portal):

```bash
export TOPAZ_API_KEY=sk_topaz_xxx
```

## CLI

```bash
topaz-starlight input.mp4 enhanced.mp4 \
    --creativity middle \
    --width 3840 --height 2160 --fps 24
```

Useful flags:

| flag | purpose |
| ---- | ------- |
| `--creativity {low,middle,high}` | Starlight Precise creativity preset |
| `--input-frame-count N` | frames per inference window (max 9000) |
| `--no-optimized` | disable optimized mode |
| `--no-clc` | disable color/light correction |
| `--video-codec h264\|hevc\|prores` | output codec |
| `--slowmo 2` | slow-motion multiplier |
| `--poll-interval 30` | status polling interval (s) |

## Library usage

```python
from topaz_starlight import (
    TopazClient,
    StarlightPreciseFilter,
    OutputSpec,
    SourceVideo,
)

client = TopazClient()  # reads TOPAZ_API_KEY

# One-shot helper: probe -> create -> upload -> wait -> download
client.enhance_with_starlight_precise(
    "in.mp4",
    "out.mp4",
    filter_config=StarlightPreciseFilter(creativity="high"),
)
```

Or step through the flow yourself:

```python
source = SourceVideo.from_file("in.mp4")
output = OutputSpec(width=3840, height=2160, frame_rate=source.frame_rate)
filt = StarlightPreciseFilter(creativity="middle", input_frame_count=120)

request_id = client.create_request(source, output, [filt])
upload_urls = client.accept_request(request_id)
parts = client.upload_parts("in.mp4", upload_urls)
client.complete_upload(request_id, parts)
status = client.wait_for_completion(request_id, on_progress=print)
client.download_result(status, "out.mp4")
```

## Notes on Starlight Precise 2.5 parameters

Per the Topaz developer docs, the `slp-2.5` filter accepts:

- `creativity`: `"low"`, `"middle"`, or `"high"`
- `input_frame_count`: integer, max 9000
- `is_optimized_mode`: bool, default true
- `enable_clc`: bool, default true
- `video_profile`, `video_codec`, `video_bit_depth`: output encoding controls

Pricing (per Topaz docs) is roughly **26 frames per credit at 1080p** and
**~12 frames per credit at 4K**.
