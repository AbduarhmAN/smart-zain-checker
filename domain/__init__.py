"""Domain Core Package - Smart Zain Checker.
Contains pure business logic, financial models, and workbook parsing.
Zero dependencies on web servers, GUI frameworks, or browser automation.
"""
from domain.models import Customer, Mismatch, CheckError, QueueJob, CheckResult

__all__ = [
    "Customer",
    "Mismatch",
    "CheckError",
    "QueueJob",
    "CheckResult",
]
