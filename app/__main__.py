import logging

import uvicorn

from .config import Config
from .main import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    app = create_app(cfg)
    # Proxy headers are handled by the app itself (TRUST_PROXY); uvicorn's access log is off
    # because our own access log never records query strings or bodies.
    uvicorn.run(app, host="0.0.0.0", port=cfg.port, proxy_headers=False, access_log=False,
                log_level="info", server_header=False)


if __name__ == "__main__":
    main()
