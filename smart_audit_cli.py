"""CLI Entrypoint for Smart Zain Checker (Gemini Edition) 16-Stage Audit & Simulation.

Usage:
  python smart_audit_cli.py --simulate
  python smart_audit_cli.py --workbook zain_data.xlsx --collector "سعد الحربي" --output-dir audit_outputs
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sys

# Ensure UTF-8 stdout on Windows safely (User Global Rule)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
elif hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from zain_checker.pipeline import run_pre_zain_pipeline
from zain_checker.reconciliation import export_comprehensive_audit_workbook
from zain_checker.simulation_suite import run_and_verify_section_11_simulation


def _prompt_file_selection_dialog() -> Path | None:
    """Open a native Windows file selection dialog (with CLI menu fallback) to pick an Excel workbook."""
    print("\n📂 جاري فتح نافذة اختيار ملف الإكسل (Select Excel Workbook)...")
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        file_path = filedialog.askopenfilename(
            parent=root,
            initialdir=str(Path.cwd()),
            title="اختر ملف الإكسل لتدقيقه قبل زين (Select Excel Workbook)",
            filetypes=[("ملفات إكسل (Excel Files)", "*.xlsx *.xls"), ("جميع الملفات", "*.*")],
        )
        root.destroy()
        if file_path:
            selected = Path(file_path)
            if selected.exists():
                print(f"✅ تم اختيار الملف: {selected}")
                return selected
    except Exception as exc:
        print(f"⚠️ تعذر فتح نافذة النظام الرسومية ({exc})، سيتم التحول للاختيار النصي.")

    # Console menu fallback if user closed dialog or tkinter unavailable
    xlsx_files = sorted(
        [p for p in Path.cwd().glob("*.xlsx") if not p.name.startswith("~$")]
    )
    print("\n" + "=" * 60)
    print("📋 اختر ملف الإكسل المراد فحصه وتدقيقه:")
    for idx, f in enumerate(xlsx_files, 1):
        print(f"  [{idx}] {f.name}")
    print("  [M] إدخال مسار ملف إكسل يدوياً (Paste file path)")
    print("=" * 60)
    try:
        choice = input("👉 أدخل رقم الملف أو الصق المسار مباشرة (اضغط Enter لاختيار 1): ").strip().strip('"')
    except EOFError:
        choice = "1"
    if not choice and xlsx_files:
        return xlsx_files[0]
    if choice.isdigit() and 1 <= int(choice) <= len(xlsx_files):
        return xlsx_files[int(choice) - 1]
    candidate = Path(choice)
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smart Zain Checker (Gemini Edition) — 16-Stage Pre-Zain Audit & Reconciliation Engine"
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run the full 20-row multi-sheet End-to-End Simulation (Section 11 / Task 10).",
    )
    parser.add_argument(
        "--pick-file",
        action="store_true",
        help="Open a file selection dialog so the user can choose which Excel workbook to audit.",
    )
    parser.add_argument(
        "--clean-sheet",
        action="store_true",
        help="Run the sequential first-seen memory Clean Sheet Builder and save <name>_الشيت_النظيف.xlsx.",
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        help="Path to the input Excel workbook to audit before Zain search.",
    )
    parser.add_argument(
        "--collector",
        type=str,
        default=None,
        help="Target collector name (default: all collectors).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("audit_outputs"),
        help="Directory to write the PreZainSearchPlan JSON and 7-Sheet Audit Excel Workbook.",
    )
    args = parser.parse_args(argv)

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.simulate:
        plan = run_and_verify_section_11_simulation(out_dir)
        print(
            json.dumps(
                {
                    "mode": "SECTION_11_FULL_SIMULATION",
                    "output_dir": str(out_dir),
                    "readiness_summary": plan.to_dict()["readiness_summary"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    workbook_path = args.workbook
    if args.pick_file or not workbook_path:
        workbook_path = _prompt_file_selection_dialog()
        if not workbook_path:
            print("❌ لم يتم اختيار أي ملف إكسل.")
            return 1

    args.workbook = workbook_path

    # Always generate the Sequential Clean Sheet (<workbook>_الشيت_النظيف.xlsx) first!
    from zain_checker.clean_sheet_builder import build_sequential_clean_sheet

    clean_res = build_sequential_clean_sheet(
        workbook_path=args.workbook,
        target_collector=args.collector or "",
    )
    print(
        json.dumps(
            {
                "mode": "SEQUENTIAL_CLEAN_SHEET_BUILDER",
                "summary": clean_res.to_summary_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.clean_sheet:
        return 0

    plan = run_pre_zain_pipeline(
        workbook_path=args.workbook,
        target_collector=args.collector,
        all_collectors=not bool(args.collector),
    )
    json_out = out_dir / f"{args.workbook.stem}_pre_zain_plan.json"
    xlsx_out = out_dir / f"{args.workbook.stem}_audit_report.xlsx"
    json_out.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    export_comprehensive_audit_workbook(plan, xlsx_out)

    print(
        json.dumps(
            {
                "mode": "WORKBOOK_PRE_ZAIN_AUDIT",
                "workbook": str(args.workbook.resolve()),
                "json_plan": str(json_out),
                "audit_excel": str(xlsx_out),
                "readiness_summary": plan.to_dict()["readiness_summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
