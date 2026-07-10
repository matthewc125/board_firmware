Attribute VB_Name = "FirmwareDatabase"
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