import logging


def configure_private_logging():
    # Only application records get INFO verbosity. Provider loggers retain their
    # normal level; raw requests, exceptions, credentials, and inputs are omitted.
    if not logging.getLogger().handlers:
        logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("prompt_studio.private").setLevel(logging.INFO)
