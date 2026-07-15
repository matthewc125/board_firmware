import csv
import io
import os
import sqlite3
from datetime import date
from functools import wraps

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

import config
import db
import firmware_status_report
import publish_database

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


@app.route("/status")
def status():
    matrix = db.firmware_status_sections()
    return render_template(
        "status.html",
        columns=matrix["columns"],
        sections=matrix["sections"],
    )


@app.route("/admin/status/layout", methods=["GET", "POST"])
@login_required
def admin_status_layout():
    if request.method == "POST":
        sections = []
        for spec in db.get_firmware_status_layout():
            raw = request.form.get(f"tools_{spec['key']}", "")
            nums = []
            for part in raw.replace(";", ",").split(","):
                part = part.strip().lower().replace("tool", "").strip()
                if not part:
                    continue
                try:
                    nums.append(int(part))
                except ValueError:
                    continue
            sections.append({"key": spec["key"], "tool_nums": nums})
        try:
            db.save_firmware_status_layout(sections)
            flash("Status tool layout saved.", "success")
            return redirect(url_for("status"))
        except Exception as exc:
            flash(f"Could not save layout: {exc}", "danger")
    return render_template(
        "admin/status_layout.html",
        sections=db.get_firmware_status_layout(),
    )


@app.route("/admin/status/firmware", methods=["GET", "POST"])
@login_required
def admin_status_firmware():
    matrix = db.firmware_status_sections()
    if request.method == "POST":
        entries = []
        for key, value in request.form.items():
            if not key.startswith("mode_"):
                continue
            field = key[len("mode_") :]
            tool_raw = request.form.get(f"tool_{field}")
            column_key = request.form.get(f"column_{field}")
            if not tool_raw or not column_key:
                continue
            try:
                tool_num = int(tool_raw)
            except ValueError:
                continue
            mode = (value or "auto").strip()
            entries.append(
                {
                    "tool_num": tool_num,
                    "column_key": column_key,
                    "mode": mode,
                    "firmware": request.form.get(f"firmware_{field}") or "",
                }
            )
        try:
            db.save_firmware_status_overrides(entries)
            flash("Status firmware overrides saved.", "success")
            return redirect(url_for("status"))
        except Exception as exc:
            flash(f"Could not save firmware overrides: {exc}", "danger")
        matrix = db.firmware_status_sections()
    return render_template(
        "admin/status_firmware.html",
        columns=matrix["columns"],
        sections=matrix["sections"],
    )


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
    if table in db.NON_EXPORT_TABLES:
        flash("That table is not available for download.", "warning")
        return redirect(url_for("data"))
    try:
        columns, rows, _ = db.fetch_table_rows(table)
        return _csv_response(rows, columns, filename=f"{table}.csv")
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("data"))


