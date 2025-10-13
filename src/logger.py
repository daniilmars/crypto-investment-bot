import logging
import sys

def setup_logger():
    """
    Configures and returns a centralized logger for the application.
    """
    # Create a logger
    logger = logging.getLogger("CryptoBotLogger")
    logger.setLevel(logging.INFO)

    # Create a handler to print logs to the console
    stream_handler = logging.StreamHandler(sys.stdout)
    
    # Create a formatter and set it for the handler
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(module)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    stream_handler.setFormatter(formatter)

    # Add the handler to the logger
    # Avoid adding handlers multiple times if the function is called again
    if not logger.handlers:
        logger.addHandler(stream_handler)

    return logger

# Create a single logger instance to be used across the application
log = setup_logger()
