# logger.py
import csv
import io
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
import sys

# Global logger instance
_logger = None


class CsvFormatter(logging.Formatter):
    """Format one logging record as a valid CSV row."""

    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"

        row = io.StringIO(newline="")
        csv.writer(row).writerow([
            datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            record.levelname,
            record.filename,
            record.funcName,
            record.lineno,
            message,
        ])
        return row.getvalue().rstrip("\r\n")

def get_logger(name="E225Project", log_dir="Logs", log_level=logging.INFO):
    """
    Get or create a logger instance
    
    Args:
        name (str): Logger name
        log_dir (str): Directory to store log files
        log_level (int): Logging level
    
    Returns:
        logging.Logger: Configured logger instance
    """
    global _logger
    
    if _logger is not None:
        return _logger
    
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_path = Path(log_dir) / date_str
    text_log_path = log_path / "log"
    csv_log_path = log_path / "csv"
    text_log_path.mkdir(parents=True, exist_ok=True)
    csv_log_path.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = text_log_path / f"{name}_{timestamp}.log"
    csv_file = csv_log_path / f"{name}_{timestamp}.csv"
    
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    logger.propagate = False
    
    if logger.handlers:
        logger.handlers.clear()
    
    file_formatter = logging.Formatter(
        fmt=(
            "%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | "
            "%(funcName)s | %(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=10,
        encoding="utf-8"
    )
    
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    csv_is_empty = not csv_file.exists() or csv_file.stat().st_size == 0
    if csv_is_empty:
        with csv_file.open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerow(
                ["Timestamp", "Level", "Source File", "Function", "Line", "Message"]
            )

    csv_handler = RotatingFileHandler(
        csv_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    csv_handler.setLevel(logging.DEBUG)
    csv_handler.setFormatter(CsvFormatter())
    logger.addHandler(csv_handler)
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    _logger = logger
    
    logger.info("Logging session started")
    logger.info(f"Logger initialized: {log_file}")
    logger.info(f"CSV logger initialized: {csv_file}")
    
    return logger

# Convenience functions
def debug(message, *args, **kwargs):
    get_logger().debug(message, *args, stacklevel=2, **kwargs)

def info(message, *args, **kwargs):
    get_logger().info(message, *args, stacklevel=2, **kwargs)

def warning(message, *args, **kwargs):
    get_logger().warning(message, *args, stacklevel=2, **kwargs)

def error(message, *args, **kwargs):
    get_logger().error(message, *args, stacklevel=2, **kwargs)

def critical(message, *args, **kwargs):
    get_logger().critical(message, *args, stacklevel=2, **kwargs)

def exception(message, *args, **kwargs):
    get_logger().exception(message, *args, stacklevel=2, **kwargs)



# Example usage
if __name__ == "__main__":
    logger = get_logger()
    logger.info("Test log message")
    logger.debug("Debug message")
    logger.warning("Warning message")
