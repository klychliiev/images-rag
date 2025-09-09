from loguru import logger


class LoggingConfig:
    def __init__(self):
        log_file = "application.log"
        log_format = "{time} {level} {message}"
        log_level = "INFO"

        logger.remove()
        logger.add(log_file, format=log_format, level=log_level, rotation="10 MB")

        self.logger = logger

    def trace(self, message):
        """Log a 'TRACE' level message."""
        self.logger.trace(message)

    def debug(self, message):
        """Log a 'DEBUG' level message."""
        self.logger.debug(message)

    def info(self, message):
        """Log an 'INFO' level message."""
        self.logger.info(message)

    def success(self, message):
        """Log a 'SUCCESS' level message."""
        self.logger.success(message)

    def warning(self, message):
        """Log a 'WARNING' level message."""
        self.logger.warning(message)

    def error(self, message):
        """Log an 'ERROR' level message."""
        self.logger.error(message)

    def critical(self, message):
        """Log a 'CRITICAL' level message."""
        self.logger.critical(message)


logger = LoggingConfig()
