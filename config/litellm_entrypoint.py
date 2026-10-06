"""Read the Compose secret, then run the official proxy with only safe audit logging."""

import json
import logging.config
import os
from pathlib import Path

LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
    "formatters": {"audit": {"format": "%(message)s"}},
    "handlers": {
        "null": {"class": "logging.NullHandler"},
        "audit": {"class": "logging.StreamHandler", "formatter": "audit"},
    },
    "root": {"handlers": ["null"]},
    "loggers": {"homelab.models": {"handlers": ["audit"], "level": "INFO", "propagate": False}},
}


def create_app(config_path="/etc/litellm/litellm.yaml", secret_path="/run/secrets/gateway_api_key"):
    # This variable exists only inside the running proxy; Compose and env files hold no key.
    os.environ["HOMELAB_GATEWAY_API_KEY"] = Path(secret_path).read_text().strip()
    os.environ["CONFIG_FILE_PATH"] = config_path
    os.environ["WORKER_CONFIG"] = json.dumps(
        {
            "config": os.environ["CONFIG_FILE_PATH"],
            "telemetry": False,
            "drop_params": False,
        }
    )
    logging.config.dictConfig(LOGGING)

    from litellm.proxy.proxy_server import app
    from litellm_hooks import ModelEndpointMiddleware

    app.add_middleware(ModelEndpointMiddleware)
    # Imported libraries install their own handlers; disable them after import too.
    # Default exception/access logs can contain request bodies or keys at any log level.
    logging.config.dictConfig(LOGGING)
    return app


def main():
    import uvicorn

    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=4000, log_config=LOGGING, access_log=False)


if __name__ == "__main__":
    main()
