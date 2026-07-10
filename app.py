import csv
import io
import os
import sqlite3
from functools import wraps

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

import config
import db

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            flash("Admin login required.", "warning")
            return redirect(url_for("admin_login", next=request.url))
        return view(*args, **kwargs)

    return wrapped


def board_label(board):
    parts = [board.get("product_name"), board.get("board_name")]
    tool = board.get("tool")
    serial = board.get("serial")
    if tool:
        parts.append(f"{tool}")
    if serial:
        parts.append(f"#{serial}")
    return " ".join(p for p in parts if p)


app.jinja_env.filters["board_label"] = board_label


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.svg",
        mimetype="image/svg+xml",
    )


@app.route("/")
def index():
    product = request.args.get("product")
    firmware = request.args.get("firmware")
    tool = request.args.get("tool")
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort", "board_id")
    order = request.args.get("order", "asc")
    return render_template(
        "index.html",
        boards=db.list_boards(
            product_name=product,
            firmware=firmware,
            tool=tool,
            search=search,
            sort=sort,
            order=order,
        ),
        history=db.list_history(
            product_name=product,
            firmware=firmware,
            tool=tool,
            search=search,
        ),
        stats=db.dashboard_stats(),
        fw_stats=db.firmware_stats(),
        tool_stats=db.tool_stats(),
        products=db.board_products(),
        available_firmware=db.firmware_for_product(product) if product else set(),
        available_products=db.products_for_firmware(firmware) if firmware else set(),
        available_tools=(
            db.tools_for_product(product) if product
            else db.tools_for_firmware(firmware) if firmware
            else set()
        ),
        available_products_for_tool=db.products_for_tool(tool) if tool else set(),
        available_firmware_for_tool=db.firmware_for_tool(tool) if tool else set(),
        product_filter=product,
        firmware_filter=firmware,
        tool_filter=tool,
        search=search,
        sort=sort,
        order=order,
    )


@app.route("/boards")
def boards():
    return redirect(url_for("index", **request.args))


@app.route("/hardware", endpoint="hardware")
def hardware():
    return _render_hardware()


@app.route("/data", methods=["GET", "POST"])
def data():
    query = request.form.get("query", "").strip() if request.method == "POST" else ""
    if not query:
        query = "SELECT * FROM current_firmware ORDER BY board_id"

    columns = []
    rows = []
    error = None
    truncated = False

    if request.method == "POST" and request.form.get("action") == "download":
        try:
            columns, rows, truncated = db.run_readonly_query(query)
            return _csv_response(rows, columns, filename="query_results.csv")
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("data"))

    if request.method == "POST":
        try:
            columns, rows, truncated = db.run_readonly_query(query)
        except ValueError as exc:
            error = str(exc)

    return render_template(
        "data.html",
        tables=db.list_tables(),
        query=query,
        columns=columns,
        rows=rows,
        error=error,
        truncated=truncated,
    )


@app.route("/data/export")
def data_export():
    table = request.args.get("table")
    if not table:
        flash("No table specified.", "warning")
        return redirect(url_for("data"))
    try:
        columns, rows, _ = db.fetch_table_rows(table)
        return _csv_response(rows, columns, filename=f"{table}.csv")
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("data"))


