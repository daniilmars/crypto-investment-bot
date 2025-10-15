import logging
import sys
import os

def setup_logger():
    """
    Configures a centralized logger to output to both the console and a log file.
    """
    logger = logging.getLogger("CryptoBotLogger")
    logger.setLevel(logging.INFO)

    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger

    # --- Formatter ---
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(module)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # --- Console Handler ---
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    # --- File Handler ---
    # Create data directory if it doesn't exist
    log_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, 'bot.log')

    # Add a file handler to write logs to a file
    file_handler = logging.FileHandler(log_file_path)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

# Create a single logger instance to be used across the application
log = setup_logger()
