import os
from os.path import abspath, dirname, join

import pandas as pd

SCRIPT_DIR = dirname(abspath(__file__))

# SQL-safe placeholders
NOT_APPLICABLE = "NOT_APPLICABLE"  # board type lacks this component (e.g. EM1)
UNKNOWN = "UNKNOWN"                # data exists but was not recorded (e.g. Tool13 ES4)

EM1_NA_FIELDS = {
    "StickerRevision", "BoardRevision", "CCB_PCB", "CCB_ASSY",
    "CCB_Rev", "CCB_SerialNumber", "DDR_FBGA",
}

COLS = [
    "Tool", "Board", "PartNumber", "PCB", "ASSY", "StickerRevision",
    "BoardRevision", "SerialNumber", "CCB_PCB", "CCB_ASSY", "CCB_Rev",
    "CCB_SerialNumber", "DDR_FBGA", "Firmware", "Date", "Notes",
]


def load_raw_data(path=None):
    if path is None:
        path = join(SCRIPT_DIR, "BoardFirmwareLog.xlsx")
    """Load raw log rows in spreadsheet order; skip blank trailing rows."""
    raw = pd.read_excel(path, sheet_name=0, header=0)
    raw = raw.rename(columns={
        "Part Number": "PartNumber",
        "Sticker Revision": "StickerRevision",
        "Board Revision": "BoardRevision",
        "Serial Number": "SerialNumber",
        "CCB PCB": "CCB_PCB",
        "CCB ASSY": "CCB_ASSY",
        "CCB Rev": "CCB_Rev",
        "CCB Serial Number": "CCB_SerialNumber",
        "DDR FBGA": "DDR_FBGA",
        "FW": "Firmware",
    })
    raw = raw.dropna(subset=["Tool", "Board", "Firmware"], how="any").reset_index(drop=True)
    raw["row_order"] = range(len(raw))
    return raw


def normalize_value(value, board, field):
    """Map empty / N/A cells to SQL-safe placeholders."""
    if pd.isna(value) or str(value).strip() == "":
        if board == "EM1" and field in EM1_NA_FIELDS:
            return NOT_APPLICABLE
        return UNKNOWN
    text = str(value).strip()
    if text.upper() == "N/A":
        return NOT_APPLICABLE
    return text


def parse_result(notes):
    """PASS unless notes explicitly record a failure."""
    text = "" if pd.isna(notes) else str(notes).strip()
    if not text:
        return "PASS"
    lower = text.lower()
    if lower == "fail" or ", fail" in lower:
        return "FAIL"
    return "PASS"


def assign_installer(row, seen_boards):
    """
    EM1              -> OpalKelly
    first board row  -> LabVIEW
    Standalone note  -> LabVIEW
    otherwise        -> Vivado
    """
    board_id = row["BoardID"]
    notes = "" if pd.isna(row["Notes"]) else str(row["Notes"])
    is_first = board_id not in seen_boards
    is_standalone = "standalone" in notes.lower()

    if row["Board"] == "EM1":
        return "OpalKelly"
    if is_first or is_standalone:
        return "LabVIEW"
    return "Vivado"


def build_current_firmware(history):
    """Latest firmware per board; last log entry wins on the same date."""
    hist = history.copy()
    hist["Date"] = pd.to_datetime(hist["Date"])
    latest = hist.groupby("BoardID", as_index=False).last()
    current = latest[["BoardID", "Board", "Firmware", "Date"]].copy()
    current["Date"] = current["Date"].dt.strftime("%Y-%m-%d")
    return current.sort_values("BoardID").reset_index(drop=True)


# ============================================================
# VBA (embedded into .xlsm by Python — source of truth is here)
# ============================================================