def _csv_response(rows, columns, filename="export.csv"):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row.get(col, "") for col in columns})
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/board/<int:board_id>")
def board_detail(board_id):
    board = db.get_board(board_id)
    if not board:
        flash("Board not found.", "danger")
        return redirect(url_for("index"))
    return render_template(
        "board_detail.html",
        board=board,
        firmware_verified=db.firmware_verified_label(board),
        history=db.board_history(board_id),
        events=db.board_events(board_id),
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == config.ADMIN_PASSWORD:
            session["admin"] = True
            flash("Logged in.", "success")
            next_url = request.args.get("next") or url_for("admin_index")
            return redirect(next_url)
        flash("Incorrect password.", "danger")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    flash("Logged out.", "info")
    return redirect(url_for("index"))


@app.route("/admin")
@login_required
def admin_index():
    return render_template(
        "admin/index.html",
        boards=db.list_boards(sort="board_id"),
        recent=db.recent_installs(5),
    )


@app.route("/admin/sql", methods=["GET", "POST"])
@login_required
def admin_sql():
    query = request.form.get("query", "").strip() if request.method == "POST" else ""
    if not query:
        query = (
            "SELECT h.event_id, b.product_name, b.board_name, h.firmware, h.fpga\n"
            "FROM firmware_history h\n"
            "JOIN boards b ON b.board_id = h.board_id\n"
            "ORDER BY h.event_date DESC, h.event_id DESC"
        )

    columns = []
    rows = []
    error = None
    truncated = False
    rowcount = None

    if request.method == "POST" and request.form.get("action") == "download":
        try:
            result = db.run_admin_query(query)
            if result["kind"] != "rows":
                flash("Only SELECT queries can be downloaded as CSV.", "warning")
                return redirect(url_for("admin_sql"))
            return _csv_response(result["rows"], result["columns"], filename="query_results.csv")
        except (ValueError, sqlite3.Error) as exc:
            flash(str(exc), "danger")
            return redirect(url_for("admin_sql"))

    if request.method == "POST":
        try:
            result = db.run_admin_query(query)
            if result["kind"] == "rows":
                columns = result["columns"]
                rows = result["rows"]
                truncated = result["truncated"]
            else:
                rowcount = result["rowcount"]
        except (ValueError, sqlite3.Error) as exc:
            error = str(exc)

    tables = db.list_tables(include_archives=True)
    return render_template(
        "admin/sql.html",
        tables=tables,
        archive_tables=db.ARCHIVE_TABLES,
        table_queries={t["name"]: db.table_preview_query(t["name"]) for t in tables},
        sql_schema=db.sql_autocomplete_schema(),
        query=query,
        columns=columns,
        rows=rows,
        error=error,
        truncated=truncated,
        rowcount=rowcount,
    )


@app.route("/admin/board/new", methods=["GET", "POST"])
@login_required
def admin_board_new():
    if request.method == "POST":
        data = _board_form_data()
        data["board_id"] = int(request.form["board_id"])
        try:
            db.insert_board(data)
            flash(f"Board {data['board_id']} created.", "success")
            return redirect(url_for("board_detail", board_id=data["board_id"]))
        except Exception as exc:
            flash(f"Could not create board: {exc}", "danger")
    return render_template("admin/board_form.html", board=None, next_id=db.next_board_id())


@app.route("/admin/board/<int:board_id>/edit", methods=["GET", "POST"])
@login_required
def admin_board_edit(board_id):
    board = db.get_board(board_id)
    if not board:
        flash("Board not found.", "danger")
        return redirect(url_for("admin_index"))
    if request.method == "POST":
        try:
            db.update_board(board_id, _board_form_data())
            flash("Board updated.", "success")
            return redirect(url_for("board_detail", board_id=board_id))
        except Exception as exc:
            flash(f"Could not update board: {exc}", "danger")
    return render_template("admin/board_form.html", board=board, next_id=board_id)


@app.route("/admin/board/<int:board_id>/delete", methods=["POST"])
@login_required
def admin_board_delete(board_id):
    board = db.get_board(board_id)
    if not board:
        flash("Board not found.", "danger")
        return redirect(url_for("admin_index"))
    try:
        db.delete_board(board_id)
        flash(f"Board {board_id} moved to deleted records.", "success")
    except Exception as exc:
        flash(f"Could not remove board: {exc}", "danger")
    return redirect(url_for("admin_index"))


@app.route("/admin/history/new", methods=["GET", "POST"])
@login_required
def admin_history_new():
    boards = db.list_boards(sort="board_id")
    if request.method == "POST":
        data = _history_form_data()
        try:
            event_id = db.insert_history(data)
            flash("Firmware record added.", "success")
            return redirect(url_for("board_detail", board_id=data["board_id"]))
        except Exception as exc:
            flash(f"Could not add record: {exc}", "danger")
    default_board_id = request.args.get("board_id", type=int)
    return render_template(
        "admin/history_form.html",
        event=None,
        boards=boards,
        default_board_id=default_board_id,
    )


@app.route("/admin/history/<int:event_id>/edit", methods=["GET", "POST"])
@login_required
def admin_history_edit(event_id):
    event = db.get_history_event(event_id)
    if not event:
        flash("Record not found.", "danger")
        return redirect(url_for("admin_index"))
    boards = db.list_boards(sort="board_id")
    if request.method == "POST":
        data = _history_form_data()
        try:
            db.update_history(event_id, data)
            flash("Firmware record updated.", "success")
            return redirect(url_for("board_detail", board_id=data["board_id"]))
        except Exception as exc:
            flash(f"Could not update record: {exc}", "danger")
    return render_template(
        "admin/history_form.html",
        event=event,
        boards=boards,
        default_board_id=event["board_id"],
    )


@app.route("/admin/history/<int:event_id>/delete", methods=["POST"])
@login_required
def admin_history_delete(event_id):
    event = db.get_history_event(event_id)
    if not event:
        flash("Record not found.", "danger")
        return redirect(url_for("admin_index"))
    board_id = event["board_id"]
    try:
        db.delete_history(event_id)
        flash("Firmware record moved to deleted records.", "success")
    except Exception as exc:
        flash(f"Could not remove record: {exc}", "danger")
    return redirect(url_for("board_detail", board_id=board_id))


@app.route("/admin/deleted")
@login_required
def admin_deleted():
    return render_template(
        "admin/deleted.html",
        deleted_boards=db.list_deleted_boards(),
        deleted_history=db.list_deleted_history(),
    )


@app.route("/admin/deleted/board/<int:board_id>/restore", methods=["POST"])
@login_required
def admin_board_restore(board_id):
    board = db.get_deleted_board(board_id)
    if not board:
        flash("Deleted board not found.", "danger")
        return redirect(url_for("admin_deleted"))
    try:
        db.restore_board(board_id)
        flash(f"Board {board_id} restored.", "success")
    except Exception as exc:
        flash(f"Could not restore board: {exc}", "danger")
    return redirect(url_for("admin_deleted"))


@app.route("/admin/deleted/history/<int:event_id>/restore", methods=["POST"])
@login_required
def admin_history_restore(event_id):
    try:
        if db.restore_history(event_id):
            flash("Firmware record restored.", "success")
        else:
            flash("Record not found.", "danger")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        flash(f"Could not restore record: {exc}", "danger")
    return redirect(url_for("admin_deleted"))


def _board_form_data():
    return {
        "tool": request.form.get("tool") or None,
        "board_slot": request.form.get("board_slot") or None,
        "manufacturer": request.form.get("manufacturer") or "PDF Solutions Inc.",
        "board_name": request.form["board_name"],
        "serial": request.form["serial"],
        "part_number": request.form.get("part_number") or None,
        "revision": request.form.get("revision") or None,
        "file_id": request.form.get("file_id") or None,
        "product_name": request.form["product_name"],
        "ddr_fbga": request.form.get("ddr_fbga") or None,
        "inventory_serial": request.form.get("inventory_serial") or None,
        "status": request.form.get("status") or None,
        "role": request.form.get("role") or None,
        "comment": request.form.get("comment") or None,
        "open_item": request.form.get("open_item") or None,
        "po": request.form.get("po") or None,
        "modified_by": request.form.get("modified_by") or None,
        "source_updated_at": request.form.get("source_updated_at") or None,
        "data_source": request.form.get("data_source") or None,
        "dc_status": request.form.get("dc_status") or None,
        "ac_status": request.form.get("ac_status") or None,
        "gcal_status": request.form.get("gcal_status") or None,
        "adc_status": request.form.get("adc_status") or None,
        "eeprom_status": request.form.get("eeprom_status") or None,
    }


def _history_form_data():
    return {
        "board_id": int(request.form["board_id"]),
        "event_date": request.form["event_date"],
        "event_time": request.form.get("event_time") or None,
        "fpga": request.form.get("fpga") or None,
        "firmware": request.form["firmware"],
        "installer": request.form.get("installer") or None,
        "result": request.form.get("result") or None,
    }


def _render_hardware():
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort", "board_id")
    order = request.args.get("order", "asc")
    return render_template(
        "hardware.html",
        boards=db.list_hardware(search=search, sort=sort, order=order),
        search=search,
        sort=sort,
        order=order,
    )


if "hardware" not in app.view_functions:
    app.add_url_rule("/hardware", endpoint="hardware", view_func=_render_hardware)


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
