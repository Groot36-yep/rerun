"""Start Rerun, or run a maintenance command.

    python -m app                  start the server
    python -m app reset-password   forget the password; the web page asks for a new one
"""
import logging
import os
import sys

import uvicorn


def reset_password():
    from .security import Sessions
    from .store import load_secrets

    sec = load_secrets()
    sec.data["password_hash"] = ""
    sec.save()
    Sessions().revoke_all()
    print("Password cleared. Open Rerun in your browser to choose a new one.")


def run():
    from .main import PORT

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(
        "app.main:web",
        host=os.environ.get("RERUN_HOST", "0.0.0.0"),
        port=PORT,
        proxy_headers=False,  # never trust X-Forwarded-For; the home-network check uses the real address
        server_header=False,
        access_log=False,
        log_level="info",
        timeout_keep_alive=30,
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["reset-password"]:
        reset_password()
    elif sys.argv[1:]:
        print(__doc__)
        sys.exit(2)
    else:
        run()
