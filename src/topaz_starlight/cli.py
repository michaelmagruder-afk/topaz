"""Command-line interface: process a video with Starlight Precise 2.5."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .client import (
    OutputSpec,
    StarlightPreciseFilter,
    TopazAPIError,
    TopazClient,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="topaz-starlight",
        description="Enhance a video using Topaz Starlight Precise 2.5 (slp-2.5).",
    )
    p.add_argument("input", help="Path to input video file")
    p.add_argument("output", help="Path to write the enhanced video")
    p.add_argument("--api-key", help="Topaz API key (defaults to TOPAZ_API_KEY env var)")

    p.add_argument("--width", type=int, help="Output width (defaults to source width)")
    p.add_argument("--height", type=int, help="Output height (defaults to source height)")
    p.add_argument("--fps", type=float, help="Output frame rate")
    p.add_argument("--container", default=None, help="Output container (mp4, mov, mkv)")

    p.add_argument(
        "--creativity",
        choices=["low", "middle", "high"],
        help="Starlight Precise creativity preset",
    )
    p.add_argument("--input-frame-count", type=int, help="Frames per inference window (max 9000)")
    p.add_argument(
        "--no-optimized",
        action="store_true",
        help="Disable Starlight optimized mode (default: enabled)",
    )
    p.add_argument(
        "--no-clc",
        action="store_true",
        help="Disable color/light correction (default: enabled)",
    )
    p.add_argument("--video-codec", help="Output video codec (e.g. h264, hevc, prores)")
    p.add_argument("--video-profile", help="Output video profile")
    p.add_argument("--video-bit-depth", type=int, help="Output video bit depth")
    p.add_argument("--slowmo", type=float, help="Slow-motion multiplier")

    p.add_argument("--poll-interval", type=float, default=15.0, help="Status polling interval (s)")
    p.add_argument("--timeout", type=float, help="Hard timeout in seconds")
    p.add_argument(
        "--part-size",
        type=int,
        default=16 * 1024 * 1024,
        help="Multipart upload chunk size in bytes (default 16 MiB)",
    )
    return p


def _print_progress(status) -> None:
    pct = f" {status.progress:.1f}%" if status.progress is not None else ""
    print(f"[topaz] status={status.status}{pct}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        client = TopazClient(api_key=args.api_key)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    filter_kwargs = {}
    if args.creativity:
        filter_kwargs["creativity"] = args.creativity
    if args.input_frame_count is not None:
        filter_kwargs["input_frame_count"] = args.input_frame_count
    if args.no_optimized:
        filter_kwargs["is_optimized_mode"] = False
    if args.no_clc:
        filter_kwargs["enable_clc"] = False
    if args.video_codec:
        filter_kwargs["video_codec"] = args.video_codec
    if args.video_profile:
        filter_kwargs["video_profile"] = args.video_profile
    if args.video_bit_depth is not None:
        filter_kwargs["video_bit_depth"] = args.video_bit_depth
    if args.slowmo is not None:
        filter_kwargs["slowmo"] = args.slowmo
    if args.fps is not None:
        filter_kwargs["fps"] = args.fps
    starlight_filter = StarlightPreciseFilter(**filter_kwargs)

    output_spec = None
    if any(v is not None for v in (args.width, args.height, args.fps, args.container)):
        from .client import SourceVideo

        probed = SourceVideo.from_file(args.input)
        output_spec = OutputSpec(
            width=args.width or probed.width,
            height=args.height or probed.height,
            frame_rate=args.fps or probed.frame_rate,
            container=args.container or probed.container,
        )

    try:
        out = client.enhance_with_starlight_precise(
            args.input,
            args.output,
            output=output_spec,
            filter_config=starlight_filter,
            part_size=args.part_size,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
            on_progress=_print_progress,
        )
    except TopazAPIError as e:
        print(f"Topaz API error: {e}", file=sys.stderr)
        return 1
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(str(Path(out).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
