"""Domain Models for Smart Zain Checker.
Immutable and pure data structures representing domain entities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class Customer:
    """Represents a customer row extracted from the workbook to be verified."""
    row_number: int
    record_type: str  # 'account', 'wallet', 'mixed'
    lookup_number: str  # Normalized number used in Zain payment portal
    contract: str
    expected_amount: int  # Expected amount in integer Halalas (1 SAR = 100 Halalas)
    expected_amount_2: Optional[int] = None  # Optional secondary amount (e.g. مبلغ العقد) in Halalas
    original_account_number: str = ""
    service_number: str = ""
    customer_name: str = ""
    collector_name: str = ""
    case_status: str = ""
    main_status: str = ""
    sub_status: str = ""
    notes: str = ""
    national_id: str = ""
    phones: str = ""
    supervisor_name: str = ""
    branch_name: str = ""
    followup_date: str = ""
    error_or_review_details: Optional[str] = None


    def to_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "record_type": self.record_type,
            "lookup_number": self.lookup_number,
            "contract": self.contract,
            "expected_amount_sar": self.expected_amount / 100.0,
            "expected_amount_2_sar": (self.expected_amount_2 / 100.0) if self.expected_amount_2 is not None else None,
            "original_account_number": self.original_account_number,
            "service_number": self.service_number,
            "customer_name": self.customer_name,
            "collector_name": self.collector_name,
            "case_status": self.case_status,
            "main_status": self.main_status,
            "sub_status": self.sub_status,
        }


@dataclass(frozen=True)
class Mismatch:
    """Represents a verified mismatch between Excel amounts and Zain portal amount."""
    customer: Customer
    website_amount: int  # In Halalas
    detected_at: datetime = field(default_factory=datetime.now)

    @property
    def difference_halalas(self) -> int:
        effective = self.customer.expected_amount
        if self.customer.expected_amount_2 is not None:
            diff1 = abs(self.website_amount - self.customer.expected_amount)
            diff2 = abs(self.website_amount - self.customer.expected_amount_2)
            effective = self.customer.expected_amount if diff1 <= diff2 else self.customer.expected_amount_2
        return self.website_amount - effective

    @property
    def difference_sar(self) -> float:
        return self.difference_halalas / 100.0


@dataclass(frozen=True)
class ProgressMismatch:
    """Lightweight representation of a mismatch stored in checkpoints."""
    sequence_index: int
    expected_amount: int  # Halalas
    website_amount: int   # Halalas

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence_index": self.sequence_index,
            "expected_amount": self.expected_amount,
            "website_amount": self.website_amount,
        }


@dataclass(frozen=True)
class CheckError:
    """Represents an error encountered during Zain verification."""
    lookup_number: str
    record_type: str
    error_code: str
    error_details: str
    row_numbers: list[int]
    customer_name: str = ""
    collector_name: str = ""
    expected_amount: Optional[int] = None
    detected_at: datetime = field(default_factory=datetime.now)


@dataclass
class CheckResult:
    """Represents the live verification outcome of a single account/service."""
    row: int
    record_type: str
    lookup_number: str
    customer_name: str
    expected_amount: float  # SAR
    live_amount: Optional[float]  # None means no verified website amount.
    status: str             # 'match', 'mismatch', 'error'
    diff_sar: Optional[float] = 0.0
    worker_id: str = "Worker 1"
    timestamp: str = ""
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "record_type": self.record_type,
            "lookup_number": self.lookup_number,
            "customer_name": self.customer_name,
            "expected_amount": self.expected_amount,
            "live_amount": self.live_amount,
            "diff_sar": self.diff_sar,
            "status": self.status,
            "worker_id": self.worker_id,
            "time": self.timestamp,
            "details": self.details,
        }


@dataclass
class QueueJob:
    """Represents a sheet job queued for sequential auditing."""
    id: str
    filename: str
    sheet_index: int
    sheet_name: str
    mode: str
    amount_target: str
    column_mapping: dict[str, Any]
    total_records: int
    target_url: str = "https://business.zain.sa/dashboard/quick-pay"
    status: str = "pending"  # 'pending', 'active', 'completed', 'failed', 'paused'
    created_at: str = ""
    result_file: str = "نتائج فحص زين.xlsx"
    checkpoint_file: str = ".zain-checkpoint.json"
    completed: int = 0
    remaining: int = 0
    matches: int = 0
    mismatches: int = 0
    errors: int = 0

    pricing: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "sheet_index": self.sheet_index,
            "sheet_name": self.sheet_name,
            "mode": self.mode,
            "amount_target": self.amount_target,
            "target_url": self.target_url,
            "column_mapping": self.column_mapping,
            "total_records": self.total_records,
            "status": self.status,
            "created_at": self.created_at,
            "result_file": self.result_file,
            "checkpoint_file": self.checkpoint_file,
            "completed": self.completed,
            "remaining": self.remaining,
            "matches": self.matches,
            "mismatches": self.mismatches,
            "errors": self.errors,
            "pricing": self.pricing,
        }
