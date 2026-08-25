"""JobPilot — AI-powered end-to-end job application pipeline."""

__version__ = "0.3.0"


def _force_utf8() -> None:
    """Make stdout/stderr UTF-8 on Windows.

    The agent loop redirects child stdout/stderr to files. On Windows those
    streams default to the ANSI code page (cp1252), so the first Unicode
    character rich emits -- an arrow, a box-drawing glyph, an emoji -- raises
    UnicodeEncodeError and kills the run mid-pipeline. Reconfiguring to UTF-8
    with errors="replace" makes that impossible.

    Also exported to the environment so subprocesses (apply workers, Claude
    Code CLI, PDF converters) inherit it.
    """
    import os
    import sys

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Detached / already-wrapped / non-reconfigurable stream. Nothing
            # to do; the caller must not crash over logging setup.
            pass


_force_utf8()