VBA_REFRESH_CURRENT_FIRMWARE = """
Public Sub RefreshCurrentFirmware()
    On Error GoTo ErrHandler

    Dim wsB As Worksheet
    Dim wsH As Worksheet
    Dim wsC As Worksheet
    Dim lastBoardRow As Long
    Dim lastHistoryRow As Long
    Dim boardRow As Long
    Dim historyRow As Long
    Dim outRow As Long
    Dim boardId As Variant
    Dim histBoardId As Variant
    Dim maxDate As Double
    Dim histDate As Double
    Dim latestFw As Variant
    Dim rowCount As Long

    Set wsB = ThisWorkbook.Worksheets("Boards")
    Set wsH = ThisWorkbook.Worksheets("FirmwareHistory")

    On Error Resume Next
    Set wsC = ThisWorkbook.Worksheets("CurrentFirmware")
    On Error GoTo ErrHandler

    If wsC Is Nothing Then
        Set wsC = ThisWorkbook.Worksheets.Add(After:=wsH)
        wsC.Name = "CurrentFirmware"
    Else
        wsC.Cells.Clear
    End If

    wsC.Range("A1:D1").Value = Array("BoardID", "Board", "Firmware", "Date")

    lastBoardRow = wsB.Cells(wsB.Rows.Count, "A").End(xlUp).Row
    lastHistoryRow = wsH.Cells(wsH.Rows.Count, "A").End(xlUp).Row
    outRow = 2
    rowCount = 0

    For boardRow = 2 To lastBoardRow
        boardId = wsB.Cells(boardRow, 1).Value
        If Len(Trim(CStr(boardId))) = 0 Then GoTo NextBoard

        maxDate = -1

        For historyRow = 2 To lastHistoryRow
            histBoardId = wsH.Cells(historyRow, 2).Value
            If CStr(histBoardId) = CStr(boardId) Then
                histDate = CDbl(CDate(wsH.Cells(historyRow, 1).Value))
                If histDate > maxDate Then
                    maxDate = histDate
                End If
            End If
        Next historyRow

        If maxDate < 0 Then GoTo NextBoard

        latestFw = Empty
        For historyRow = 2 To lastHistoryRow
            histBoardId = wsH.Cells(historyRow, 2).Value
            If CStr(histBoardId) = CStr(boardId) Then
                histDate = CDbl(CDate(wsH.Cells(historyRow, 1).Value))
                If histDate = maxDate Then
                    latestFw = wsH.Cells(historyRow, 5).Value
                End If
            End If
        Next historyRow

        wsC.Cells(outRow, 1).Value = boardId
        wsC.Cells(outRow, 2).Value = wsB.Cells(boardRow, 3).Value
        wsC.Cells(outRow, 3).Value = latestFw
        wsC.Cells(outRow, 4).Value = CDate(maxDate)
        wsC.Cells(outRow, 4).NumberFormat = "yyyy-mm-dd"
        outRow = outRow + 1
        rowCount = rowCount + 1
NextBoard:
    Next boardRow

    wsC.Columns("A:D").AutoFit
    Exit Sub

ErrHandler:
    MsgBox "RefreshCurrentFirmware failed:" & vbCrLf & vbCrLf & Err.Description, vbCritical
End Sub
""".strip()

VBA_WORKBOOK_OPEN = """
Private Sub Workbook_Open()
    RefreshCurrentFirmware
End Sub
""".strip()


def write_bas_file(path=None):
    if path is None:
        path = join(SCRIPT_DIR, "firmware_database_macros.bas")
    """Export the VBA module to a .bas file (manual fallback if COM embed fails)."""
    content = (
        'Attribute VB_Name = "FirmwareDatabase"\r\n'
        + VBA_REFRESH_CURRENT_FIRMWARE.replace("\n", "\r\n")
    )
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(content)


