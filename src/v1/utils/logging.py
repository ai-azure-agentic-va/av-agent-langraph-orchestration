import logging
import logging.config
import logging.handlers
import contextvars
import json
import uuid
import sys
import os
import re
from datetime import datetime, timezone

PROCESS_RUN_ID = f"run_{uuid.uuid4().hex[:8]}"

# ---------------------------------------------------------
# CUSTOM LEVEL SETUP
# ---------------------------------------------------------
OUTPUT_LEVEL_NUM = 26
OUTPUT_LEVEL_NAME = "OUTPUT"

logging.addLevelName(OUTPUT_LEVEL_NUM, OUTPUT_LEVEL_NAME)

def output(self, message, *args, **kwargs):
    if self.isEnabledFor(OUTPUT_LEVEL_NUM):
        self._log(OUTPUT_LEVEL_NUM, message, args, **kwargs)

logging.Logger.output = output

# ---------------------------------------------------------
# CONTEXT VARIABLES & FILTERS
# ---------------------------------------------------------
current_task_id = contextvars.ContextVar("task_id", default="system")

class TaskIdFilter(logging.Filter):
    """Injects the task_id into the log record."""
    def filter(self, record):
        record.task_id = current_task_id.get()
        return True

class ExactLevelFilter(logging.Filter):
    """Ensures a handler only processes ONE specific log level."""
    def __init__(self, level_num):
        super().__init__()
        self.level_num = level_num

    def filter(self, record):
        return record.levelno == self.level_num

class MinimumLevelFilter(logging.Filter):
    """Ensures a handler processes a minimum level (Useful for catching Error AND Critical in one file)."""
    def __init__(self, level_num):
        super().__init__()
        self.level_num = level_num

    def filter(self, record):
        return record.levelno >= self.level_num

class Base64TruncationFilter(logging.Filter):
    """Globally truncates massive base64 image strings in any log message or its arguments."""
    def __init__(self):
        super().__init__()
        # Matches data:image/png;base64,XXXXXX...
        self.b64_pattern = re.compile(r'(data:image/[a-zA-Z0-9+]+;base64,)[a-zA-Z0-9+/=]{200,}')
        # Matches "data": "XXXXXX..." (Common in JSON payloads)
        self.raw_b64_pattern = re.compile(r'("data"\s*:\s*\\?")[a-zA-Z0-9+/=\\]{200,}(\\?")')
        # Absolute fallback: Truncate any continuous string of Base64 chars longer than 5000 characters
        self.fallback_pattern = re.compile(r'[a-zA-Z0-9+/=]{5000,}')

    def _truncate(self, text):
        if not isinstance(text, str):
            return text
        text = self.b64_pattern.sub(r'\1[...BASE64_IMAGE_TRUNCATED...]', text)
        text = self.raw_b64_pattern.sub(r'\1[...BASE64_IMAGE_TRUNCATED...]\2', text)
        text = self.fallback_pattern.sub(r'[...BASE64_IMAGE_TRUNCATED...]', text)
        return text

    def filter(self, record):
        # Truncate the main message if it contains the base64 string
        if isinstance(record.msg, str):
            record.msg = self._truncate(record.msg)
        
        # Truncate the arguments if the base64 string is passed via string formatting
        if record.args:
            record.args = tuple(self._truncate(arg) if isinstance(arg, str) else arg for arg in record.args)
            
        return True

# ---------------------------------------------------------
# JSON FORMATTER
# ---------------------------------------------------------
class JSONFormatter(logging.Formatter):
    """Formats standard python logging records into JSON."""
    def format(self, record):
        log_record = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "run_id": PROCESS_RUN_ID,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if hasattr(record, "task_id"):
            log_record["task_id"] = record.task_id
        if hasattr(record, "duration_ms"):
            log_record["duration_ms"] = record.duration_ms
        
        # This natively extracts the traceback and adds it to the JSON if exc_info is present
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_record)

