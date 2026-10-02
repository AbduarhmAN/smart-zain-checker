"""4-Tier Internal Data Model & Epistemic Enums for Smart Zain Checker (Gemini Edition).

Implements Section 10 (Task 8: Internal Data Model), Section 4 (Task 6: The 9 Canonical
Engineering Concepts), Section 3 (Data Validation Rules), and Section 9 (Task 9:
Scope-Safe Post-Zain Result Binding).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class AmountState(str, Enum):
    """Strict 5-way (+ invalid text) classification of the file amount cell (Stage 8)."""

    VALID_POSITIVE = "VALID_POSITIVE"
    EXPLICIT_ZERO = "EXPLICIT_ZERO"
    MISSING_BLANK = "MISSING_BLANK"
    INVALID_NULL_OR_FORMULA = "INVALID_NULL_OR_FORMULA"
    NEGATIVE_AMOUNT = "NEGATIVE_AMOUNT"
    INVALID_TEXT = "INVALID_TEXT"


class RowValidationStatus(str, Enum):
    """Row-level validation status before scope filtering (Stage 9 & 11)."""

    VALID = "VALID"
    WARNING_ORPHAN_SERVICE = "WARNING_ORPHAN_SERVICE"
    HOLD_DUPLICATE_ROW = "HOLD_DUPLICATE_ROW"
    HOLD_DUPLICATE_SERVICE = "HOLD_DUPLICATE_SERVICE"
    ERROR_CONFLICTING_SERVICE = "ERROR_CONFLICTING_SERVICE"
    ERROR_INVALID_IDENTIFIER = "ERROR_INVALID_IDENTIFIER"
    ERROR_INVALID_AMOUNT = "ERROR_INVALID_AMOUNT"
    ERROR_MERGED_CELL = "ERROR_MERGED_CELL"


class AccountFileScopeStatus(str, Enum):
    """Completeness of an Account Group within the input workbook (Stage 13)."""

    FILE_SCOPE_COMPLETE = "FILE_SCOPE_COMPLETE"
    FILE_SCOPE_INCOMPLETE_PARTIAL_EXCLUSION = "FILE_SCOPE_INCOMPLETE_PARTIAL_EXCLUSION"
    FILE_SCOPE_INCOMPLETE_HAS_ERRORS = "FILE_SCOPE_INCOMPLETE_HAS_ERRORS"
    FILE_SCOPE_INCOMPLETE_DUPLICATE_CONFLICT = "FILE_SCOPE_INCOMPLETE_DUPLICATE_CONFLICT"
    FILE_SCOPE_INCOMPLETE_SPLIT_COLLECTOR = "FILE_SCOPE_INCOMPLETE_SPLIT_COLLECTOR"

    @property
    def is_complete(self) -> bool:
        return self == AccountFileScopeStatus.FILE_SCOPE_COMPLETE


class AccountConceptTag(str, Enum):
    """The 9 Canonical Engineering Concepts for Account & Service Auditing (Section 4 / Task 6)."""

    REPEATED_ACCOUNT_KEY = "REPEATED_ACCOUNT_KEY"
    VALID_MULTI_SERVICE_ACCOUNT = "VALID_MULTI_SERVICE_ACCOUNT"
    DUPLICATE_SERVICE_SAME_DATA = "DUPLICATE_SERVICE_SAME_DATA"
    CONFLICTING_SERVICE_ENTITY = "CONFLICTING_SERVICE_ENTITY"
    PARTIAL_SCOPE_ACCOUNT = "PARTIAL_SCOPE_ACCOUNT"
    SEARCHABLE_ACCOUNT_GROUP = "SEARCHABLE_ACCOUNT_GROUP"
    REVIEW_REQUIRED_ACCOUNT = "REVIEW_REQUIRED_ACCOUNT"
    FILE_SCOPE_COMPLETE = "FILE_SCOPE_COMPLETE"
    FILE_SCOPE_INCOMPLETE = "FILE_SCOPE_INCOMPLETE"


class SheetClassification(str, Enum):
    """Worksheet classification during Stage 1 discovery."""

    CANDIDATE_DATA_SHEET = "Candidate Data Sheet"
    SUMMARY_OR_PIVOT_SHEET = "Summary / Pivot Sheet"
    EMPTY_OR_NON_RELEVANT_SHEET = "Empty / Non-Relevant Sheet"


class PrefixClassification(str, Enum):
    """Service prefix classification governing Zain Search Routing (Section 1.B & Stage 9)."""

    STARTS_WITH_2 = "STARTS_WITH_2"
    NON_2_PREFIX = "NON_2_PREFIX"
    INVALID_OR_MISSING = "INVALID_OR_MISSING"


class ReconciliationStatus(str, Enum):
    """Scope-Safe Post-Zain Reconciliation Outcomes (Section 9 / Task 9)."""

    PENDING = "PENDING"
    SERVICE_MATCH = "SERVICE_MATCH"
    SERVICE_AMOUNT_DIFFERENCE = "SERVICE_AMOUNT_DIFFERENCE"
    ACCOUNT_TOTAL_MATCH = "ACCOUNT_TOTAL_MATCH"
    ACCOUNT_TOTAL_DIFFERENCE = "ACCOUNT_TOTAL_DIFFERENCE"
    SCOPE_MISMATCH_WARNING_LOCKED = (
        "SCOPE_MISMATCH_WARNING: Partial/Incomplete File Scope — Direct Comparison Locked"
    )
    SEARCH_ERROR_NEEDS_REVIEW = "SEARCH_ERROR_NEEDS_REVIEW"
    AMBIGUOUS_RESULT_NEEDS_REVIEW = "AMBIGUOUS_RESULT_NEEDS_REVIEW"


def _dec_str(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return f"{value:.2f}"


# ==========================================
# A. مستوى الصف الأصلي (Source Row)
# ==========================================
@dataclass
class SourceRow:
    row_uid: str                               # معرف داخلي فريد مثل "Sheet1_Main!R14"
    source_file_name: str                      # اسم الملف الأصلي
    source_file_sha256: str                    # بصمة SHA-256 للملف
    source_sheet: str                          # اسم الورقة في Excel
    source_row: int                            # رقم السطر الفعلي في Excel (1-based)
    is_hidden_in_excel: bool                   # هل السطر مخفي أو مفلتر؟
    raw_cells_snapshot: Dict[str, Any]         # القيم الخام كما قرئت قبل أي تعديل

    # البيانات بعد التنظيف (Normalized Attributes)
    collector_name_raw: Optional[str] = None
    collector_name_norm: Optional[str] = None
    customer_name_raw: Optional[str] = None
    customer_name_norm: Optional[str] = None
    account_number_raw: Optional[str] = None
    account_number_norm: Optional[str] = None
    service_number_raw: Optional[str] = None
    service_number_norm: Optional[str] = None
    file_amount_raw: Any = None
    file_amount_decimal: Optional[Decimal] = None
    amount_state: AmountState = AmountState.MISSING_BLANK
    main_status_raw: Optional[str] = None
    main_status_norm: Optional[str] = None
    sub_status_raw: Optional[str] = None
    sub_status_norm: Optional[str] = None
    metadata_amounts: Dict[str, Any] = field(default_factory=dict)

    # خصائص التوجيه والتحقق (Scoping & Validation)
    prefix_classification: PrefixClassification = PrefixClassification.INVALID_OR_MISSING
    service_starts_with_2: Optional[bool] = None
    collector_in_scope: bool = True
    status_in_scope: bool = True
    include_in_scope: bool = True
    exclusion_reasons: List[str] = field(default_factory=list)
    validation_status: RowValidationStatus = RowValidationStatus.VALID
    validation_errors: List[str] = field(default_factory=list)
    validation_rule_codes: List[str] = field(default_factory=list)
    normalization_notes: List[str] = field(default_factory=list)
    routing_disposition: str = "UNCLASSIFIED"  # SERVICE_SEARCH | ACCOUNT_SEARCH | EXCLUDED | BLOCKED_REVIEW

    # الروابط العكسية (Traceability Links)
    linked_account_group_key: Optional[str] = None
    linked_search_task_id: Optional[str] = None

    # نتائج ما بعد البحث (Post-Zain Reconciliation Fields)
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.PENDING
    zain_service_amount: Optional[Decimal] = None
    zain_account_total_context: Optional[Decimal] = None
    service_variance: Optional[Decimal] = None
    reconciliation_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "row_uid": self.row_uid,
            "source_file_name": self.source_file_name,
            "source_file_sha256": self.source_file_sha256,
            "source_sheet": self.source_sheet,
            "source_row": self.source_row,
            "is_hidden_in_excel": self.is_hidden_in_excel,
            "raw_cells_snapshot": self.raw_cells_snapshot,
            "collector_name_raw": self.collector_name_raw,
            "collector_name_norm": self.collector_name_norm,
            "customer_name_raw": self.customer_name_raw,
            "customer_name_norm": self.customer_name_norm,
            "account_number_raw": self.account_number_raw,
            "account_number_norm": self.account_number_norm,
            "service_number_raw": self.service_number_raw,
            "service_number_norm": self.service_number_norm,
            "file_amount_raw": str(self.file_amount_raw) if self.file_amount_raw is not None else None,
            "file_amount_decimal": _dec_str(self.file_amount_decimal),
            "amount_state": self.amount_state.value,
            "main_status_raw": self.main_status_raw,
            "main_status_norm": self.main_status_norm,
            "prefix_classification": self.prefix_classification.value,
            "service_starts_with_2": self.service_starts_with_2,
            "include_in_scope": self.include_in_scope,
            "exclusion_reasons": self.exclusion_reasons,
            "validation_status": self.validation_status.value,
            "validation_errors": self.validation_errors,
            "validation_rule_codes": self.validation_rule_codes,
            "normalization_notes": self.normalization_notes,
            "routing_disposition": self.routing_disposition,
            "linked_account_group_key": self.linked_account_group_key,
            "linked_search_task_id": self.linked_search_task_id,
            "reconciliation_status": self.reconciliation_status.value,
            "zain_service_amount": _dec_str(self.zain_service_amount),
            "zain_account_total_context": _dec_str(self.zain_account_total_context),
            "service_variance": _dec_str(self.service_variance),
            "reconciliation_note": self.reconciliation_note,
        }


# ==========================================
# B. كيان الخدمة المستقل (Service Entity)
# ==========================================
@dataclass
class ServiceEntity:
    """Represents a distinct debt/service entity without merging across rows or accounts."""

    service_number: str
    account_numbers: List[str]
    source_row_uids: List[str]
    starts_with_2: bool
    file_amount: Optional[Decimal]
    amount_state: AmountState
    main_status: str
    is_unique: bool
    duplicate_or_conflict_type: Optional[str] = None
    linked_search_task_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service_number": self.service_number,
            "account_numbers": self.account_numbers,
            "source_row_uids": self.source_row_uids,
            "starts_with_2": self.starts_with_2,
            "file_amount": _dec_str(self.file_amount),
            "amount_state": self.amount_state.value,
            "main_status": self.main_status,
            "is_unique": self.is_unique,
            "duplicate_or_conflict_type": self.duplicate_or_conflict_type,
            "linked_search_task_id": self.linked_search_task_id,
        }


# ==========================================
# C. مستوى الحساب المجمّع (Account Group)
# ==========================================
@dataclass
class AccountGroup:
    account_number: str                        # رقم الحساب المطبّع
    source_row_uids: List[str]                 # جميع الصفوف المرتبطة بالحساب عبر كل الأوراق
    distinct_sheets: List[str]                 # الأوراق التي ظهر فيها الحساب
    distinct_customer_names: List[str]         # لكشف تعارض الأسماء تحت نفس الحساب
    distinct_collectors: List[str]             # لكشف انقسام الحساب بين أكثر من محصل

    # تصنيف الخدمات داخل الحساب (بدون دمجها)
    all_service_rows: List[str]                # كافة صفوف الحساب
    included_valid_service_rows: List[str]     # الصفوف الصالحة الداخلة في الفحص
    included_starts_with_2_rows: List[str]     # خدمات تبدأ بـ 2 (تحتاج Service Search)
    included_non_2_rows: List[str]             # خدمات لا تبدأ بـ 2 (تحتاج Account Search)
    excluded_service_rows: List[str]           # الصفوف المستبعدة (بالحالة أو المحصل)
    invalid_or_conflict_rows: List[str]        # الصفوف التالفة أو المتكررة

    # المجاميع المالية المفصولة حسب النطاق (Strict Scoped Totals)
    included_starts_with_2_sum: Decimal = Decimal("0.00")
    included_non_2_sum: Decimal = Decimal("0.00")
    included_total_sum: Decimal = Decimal("0.00")
    excluded_known_amount_sum: Decimal = Decimal("0.00")
    all_known_file_rows_sum: Decimal = Decimal("0.00")

    # قرارات النطاق والبحث
    requires_service_searches: bool = False
    requires_account_search: bool = False
    file_scope_status: AccountFileScopeStatus = AccountFileScopeStatus.FILE_SCOPE_COMPLETE
    scope_incompleteness_reasons: List[str] = field(default_factory=list)
    concept_tags: List[AccountConceptTag] = field(default_factory=list)
    customer_name_warnings: List[str] = field(default_factory=list)
    account_search_task_id: Optional[str] = None

    # نتائج ما بعد البحث على مستوى الحساب (Post-Zain Account-Level State)
    zain_account_total: Optional[Decimal] = None
    account_reconciliation_status: ReconciliationStatus = ReconciliationStatus.PENDING
    account_variance: Optional[Decimal] = None
    account_reconciliation_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_number": self.account_number,
            "source_row_uids": self.source_row_uids,
            "distinct_sheets": self.distinct_sheets,
            "distinct_customer_names": self.distinct_customer_names,
            "distinct_collectors": self.distinct_collectors,
            "all_service_rows": self.all_service_rows,
            "included_valid_service_rows": self.included_valid_service_rows,
            "included_starts_with_2_rows": self.included_starts_with_2_rows,
            "included_non_2_rows": self.included_non_2_rows,
            "excluded_service_rows": self.excluded_service_rows,
            "invalid_or_conflict_rows": self.invalid_or_conflict_rows,
            "included_starts_with_2_sum": _dec_str(self.included_starts_with_2_sum),
            "included_non_2_sum": _dec_str(self.included_non_2_sum),
            "included_total_sum": _dec_str(self.included_total_sum),
            "excluded_known_amount_sum": _dec_str(self.excluded_known_amount_sum),
            "all_known_file_rows_sum": _dec_str(self.all_known_file_rows_sum),
            "requires_service_searches": self.requires_service_searches,
            "requires_account_search": self.requires_account_search,
            "file_scope_status": self.file_scope_status.value,
            "scope_incompleteness_reasons": self.scope_incompleteness_reasons,
            "concept_tags": [tag.value for tag in self.concept_tags],
            "customer_name_warnings": self.customer_name_warnings,
            "account_search_task_id": self.account_search_task_id,
            "zain_account_total": _dec_str(self.zain_account_total),
            "account_reconciliation_status": self.account_reconciliation_status.value,
            "account_variance": _dec_str(self.account_variance),
            "account_reconciliation_note": self.account_reconciliation_note,
        }


# ==========================================
# D. مستوى مهمة البحث (Search Task)
# ==========================================
@dataclass
class SearchTask:
    task_id: str                               # معرف المهمة مثل "SRV-001" أو "ACC-001"
    search_type: str                           # "SERVICE" أو "ACCOUNT"
    search_key: str                            # الرقم الذي سيُبحث عنه في زين
    account_number: Optional[str]              # رقم الحساب المرجعي
    service_number: Optional[str]              # يملأ فقط إذا كان search_type == "SERVICE"

    linked_row_uids: List[str]                 # الصفوف المرتبطة بهذه المهمة (1 لـ SERVICE، و1..N لـ ACCOUNT)
    linked_service_numbers: List[str]          # الخدمات المرتبطة بهذه المهمة
    source_sheets: List[str]                   # الأوراق المصدرية

    expected_result_scope: str                 # "SERVICE_LEVEL" أو "ACCOUNT_LEVEL_TOTAL"
    file_comparison_amount: Optional[Decimal]  # المبلغ المقابل في نفس النطاق (أو None إذا النطاق غير مكتمل)
    is_file_scope_complete: bool               # هل يُسمح بالمقارنة المباشرة؟
    creation_reason: str                       # سبب إنشاء المهمة للتدقيق

    # حقول إضافية لمهمة البحث بالحساب (AccountSearchTask Context)
    triggering_services: List[str] = field(default_factory=list)
    triggering_source_rows: List[str] = field(default_factory=list)
    all_account_services_in_file: List[str] = field(default_factory=list)
    file_included_non2_sum: Optional[Decimal] = None
    file_all_included_sum: Optional[Decimal] = None
    file_all_known_rows_sum: Optional[Decimal] = None
    account_file_scope_status: Optional[AccountFileScopeStatus] = None

    # حقول ما بعد البحث (Post-Zain Execution State)
    search_status: str = "PENDING"             # PENDING | SUCCESS | SEARCH_ERROR | AMBIGUOUS
    zain_observed_amount: Optional[Decimal] = None
    raw_observed_screen_text: Optional[str] = None
    error_or_review_details: Optional[str] = None
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.PENDING
    reconciliation_variance: Optional[Decimal] = None
    reconciliation_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "search_type": self.search_type,
            "search_key": self.search_key,
            "account_number": self.account_number,
            "service_number": self.service_number,
            "linked_row_uids": self.linked_row_uids,
            "linked_service_numbers": self.linked_service_numbers,
            "source_sheets": self.source_sheets,
            "expected_result_scope": self.expected_result_scope,
            "file_comparison_amount": _dec_str(self.file_comparison_amount),
            "is_file_scope_complete": self.is_file_scope_complete,
            "creation_reason": self.creation_reason,
            "triggering_services": self.triggering_services,
            "triggering_source_rows": self.triggering_source_rows,
            "all_account_services_in_file": self.all_account_services_in_file,
            "file_included_non2_sum": _dec_str(self.file_included_non2_sum),
            "file_all_included_sum": _dec_str(self.file_all_included_sum),
            "file_all_known_rows_sum": _dec_str(self.file_all_known_rows_sum),
            "account_file_scope_status": (
                self.account_file_scope_status.value if self.account_file_scope_status else None
            ),
            "search_status": self.search_status,
            "zain_observed_amount": _dec_str(self.zain_observed_amount),
            "raw_observed_screen_text": self.raw_observed_screen_text,
            "error_or_review_details": self.error_or_review_details,
            "reconciliation_status": self.reconciliation_status.value,
            "reconciliation_variance": _dec_str(self.reconciliation_variance),
            "reconciliation_note": self.reconciliation_note,
        }


# ==========================================
# E. خطة البحث المختومة قبل زين (PreZainSearchPlan)
# ==========================================
@dataclass
class PreZainSearchPlan:
    source_file_name: str
    source_file_sha256: str
    canonical_amount_column_info: Dict[str, Any]
    sheet_discovery_report: List[Dict[str, Any]]
    warnings: List[str]

    source_rows: List[SourceRow]
    service_entities: List[ServiceEntity]
    account_groups: List[AccountGroup]
    service_search_tasks: List[SearchTask]
    account_search_tasks: List[SearchTask]

    # Readiness Gate & Checksum Metrics
    total_input_rows: int
    service_searchable_rows_count: int
    account_searchable_rows_count: int
    excluded_rows_count: int
    blocked_or_review_rows_count: int
    invariant_balanced: bool
    ready_for_zain_execution: bool

    @property
    def all_search_tasks(self) -> List[SearchTask]:
        return self.service_search_tasks + self.account_search_tasks

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_file_name": self.source_file_name,
            "source_file_sha256": self.source_file_sha256,
            "canonical_amount_column_info": self.canonical_amount_column_info,
            "sheet_discovery_report": self.sheet_discovery_report,
            "warnings": self.warnings,
            "readiness_summary": {
                "total_input_rows": self.total_input_rows,
                "service_searchable_rows_count": self.service_searchable_rows_count,
                "account_searchable_rows_count": self.account_searchable_rows_count,
                "excluded_rows_count": self.excluded_rows_count,
                "blocked_or_review_rows_count": self.blocked_or_review_rows_count,
                "service_search_tasks_count": len(self.service_search_tasks),
                "deduplicated_account_search_tasks_count": len(self.account_search_tasks),
                "total_planned_zain_searches": len(self.service_search_tasks) + len(self.account_search_tasks),
                "invariant_balanced": self.invariant_balanced,
                "READY_FOR_ZAIN_EXECUTION": self.ready_for_zain_execution,
            },
            "source_rows": [r.to_dict() for r in self.source_rows],
            "service_entities": [s.to_dict() for s in self.service_entities],
            "account_groups": [g.to_dict() for g in self.account_groups],
            "service_search_tasks": [t.to_dict() for t in self.service_search_tasks],
            "account_search_tasks": [t.to_dict() for t in self.account_search_tasks],
        }