def embed_vba_macro(xlsx_path, xlsm_path):
    """
    Build the macro-enabled workbook from the xlsx using Excel COM.
    Returns (success: bool, error_message: str | None).
    """
    try:
        import win32com.client as win32
    except ImportError:
        return False, "pywin32 is not installed (pip install pywin32)"

    xlsx_path = os.path.abspath(xlsx_path)
    xlsm_path = os.path.abspath(xlsm_path)

    excel = win32.Dispatch("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False

    wb = None
    try:
        wb = excel.Workbooks.Open(xlsx_path)

        for comp in list(wb.VBProject.VBComponents):
            if comp.Name == "FirmwareDatabase":
                wb.VBProject.VBComponents.Remove(comp)

        module = wb.VBProject.VBComponents.Add(1)
        module.Name = "FirmwareDatabase"
        module.CodeModule.AddFromString(
            VBA_REFRESH_CURRENT_FIRMWARE.replace("\n", "\r\n")
        )

        this_wb = wb.VBProject.VBComponents("ThisWorkbook")
        existing = ""
        if this_wb.CodeModule.CountOfLines > 0:
            existing = this_wb.CodeModule.Lines(1, this_wb.CodeModule.CountOfLines)
        if "Workbook_Open" not in existing:
            this_wb.CodeModule.AddFromString(
                "\r\n" + VBA_WORKBOOK_OPEN.replace("\n", "\r\n")
            )

        if os.path.exists(xlsm_path):
            os.remove(xlsm_path)
        wb.SaveAs(xlsm_path, FileFormat=52)

        excel.Run(f"'{os.path.basename(xlsm_path)}'!RefreshCurrentFirmware")
        wb.Save()
        return True, None
    except Exception as exc:
        return False, str(exc)
    finally:
        if wb is not None:
            wb.Close(SaveChanges=False)
        excel.Quit()


# ============================================================
# LOAD + NORMALIZE
# ============================================================

df = load_raw_data()

df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
df["Notes"] = df["Notes"].fillna("").astype(str)
df["Result"] = df["Notes"].apply(parse_result)

for field in COLS:
    if field not in df.columns or field in ("Date", "Notes", "Firmware"):
        continue
    df[field] = df.apply(lambda row: normalize_value(row[field], row["Board"], field), axis=1)

# ============================================================
# BOARDS TABLE (DIMENSION)
# ============================================================

boards = df[
    [
        "Tool", "Board", "SerialNumber", "PartNumber",
        "StickerRevision", "BoardRevision", "DDR_FBGA",
    ]
].drop_duplicates().reset_index(drop=True)

boards.insert(0, "BoardID", range(1, len(boards) + 1))

# ============================================================
# FACT TABLE
# ============================================================

df = df.merge(
    boards,
    on=[
        "Tool", "Board", "SerialNumber", "PartNumber",
        "StickerRevision", "BoardRevision", "DDR_FBGA",
    ],
    how="left",
)

# ============================================================
# INSTALLER (evaluated in raw row order)
# ============================================================

df["Installer"] = None
seen_boards = set()

for i, row in df.sort_values("row_order").iterrows():
    installer = assign_installer(row, seen_boards)
    df.at[i, "Installer"] = installer
    seen_boards.add(row["BoardID"])

# ============================================================
# FINAL TABLES
# ============================================================

history = df[
    ["Date", "BoardID", "Tool", "Board", "Firmware", "Installer", "Result", "row_order"]
]

history = history.sort_values(
    ["Date", "row_order"],
    ascending=[True, True],
).drop(columns=["row_order"])

current = build_current_firmware(history)

# ============================================================
# WRITE EXCEL
#   .xlsx  - all 3 sheets (Python-built CurrentFirmware)
#   .xlsm  - same data + embedded VBA for Excel-only refresh later
# ============================================================

OUTPUT_XLSX = join(SCRIPT_DIR, "firmware_database.xlsx")
OUTPUT_XLSM = join(SCRIPT_DIR, "firmware_database.xlsm")

with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as writer:
    boards.to_excel(writer, sheet_name="Boards", index=False)
    history.to_excel(writer, sheet_name="FirmwareHistory", index=False)
    current.to_excel(writer, sheet_name="CurrentFirmware", index=False)

write_bas_file()

ok, err = embed_vba_macro(OUTPUT_XLSX, OUTPUT_XLSM)

print(f"Created {OUTPUT_XLSX}")
print("  Boards            - one row per physical board")
print("  FirmwareHistory   - full flash log")
print("  CurrentFirmware   - latest firmware per board (Python)")
print("  firmware_database_macros.bas - VBA source export")
print("Re-run after updating BoardFirmwareLog.xlsx")

if ok:
    print(f"\nCreated {OUTPUT_XLSM} with embedded macro.")
    print("Use the .xlsm for day-to-day Excel work:")
    print("  - Edit Boards / FirmwareHistory")
    print("  - CurrentFirmware auto-refreshes on open (Enable Macros)")
    print("  - Or: Developer -> Macros -> RefreshCurrentFirmware")
else:
    print(f"\nCould not embed macro into .xlsm: {err}")
    print("If the error mentions 'VBProject', enable in Excel:")
    print("  File -> Options -> Trust Center -> Trust Center Settings")
    print("  -> Macro Settings -> check 'Trust access to the VBA project object model'")
    print("Then re-run board_log.py, or import firmware_database_macros.bas manually.")
