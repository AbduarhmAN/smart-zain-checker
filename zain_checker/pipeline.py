"""16-Stage Pre-Zain Processing Pipeline for Smart Zain Checker (Gemini Edition).

Implements:
- Section 2: End-to-End Pre-Zain Process (Stages 1 to 15 / 24 Pre-Zain Points)
- Section 3: Data Validation Matrix (VAL-ACC-01..03, VAL-SRV-01..03, VAL-AMT-01..03)
- Section 4: Account Grouping Audit & The 9 Canonical Engineering Concepts
- Section 5 & 6: Service Search Logic & Deduplicated Account Search Logic
- Section 7 & 8: All 28 Practical Cases & All 38 Edge Cases
- Section 12: All 20 Logical Prohibitions
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import unicodedata
from xml.etree import ElementTree
from zipfile import ZipFile

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries

from .domain_models import (
    AccountConceptTag,
    AccountFileScopeStatus,
    AccountGroup,
    AmountState,
    PreZainSearchPlan,
    PrefixClassification,
    RowValidationStatus,
    SearchTask,
    ServiceEntity,
    SheetClassification,
    SourceRow,
)


# ============================================================================
# Constants & Canonical Header Dictionary (Stage 2)
# ============================================================================

ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
EASTERN_ARABIC_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
WESTERN_DIGITS = "0123456789"
DIGIT_TRANSLATION_TABLE = str.maketrans(
    ARABIC_INDIC_DIGITS + EASTERN_ARABIC_DIGITS,
    WESTERN_DIGITS + WESTERN_DIGITS,
)

# Invisible & control characters stripped from leading/trailing edges (Edge Case #1 & #2)
INVISIBLE_EDGE_CHARS = (
    " \t\r\n\u00a0\u200b\u200c\u200d\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff"
)
INVISIBLE_CONTROL_REGEX = re.compile(
    r"[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]"
)

NULL_LIKE_TOKENS: Set[str] = {
    "null",
    "none",
    "nan",
    "n/a",
    "na",
    "#n/a",
    "#value!",
    "#ref!",
    "#div/0!",
    "#name?",
    "#num!",
    "#null!",
    "-",
    "--",
    "—",
}

FORMULA_ERROR_TOKENS: Set[str] = {
    "#n/a",
    "#value!",
    "#ref!",
    "#div/0!",
    "#name?",
    "#num!",
    "#null!",
    "#error!",
}

CANONICAL_HEADER_DICTIONARY: Dict[str, Tuple[str, ...]] = {
    "account_number": (
        "رقم الحساب",
        "الحساب",
        "رقم العقد",
        "account no",
        "account number",
        "acc_no",
        "account",
        "contract",
    ),
    "service_number": (
        "رقم الخدمة",
        "الخدمة",
        "رقم العقد/الخدمة",
        "رقم المحفظة",
        "المحفظة",
        "service no",
        "service number",
        "msisdn/service",
        "msisdn",
        "service",
    ),
    "file_amount": (
        "مبلغ المديونية في ملف المحصل",
        "مديونية المحصل",
        "المبلغ المتبقي",
        "المبلغ المطلوب",
        "مبلغ المديونية",
        "مبلغ الميدونية",
        "المبلغ",
        "الرصيد",
        "الاجمالي",
        "الإجمالي",
        "amount",
        "balance",
        "file amount",
    ),
    "main_status": (
        "الحالة الرئيسية",
        "الحالة",
        "main status",
        "status",
    ),
    "sub_status": (
        "الحالة الفرعية",
        "sub status",
        "sub_status",
    ),
    "collector_name": (
        "اسم المحصل",
        "المحصل",
        "collector",
        "collector name",
    ),
    "customer_name": (
        "اسم العميل",
        "العميل",
        "customer name",
        "customer",
    ),
}

# Priority order when resolving multiple amount columns (Stage 3)
CANONICAL_AMOUNT_PRIORITY: Tuple[str, ...] = (
    "مبلغ المديونيه في ملف المحصل",
    "مديونيه المحصل",
    "المبلغ المتبقي",
    "المبلغ المطلوب",
    "مبلغ المديونيه",
    "مبلغ الميدونيه",
    "المبلغ",
    "الرصيد",
    "الاجمالي",
    "amount",
    "balance",
)

DEFAULT_EXCLUDED_STATUS_KEYWORDS: Tuple[str, ...] = (
    "مغلق نهائيا",
    "مغلق نهائي",
    "مغلق",
    "مرفوع سابقا",
    "مرفوع",
    "اعفاء",
    "مستبعد",
    "ملغي",
    "مسدد بالكامل",
    "closed",
    "excluded",
)


class SchemaFatalError(RuntimeError):
    """Raised when mandatory columns cannot be discovered in the workbook (Stage 2)."""


class AmbiguousAmountColumnsError(RuntimeError):
    """Raised when multiple financial columns exist without a deterministic canonical winner (Stage 3)."""


# ============================================================================
# Text & Numeric Normalization Utilities (Stages 5, 6, 7, 8, 10)
# ============================================================================

def normalize_arabic_text(value: Any) -> str:
    """Canonical non-destructive Arabic/English text normalizer (Edge Cases #2, #16, #17, #18, #19).

    - Applies NFKC Unicode normalization
    - Converts Arabic-Indic & Eastern Arabic digits to ASCII
    - Removes tatweel/kashida (ـ) and directional/zero-width marks
    - Unifies Alef variants (أ/إ/آ -> ا), Taa Marbuta (ة -> ه), Alef Maqsura (ى -> ي)
    - Strips Arabic diacritics (tashkeel)
    - Collapses multiple spaces and casefolds Latin text
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.translate(DIGIT_TRANSLATION_TABLE)
    text = INVISIBLE_CONTROL_REGEX.sub("", text)
    # Remove Arabic tashkeel (diacritics) and tatweel (\u0640)
    text = re.sub(r"[\u0640\u064b-\u065f\u0670]", "", text)
    text = (
        text.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ٱ", "ا")
        .replace("ة", "ه")
        .replace("ى", "ي")
    )
    text = re.sub(r"\s+", " ", text.strip(INVISIBLE_EDGE_CHARS))
    return text.casefold()


def _reconstruct_from_format_mask(numeric_val: int, number_format: Optional[str]) -> Optional[str]:
    """Reconstructs leading zeros if an integer cell has a custom zero-padding mask like '0000000000' (Stage 5)."""
    if not number_format or numeric_val < 0:
        return None
    clean_fmt = number_format.strip().split(";")[0].strip()
    if re.fullmatch(r"0{2,20}", clean_fmt):
        target_len = len(clean_fmt)
        raw_digits = str(numeric_val)
        if len(raw_digits) < target_len:
            return raw_digits.zfill(target_len)
    return None


def sanitize_identifier_cell(
    raw_value: Any,
    data_type: Optional[str] = None,
    number_format: Optional[str] = None,
    cached_value: Any = None,
) -> Tuple[Optional[str], List[str], List[str]]:
    """Non-destructive identifier extraction & sanitization (Stages 5, 6, 7).

    Returns:
        (normalized_identifier_or_None, validation_errors, normalization_notes)
    """
    notes: List[str] = []
    errors: List[str] = []

    effective_val = raw_value
    if data_type == "f":
        if cached_value is None or str(cached_value).strip().casefold() in FORMULA_ERROR_TOKENS:
            return None, ["FORMULA_EVALUATION_ERROR_IN_IDENTIFIER"], notes
        effective_val = cached_value
        notes.append("FORMULA_EVALUATED")

    if effective_val is None:
        return None, [], notes

    if isinstance(effective_val, bool):
        return None, ["BOOLEAN_IN_IDENTIFIER"], notes

    # Case 1: Stored as Numeric (int or float) in Excel XML
    if isinstance(effective_val, (int, float)):
        if isinstance(effective_val, float):
            if not math.isfinite(effective_val):
                return None, ["NON_FINITE_NUMERIC_IDENTIFIER"], notes
            if not effective_val.is_integer():
                return str(effective_val), ["CORRUPTED_DECIMAL_IDENTIFIER"], notes
            notes.append("Removed .0 float suffix from numeric cell")
        int_val = int(effective_val)
        if int_val < 0:
            return str(int_val), ["NEGATIVE_IDENTIFIER"], notes
        digits_str = str(int_val)
        # Stage 6: Scientific notation / precision loss check (>15 digits in Excel float)
        if len(digits_str) > 15:
            return digits_str, ["CORRUPTED_IDENTIFIER_SCIENTIFIC_NOTATION"], notes
        if len(digits_str) >= 12 and digits_str.endswith("00000"):
            # Suspicious trailing zeros from float scientific truncation
            return digits_str, ["CORRUPTED_IDENTIFIER_SCIENTIFIC_NOTATION"], notes

        # Stage 5: Check Format Mask Reconstruction for leading zeros
        reconstructed = _reconstruct_from_format_mask(int_val, number_format)
        if reconstructed is not None:
            notes.append("Reconstructed Leading Zero from Format Mask")
            return reconstructed, [], notes

        return digits_str, [], notes

    # Case 2: Stored as String
    raw_str = str(effective_val)
    # Strip edge spaces and invisible control marks
    cleaned = INVISIBLE_CONTROL_REGEX.sub("", raw_str.strip(INVISIBLE_EDGE_CHARS))
    if cleaned != raw_str:
        notes.append("Stripped leading/trailing whitespace or invisible marks")

    # Convert Arabic-Indic / Persian digits
    translated = cleaned.translate(DIGIT_TRANSLATION_TABLE)
    if translated != cleaned:
        notes.append("Converted Arabic/Persian digits to ASCII")
    cleaned = translated

    if not cleaned or cleaned.casefold() in NULL_LIKE_TOKENS:
        return None, [], notes

    # Stage 6: Literal Scientific Notation in string (e.g., "2.01234E+11" or "2.011E+11")
    if re.search(r"\d+(?:\.\d+)?[eE][+-]?\d+", cleaned):
        return cleaned, ["CORRUPTED_IDENTIFIER_SCIENTIFIC_NOTATION"], notes

    # Stage 7: Handle ".0" suffix from float-to-string export ONLY if integer part is pure digits
    if re.fullmatch(r"\d+\.0+", cleaned):
        cleaned = cleaned.split(".", 1)[0]
        notes.append("Removed .0 suffix from string identifier")

    # Stage 7 Protection Rule: Never silently strip internal hyphens, slashes, symbols, or letters!
    if not cleaned.isascii() or not cleaned.isdigit():
        return cleaned, ["CORRUPTED_OR_NON_STANDARD_IDENTIFIER_HOLD"], notes

    return cleaned, [], notes


def classify_and_parse_amount(
    raw_value: Any,
    data_type: Optional[str] = None,
    cached_value: Any = None,
    account_norm: Optional[str] = None,
    service_norm: Optional[str] = None,
    sanity_max_amount: Decimal = Decimal("1000000.00"),
) -> Tuple[Optional[Decimal], AmountState, List[str], List[str]]:
    """Strict 5-Way Classification of the File Amount Cell (Stage 8).

    Returns:
        (parsed_decimal_or_None, amount_state, validation_errors, notes)
    """
    notes: List[str] = []
    errors: List[str] = []

    effective_val = raw_value
    if data_type == "f":
        if cached_value is None:
            return (
                None,
                AmountState.INVALID_NULL_OR_FORMULA,
                ["VAL-AMT-01: FORMULA_EVALUATION_ERROR (Uncached Formula)"],
                notes,
            )
        if str(cached_value).strip().casefold() in FORMULA_ERROR_TOKENS:
            return (
                None,
                AmountState.INVALID_NULL_OR_FORMULA,
                [f"VAL-AMT-01: FORMULA_ERROR ({cached_value})"],
                notes,
            )
        effective_val = cached_value
        notes.append("FORMULA_EVALUATED")

    # 1. Blank check (Stage 8: State #3 MISSING_BLANK)
    if effective_val is None:
        return None, AmountState.MISSING_BLANK, ["VAL-AMT-01: MISSING_AMOUNT_REVIEW (Blank)"], notes

    if isinstance(effective_val, bool):
        return None, AmountState.INVALID_TEXT, ["VAL-AMT-02: BOOLEAN_AMOUNT_ERROR"], notes

    if isinstance(effective_val, (int, float, Decimal)):
        if isinstance(effective_val, float) and not math.isfinite(effective_val):
            return (
                None,
                AmountState.INVALID_NULL_OR_FORMULA,
                ["VAL-AMT-01: NON_FINITE_AMOUNT"],
                notes,
            )
        dec_val = Decimal(str(round(effective_val, 4) if isinstance(effective_val, float) else effective_val))
    else:
        raw_text = str(effective_val)
        cleaned = INVISIBLE_CONTROL_REGEX.sub("", raw_text.strip(INVISIBLE_EDGE_CHARS))
        if not cleaned:
            return None, AmountState.MISSING_BLANK, ["VAL-AMT-01: MISSING_AMOUNT_REVIEW (Whitespace Blank)"], notes

        cleaned_trans = cleaned.translate(DIGIT_TRANSLATION_TABLE)
        lower_tok = cleaned_trans.casefold()

        # 2. Null / NaN / Formula Error check (Stage 8: State #4 INVALID_NULL_OR_FORMULA)
        if lower_tok in FORMULA_ERROR_TOKENS or lower_tok in NULL_LIKE_TOKENS:
            return (
                None,
                AmountState.INVALID_NULL_OR_FORMULA,
                [f"VAL-AMT-01: INVALID_AMOUNT_ERROR ({cleaned})"],
                notes,
            )

        # Accounting negative format e.g. "(250.00)"
        is_accounting_neg = False
        if re.fullmatch(r"\(\s*[\d,.\s]+\s*\)", cleaned_trans):
            is_accounting_neg = True
            cleaned_trans = "-" + cleaned_trans.strip("() ")

        # Strip explicit currency symbols (SAR, ريال, ر.س, SR)
        currency_stripped = re.sub(
            r"(?:\bSAR\b|\bSR\b|ريال\s*سعودي|ريال|ر\.س|رس)",
            "",
            cleaned_trans,
            flags=re.IGNORECASE,
        ).strip()
        if currency_stripped != cleaned_trans:
            notes.append("Stripped currency label from amount text")

        # Replace Arabic thousand/decimal separators and normalize leading decimal (.00 -> 0.00)
        currency_stripped = currency_stripped.replace("٬", ",").replace("٫", ".")
        currency_stripped = re.sub(r"^([+-]?)\.(\d)", r"\g<1>0.\2", currency_stripped)

        # Reject ambiguous multi-number text like "400 أو 500" or "300 - 400"
        number_clusters = re.findall(r"[+-]?\d[\d,.]*", currency_stripped)
        if len(number_clusters) != 1:
            return (
                None,
                AmountState.INVALID_TEXT,
                [f"VAL-AMT-02: INVALID_OR_AMBIGUOUS_AMOUNT_TEXT ({cleaned})"],
                notes,
            )

        # Ensure no remaining unexpected text characters outside the number cluster
        leftover = re.sub(r"[+-]?\d[\d,.]*", "", currency_stripped).strip()
        if leftover:
            return (
                None,
                AmountState.INVALID_TEXT,
                [f"VAL-AMT-02: INVALID_TEXT_IN_AMOUNT ({cleaned})"],
                notes,
            )

        token = number_clusters[0].replace(",", "")
        try:
            dec_val = Decimal(token)
            if is_accounting_neg and dec_val > 0:
                dec_val = -dec_val
        except (InvalidOperation, ValueError):
            return (
                None,
                AmountState.INVALID_TEXT,
                [f"VAL-AMT-02: INVALID_DECIMAL_AMOUNT ({cleaned})"],
                notes,
            )

    # Check decimal precision (max 2 decimal places)
    quantized = dec_val.quantize(Decimal("0.01"))
    if (dec_val * Decimal(100)) != (dec_val * Decimal(100)).to_integral_value():
        return (
            dec_val,
            AmountState.INVALID_TEXT,
            [f"VAL-AMT-02: AMOUNT_PRECISION_OVER_TWO_DECIMALS ({dec_val})"],
            notes,
        )

    # 3. Negative Amount check (Stage 8: State #5 NEGATIVE_AMOUNT)
    if quantized < Decimal("0.00"):
        return (
            quantized,
            AmountState.NEGATIVE_AMOUNT,
            [f"VAL-AMT-02: NEGATIVE_AMOUNT_HOLD ({quantized})"],
            notes,
        )

    # 4. Explicit Zero check (Stage 8: State #2 EXPLICIT_ZERO & VAL-AMT-03)
    if quantized == Decimal("0.00"):
        notes.append("VAL-AMT-03: EXPLICIT_ZERO_AMOUNT (Confirmed 0.00)")
        return Decimal("0.00"), AmountState.EXPLICIT_ZERO, [], notes

    # 5. Valid Positive Amount + Outlier / ID-in-Amount Sanity Check (Edge Case #24)
    int_str = str(int(quantized))
    if (
        quantized > sanity_max_amount
        or (account_norm and len(account_norm) >= 6 and int_str == account_norm)
        or (service_norm and len(service_norm) >= 6 and int_str == service_norm)
    ):
        return (
            quantized,
            AmountState.VALID_POSITIVE,
            [f"VAL-AMT-02: SUSPECTED_ID_IN_AMOUNT_COLUMN_HOLD ({quantized})"],
            notes,
        )

    return quantized, AmountState.VALID_POSITIVE, [], notes


# ============================================================================
# Stage 1, 2, 3: Sheet Discovery, Header Detection & Canonical Amount Lock
# ============================================================================

def _match_header_category(header_text: str) -> Optional[str]:
    norm = normalize_arabic_text(header_text)
    if not norm:
        return None
    for category, synonyms in CANONICAL_HEADER_DICTIONARY.items():
        for syn in synonyms:
            norm_syn = normalize_arabic_text(syn)
            if norm == norm_syn or (len(norm_syn) >= 5 and norm_syn in norm):
                return category
    return None


def detect_header_and_schema(
    sheet,
    max_scan_rows: int = 15,
    preferred_amount_column: Optional[int] = None,
    custom_column_mapping: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Scans rows 1..15 to find the header row and map columns (Stage 2 & Stage 3)."""
    if custom_column_mapping:
        header_row = int(custom_column_mapping.get("header_row", 1))
        mapped_cols = {
            k: v for k, v in custom_column_mapping.items() if k != "header_row" and v is not None and int(v) > 0
        }
        return {
            "header_row": header_row,
            "columns": mapped_cols,
            "candidate_amount_columns": [
                {
                    "column": mapped_cols["file_amount"],
                    "letter": get_column_letter(mapped_cols["file_amount"]),
                    "header": str(sheet.cell(header_row, mapped_cols["file_amount"]).value or ""),
                }
            ]
            if "file_amount" in mapped_cols
            else [],
            "match_score": len(mapped_cols),
        }

    best_row = 1
    best_score = -1
    best_mapping: Dict[str, int] = {}
    best_amount_candidates: List[Dict[str, Any]] = []

    max_r = min(max_scan_rows, sheet.max_row or 1)
    max_c = min(60, sheet.max_column or 1)

    for r in range(1, max_r + 1):
        row_mapping: Dict[str, int] = {}
        amount_candidates: List[Dict[str, Any]] = []
        for c in range(1, max_c + 1):
            raw_hdr = sheet.cell(r, c).value
            if raw_hdr is None:
                continue
            cat = _match_header_category(str(raw_hdr))
            if cat == "file_amount":
                amount_candidates.append(
                    {
                        "column": c,
                        "letter": get_column_letter(c),
                        "header": str(raw_hdr).strip(),
                        "norm_header": normalize_arabic_text(raw_hdr),
                    }
                )
            elif cat and cat not in row_mapping:
                row_mapping[cat] = c

        score = len(row_mapping) + (1 if amount_candidates else 0)
        if score > best_score:
            best_score = score
            best_row = r
            best_mapping = row_mapping
            best_amount_candidates = amount_candidates

    # Stage 3: Resolve Canonical Amount Column Lock
    if preferred_amount_column and preferred_amount_column > 0:
        best_mapping["file_amount"] = preferred_amount_column
    elif best_amount_candidates:
        if len(best_amount_candidates) == 1:
            best_mapping["file_amount"] = best_amount_candidates[0]["column"]
        else:
            # Rank by CANONICAL_AMOUNT_PRIORITY
            selected_col = None
            for priority_hdr in CANONICAL_AMOUNT_PRIORITY:
                norm_p = normalize_arabic_text(priority_hdr)
                for cand in best_amount_candidates:
                    if cand["norm_header"] == norm_p or norm_p in cand["norm_header"]:
                        selected_col = cand["column"]
                        break
                if selected_col is not None:
                    break
            if selected_col is None:
                names = ", ".join(f"{c['letter']}:{c['header']}" for c in best_amount_candidates)
                raise AmbiguousAmountColumnsError(
                    f"Ambiguous Amount Columns detected in sheet '{sheet.title}': {names}. "
                    "Please lock one Canonical File Amount Column before execution."
                )
            best_mapping["file_amount"] = selected_col

    return {
        "header_row": best_row,
        "columns": best_mapping,
        "candidate_amount_columns": best_amount_candidates,
        "match_score": best_score,
    }


def _extract_sheet_xml_metadata(
    archive: ZipFile, worksheet_path: str
) -> Tuple[Set[int], List[Tuple[int, int, int, int]], Optional[str]]:
    """Extracts hidden row numbers, merged cell ranges, and autoFilter ref from sheet XML."""
    hidden_rows: Set[int] = set()
    merged_ranges: List[Tuple[int, int, int, int]] = []
    filter_ref: Optional[str] = None

    xml_path = worksheet_path.lstrip("/")
    if xml_path not in archive.namelist():
        return hidden_rows, merged_ranges, filter_ref

    with archive.open(xml_path) as stream:
        for _, element in ElementTree.iterparse(stream, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "row" and element.get("hidden") in ("1", "true"):
                r_attr = element.get("r")
                if r_attr and r_attr.isdigit():
                    hidden_rows.add(int(r_attr))
            elif tag == "mergeCell":
                ref = element.get("ref")
                if ref:
                    merged_ranges.append(range_boundaries(ref))
            elif tag == "autoFilter":
                filter_ref = element.get("ref")
            element.clear()

    return hidden_rows, merged_ranges, filter_ref


def _is_cell_in_vertical_merge(
    row: int, col: int, merged_ranges: Sequence[Tuple[int, int, int, int]]
) -> bool:
    """Returns True if (row, col) participates in a vertical multi-row merged cell range (Edge Case #10)."""
    for min_col, min_row, max_col, max_row in merged_ranges:
        if min_row <= row <= max_row and min_col <= col <= max_col and max_row > min_row:
            return True
    return False


# ============================================================================
# Main 16-Stage Pre-Zain Pipeline Runner
# ============================================================================

def run_pre_zain_pipeline(
    workbook_path: Path,
    target_collector: Optional[str] = None,
    all_collectors: bool = True,
    included_statuses: Optional[Sequence[str]] = None,
    excluded_statuses: Optional[Sequence[str]] = None,
    selected_sheets: Optional[Sequence[str]] = None,
    include_hidden_sheets: bool = False,
    include_hidden_rows: bool = True,
    preferred_amount_column: Optional[int] = None,
    custom_sheet_mappings: Optional[Dict[str, Dict[str, int]]] = None,
    sanity_max_amount: Decimal = Decimal("1000000.00"),
) -> PreZainSearchPlan:
    """Executes the complete Pre-Zain Processing Pipeline (Stages 1 to 15) on an Excel workbook."""
    workbook_path = Path(workbook_path).resolve()
    if not workbook_path.exists():
        raise FileNotFoundError(f"Workbook not found: {workbook_path}")

    source_file_name = workbook_path.name
    source_file_sha256 = hashlib.sha256(workbook_path.read_bytes()).hexdigest()
    pipeline_warnings: List[str] = []

    norm_target_collector = normalize_arabic_text(target_collector) if target_collector else ""
    if norm_target_collector and norm_target_collector not in ("all", "الكل"):
        all_collectors = False
    else:
        all_collectors = True

    norm_included_statuses: Set[str] = (
        {normalize_arabic_text(s) for s in included_statuses if normalize_arabic_text(s)}
        if included_statuses
        else set()
    )
    norm_excluded_statuses: Set[str] = (
        {normalize_arabic_text(s) for s in excluded_statuses if normalize_arabic_text(s)}
        if excluded_statuses
        else {normalize_arabic_text(s) for s in DEFAULT_EXCLUDED_STATUS_KEYWORDS}
    )

    wb_formulas = load_workbook(workbook_path, read_only=False, data_only=False)
    wb_cached = load_workbook(workbook_path, read_only=False, data_only=True)

    sheet_discovery_report: List[Dict[str, Any]] = []
    canonical_amount_info: Dict[str, Any] = {}
    all_source_rows: List[SourceRow] = []

    try:
        with ZipFile(workbook_path, "r") as archive:
            # ----------------------------------------------------------------
            # Stage 1 & 2 & 3: Workbook & Multi-Sheet Discovery + Schema Lock
            # ----------------------------------------------------------------
            for sheet in wb_formulas.worksheets:
                sheet_name = sheet.title
                is_sheet_hidden = getattr(sheet, "sheet_state", "visible") != "visible"
                if is_sheet_hidden:
                    pipeline_warnings.append(
                        f"Warning: Hidden Sheet Detected ('{sheet_name}', state={sheet.sheet_state})"
                    )

                if selected_sheets and sheet_name not in selected_sheets:
                    continue
                if is_sheet_hidden and not include_hidden_sheets and not (selected_sheets and sheet_name in selected_sheets):
                    sheet_discovery_report.append(
                        {
                            "sheet_name": sheet_name,
                            "is_hidden": True,
                            "classification": SheetClassification.EMPTY_OR_NON_RELEVANT_SHEET.value,
                            "included_in_scan": False,
                            "reason": "Hidden sheet excluded by safe default policy",
                        }
                    )
                    continue

                custom_map = (custom_sheet_mappings or {}).get(sheet_name)
                schema = detect_header_and_schema(
                    sheet,
                    preferred_amount_column=preferred_amount_column,
                    custom_column_mapping=custom_map,
                )
                cols = schema["columns"]
                has_mandatory = all(k in cols for k in ("account_number", "service_number", "file_amount"))

                if not has_mandatory:
                    # Classify as Summary/Pivot or Empty/Non-Relevant
                    classification = (
                        SheetClassification.EMPTY_OR_NON_RELEVANT_SHEET
                        if (sheet.max_row or 0) <= 2
                        else SheetClassification.SUMMARY_OR_PIVOT_SHEET
                    )
                    sheet_discovery_report.append(
                        {
                            "sheet_name": sheet_name,
                            "is_hidden": is_sheet_hidden,
                            "classification": classification.value,
                            "included_in_scan": False,
                            "detected_columns": cols,
                        }
                    )
                    if selected_sheets and sheet_name in selected_sheets:
                        missing_keys = [
                            k for k in ("account_number", "service_number", "file_amount") if k not in cols
                        ]
                        raise SchemaFatalError(
                            f"Schema Fatal Error in sheet '{sheet_name}': Missing critical columns {missing_keys}"
                        )
                    continue

                sheet_discovery_report.append(
                    {
                        "sheet_name": sheet_name,
                        "is_hidden": is_sheet_hidden,
                        "classification": SheetClassification.CANDIDATE_DATA_SHEET.value,
                        "included_in_scan": True,
                        "header_row": schema["header_row"],
                        "locked_columns": cols,
                        "candidate_amount_columns": schema["candidate_amount_columns"],
                    }
                )
                canonical_amount_info[sheet_name] = {
                    "locked_amount_column": cols["file_amount"],
                    "locked_amount_column_letter": get_column_letter(cols["file_amount"]),
                    "locked_amount_header": str(
                        sheet.cell(schema["header_row"], cols["file_amount"]).value or ""
                    ).strip(),
                    "candidate_amount_columns": schema["candidate_amount_columns"],
                    "immutable_lock": True,
                }

                # ------------------------------------------------------------
                # Stage 4: Structural Integrity Scan & Raw Lineage Extraction
                # ------------------------------------------------------------
                value_sheet = wb_cached[sheet_name]
                ws_path = getattr(sheet, "_worksheet_path", f"xl/worksheets/sheet{wb_formulas.sheetnames.index(sheet_name)+1}.xml")
                hidden_rows, merged_ranges, filter_ref = _extract_sheet_xml_metadata(archive, ws_path)
                if not merged_ranges and sheet.merged_cells.ranges:
                    merged_ranges = [range_boundaries(str(rng)) for rng in sheet.merged_cells.ranges]

                header_row = schema["header_row"]
                max_col = max(list(cols.values()) + [sheet.max_column or 1])

                for row_num in range(header_row + 1, (sheet.max_row or header_row) + 1):
                    raw_snapshot: Dict[str, Any] = {}
                    non_empty_cell_found = False

                    for c_idx in range(1, min(max_col + 1, 65)):
                        cell = sheet.cell(row_num, c_idx)
                        c_val = cell.value
                        if c_val is not None and str(c_val).strip() != "":
                            non_empty_cell_found = True
                        if c_val is not None:
                            cached_c_val = value_sheet.cell(row_num, c_idx).value
                            raw_snapshot[get_column_letter(c_idx)] = {
                                "value": str(c_val) if not isinstance(c_val, (int, float, bool)) else c_val,
                                "data_type": cell.data_type,
                                "number_format": cell.number_format,
                                "cached_value": (
                                    str(cached_c_val)
                                    if cached_c_val is not None and not isinstance(cached_c_val, (int, float, bool))
                                    else cached_c_val
                                )
                                if cell.data_type == "f"
                                else None,
                            }

                    if not non_empty_cell_found:
                        continue

                    is_row_hidden = (row_num in hidden_rows) or bool(
                        sheet.row_dimensions[row_num].hidden if row_num in sheet.row_dimensions else False
                    )

                    def _get_cell_tuple(col_key: str) -> Tuple[Any, Optional[str], Optional[str], Any]:
                        c_num = cols.get(col_key)
                        if not c_num:
                            return None, None, None, None
                        f_cell = sheet.cell(row_num, c_num)
                        v_cell = value_sheet.cell(row_num, c_num)
                        return f_cell.value, f_cell.data_type, f_cell.number_format, v_cell.value

                    acc_raw, acc_dtype, acc_fmt, acc_cached = _get_cell_tuple("account_number")
                    srv_raw, srv_dtype, srv_fmt, srv_cached = _get_cell_tuple("service_number")
                    amt_raw, amt_dtype, amt_fmt, amt_cached = _get_cell_tuple("file_amount")
                    sts_raw, _, _, sts_cached = _get_cell_tuple("main_status")
                    sub_raw, _, _, sub_cached = _get_cell_tuple("sub_status")
                    col_raw, _, _, col_cached = _get_cell_tuple("collector_name")
                    cus_raw, _, _, cus_cached = _get_cell_tuple("customer_name")

                    # Collect reference metadata amounts from any other amount columns (Stage 3)
                    metadata_amounts: Dict[str, Any] = {}
                    for cand in schema["candidate_amount_columns"]:
                        if cand["column"] != cols["file_amount"]:
                            metadata_amounts[cand["header"]] = value_sheet.cell(row_num, cand["column"]).value

                    # --------------------------------------------------------
                    # Stage 5, 6, 7: Non-Destructive Sanitization
                    # --------------------------------------------------------
                    acc_norm, acc_errs, acc_notes = sanitize_identifier_cell(
                        acc_raw, acc_dtype, acc_fmt, acc_cached
                    )
                    srv_norm, srv_errs, srv_notes = sanitize_identifier_cell(
                        srv_raw, srv_dtype, srv_fmt, srv_cached
                    )

                    # --------------------------------------------------------
                    # Stage 8: Strict 5-Way Amount Classification
                    # --------------------------------------------------------
                    amt_dec, amt_state, amt_errs, amt_notes = classify_and_parse_amount(
                        amt_raw,
                        amt_dtype,
                        amt_cached,
                        account_norm=acc_norm if not acc_errs else None,
                        service_norm=srv_norm if not srv_errs else None,
                        sanity_max_amount=sanity_max_amount,
                    )

                    sts_val = sts_cached if (sts_raw is not None and str(sts_raw).startswith("=")) else sts_raw
                    sub_val = sub_cached if (sub_raw is not None and str(sub_raw).startswith("=")) else sub_raw
                    col_val = col_cached if (col_raw is not None and str(col_raw).startswith("=")) else col_raw
                    cus_val = cus_cached if (cus_raw is not None and str(cus_raw).startswith("=")) else cus_raw

                    row_obj = SourceRow(
                        row_uid=f"{sheet_name}!R{row_num}",
                        source_file_name=source_file_name,
                        source_file_sha256=source_file_sha256,
                        source_sheet=sheet_name,
                        source_row=row_num,
                        is_hidden_in_excel=is_row_hidden,
                        raw_cells_snapshot=raw_snapshot,
                        collector_name_raw=str(col_val) if col_val is not None else None,
                        collector_name_norm=normalize_arabic_text(col_val),
                        customer_name_raw=str(cus_val) if cus_val is not None else None,
                        customer_name_norm=normalize_arabic_text(cus_val),
                        account_number_raw=str(acc_raw) if acc_raw is not None else None,
                        account_number_norm=acc_norm,
                        service_number_raw=str(srv_raw) if srv_raw is not None else None,
                        service_number_norm=srv_norm,
                        file_amount_raw=amt_raw,
                        file_amount_decimal=amt_dec,
                        amount_state=amt_state,
                        main_status_raw=str(sts_val) if sts_val is not None else None,
                        main_status_norm=normalize_arabic_text(sts_val),
                        sub_status_raw=str(sub_val) if sub_val is not None else None,
                        sub_status_norm=normalize_arabic_text(sub_val),
                        metadata_amounts=metadata_amounts,
                        normalization_notes=acc_notes + srv_notes + amt_notes,
                    )

                    # Check Merged Cells (Edge Case #10 & Example 28)
                    merged_amt = _is_cell_in_vertical_merge(row_num, cols["file_amount"], merged_ranges)
                    merged_srv = _is_cell_in_vertical_merge(row_num, cols["service_number"], merged_ranges)
                    merged_acc = _is_cell_in_vertical_merge(row_num, cols["account_number"], merged_ranges)
                    if merged_amt or merged_srv:
                        row_obj.validation_status = RowValidationStatus.ERROR_MERGED_CELL
                        row_obj.validation_errors.append("HARD_STOP: MERGED_AMOUNT_OR_SERVICE_CELL")
                    elif merged_acc:
                        row_obj.validation_status = RowValidationStatus.ERROR_MERGED_CELL
                        row_obj.validation_errors.append("HOLD: MERGED_ACCOUNT_CELL_NEEDS_CONFIRMATION")

                    # --------------------------------------------------------
                    # Stage 9: Row-Level Identifier & Amount Validation
                    # --------------------------------------------------------
                    # Service Validation (VAL-SRV-01, VAL-SRV-02)
                    if srv_errs:
                        row_obj.validation_status = RowValidationStatus.ERROR_INVALID_IDENTIFIER
                        for e in srv_errs:
                            row_obj.validation_errors.append(f"VAL-SRV-02: {e}")
                        row_obj.validation_rule_codes.append("VAL-SRV-02")
                        row_obj.prefix_classification = PrefixClassification.INVALID_OR_MISSING
                        row_obj.service_starts_with_2 = None
                    elif not srv_norm:
                        row_obj.validation_status = RowValidationStatus.ERROR_INVALID_IDENTIFIER
                        row_obj.validation_errors.append("VAL-SRV-01: MISSING_SERVICE_NUMBER")
                        row_obj.validation_rule_codes.append("VAL-SRV-01")
                        row_obj.prefix_classification = PrefixClassification.INVALID_OR_MISSING
                        row_obj.service_starts_with_2 = None
                    else:
                        if srv_norm.startswith("2"):
                            row_obj.prefix_classification = PrefixClassification.STARTS_WITH_2
                            row_obj.service_starts_with_2 = True
                        else:
                            row_obj.prefix_classification = PrefixClassification.NON_2_PREFIX
                            row_obj.service_starts_with_2 = False

                    # Account Validation (VAL-ACC-01, VAL-ACC-02, VAL-ACC-03)
                    if acc_errs:
                        row_obj.validation_status = RowValidationStatus.ERROR_INVALID_IDENTIFIER
                        for e in acc_errs:
                            row_obj.validation_errors.append(f"VAL-ACC-03: {e}")
                        row_obj.validation_rule_codes.append("VAL-ACC-03")
                        row_obj.account_number_norm = None
                    elif not acc_norm:
                        if row_obj.service_starts_with_2 is True and not srv_errs:
                            # VAL-ACC-02: Orphan Service Starting with 2 -> Searchable by Service!
                            if row_obj.validation_status == RowValidationStatus.VALID:
                                row_obj.validation_status = RowValidationStatus.WARNING_ORPHAN_SERVICE
                            row_obj.normalization_notes.append(
                                "VAL-ACC-02: ORPHAN_SERVICE_SEARCHABLE (Missing Account, Service Starts with 2)"
                            )
                            row_obj.validation_rule_codes.append("VAL-ACC-02")
                        else:
                            # VAL-ACC-01: Missing Account for Non-2 or Missing Service -> HARD STOP
                            row_obj.validation_status = RowValidationStatus.ERROR_INVALID_IDENTIFIER
                            row_obj.validation_errors.append(
                                "VAL-ACC-01: MISSING_ACCOUNT_NO_FOR_NON_2_SERVICE"
                            )
                            row_obj.validation_rule_codes.append("VAL-ACC-01")

                    # Amount Validation (VAL-AMT-01, VAL-AMT-02, VAL-AMT-03)
                    if amt_errs:
                        if row_obj.validation_status in (
                            RowValidationStatus.VALID,
                            RowValidationStatus.WARNING_ORPHAN_SERVICE,
                        ):
                            row_obj.validation_status = RowValidationStatus.ERROR_INVALID_AMOUNT
                        row_obj.validation_errors.extend(amt_errs)
                        for e in amt_errs:
                            code = e.split(":", 1)[0].strip()
                            if code not in row_obj.validation_rule_codes:
                                row_obj.validation_rule_codes.append(code)
                    elif amt_state == AmountState.EXPLICIT_ZERO:
                        row_obj.validation_rule_codes.append("VAL-AMT-03")

                    all_source_rows.append(row_obj)
    finally:
        wb_formulas.close()
        wb_cached.close()

    if not any(item["included_in_scan"] for item in sheet_discovery_report):
        raise SchemaFatalError(
            f"No valid candidate data sheets with mandatory columns (account, service, amount) found in {workbook_path.name}."
        )

    # ------------------------------------------------------------------------
    # Stage 10 & 11: Global Cross-Sheet Duplicate & Conflict Audit (PRE-FILTER)
    # ------------------------------------------------------------------------
    # Must run BEFORE excluding rows by status/collector (Example 27)!
    services_registry: Dict[str, List[SourceRow]] = defaultdict(list)
    exact_row_signatures: Dict[Tuple[Any, ...], List[SourceRow]] = defaultdict(list)

    for r in all_source_rows:
        sig = (
            r.account_number_norm,
            r.service_number_norm,
            r.file_amount_decimal,
            r.main_status_norm,
            r.collector_name_norm,
        )
        exact_row_signatures[sig].append(r)
        if r.service_number_norm and "VAL-SRV-02" not in r.validation_rule_codes:
            services_registry[r.service_number_norm].append(r)

    # Audit service duplicates & conflicts across the entire workbook
    tainted_accounts_by_cross_service: Set[str] = set()
    service_entities: List[ServiceEntity] = []

    for srv_num, srv_rows in sorted(services_registry.items()):
        distinct_accs = sorted({r.account_number_norm or "<ORPHAN>" for r in srv_rows})
        distinct_amts = {
            r.file_amount_decimal if r.file_amount_decimal is not None else f"RAW:{r.file_amount_raw}"
            for r in srv_rows
        }
        distinct_statuses = {r.main_status_norm for r in srv_rows}

        conflict_type: Optional[str] = None
        if len(srv_rows) > 1:
            if len(distinct_accs) > 1:
                conflict_type = "CROSS_ACCOUNT_SERVICE_CONFLICT"
                for r in srv_rows:
                    r.validation_status = RowValidationStatus.ERROR_CONFLICTING_SERVICE
                    r.validation_errors.append(
                        f"VAL-SRV-03: CROSS_ACCOUNT_SERVICE_CONFLICT (Service {srv_num} under accounts {distinct_accs})"
                    )
                    if "VAL-SRV-03" not in r.validation_rule_codes:
                        r.validation_rule_codes.append("VAL-SRV-03")
                    if r.account_number_norm:
                        tainted_accounts_by_cross_service.add(r.account_number_norm)
            elif len(distinct_amts) > 1:
                conflict_type = "CONFLICTING_SERVICE_AMOUNT"
                for r in srv_rows:
                    r.validation_status = RowValidationStatus.ERROR_CONFLICTING_SERVICE
                    r.validation_errors.append(
                        f"VAL-SRV-03: CONFLICTING_SERVICE_AMOUNT (Service {srv_num} has multiple amounts)"
                    )
                    if "VAL-SRV-03" not in r.validation_rule_codes:
                        r.validation_rule_codes.append("VAL-SRV-03")
            elif len(distinct_statuses) > 1:
                conflict_type = "DUPLICATE_SERVICE_STATUS_CONFLICT"
                for r in srv_rows:
                    r.validation_status = RowValidationStatus.ERROR_CONFLICTING_SERVICE
                    r.validation_errors.append(
                        f"VAL-SRV-03: DUPLICATE_SERVICE_STATUS_CONFLICT (Service {srv_num} has statuses {sorted(distinct_statuses)})"
                    )
                    if "VAL-SRV-03" not in r.validation_rule_codes:
                        r.validation_rule_codes.append("VAL-SRV-03")
            else:
                # Same account + same amount + same status -> Hold Duplicate Service (Never sum twice!)
                conflict_type = "DUPLICATE_SERVICE_SAME_AMOUNT"
                for r in srv_rows:
                    if r.validation_status in (
                        RowValidationStatus.VALID,
                        RowValidationStatus.WARNING_ORPHAN_SERVICE,
                    ):
                        r.validation_status = RowValidationStatus.HOLD_DUPLICATE_SERVICE
                    r.validation_errors.append(
                        f"VAL-SRV-03: DUPLICATE_SERVICE_SAME_AMOUNT (Service {srv_num} repeated in {[x.row_uid for x in srv_rows]})"
                    )
                    if "VAL-SRV-03" not in r.validation_rule_codes:
                        r.validation_rule_codes.append("VAL-SRV-03")

        first_r = srv_rows[0]
        service_entities.append(
            ServiceEntity(
                service_number=srv_num,
                account_numbers=[a for a in distinct_accs if a != "<ORPHAN>"],
                source_row_uids=[r.row_uid for r in srv_rows],
                starts_with_2=bool(first_r.service_starts_with_2),
                file_amount=first_r.file_amount_decimal if len(distinct_amts) == 1 else None,
                amount_state=first_r.amount_state,
                main_status=first_r.main_status_raw or "",
                is_unique=(len(srv_rows) == 1),
                duplicate_or_conflict_type=conflict_type,
            )
        )

    # ------------------------------------------------------------------------
    # Stage 12: Non-Destructive Scope Tagging (Collector & Main Status)
    # ------------------------------------------------------------------------
    for r in all_source_rows:
        # 1. Collector scope check
        if not all_collectors and norm_target_collector:
            if r.collector_name_norm != norm_target_collector and norm_target_collector not in (
                r.collector_name_norm or ""
            ):
                r.collector_in_scope = False
                r.exclusion_reasons.append("EXCLUDED_BY_COLLECTOR")

        # 2. Main Status scope check
        st_norm = r.main_status_norm or ""
        if norm_included_statuses:
            if st_norm not in norm_included_statuses:
                r.status_in_scope = False
                r.exclusion_reasons.append("EXCLUDED_BY_STATUS")
        else:
            if any(excl_kw in st_norm for excl_kw in norm_excluded_statuses if excl_kw):
                r.status_in_scope = False
                r.exclusion_reasons.append("EXCLUDED_BY_STATUS")

        # 3. Hidden row scope check
        if r.is_hidden_in_excel and not include_hidden_rows:
            r.exclusion_reasons.append("EXCLUDED_HIDDEN_ROW")

        r.include_in_scope = (
            r.collector_in_scope
            and r.status_in_scope
            and not (r.is_hidden_in_excel and not include_hidden_rows)
        )

    # ------------------------------------------------------------------------
    # Stage 13: Global Cross-Sheet Account Grouping & Scope Completeness
    # ------------------------------------------------------------------------
    rows_by_account: Dict[str, List[SourceRow]] = defaultdict(list)
    for r in all_source_rows:
        if r.account_number_norm:
            rows_by_account[r.account_number_norm].append(r)
            r.linked_account_group_key = r.account_number_norm

    account_groups: List[AccountGroup] = []
    account_group_map: Dict[str, AccountGroup] = {}

    for acc_num, acc_rows in sorted(rows_by_account.items()):
        distinct_sheets = sorted({r.source_sheet for r in acc_rows})
        distinct_names = sorted({r.customer_name_raw.strip() for r in acc_rows if r.customer_name_raw and r.customer_name_raw.strip()})
        distinct_norm_names = sorted({r.customer_name_norm for r in acc_rows if r.customer_name_norm})
        distinct_collectors = sorted({r.collector_name_norm for r in acc_rows if r.collector_name_norm})

        all_uids = [r.row_uid for r in acc_rows]
        included_valid_uids: List[str] = []
        included_s2_uids: List[str] = []
        included_non2_uids: List[str] = []
        excluded_uids: List[str] = []
        invalid_or_conflict_uids: List[str] = []

        inc_s2_sum = Decimal("0.00")
        inc_non2_sum = Decimal("0.00")
        inc_total_sum = Decimal("0.00")
        excl_known_sum = Decimal("0.00")

        for r in acc_rows:
            is_row_valid = r.validation_status == RowValidationStatus.VALID
            if not is_row_valid:
                invalid_or_conflict_uids.append(r.row_uid)
                if r.file_amount_decimal is not None and r.amount_state in (
                    AmountState.VALID_POSITIVE,
                    AmountState.EXPLICIT_ZERO,
                ):
                    # Preserve in known rows sum ONLY if not a duplicate service clone
                    if r.validation_status not in (
                        RowValidationStatus.HOLD_DUPLICATE_SERVICE,
                        RowValidationStatus.HOLD_DUPLICATE_ROW,
                    ):
                        excl_known_sum += r.file_amount_decimal
            elif not r.include_in_scope:
                excluded_uids.append(r.row_uid)
                if r.file_amount_decimal is not None:
                    excl_known_sum += r.file_amount_decimal
            else:
                included_valid_uids.append(r.row_uid)
                amt = r.file_amount_decimal or Decimal("0.00")
                inc_total_sum += amt
                if r.service_starts_with_2:
                    included_s2_uids.append(r.row_uid)
                    inc_s2_sum += amt
                else:
                    included_non2_uids.append(r.row_uid)
                    inc_non2_sum += amt

        # Special handling when all rows in account are duplicate services of the same amount:
        # All known file sum should reflect the single distinct service amount for reference
        if not included_valid_uids and all(
            r.validation_status in (RowValidationStatus.HOLD_DUPLICATE_SERVICE, RowValidationStatus.HOLD_DUPLICATE_ROW)
            for r in acc_rows
        ):
            first_amt = acc_rows[0].file_amount_decimal or Decimal("0.00")
            all_known_sum = first_amt
        else:
            all_known_sum = inc_total_sum + excl_known_sum

        # Evaluate Customer Name Variance within same account (Example 17 & Edge Case #30)
        name_warnings: List[str] = []
        if len(distinct_norm_names) > 1:
            token_sets = [set(n.split()) for n in distinct_norm_names if n]
            common_tokens = set.intersection(*token_sets) if token_sets else set()
            if common_tokens:
                name_warnings.append("MINOR_NAME_VARIANCE")
            else:
                name_warnings.append("SEVERE_CUSTOMER_NAME_MISMATCH_UNDER_SAME_ACCOUNT")

        # Evaluate Account Scope Completeness (Stage 13)
        incompleteness_reasons: List[str] = []
        if acc_num in tainted_accounts_by_cross_service:
            incompleteness_reasons.append("CROSS_ACCOUNT_SERVICE_CONFLICT")
        if any(
            r.validation_status
            in (
                RowValidationStatus.HOLD_DUPLICATE_SERVICE,
                RowValidationStatus.HOLD_DUPLICATE_ROW,
                RowValidationStatus.ERROR_CONFLICTING_SERVICE,
            )
            for r in acc_rows
        ):
            incompleteness_reasons.append("DUPLICATE_OR_CONFLICTING_SERVICE_IN_ACCOUNT")
        if any(
            r.validation_status
            in (
                RowValidationStatus.ERROR_INVALID_IDENTIFIER,
                RowValidationStatus.ERROR_INVALID_AMOUNT,
                RowValidationStatus.ERROR_MERGED_CELL,
            )
            for r in acc_rows
        ):
            incompleteness_reasons.append("VALIDATION_ERRORS_IN_ACCOUNT_ROWS")
        if any("EXCLUDED_BY_COLLECTOR" in r.exclusion_reasons for r in acc_rows):
            incompleteness_reasons.append("SPLIT_ACROSS_COLLECTORS")
        if any(
            reason in ("EXCLUDED_BY_STATUS", "EXCLUDED_HIDDEN_ROW")
            for r in acc_rows
            for reason in r.exclusion_reasons
        ):
            incompleteness_reasons.append("PARTIAL_EXCLUSION_BY_STATUS_OR_HIDDEN")

        if not incompleteness_reasons:
            scope_status = AccountFileScopeStatus.FILE_SCOPE_COMPLETE
        elif "SPLIT_ACROSS_COLLECTORS" in incompleteness_reasons:
            scope_status = AccountFileScopeStatus.FILE_SCOPE_INCOMPLETE_SPLIT_COLLECTOR
        elif "DUPLICATE_OR_CONFLICTING_SERVICE_IN_ACCOUNT" in incompleteness_reasons or "CROSS_ACCOUNT_SERVICE_CONFLICT" in incompleteness_reasons:
            scope_status = AccountFileScopeStatus.FILE_SCOPE_INCOMPLETE_DUPLICATE_CONFLICT
        elif "VALIDATION_ERRORS_IN_ACCOUNT_ROWS" in incompleteness_reasons:
            scope_status = AccountFileScopeStatus.FILE_SCOPE_INCOMPLETE_HAS_ERRORS
        else:
            scope_status = AccountFileScopeStatus.FILE_SCOPE_INCOMPLETE_PARTIAL_EXCLUSION

        # Assign the 9 Canonical Engineering Concept Tags (Section 4 / Task 6)
        concepts: List[AccountConceptTag] = []
        if len(acc_rows) >= 2:
            concepts.append(AccountConceptTag.REPEATED_ACCOUNT_KEY)
            distinct_valid_srvs = {
                r.service_number_norm
                for r in acc_rows
                if r.service_number_norm
                and r.validation_status == RowValidationStatus.VALID
            }
            if len(distinct_valid_srvs) == len(acc_rows):
                concepts.append(AccountConceptTag.VALID_MULTI_SERVICE_ACCOUNT)

        if any(r.validation_status == RowValidationStatus.HOLD_DUPLICATE_SERVICE for r in acc_rows):
            concepts.append(AccountConceptTag.DUPLICATE_SERVICE_SAME_DATA)
        if any(r.validation_status == RowValidationStatus.ERROR_CONFLICTING_SERVICE for r in acc_rows):
            concepts.append(AccountConceptTag.CONFLICTING_SERVICE_ENTITY)
        if excluded_uids and included_valid_uids:
            concepts.append(AccountConceptTag.PARTIAL_SCOPE_ACCOUNT)
        if included_valid_uids:
            concepts.append(AccountConceptTag.SEARCHABLE_ACCOUNT_GROUP)
        if invalid_or_conflict_uids or acc_num in tainted_accounts_by_cross_service:
            concepts.append(AccountConceptTag.REVIEW_REQUIRED_ACCOUNT)
        if scope_status == AccountFileScopeStatus.FILE_SCOPE_COMPLETE:
            concepts.append(AccountConceptTag.FILE_SCOPE_COMPLETE)
        else:
            concepts.append(AccountConceptTag.FILE_SCOPE_INCOMPLETE)

        grp = AccountGroup(
            account_number=acc_num,
            source_row_uids=all_uids,
            distinct_sheets=distinct_sheets,
            distinct_customer_names=distinct_names,
            distinct_collectors=distinct_collectors,
            all_service_rows=all_uids,
            included_valid_service_rows=included_valid_uids,
            included_starts_with_2_rows=included_s2_uids,
            included_non_2_rows=included_non2_uids,
            excluded_service_rows=excluded_uids,
            invalid_or_conflict_rows=invalid_or_conflict_uids,
            included_starts_with_2_sum=inc_s2_sum,
            included_non_2_sum=inc_non2_sum,
            included_total_sum=inc_total_sum,
            excluded_known_amount_sum=excl_known_sum,
            all_known_file_rows_sum=all_known_sum,
            requires_service_searches=bool(included_s2_uids),
            requires_account_search=bool(included_non2_uids),
            file_scope_status=scope_status,
            scope_incompleteness_reasons=incompleteness_reasons,
            concept_tags=concepts,
            customer_name_warnings=name_warnings,
        )
        account_groups.append(grp)
        account_group_map[acc_num] = grp

    # ------------------------------------------------------------------------
    # Stage 14: Build Service Search Tasks (1:1) & Deduplicated Account Tasks (N:1)
    # ------------------------------------------------------------------------
    service_search_tasks: List[SearchTask] = []
    account_search_tasks: List[SearchTask] = []
    account_task_by_key: Dict[str, SearchTask] = {}

    srv_counter = 0
    acc_counter = 0

    service_searchable_rows_count = 0
    account_searchable_rows_count = 0
    excluded_rows_count = 0
    blocked_or_review_rows_count = 0

    row_by_uid = {r.row_uid: r for r in all_source_rows}

    for r in all_source_rows:
        # 1. Check if row has validation/duplicate/conflict errors
        if r.validation_status not in (
            RowValidationStatus.VALID,
            RowValidationStatus.WARNING_ORPHAN_SERVICE,
        ):
            r.routing_disposition = "BLOCKED_REVIEW"
            blocked_or_review_rows_count += 1
            continue

        # 2. Check if row is excluded by status/collector/hidden filter
        if not r.include_in_scope:
            r.routing_disposition = "EXCLUDED"
            excluded_rows_count += 1
            continue

        # 3. Valid & Included Row -> Route by Service Prefix (Section 1.B & Stage 14)
        if r.service_starts_with_2:
            srv_counter += 1
            task_id = f"SRV-{srv_counter:03d}"
            r.routing_disposition = "SERVICE_SEARCH"
            r.linked_search_task_id = task_id
            service_searchable_rows_count += 1

            reason = "Valid included service starting with 2 -> Direct Service-Level Search"
            if r.amount_state == AmountState.EXPLICIT_ZERO:
                reason = "Valid included service starting with 2 (Explicit Zero 0.00) -> Direct Service-Level Search"
            elif r.validation_status == RowValidationStatus.WARNING_ORPHAN_SERVICE:
                reason = "Valid orphan service starting with 2 (Missing Account) -> Direct Service-Level Search"

            srv_task = SearchTask(
                task_id=task_id,
                search_type="SERVICE",
                search_key=r.service_number_norm or "",
                account_number=r.account_number_norm,
                service_number=r.service_number_norm,
                linked_row_uids=[r.row_uid],
                linked_service_numbers=[r.service_number_norm or ""],
                source_sheets=[r.source_sheet],
                expected_result_scope="SERVICE_LEVEL",
                file_comparison_amount=r.file_amount_decimal,
                is_file_scope_complete=True,  # Service-level scope is 1:1 self-contained
                creation_reason=reason,
            )
            service_search_tasks.append(srv_task)
        else:
            # Non-2 Service -> Route to Deduplicated Account Search!
            acc_key = r.account_number_norm
            if not acc_key or acc_key not in account_group_map:
                r.routing_disposition = "BLOCKED_REVIEW"
                blocked_or_review_rows_count += 1
                continue

            r.routing_disposition = "ACCOUNT_SEARCH"
            account_searchable_rows_count += 1
            grp = account_group_map[acc_key]

            if acc_key not in account_task_by_key:
                acc_counter += 1
                task_id = f"ACC-{acc_counter:03d}"
                grp.account_search_task_id = task_id
                all_acc_srvs = [
                    row_by_uid[uid].service_number_norm or ""
                    for uid in grp.all_service_rows
                    if row_by_uid[uid].service_number_norm
                ]
                acc_task = SearchTask(
                    task_id=task_id,
                    search_type="ACCOUNT",
                    search_key=acc_key,
                    account_number=acc_key,
                    service_number=None,
                    linked_row_uids=[r.row_uid],
                    linked_service_numbers=[r.service_number_norm or ""],
                    source_sheets=list(grp.distinct_sheets),
                    expected_result_scope="ACCOUNT_LEVEL_TOTAL",
                    file_comparison_amount=(
                        grp.included_total_sum if grp.file_scope_status.is_complete else None
                    ),
                    is_file_scope_complete=grp.file_scope_status.is_complete,
                    creation_reason=(
                        f"Account {acc_key} contains included service(s) not starting with 2 -> "
                        "Deduplicated Account-Level Search"
                    ),
                    triggering_services=[r.service_number_norm or ""],
                    triggering_source_rows=[r.row_uid],
                    all_account_services_in_file=all_acc_srvs,
                    file_included_non2_sum=grp.included_non_2_sum,
                    file_all_included_sum=grp.included_total_sum,
                    file_all_known_rows_sum=grp.all_known_file_rows_sum,
                    account_file_scope_status=grp.file_scope_status,
                )
                account_task_by_key[acc_key] = acc_task
                account_search_tasks.append(acc_task)
            else:
                acc_task = account_task_by_key[acc_key]
                acc_task.linked_row_uids.append(r.row_uid)
                if r.service_number_norm and r.service_number_norm not in acc_task.linked_service_numbers:
                    acc_task.linked_service_numbers.append(r.service_number_norm)
                if r.service_number_norm and r.service_number_norm not in acc_task.triggering_services:
                    acc_task.triggering_services.append(r.service_number_norm)
                acc_task.triggering_source_rows.append(r.row_uid)

            r.linked_search_task_id = account_task_by_key[acc_key].task_id

    # Link service entities to their search tasks
    srv_task_by_srv_num = {t.service_number: t.task_id for t in service_search_tasks if t.service_number}
    for se in service_entities:
        if se.service_number in srv_task_by_srv_num:
            se.linked_search_task_id = srv_task_by_srv_num[se.service_number]
        elif se.account_numbers and se.account_numbers[0] in account_task_by_key and se.is_unique:
            se.linked_search_task_id = account_task_by_key[se.account_numbers[0]].task_id

    # ------------------------------------------------------------------------
    # Stage 15: Pre-Zain Readiness Gate & Mathematical Invariant Check
    # ------------------------------------------------------------------------
    total_input = len(all_source_rows)
    total_accounted = (
        service_searchable_rows_count
        + account_searchable_rows_count
        + excluded_rows_count
        + blocked_or_review_rows_count
    )
    invariant_balanced = (total_input == total_accounted)

    # Verify zero duplicate search keys per task type
    unique_srv_keys = {t.search_key for t in service_search_tasks}
    unique_acc_keys = {t.search_key for t in account_search_tasks}
    no_duplicate_tasks = (
        len(unique_srv_keys) == len(service_search_tasks)
        and len(unique_acc_keys) == len(account_search_tasks)
    )

    if not invariant_balanced:
        raise AssertionError(
            f"Readiness Invariant Check Failed: Total Input ({total_input}) != "
            f"Accounted ({total_accounted})."
        )
    if not no_duplicate_tasks:
        raise AssertionError("Readiness Check Failed: Duplicate search tasks detected.")

    ready_lock = bool(invariant_balanced and no_duplicate_tasks)

    return PreZainSearchPlan(
        source_file_name=source_file_name,
        source_file_sha256=source_file_sha256,
        canonical_amount_column_info=canonical_amount_info,
        sheet_discovery_report=sheet_discovery_report,
        warnings=pipeline_warnings,
        source_rows=all_source_rows,
        service_entities=service_entities,
        account_groups=account_groups,
        service_search_tasks=service_search_tasks,
        account_search_tasks=account_search_tasks,
        total_input_rows=total_input,
        service_searchable_rows_count=service_searchable_rows_count,
        account_searchable_rows_count=account_searchable_rows_count,
        excluded_rows_count=excluded_rows_count,
        blocked_or_review_rows_count=blocked_or_review_rows_count,
        invariant_balanced=invariant_balanced,
        ready_for_zain_execution=ready_lock,
    )
