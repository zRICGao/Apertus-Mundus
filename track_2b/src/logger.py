"""Shared console logger, with no file or network side effects."""
import logging
import os


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"apertus_mundus.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return logger
