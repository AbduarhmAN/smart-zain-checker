from datetime import datetime
from typing import TextIO


def format_console_message(message: str) -> str:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return f"[{timestamp}] {message}"


def log(message: str, *, file: TextIO | None = None) -> None:
    print(format_console_message(message), file=file, flush=True)

