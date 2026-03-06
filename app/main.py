"""Entry point: ``python -m app.main`` starts the Gradio app."""

from __future__ import annotations

import sys

from sqlalchemy.exc import SQLAlchemyError

from app.bootstrap import build_context
from app.config import ConfigError, configure_logging, get_settings
from app.ui import build_demo, launch_kwargs


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        ctx = build_context(settings)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        url = settings.safe_database_url
        print(f"Could not initialise the database ({url}): {exc}", file=sys.stderr)
        return 1

    demo = build_demo(ctx)
    demo.queue().launch(
        server_name=settings.server_host,
        server_port=settings.server_port,
        **launch_kwargs(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
