"""stdio entry point. Nothing here binds a socket."""

from data.schema import init_db, seed_local_user


def main() -> None:
    # Same pair the desktop app runs on launch. Idempotent: an existing
    # database gains mcp_audit and is not otherwise rewritten, beyond the
    # closed-account backfill init_db has always done.
    init_db()
    seed_local_user()
    from balance_mcp.server import build_server

    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