@app.route("/data/firmware-status")
def firmware_status_export():
    """Download Tool × board-type firmware matrix as a colored Excel workbook."""
    payload = firmware_status_report.build_firmware_status_workbook()
    filename = firmware_status_report.default_filename()
    return Response(
        payload,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@app.route("/admin/publish-database/preview", methods=["GET"])
@login_required
def admin_publish_database_preview():
    return jsonify(publish_database.preview_database_publish())


@app.route("/admin/publish-database", methods=["POST"])
@login_required
def admin_publish_database():
    result = publish_database.publish_database_to_github()
    category = "success" if result["ok"] else "danger"
    message = result["message"]
    detail = (result.get("detail") or "").strip()
    if detail:
        # Keep flash short; full git noise is often huge
        first_line = detail.splitlines()[0]
        message = f"{message} ({first_line})"
    flash(message, category)
    return redirect(url_for("admin_index"))


@app.route("/admin/firmware-catalog", methods=["GET"])
@login_required
def admin_firmware_catalog():
    from collections import OrderedDict

    edit_id = request.args.get("edit", type=int)
    entry = db.get_firmware_catalog_entry(edit_id) if edit_id else None
    catalog = db.list_firmware_catalog()
    catalog_by_family = OrderedDict()
    for family in db.list_firmware_families():
        catalog_by_family[family] = []
    for row in catalog:
        catalog_by_family.setdefault(row["family"], []).append(row)
    catalog_by_family = OrderedDict(
        (family, rows) for family, rows in catalog_by_family.items() if rows
    )
    families = db.list_firmware_families()
    return render_template(
        "admin/firmware_catalog.html",
        catalog=catalog,
        catalog_by_family=catalog_by_family.items(),
        entry=entry,
        families=families,
    )


@app.route("/admin/firmware-catalog/save", methods=["POST"])
@app.route("/admin/firmware-catalog/<int:catalog_id>/save", methods=["POST"])
@login_required
def admin_firmware_catalog_save(catalog_id=None):
    data = {
        "family": (request.form.get("family") or "").strip().upper(),
        "version": (request.form.get("version") or "").strip(),
        "fpga": (request.form.get("fpga") or "").strip() or None,
        "release_date": (request.form.get("release_date") or "").strip() or None,
        "notes": (request.form.get("notes") or "").strip() or None,
        "tools": (request.form.get("tools") or "").strip() or None,
        "is_field_deployed": bool(request.form.get("is_field_deployed")),
        "in_status_ranking": bool(request.form.get("in_status_ranking")),
    }
    try:
        if not data["family"] or not data["version"]:
            raise ValueError("Family and version are required.")
        if catalog_id:
            db.update_firmware_catalog(catalog_id, data)
        else:
            db.insert_firmware_catalog(data)
        flash("Firmware catalog entry saved.", "success")
    except Exception as exc:
        flash(f"Could not save catalog entry: {exc}", "danger")
    return redirect(url_for("admin_firmware_catalog"))


@app.route("/admin/firmware-catalog/<int:catalog_id>/set-field", methods=["POST"])
@login_required
def admin_firmware_catalog_set_field(catalog_id):
    try:
        db.set_firmware_catalog_field_deployed(catalog_id)
        flash("Field-deployed version updated.", "success")
    except Exception as exc:
        flash(f"Could not set field-deployed: {exc}", "danger")
    return redirect(url_for("admin_firmware_catalog"))


@app.route("/admin/firmware-catalog/<int:catalog_id>/delete", methods=["POST"])
@login_required
def admin_firmware_catalog_delete(catalog_id):
    try:
        db.delete_firmware_catalog(catalog_id)
        flash("Catalog entry deleted.", "success")
    except Exception as exc:
        flash(f"Could not delete entry: {exc}", "danger")
    return redirect(url_for("admin_firmware_catalog"))


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

    tables = db.list_tables(include_archives=True, include_internal=True)
    return render_template(
        "admin/sql.html",
        tables=tables,
        archive_tables=db.ARCHIVE_TABLES,
        table_queries={t["name"]: db.table_preview_query(t["name"]) for t in tables},
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


BOARD_EVENT_TYPES = (
    "install",
    "remove",
    "location",
    "receive",
    "repair",
    "test",
    "note",
)


@app.route("/admin/event/new", methods=["GET", "POST"])
@login_required
def admin_event_new():
    boards = db.list_boards(sort="board_id")
    default_board_id = request.args.get("board_id", type=int)
    default_tool = request.args.get("tool") or ""
    if request.method == "POST":
        data = _event_form_data()
        try:
            db.insert_board_event(data)
            if request.form.get("update_location") and data.get("tool"):
                db.set_board_tool(data["board_id"], data["tool"])
            flash("Board event added.", "success")
            return redirect(url_for("board_detail", board_id=data["board_id"]))
        except Exception as exc:
            flash(f"Could not add event: {exc}", "danger")
    return render_template(
        "admin/event_form.html",
        event=None,
        boards=boards,
        default_board_id=default_board_id,
        default_date=date.today().isoformat(),
        default_event_type="install",
        default_tool=default_tool,
        default_description="",
        update_location_default=True,
        event_types=BOARD_EVENT_TYPES,
    )


@app.route("/admin/event/<int:event_id>/edit", methods=["GET", "POST"])
@login_required
def admin_event_edit(event_id):
    event = db.get_board_event(event_id)
    if not event:
        flash("Event not found.", "danger")
        return redirect(url_for("admin_index"))
    boards = db.list_boards(sort="board_id")
    if request.method == "POST":
        data = _event_form_data()
        try:
            db.update_board_event(event_id, data)
            if request.form.get("update_location") and data.get("tool"):
                db.set_board_tool(data["board_id"], data["tool"])
            flash("Board event updated.", "success")
            return redirect(url_for("board_detail", board_id=data["board_id"]))
        except Exception as exc:
            flash(f"Could not update event: {exc}", "danger")
    return render_template(
        "admin/event_form.html",
        event=event,
        boards=boards,
        default_board_id=event["board_id"],
        default_date=event["event_date"],
        default_event_type=event["event_type"],
        default_tool=event.get("tool") or "",
        default_description=event.get("description") or "",
        update_location_default=False,
        event_types=BOARD_EVENT_TYPES,
    )


@app.route("/admin/event/<int:event_id>/delete", methods=["POST"])
@login_required
def admin_event_delete(event_id):
    event = db.get_board_event(event_id)
    if not event:
        flash("Event not found.", "danger")
        return redirect(url_for("admin_index"))
    board_id = event["board_id"]
    try:
        db.delete_board_event(event_id)
        flash("Board event deleted.", "success")
    except Exception as exc:
        flash(f"Could not delete event: {exc}", "danger")
    return redirect(url_for("board_detail", board_id=board_id))


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
        **_history_catalog_context(boards),
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
        **_history_catalog_context(boards),
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


def _history_catalog_context(boards):
    catalog_by_family = db.firmware_catalog_by_family()
    catalog_families = {
        board["board_id"]: db.catalog_family_for_board(
            board.get("product_name"), board.get("board_name")
        )
        or ""
        for board in boards
    }
    return {
        "catalog_by_family": catalog_by_family,
        "catalog_families": catalog_families,
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


def _event_form_data():
    tool = db.normalize_tool_name(request.form.get("tool"))
    event_type = (request.form.get("event_type") or "").strip().lower()
    if event_type not in BOARD_EVENT_TYPES:
        event_type = "note"
    description = (request.form.get("description") or "").strip()
    if not description:
        raise ValueError("Description is required.")
    return {
        "board_id": int(request.form["board_id"]),
        "event_date": request.form["event_date"],
        "event_time": request.form.get("event_time") or None,
        "event_type": event_type,
        "description": description,
        "tool": tool,
        "source": "admin",
        "source_ref": None,
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
