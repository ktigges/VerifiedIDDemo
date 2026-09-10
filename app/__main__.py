import logging

from app import create_app


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
startup_log = logging.getLogger("startup")
startup_log.info("Starting Microsoft Entra Verified ID employee demo")
startup_log.info("Loading configuration, routes, and service clients")

app = create_app()
config = app.config["DEMO_CONFIG"]
logging.getLogger().setLevel(logging.DEBUG if config["debug"] else logging.INFO)
startup_log.info(
    "Application loaded port=%s debug=%s public_url=%s",
    config["port"],
    config["debug"],
    config.get("publicBaseUrl") or "not configured",
)

if __name__ == "__main__":
    startup_log.info("Launching Flask server; waiting for the listening URL")
    if config["debug"]:
        startup_log.info("Development auto-reload is enabled; startup messages appear again after reload")
    app.run(
        host="0.0.0.0",
        port=config["port"],
        debug=config["debug"],
        use_reloader=config["debug"],
    )