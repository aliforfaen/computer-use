"""Bounded kwin-mcp AT-SPI worker with enough text for task-owned documents.

The pinned driver launches AT-SPI in a child Python process and caps text at
200 characters. This wrapper adjusts that process-local cap before entering
the driver's unchanged request loop. A cap of 4097 lets callers reject text
outside the service's 4096-character limit without claiming it was complete.
"""

from __future__ import annotations

import sys


MAX_TEXT_CHARS = 4097


def main() -> int:
    if sys.argv[1:] != ["--serve"]:
        return 2
    from kwin_mcp import accessibility

    accessibility._MAX_TEXT_CHARS = MAX_TEXT_CHARS
    accessibility._serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