# ---------------------------------------------------------
# DICTIONARY CONFIGURATION
# ---------------------------------------------------------
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": JSONFormatter,
        },
        "console_formatter": {
            "format": "[dim][%(task_id)s][/dim] %(message)s",
            "datefmt": "[%X]",
        },
    },
    "filters": {
        "task_id_filter": {"()": TaskIdFilter},
        "truncate_base64": {"()": Base64TruncationFilter},
        
        # Exact level filters for splitting files
        "only_debug": {"()": ExactLevelFilter, "level_num": logging.DEBUG},
        "only_info": {"()": ExactLevelFilter, "level_num": logging.INFO},
        "only_audit": {"()": ExactLevelFilter, "level_num": OUTPUT_LEVEL_NUM},
        "only_warning": {"()": ExactLevelFilter, "level_num": logging.WARNING},
        
        # We use minimum level for error so it catches both ERROR (40) and CRITICAL (50)
        "error_and_above": {"()": MinimumLevelFilter, "level_num": logging.ERROR}, 
    },
    "handlers": {
        # 1. Console Output (Human Readable)
        "console": {
            "class": "rich.logging.RichHandler",
            "formatter": "console_formatter",
            "filters": ["task_id_filter", "truncate_base64"],
            "rich_tracebacks": True, # This makes exceptions print beautifully in the console
            "show_path": True,
            "markup": True,
        },
        
        # 2. The "All Logs" File
        "file_all": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logs/all_logs.jsonl",
            "maxBytes": 52428800, # 50 MB per file
            "backupCount": 10,    # Keep up to 500MB of history
            "formatter": "json",
            "filters": ["task_id_filter", "truncate_base64"],
        },
        
        # 3. Separated Level Files
        "file_debug": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logs/debug.jsonl",
            "maxBytes": 10485760, # 10 MB
            "backupCount": 3,
            "formatter": "json",
            "filters": ["task_id_filter", "truncate_base64", "only_debug"],
        },
        "file_info": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logs/info.jsonl",
            "maxBytes": 10485760,
            "backupCount": 3,
            "formatter": "json",
            "filters": ["task_id_filter", "truncate_base64", "only_info"],
        },
        "file_output": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logs/output.jsonl",
            "maxBytes": 10485760,
            "backupCount": 3,
            "formatter": "json",
            "filters": ["task_id_filter", "truncate_base64", "only_audit"],
        },
        "file_warning": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logs/warning.jsonl",
            "maxBytes": 10485760,
            "backupCount": 3,
            "formatter": "json",
            "filters": ["task_id_filter", "truncate_base64", "only_warning"],
        },
        "file_error": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logs/error.jsonl",
            "maxBytes": 10485760,
            "backupCount": 5, # Keep more errors
            "formatter": "json",
            "filters": ["task_id_filter", "truncate_base64", "error_and_above"],
        }
    },
    "root": {
        "level": "DEBUG", # Capture absolutely everything at the root
        "handlers": [
            "console", 
            "file_all", 
            "file_debug", 
            "file_info", 
            "file_output", 
            "file_warning", 
            "file_error",
        ],
    },
    "loggers": {
        # Keep underlying noisy libraries muted
        "websockets.client": {"level": "INFO"},
        "websockets.server": {"level": "INFO"},
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
        "urllib3": {"level": "WARNING"},
        "openai": {"level": "WARNING"},
        "langchain": {"level": "WARNING"},
        "langsmith": {"level": "ERROR"},
    },
}

# Ensure the 'logs' directory exists before starting
os.makedirs("logs", exist_ok=True)

# ---------------------------------------------------------
# UNHANDLED EXCEPTION CATCHER
# ---------------------------------------------------------
def handle_unhandled_exception(exc_type, exc_value, exc_traceback):
    """
    Hooks into Python's global exception handler. 
    Prevents crash logs from bypassing the logging system.
    """
    # Don't intercept keyboard interrupts (Ctrl+C)
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    # Log the exception as CRITICAL, which triggers our error handlers
    logger = logging.getLogger("system.uncaught")
    logger.critical(
        "Uncaught Exception", 
        exc_info=(exc_type, exc_value, exc_traceback)
    )

# Override the default global exception hook
sys.excepthook = handle_unhandled_exception