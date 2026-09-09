#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宸赋智控系统 - 多工序生产排控平台
技术栈：Streamlit + SQLite + Pandas
AIPA = Advanced-Intelligent Production Administration（高级智能生产管控）
"""
import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime, date, timedelta
from io import BytesIO
import os
import time
import tempfile
import shutil
# GitHub仓库基准模板库，本地pmc_aps.db复制一份重命名为 default_pmc_aps.db上传仓库根目录
DEFAULT_DB_PATH = "default_pmc_aps.db"
# 云环境临时运行库，用户操作数据存在这里，刷新/重启自动清空
RUNTIME_DB_PATH = "/tmp/runtime_pmc_aps.db"

# 每次页面加载自动复制模板数据库
if os.path.exists(DEFAULT_DB_PATH):
    shutil.copyfile(DEFAULT_DB_PATH, RUNTIME_DB_PATH)
else:
    conn_init = sqlite3.connect(RUNTIME_DB_PATH, check_same_thread=False)
    create_all_tables(conn_init)
    conn_init.close()

# 统一数据库连接函数，全文所有 sqlite3.connect(DB_PATH,...) 全部替换为 get_db_conn()
def get_db_conn():
    return sqlite3.connect(RUNTIME_DB_PATH, check_same_thread=False)

# ============================================================
# 工具函数
# ============================================================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def today_str():
    return date.today().strftime("%Y-%m-%d")

def sf(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default

def si(v, default=0):
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default

def get_conn():
    conn = get_db_conn()
    conn.row_factory = sqlite3.Row
    return conn

def qone(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()

def add_log(conn, etype, eid, action, detail=""):
    conn.execute(
        "INSERT INTO operation_logs(entity_type,entity_id,action,detail,created_at) VALUES(?,?,?,?,?)",
        (etype, str(eid), action, detail, now_str()))
    conn.commit()

def get_setting(conn, key, default=""):
    row = qone(conn, "SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default

def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))
    conn.commit()

def renumber_table(conn, table_name):
    """删除记录后重新编号，从1开始连续，无空缺（仅删除时调用）"""
    rows = conn.execute(f"SELECT id FROM {table_name} ORDER BY id").fetchall()
    for i, row in enumerate(rows, 1):
        conn.execute(f"UPDATE {table_name} SET id=? WHERE id=?", (i, row["id"]))
    conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table_name,))

# ============================================================
# 产能与周期计算公式（固定规则，不可修改）
# ============================================================
def calc_injection_hour_capacity(cycle_sec, cavity_num):
    """注塑小时产能 = 3600 ÷ 成型周期(秒) × 模穴数（纯理论，不含损耗）"""
    c = float(cycle_sec or 0)
    n = int(cavity_num or 0)
    if c <= 0 or n <= 0:
        return 0.0
    return round(3600.0 / c * n, 2)

def calc_shift_capacity(hour_capacity, shift_hours):
    """班产 = 小时产能 × 班次时长（纯理论，不含损耗；班次时长由工序配置手工填写）"""
    return round(float(hour_capacity or 0) * float(shift_hours or 0), 2)

def calc_base_cycle(need_qty, shift_capacity):
    """基础生产周期 = 生产数量 ÷ 班产（纯生产时间，不含损耗）"""
    q = float(need_qty or 0)
    s = float(shift_capacity or 0)
    if s <= 0:
        return 0.0
    return round(q / s, 4)

def calc_schedule_cycle(base_cycle, is_injection, mold_change_ratio=0, material_prep_ratio=0):
    """
    排产实际周期（仅排产时叠加损耗）：
    注塑：基础周期 × (1 + 换模比例) × (1 + 备料比例)
    通用：基础周期 × (1 + 备料比例)
    比例按百分比数值传入（如 5 表示 5%）
    """
    b = float(base_cycle or 0)
    if b <= 0:
        return 0.0
    mc = float(mold_change_ratio or 0) / 100.0
    mp = float(material_prep_ratio or 0) / 100.0
    if is_injection:
        return round(b * (1 + mc) * (1 + mp), 4)
    return round(b * (1 + mp), 4)

def get_fg_stock(conn, product_code):
    if not product_code:
        return 0.0
    row = qone(conn, "SELECT stock_qty FROM fg_inventory WHERE product_code=?", (product_code,))
    return float(row["stock_qty"] or 0) if row else 0.0

def get_semi_stock(conn, item_code, process_code):
    row = qone(conn, "SELECT current_qty FROM semi_product_stock WHERE item_code=? AND process_code=?",
               (item_code, process_code))
    return float(row["current_qty"] or 0) if row else 0.0

def consume_materials(conn, item_code, process_code, process_name, order_no, report_qty):
    """
    报工时按BOM自动扣减“本工序”绑定消耗的物料。
    消耗量 = 本次报工件数 × 单件用量（单件用量按每个成品计，注塑不乘模穴数）。
    注塑物料单位一般为公斤（如每件0.05公斤=50克），包装/装配一般为个，单位取物料档案。
    返回扣减明细，供页面展示与缺料预警。
    """
    boms = conn.execute("""SELECT * FROM item_bom WHERE item_code=?
                           AND IFNULL(consume_process_code,'')=IFNULL(?, '')""",
                        (item_code, process_code or "")).fetchall()
    details = []
    for b in boms:
        usage = sf(b["unit_usage"], 0)
        if usage <= 0 or report_qty <= 0:
            continue
        consume_qty = round(report_qty * usage, 4)
        mat = qone(conn, "SELECT * FROM materials WHERE code=?", (b["material_code"],))
        unit = (mat["unit"] if mat and mat["unit"] else "")
        before = sf(mat["stock_qty"], 0) if mat else 0
        after = round(before - consume_qty, 4)
        if mat:
            conn.execute("UPDATE materials SET stock_qty=? WHERE code=?", (after, b["material_code"]))
        conn.execute("""INSERT INTO material_io(material_code,material_name,io_type,qty,remark,created_at)
                        VALUES(?,?,?,?,?,?)""",
                     (b["material_code"], b["material_name"], "生产领料", consume_qty,
                      f"工单{order_no} {process_name}报工{report_qty}件×单件{usage}{unit}", now_str()))
        details.append({"code": b["material_code"], "name": b["material_name"], "unit": unit,
                        "usage": usage, "consume": consume_qty, "before": before, "after": after})
    return details

# ============================================================
# 数据库初始化
# ============================================================
def init_db(conn):
    c = conn.cursor()
    DB_VERSION = "20260909_v3_final"
    try:
        old = qone(conn, "SELECT value FROM settings WHERE key='db_version'")
        if old and old["value"] != DB_VERSION:
            c.executescript("""
            DROP TABLE IF EXISTS users;
            DROP TABLE IF EXISTS personnel;
            DROP TABLE IF EXISTS materials;
            DROP TABLE IF EXISTS machine_type_dict;
            DROP TABLE IF EXISTS machines;
            DROP TABLE IF EXISTS process_template;
            DROP TABLE IF EXISTS products;
            DROP TABLE IF EXISTS product_process;
            DROP TABLE IF EXISTS item_bom;
            DROP TABLE IF EXISTS orders;
            DROP TABLE IF EXISTS work_order_process;
            DROP TABLE IF EXISTS schedules;
            DROP TABLE IF EXISTS material_io;
            DROP TABLE IF EXISTS semi_product_io;
            DROP TABLE IF EXISTS semi_product_stock;
            DROP TABLE IF EXISTS fg_inventory;
            DROP TABLE IF EXISTS fg_io;
            DROP TABLE IF EXISTS settings;
            DROP TABLE IF EXISTS operation_logs;
            """)
            conn.commit()
    except Exception:
        pass

    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT DEFAULT '车间操作员',
        workshop TEXT DEFAULT '',
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS personnel(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        name TEXT,
        post TEXT,
        status TEXT DEFAULT '在职',
        skill TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS materials(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        name TEXT,
        mtype TEXT DEFAULT '主材',
        unit TEXT DEFAULT '个',
        stock_qty REAL DEFAULT 0,
        safe_stock REAL DEFAULT 0
    );
    -- 机台类型字典
    CREATE TABLE IF NOT EXISTS machine_type_dict(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type_name TEXT UNIQUE,
        remark TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS machines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        name TEXT,
        status TEXT DEFAULT '闲置',
        mtype TEXT DEFAULT '注塑机'
    );
    -- 工序模板（精简：只保留基础信息，不带产能参数）
    CREATE TABLE IF NOT EXISTS process_template(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        process_code TEXT UNIQUE,
        process_name TEXT,
        workshop_name TEXT DEFAULT '',
        is_injection INTEGER DEFAULT 0,
        remark TEXT DEFAULT ''
    );
    -- 产品主数据：唯一成品品号
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        name TEXT,
        spec TEXT,
        customer TEXT DEFAULT '',
        material TEXT DEFAULT '',
        remark TEXT DEFAULT ''
    );
    -- 产品工艺路线：单品号多工序，班次时长手工填写
    CREATE TABLE IF NOT EXISTS product_process(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_code TEXT NOT NULL,
        process_code TEXT DEFAULT '',
        process_name TEXT DEFAULT '',
        process_seq INTEGER DEFAULT 1,
        workshop_name TEXT DEFAULT '',
        machine_type TEXT DEFAULT '',
        hour_capacity REAL DEFAULT 0,
        shift_hours REAL DEFAULT 8,
        shift_capacity REAL DEFAULT 0,
        prep_ratio REAL DEFAULT 0,
        cavity_num INTEGER,
        cycle_time REAL,
        mold_ratio REAL,
        is_injection INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1,
        remark TEXT DEFAULT ''
    );
    -- 单层BOM：物料绑定消耗工序
    CREATE TABLE IF NOT EXISTS item_bom(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_code TEXT NOT NULL,
        material_code TEXT,
        material_name TEXT,
        unit_usage REAL DEFAULT 0,
        consume_process_code TEXT DEFAULT '',
        consume_process_name TEXT DEFAULT '',
        remark TEXT DEFAULT ''
    );
    -- 生产总工单（无班次字段，班次时长取自工序配置）
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT UNIQUE,
        item_code TEXT,
        item_name TEXT,
        spec TEXT,
        order_qty REAL DEFAULT 0,
        stock_qty REAL DEFAULT 0,
        need_qty REAL DEFAULT 0,
        expect_start_time TEXT,
        expect_finish_time TEXT,
        eval_finish_time TEXT,
        plan_start_date TEXT,
        plan_end_date TEXT,
        total_cycle REAL DEFAULT 0,
        status TEXT DEFAULT '新建订单',
        warn_level TEXT DEFAULT '',
        remark TEXT,
        created_at TEXT
    );
    -- 工序子工单
    CREATE TABLE IF NOT EXISTS work_order_process(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        main_order_no TEXT,
        order_id INTEGER,
        item_code TEXT,
        process_seq INTEGER,
        process_code TEXT,
        process_name TEXT,
        workshop_name TEXT,
        machine_type TEXT,
        plan_qty REAL DEFAULT 0,
        report_qty REAL DEFAULT 0,
        sub_status TEXT DEFAULT '生产中',
        plan_start TEXT,
        plan_end TEXT,
        actual_start TEXT,
        actual_end TEXT,
        hour_capacity REAL DEFAULT 0,
        shift_hours REAL DEFAULT 0,
        shift_capacity REAL DEFAULT 0,
        schedule_cycle REAL DEFAULT 0,
        cavity_num INTEGER,
        cycle_time REAL,
        mold_ratio REAL,
        prep_ratio REAL DEFAULT 0,
        is_injection INTEGER DEFAULT 0,
        remark TEXT DEFAULT ''
    );
    -- 排产记录（无班次字段）
    CREATE TABLE IF NOT EXISTS schedules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        wo_id INTEGER,
        item_code TEXT,
        item_name TEXT,
        process_name TEXT,
        machine_code TEXT,
        plan_start TEXT,
        plan_end TEXT,
        schedule_cycle REAL,
        hour_capacity REAL,
        shift_hours REAL,
        shift_capacity REAL,
        cavity_num INTEGER,
        cycle_time REAL,
        mold_ratio REAL,
        prep_ratio REAL,
        operator TEXT,
        remark TEXT,
        scheduled INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS material_io(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        material_code TEXT,
        material_name TEXT,
        io_type TEXT,
        qty REAL,
        remark TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS semi_product_io(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_no TEXT,
        bill_type TEXT,
        item_code TEXT,
        process_code TEXT,
        process_name TEXT,
        source_order_no TEXT,
        qty REAL DEFAULT 0,
        warehouse TEXT DEFAULT '半成品仓',
        unit_cost REAL DEFAULT 0,
        operator TEXT,
        operate_time TEXT,
        audit_status TEXT DEFAULT '已审核',
        remark TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS semi_product_stock(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_code TEXT,
        process_code TEXT,
        process_name TEXT,
        warehouse TEXT DEFAULT '半成品仓',
        beginning_qty REAL DEFAULT 0,
        total_in REAL DEFAULT 0,
        total_out REAL DEFAULT 0,
        current_qty REAL DEFAULT 0,
        moving_cost REAL DEFAULT 0,
        lock_qty REAL DEFAULT 0,
        update_time TEXT
    );
    CREATE TABLE IF NOT EXISTS fg_inventory(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_code TEXT UNIQUE,
        product_name TEXT,
        spec TEXT,
        stock_qty REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS fg_io(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_code TEXT,
        product_name TEXT,
        spec TEXT,
        out_qty REAL DEFAULT 0,
        in_qty REAL DEFAULT 0,
        balance REAL DEFAULT 0,
        remark TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS operation_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT,
        entity_id TEXT,
        action TEXT,
        detail TEXT,
        created_at TEXT
    );
    """)

    # 默认设置
    if not qone(conn, "SELECT 1 FROM settings WHERE key='warn_threshold_hours'"):
        set_setting(conn, "warn_threshold_hours", "24")
        set_setting(conn, "material_types", "主材,辅材,包装材料,其他")
    # 默认机台类型
    if not qone(conn, "SELECT 1 FROM machine_type_dict"):
        for t in ["注塑机", "装配机", "包装机", "冲压机"]:
            conn.execute("INSERT INTO machine_type_dict(type_name) VALUES(?)", (t,))
    # 默认工序模板（精简，只含基础信息）
    if not qone(conn, "SELECT 1 FROM process_template WHERE process_code='INJ'"):
        conn.execute("INSERT INTO process_template(process_code,process_name,workshop_name,is_injection) VALUES('INJ','注塑','注塑车间',1)")
        conn.execute("INSERT INTO process_template(process_code,process_name,workshop_name,is_injection) VALUES('ASM','装配','装配车间',0)")
        conn.execute("INSERT INTO process_template(process_code,process_name,workshop_name,is_injection) VALUES('PKG','包装','包装车间',0)")
    # 默认管理员
    if not qone(conn, "SELECT 1 FROM users WHERE username='admin'"):
        conn.execute("INSERT INTO users(username,password,role,workshop,created_at) VALUES('admin','123456','总调度','',?)",
                     (now_str(),))
    # 状态口径迁移：排产后工序即为生产中，历史“待生产”统一改为“生产中”
    conn.execute("UPDATE work_order_process SET sub_status='生产中' WHERE sub_status='待生产'")
    # 兼容旧库：订单表补充“评估完工时间”列（不重建、不清数据）
    order_cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "eval_finish_time" not in order_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN eval_finish_time TEXT")
    set_setting(conn, "db_version", DB_VERSION)
    conn.commit()

# ============================================================
# 通用UI组件
# ============================================================
def editable_table(df, key, col_config=None):
    if df.empty:
        df = pd.DataFrame(columns=df.columns)
    if "选中" not in df.columns:
        df.insert(0, "选中", False)
    edited = st.data_editor(df, num_rows="dynamic", width="stretch",
                            hide_index=True, column_config=col_config or {}, key=key)
    return edited

def selected_ids(edited_df, id_col="编号"):
    if edited_df.empty or "选中" not in edited_df.columns:
        return []
    mask = edited_df["选中"].astype(bool)
    ids = edited_df[mask][id_col].dropna().tolist()
    return [si(i) for i in ids if si(i) > 0]

def export_excel(df, filename):
    buf = BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    st.download_button("导出Excel", buf, file_name=filename,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ============================================================
# 数据备份与恢复（程序升级/换电脑/出错时，数据可完整导回，不丢失）
# ============================================================
# 纳入 Excel 备份的基础资料表（表头用数据库英文字段名，导入时按字段名精确匹配，不会错位）
BACKUP_TABLES = [
    ("personnel", "人员"),
    ("materials", "物料"),
    ("machine_type_dict", "机台类型"),
    ("machines", "机台"),
    ("process_template", "工序模板"),
    ("products", "产品"),
    ("product_process", "工序配置"),
    ("item_bom", "BOM"),
    ("users", "用户"),
]

def table_columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

def backup_master_excel(conn):
    """全部基础资料导出为一个多sheet Excel，表头用数据库英文字段名，导入精确匹配防错位"""
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame([
            ["用途", "宸赋智控系统基础资料备份文件"],
            ["规则", "每个工作表(sheet)对应一张表，表头为数据库英文字段，请勿修改/删除表头行"],
            ["编辑", "可在数据行增删改内容，保存后可在系统设置→数据备份恢复中导回"],
        ], columns=["项目", "说明"]).to_excel(writer, sheet_name="说明", index=False)
        for table, _cn in BACKUP_TABLES:
            cols = table_columns(conn, table)
            df = pd.read_sql(f"SELECT {','.join(cols)} FROM {table} ORDER BY id", conn)
            df.to_excel(writer, sheet_name=table, index=False)
    buf.seek(0)
    return buf

def _import_one_table(conn, table, df, mode="追加"):
    """通用单表导入。覆盖=先清空再导入(保留文件编号)；追加=按业务唯一编码更新或新增。整体事务，失败回滚"""
    db_cols = table_columns(conn, table)
    cols = [c for c in db_cols if c in df.columns]
    if not cols:
        raise ValueError("文件表头与系统字段不匹配，请使用系统下载的模板")
    try:
        conn.execute("BEGIN")
        if mode == "覆盖":
            conn.execute(f"DELETE FROM {table}")
            insert_cols, verb = cols, "INSERT"
        else:
            insert_cols, verb = [c for c in cols if c != "id"], "INSERT OR REPLACE"
        ph = ",".join(["?"] * len(insert_cols))
        col_sql = ",".join(insert_cols)
        n = 0
        for _, row in df.iterrows():
            if all(pd.isna(row[c]) for c in insert_cols):
                continue  # 跳过整行空行
            vals = [None if pd.isna(row[c]) else row[c] for c in insert_cols]
            conn.execute(f"{verb} INTO {table}({col_sql}) VALUES({ph})", vals)
            n += 1
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise

def restore_master_excel(conn, uploaded_bytes, mode="覆盖"):
    """从备份Excel导入基础资料（多sheet）。整体事务，失败回滚"""
    xls = pd.read_excel(BytesIO(uploaded_bytes), sheet_name=None, engine="openpyxl")
    report = []
    try:
        conn.execute("BEGIN")
        for table, cn in BACKUP_TABLES:
            if table not in xls:
                continue
            n = _import_one_table(conn, table, xls[table], mode)
            report.append(f"{cn} {n}条")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report


def backup_db_bytes():
    """读取整库文件字节：含基础资料+订单+排产+库存+报工+日志，100%完整"""
    with open(DB_PATH, "rb") as f:
        return f.read()

def restore_db_file(conn, uploaded_bytes):
    """用整库.db备份在线恢复（sqlite backup API），恢复后需 rerun 重建连接"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    try:
        tmp.write(uploaded_bytes)
        tmp.flush()
        tmp.close()
        try:
            src = sqlite3.connect(tmp.name)
            ok = src.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error:
            try:
                src.close()
            except Exception:
                pass
            raise ValueError("上传文件不是有效的数据库备份(.db)文件")
        if ok != "ok":
            src.close()
            raise ValueError("备份文件损坏，完整性校验未通过")
        conn.commit()
        conn.close()
        dst = sqlite3.connect(DB_PATH)
        src.backup(dst)
        dst.close()
        src.close()
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

# ============================================================
# 通用 Excel 导入（兼容页面导出的中文表头，外部编辑后可直接导回）
# ============================================================
# 中文列名 -> 数据库英文字段
TABLE_CN_MAP = {
    "personnel": {"工号": "code", "姓名": "name", "岗位": "post", "状态": "status", "技能": "skill"},
    "materials": {"物料编号": "code", "名称": "name", "类型": "mtype", "单位": "unit",
                  "当前库存": "stock_qty", "安全库存": "safe_stock"},
    "machine_type_dict": {"机台类型": "type_name", "备注": "remark"},
    "machines": {"机台编号": "code", "机台名称": "name", "状态": "status", "机台类型": "mtype"},
    "process_template": {"工序编码": "process_code", "工序名称": "process_name", "所属车间": "workshop_name",
                         "注塑工序": "is_injection", "备注": "remark"},
    "products": {"品号": "code", "品名": "name", "规格": "spec", "客户": "customer",
                 "材质": "material", "备注": "remark"},
}

def import_table_excel(conn, table, data_bytes, mode, cn_map, yn_cols=()):
    """把外部编辑好的Excel导入单张基础表。兼容中文/英文表头；整体事务，失败回滚。返回导入条数"""
    df = pd.read_excel(BytesIO(data_bytes), engine="openpyxl")
    df = df.drop(columns=[c for c in ["选中", "编号"] if c in df.columns], errors="ignore")
    df = df.rename(columns=cn_map)
    db_cols = table_columns(conn, table)
    cols = [c for c in db_cols if c in df.columns]
    if mode == "追加" and "id" in cols:
        cols.remove("id")
    if not cols:
        raise ValueError("表头与系统不匹配，请先用本页“导出Excel”作为模板再编辑")
    try:
        conn.execute("BEGIN")
        if mode == "覆盖":
            conn.execute(f"DELETE FROM {table}")
        verb = "INSERT" if mode == "覆盖" else "INSERT OR REPLACE"
        ph = ",".join(["?"] * len(cols))
        csql = ",".join(cols)
        n = 0
        for _, row in df.iterrows():
            vals, empty = [], True
            for c in cols:
                v = row[c]
                if pd.isna(v):
                    v = None
                if c in yn_cols:
                    v = 1 if str(v).strip() in ("是", "1", "True", "true", "Y", "y") else 0
                if v not in (None, ""):
                    empty = False
                vals.append(v)
            if empty:
                continue
            conn.execute(f"{verb} INTO {table}({csql}) VALUES({ph})", vals)
            n += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n

def import_widget(conn, table, key_prefix, yn_cols=()):
    """基础资料页通用导入控件：可先用导出做模板，外部编辑后导回"""
    cn_map = TABLE_CN_MAP.get(table, {})
    with st.expander("📥 导入Excel（先点上方“导出Excel”做模板，外部编辑后导回）"):
        up = st.file_uploader("选择要导入的 Excel", type=["xlsx"], key=f"{key_prefix}_up")
        mode = st.radio("导入方式", ["追加（保留原有，相同编码自动更新）", "覆盖（先清空再导入）"],
                        horizontal=True, key=f"{key_prefix}_mode")
        if up is not None and st.button("开始导入", key=f"{key_prefix}_btn", type="primary"):
            try:
                with st.spinner("正在导入..."):
                    m = "覆盖" if mode.startswith("覆盖") else "追加"
                    n = import_table_excel(conn, table, up.getvalue(), m, cn_map, yn_cols)
                st.success(f"导入完成，共处理 {n} 条")
                time.sleep(0.6)
                st.rerun()
            except Exception as e:
                st.error(f"导入失败（已回滚，原数据未改动）：{e}")

def import_orders_excel(conn, data_bytes, mode):
    """订单专用导入：自动补品名/规格、算需生产数、生成工单号"""
    cn = {"品号": "item_code", "品名": "item_name", "规格": "spec", "订单数量": "order_qty",
          "库存数量": "stock_qty", "预计开工时间": "expect_start_time",
          "预计完工时间": "expect_finish_time", "备注": "remark"}
    df = pd.read_excel(BytesIO(data_bytes), engine="openpyxl")
    df = df.drop(columns=[c for c in ["选中", "编号", "工单号"] if c in df.columns], errors="ignore")
    df = df.rename(columns=cn)
    if "item_code" not in df.columns or "order_qty" not in df.columns:
        raise ValueError("至少需要“品号”和“订单数量”两列，请先用订单页“导出Excel”做模板")
    try:
        conn.execute("BEGIN")
        if mode == "覆盖":
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM schedules")
            conn.execute("DELETE FROM work_order_process")
        n = 0
        ts = datetime.now().strftime("%m%d%H%M%S")
        for i, row in df.iterrows():
            code = None if pd.isna(row.get("item_code")) else str(row.get("item_code")).strip()
            qty = sf(row.get("order_qty"), 0)
            if not code or code == "nan" or qty <= 0:
                continue
            name = row.get("item_name")
            name = "" if pd.isna(name) else str(name)
            spec = row.get("spec")
            spec = "" if pd.isna(spec) else str(spec)
            if not name:
                p = qone(conn, "SELECT name,spec FROM products WHERE code=?", (code,))
                if p:
                    name = p["name"] or ""
                    spec = spec or (p["spec"] or "")
            stock = sf(row.get("stock_qty"), 0)
            need = max(0, qty - stock)
            es = row.get("expect_start_time")
            es = "" if pd.isna(es) else str(es)[:10]
            ef = row.get("expect_finish_time")
            ef = "" if pd.isna(ef) else str(ef)[:10]
            rmk = row.get("remark")
            rmk = "" if pd.isna(rmk) else str(rmk)
            order_no = f"ORD{ts}{n+1:02d}"
            eval_dt, _d, _det = evaluate_finish(conn, code, need, es or today_str())
            conn.execute("""INSERT INTO orders(order_no,item_code,item_name,spec,order_qty,stock_qty,need_qty,
                            expect_start_time,expect_finish_time,eval_finish_time,status,remark,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (order_no, code, name, spec, qty, stock, need, es or today_str(),
                          ef or None, eval_dt, "新建订单", rmk, now_str()))
            n += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n


def get_material_types(conn):
    val = get_setting(conn, "material_types", "主材,辅材,包装材料,其他")
    return [t.strip() for t in val.split(",") if t.strip()]

# 需要保留两位小数的计量单位（重量/体积类）；其余计件单位（个/瓶/张/套/卷/箱/米等）显示整数
DECIMAL_UNITS = {"公斤", "千克", "kg", "KG", "Kg", "克", "g", "G", "升", "L", "l", "毫升", "ml", "ML"}

def fmt_stock_by_unit(df, stock_cols=("当前库存", "安全库存"), unit_col="单位"):
    """仅格式化【显示】：重量/体积单位显示2位小数字符串，计件单位显示整数字符串；不改数据库真实数值"""
    if df is None or df.empty or unit_col not in df.columns:
        return df
    out = df.copy()
    for col in stock_cols:
        if col not in out.columns:
            continue
        def _f(r):
            try:
                v = float(r[col])
            except Exception:
                return r[col]
            u = str(r.get(unit_col, "") or "").strip()
            if u in DECIMAL_UNITS:
                return f"{v:.2f}"
            return str(int(round(v)))
        out[col] = out.apply(_f, axis=1)
    return out

def fmt_int_cols(df, cols):
    """计件数量列显示为整数（无小数），仅改显示"""
    if df is None or df.empty:
        return df
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).round().astype(int)
    return out

def get_machine_types(conn):
    rows = conn.execute("SELECT type_name FROM machine_type_dict ORDER BY id").fetchall()
    return [r["type_name"] for r in rows]

def get_process_list(conn):
    rows = conn.execute(
        "SELECT process_code,process_name,is_injection,workshop_name FROM process_template ORDER BY id").fetchall()
    return [(r["process_code"], r["process_name"], bool(r["is_injection"]), r["workshop_name"] or "") for r in rows]

def next_process_seq(conn, item_code):
    """工序序号自动从1开始连续"""
    row = qone(conn, "SELECT COALESCE(MAX(process_seq),0) as m FROM product_process WHERE item_code=?", (item_code,))
    return si(row["m"], 0) + 1

# ============================================================
# 页面：基础资料
# ============================================================
def page_master(conn):
    st.header("基础资料")
    t1, t2, t3, t4 = st.tabs(["人员配置", "物料配置", "机台配置", "工序模板"])

    # ---- 人员 ----
    with t1:
        st.markdown("#### 人员配置")
        with st.expander("添加/编辑人员"):
            with st.form("person_f", clear_on_submit=True):
                c1, c2, c3, c4 = st.columns(4)
                pc = c1.text_input("工号")
                pn = c2.text_input("姓名")
                pp = c3.text_input("岗位")
                ps = c4.selectbox("状态", ["在职", "离职", "休假"])
                if st.form_submit_button("保存"):
                    if pc and pn:
                        conn.execute("INSERT INTO personnel(code,name,post,status) VALUES(?,?,?,?) "
                                     "ON CONFLICT(code) DO UPDATE SET name=excluded.name,post=excluded.post,status=excluded.status",
                                     (pc, pn, pp, ps))
                        conn.commit()
                        st.success("保存成功")
        df = pd.read_sql("SELECT id as 编号,code as 工号,name as 姓名,post as 岗位,status as 状态 FROM personnel ORDER BY id", conn)
        ed = editable_table(df, "person_tbl")
        c1, c2 = st.columns(2)
        if c1.button("保存修改", key="person_save", type="primary"):
            with st.spinner("正在保存..."):
                n = 0
                for _, r in ed.iterrows():
                    code = str(r.get("工号", "") or "").strip()
                    if not code or code == "nan":
                        continue
                    rid = r.get("编号")
                    if pd.notna(rid) and si(rid) > 0:
                        conn.execute("UPDATE personnel SET code=?,name=?,post=?,status=? WHERE id=?",
                                     (code, str(r.get("姓名","")or""), str(r.get("岗位","")or""),
                                      str(r.get("状态","")or"在职"), si(rid)))
                        n += 1
                conn.commit()
            st.success(f"保存 {n} 条")
            st.rerun()
        if c2.button("删除选中", key="person_del"):
            ids = selected_ids(ed)
            if ids:
                with st.spinner("正在删除..."):
                    conn.execute(f"DELETE FROM personnel WHERE id IN ({','.join(['?']*len(ids))})", ids)
                    renumber_table(conn, "personnel")
                    conn.commit()
                st.rerun()
        export_excel(df.drop(columns=["选中"], errors="ignore"), "人员配置.xlsx")
        import_widget(conn, "personnel", "imp_person")

    # ---- 物料 ----
    with t2:
        st.markdown("#### 物料配置")
        mtypes = get_material_types(conn)
        with st.expander("添加/编辑物料"):
            with st.form("mat_f", clear_on_submit=True):
                c1, c2, c3, c4, c5, c6 = st.columns(6)
                mc = c1.text_input("物料编号")
                mn = c2.text_input("名称")
                mt = c3.selectbox("类型", mtypes)
                mu = c4.selectbox("单位", ["个", "公斤", "克", "米", "张", "套", "卷", "箱", "瓶"])
                ms = c5.number_input("当前库存", min_value=0.0, step=1.0)
                mss = c6.number_input("安全库存", min_value=0.0, step=1.0)
                if st.form_submit_button("保存"):
                    if mc and mn:
                        conn.execute("INSERT INTO materials(code,name,mtype,unit,stock_qty,safe_stock) VALUES(?,?,?,?,?,?) "
                                     "ON CONFLICT(code) DO UPDATE SET name=excluded.name,mtype=excluded.mtype,unit=excluded.unit,stock_qty=excluded.stock_qty,safe_stock=excluded.safe_stock",
                                     (mc, mn, mt, mu, ms, mss))
                        conn.commit()
                        st.success("保存成功")
        ft = st.selectbox("筛选类型", ["全部"] + mtypes, key="mat_ft")
        sql = "SELECT id as 编号,code as 物料编号,name as 名称,mtype as 类型,unit as 单位,stock_qty as 当前库存,safe_stock as 安全库存 FROM materials"
        if ft != "全部":
            sql += f" WHERE mtype='{ft}'"
        sql += " ORDER BY id"
        df = pd.read_sql(sql, conn)
        df = fmt_stock_by_unit(df)
        ed = editable_table(df, "mat_tbl", col_config={"类型": st.column_config.SelectboxColumn("类型", options=mtypes),
                                                        "单位": st.column_config.SelectboxColumn("单位", options=["个","公斤","克","米","张","套","卷","箱","瓶"])})
        c1, c2 = st.columns(2)
        if c1.button("保存修改", key="mat_save", type="primary"):
            with st.spinner("正在保存..."):
                n = 0
                for _, r in ed.iterrows():
                    code = str(r.get("物料编号", "") or "").strip()
                    if not code or code == "nan":
                        continue
                    rid = r.get("编号")
                    if pd.notna(rid) and si(rid) > 0:
                        conn.execute("UPDATE materials SET code=?,name=?,mtype=?,unit=?,stock_qty=?,safe_stock=? WHERE id=?",
                                     (code, str(r.get("名称","")or""), str(r.get("类型","")or"主材"),
                                      str(r.get("单位","")or"个"), sf(r.get("当前库存",0)), sf(r.get("安全库存",0)), si(rid)))
                        n += 1
                conn.commit()
            st.success(f"保存 {n} 条")
            st.rerun()
        if c2.button("删除选中", key="mat_del"):
            ids = selected_ids(ed)
            if ids:
                with st.spinner("正在删除..."):
                    conn.execute(f"DELETE FROM materials WHERE id IN ({','.join(['?']*len(ids))})", ids)
                    renumber_table(conn, "materials")
                    conn.commit()
                st.rerun()
        export_excel(df.drop(columns=["选中"], errors="ignore"), "物料配置.xlsx")
        import_widget(conn, "materials", "imp_mat")

    # ---- 机台配置 + 机台类型管理 ----
    with t3:
        st.markdown("#### 机台配置")
        machine_types = get_machine_types(conn)
        with st.expander("添加/编辑机台"):
            with st.form("mach_f", clear_on_submit=True):
                c1, c2, c3, c4 = st.columns(4)
                mc = c1.text_input("机台编号")
                mn = c2.text_input("机台名称")
                ms = c3.selectbox("状态", ["闲置", "生产", "维修"])
                mt = c4.selectbox("机台类型", machine_types if machine_types else ["注塑机"])
                if st.form_submit_button("保存"):
                    if mc and mn:
                        conn.execute("INSERT INTO machines(code,name,status,mtype) VALUES(?,?,?,?) "
                                     "ON CONFLICT(code) DO UPDATE SET name=excluded.name,status=excluded.status,mtype=excluded.mtype",
                                     (mc, mn, ms, mt))
                        conn.commit()
                        st.success("保存成功")
        df = pd.read_sql("SELECT id as 编号,code as 机台编号,name as 机台名称,status as 状态,mtype as 机台类型 FROM machines ORDER BY id", conn)
        ed = editable_table(df, "mach_tbl", col_config={"机台类型": st.column_config.SelectboxColumn("机台类型", options=machine_types)})
        c1, c2 = st.columns(2)
        if c1.button("保存修改", key="mach_save", type="primary"):
            with st.spinner("正在保存..."):
                n = 0
                for _, r in ed.iterrows():
                    code = str(r.get("机台编号", "") or "").strip()
                    if not code or code == "nan":
                        continue
                    rid = r.get("编号")
                    if pd.notna(rid) and si(rid) > 0:
                        conn.execute("UPDATE machines SET code=?,name=?,status=?,mtype=? WHERE id=?",
                                     (code, str(r.get("机台名称","")or""), str(r.get("状态","")or"闲置"),
                                      str(r.get("机台类型","")or"注塑机"), si(rid)))
                        n += 1
                conn.commit()
            st.success(f"保存 {n} 条")
            st.rerun()
        if c2.button("删除选中", key="mach_del"):
            ids = selected_ids(ed)
            if ids:
                with st.spinner("正在删除..."):
                    conn.execute(f"DELETE FROM machines WHERE id IN ({','.join(['?']*len(ids))})", ids)
                    renumber_table(conn, "machines")
                    conn.commit()
                st.rerun()
        export_excel(df.drop(columns=["选中"], errors="ignore"), "机台配置.xlsx")
        import_widget(conn, "machines", "imp_mach")

        st.markdown("---")
        st.markdown("#### 机台类型管理")
        with st.expander("添加机台类型"):
            with st.form("mtype_f", clear_on_submit=True):
                tc1, tc2 = st.columns(2)
                tn = tc1.text_input("机台类型名称")
                tr = tc2.text_input("备注")
                if st.form_submit_button("保存类型"):
                    if tn:
                        conn.execute("INSERT INTO machine_type_dict(type_name,remark) VALUES(?,?) "
                                     "ON CONFLICT(type_name) DO UPDATE SET remark=excluded.remark", (tn, tr))
                        conn.commit()
                        st.success("保存成功")
                        st.rerun()
        tdf = pd.read_sql("SELECT id as 编号,type_name as 机台类型,remark as 备注 FROM machine_type_dict ORDER BY id", conn)
        ted = editable_table(tdf, "mtype_tbl")
        tc1, tc2 = st.columns(2)
        if tc1.button("保存修改", key="mtype_save", type="primary"):
            with st.spinner("正在保存..."):
                n = 0
                for _, r in ted.iterrows():
                    rid = r.get("编号")
                    if pd.notna(rid) and si(rid) > 0:
                        conn.execute("UPDATE machine_type_dict SET type_name=?,remark=? WHERE id=?",
                                     (str(r.get("机台类型","")or""), str(r.get("备注","")or""), si(rid)))
                        n += 1
                conn.commit()
            st.success(f"保存 {n} 条")
            st.rerun()
        if tc2.button("删除选中", key="mtype_del"):
            ids = selected_ids(ted)
            if ids:
                with st.spinner("正在删除..."):
                    conn.execute(f"DELETE FROM machine_type_dict WHERE id IN ({','.join(['?']*len(ids))})", ids)
                    renumber_table(conn, "machine_type_dict")
                    conn.commit()
                st.rerun()
        import_widget(conn, "machine_type_dict", "imp_mtype")

    # ---- 工序模板（精简：只保留基础信息） ----
    with t4:
        st.markdown("#### 工序模板（可自由新增任意工序）")
        with st.expander("添加工序模板"):
            with st.form("ptpl_f", clear_on_submit=True):
                c1, c2, c3, c4 = st.columns(4)
                pcode = c1.text_input("工序编码")
                pname = c2.text_input("工序名称")
                pws = c3.text_input("所属车间")
                pinj = c4.selectbox("是否注塑工序", ["否", "是"])
                premk = st.text_input("备注", key="ptpl_rmk")
                if st.form_submit_button("保存工序模板"):
                    if pcode and pname:
                        inj_val = 1 if pinj == "是" else 0
                        conn.execute("""INSERT INTO process_template(process_code,process_name,workshop_name,is_injection,remark)
                                        VALUES(?,?,?,?,?) ON CONFLICT(process_code) DO UPDATE SET
                                        process_name=excluded.process_name,workshop_name=excluded.workshop_name,
                                        is_injection=excluded.is_injection,remark=excluded.remark""",
                                     (pcode, pname, pws, inj_val, premk))
                        conn.commit()
                        st.success("保存成功")
        df = pd.read_sql("""SELECT id as 编号,process_code as 工序编码,process_name as 工序名称,
                            workshop_name as 所属车间,
                            CASE WHEN is_injection=1 THEN '是' ELSE '否' END as 注塑工序,remark as 备注
                            FROM process_template ORDER BY id""", conn)
        ed = editable_table(df, "ptpl_tbl")
        c1, c2 = st.columns(2)
        if c1.button("保存修改", key="ptpl_save", type="primary"):
            with st.spinner("正在保存..."):
                n = 0
                for _, r in ed.iterrows():
                    code = str(r.get("工序编码", "") or "").strip()
                    if not code or code == "nan":
                        continue
                    rid = r.get("编号")
                    if pd.notna(rid) and si(rid) > 0:
                        inj = 1 if str(r.get("注塑工序", "否")) == "是" else 0
                        conn.execute("""UPDATE process_template SET process_code=?,process_name=?,workshop_name=?,
                                        is_injection=?,remark=? WHERE id=?""",
                                     (code, str(r.get("工序名称","")or""), str(r.get("所属车间","")or""),
                                      inj, str(r.get("备注","")or""), si(rid)))
                        n += 1
                conn.commit()
            st.success(f"保存 {n} 条")
            st.rerun()
        if c2.button("删除选中", key="ptpl_del"):
            ids = selected_ids(ed)
            if ids:
                with st.spinner("正在删除..."):
                    conn.execute(f"DELETE FROM process_template WHERE id IN ({','.join(['?']*len(ids))})", ids)
                    renumber_table(conn, "process_template")
                    conn.commit()
                st.rerun()
        export_excel(df.drop(columns=["选中"], errors="ignore"), "工序模板.xlsx")
        import_widget(conn, "process_template", "imp_ptpl", yn_cols=["is_injection"])

# ============================================================
# 页面：产品工艺配置
# ============================================================
def page_product(conn):
    st.header("产品工艺配置")
    t1, t2, t3 = st.tabs(["产品信息", "工序配置", "BOM配置"])

    # ---- 产品信息 ----
    with t1:
        st.markdown("#### 产品信息（唯一成品品号）")
        with st.expander("添加/编辑产品"):
            with st.form("prod_f", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                pc = c1.text_input("品号")
                pn = c2.text_input("品名")
                ps = c3.text_input("规格")
                c4, c5 = st.columns(2)
                pcust = c4.text_input("客户")
                pmat = c5.text_input("材质")
                premk = st.text_input("备注")
                if st.form_submit_button("保存"):
                    if pc and pn:
                        conn.execute("""INSERT INTO products(code,name,spec,customer,material,remark)
                                        VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET
                                        name=excluded.name,spec=excluded.spec,customer=excluded.customer,
                                        material=excluded.material,remark=excluded.remark""",
                                     (pc, pn, ps, pcust, pmat, premk))
                        conn.commit()
                        st.success("保存成功")
        df = pd.read_sql("SELECT id as 编号,code as 品号,name as 品名,spec as 规格,customer as 客户,material as 材质,remark as 备注 FROM products ORDER BY id", conn)
        ed = editable_table(df, "prod_tbl")
        c1, c2 = st.columns(2)
        if c1.button("保存修改", key="prod_save", type="primary"):
            with st.spinner("正在保存..."):
                n = 0
                for _, r in ed.iterrows():
                    code = str(r.get("品号", "") or "").strip()
                    if not code or code == "nan":
                        continue
                    rid = r.get("编号")
                    if pd.notna(rid) and si(rid) > 0:
                        conn.execute("UPDATE products SET code=?,name=?,spec=?,customer=?,material=?,remark=? WHERE id=?",
                                     (code, str(r.get("品名","")or""), str(r.get("规格","")or""),
                                      str(r.get("客户","")or""), str(r.get("材质","")or""),
                                      str(r.get("备注","")or""), si(rid)))
                        n += 1
                conn.commit()
            st.success(f"保存 {n} 条")
            st.rerun()
        if c2.button("删除选中", key="prod_del"):
            ids = selected_ids(ed)
            if ids:
                with st.spinner("正在删除..."):
                    conn.execute(f"DELETE FROM products WHERE id IN ({','.join(['?']*len(ids))})", ids)
                    renumber_table(conn, "products")
                    conn.commit()
                st.rerun()
        export_excel(df.drop(columns=["选中"], errors="ignore"), "产品信息.xlsx")
        import_widget(conn, "products", "imp_prod")

    # ---- 工序配置 ----
    with t2:
        st.markdown("#### 工序配置（按产品自由勾选、排序工序；班次时长手工填写）")
        procs = get_process_list(conn)
        prods = conn.execute("SELECT code,name FROM products ORDER BY code").fetchall()
        machs = conn.execute("SELECT code FROM machines ORDER BY code").fetchall()
        mach_opts = [r["code"] for r in machs]

        if prods:
            pidx = st.selectbox("选择产品", range(len(prods)),
                                format_func=lambda i: f"{prods[i]['code']} {prods[i]['name']}", key="cfg_prod")
            pcode = prods[pidx]["code"]
        else:
            st.warning("暂无产品，请先在产品信息中添加")
            pcode = ""

        with st.expander("添加工序"):
            if procs:
                spidx = st.selectbox("选择工序", range(len(procs)),
                                     format_func=lambda i: f"{procs[i][0]} {procs[i][1]}", key="cfg_proc")
                sp = procs[spidx]
                sp_code, sp_name, sp_inj, sp_ws = sp
            else:
                st.warning("暂无工序模板，请先到基础资料→工序模板添加")
                sp_code, sp_name, sp_inj, sp_ws = "", "", False, ""

            default_seq = next_process_seq(conn, pcode) if pcode else 1
            with st.form("op_f", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                op_seq = c1.number_input("工序序号", min_value=1, step=1, value=default_seq)
                op_mach = c2.selectbox("推荐机台", mach_opts if mach_opts else ["无"], key="op_mach")
                op_shift = c3.number_input("班次时长(小时,手工填写)", min_value=0.0, step=0.5, value=8.0)
                if sp_inj:
                    c4, c5, c6 = st.columns(3)
                    op_cyc = c4.number_input("成型周期(秒)", min_value=0.0, step=0.1, key="op_cyc")
                    op_cav = c5.number_input("模穴数", min_value=0, step=1, key="op_cav")
                    op_mold = c6.number_input("换模时间比例(%)", min_value=0.0, step=1.0, key="op_mold")
                    op_hour = 0.0
                    op_prep = st.number_input("备料时间比例(%)", min_value=0.0, step=1.0, key="op_prep_inj")
                else:
                    op_cyc, op_cav, op_mold = None, None, None
                    c4, c5 = st.columns(2)
                    op_hour = c4.number_input("小时产能", min_value=0.0, step=1.0, key="op_hour_gen")
                    op_prep = c5.number_input("备料时间比例(%)", min_value=0.0, step=1.0, key="op_prep_gen")
                op_rmk = st.text_input("备注", key="op_rmk")
                if st.form_submit_button("保存工序"):
                    if pcode and sp_code:
                        if sp_inj:
                            hour = calc_injection_hour_capacity(op_cyc, op_cav)
                        else:
                            hour = op_hour
                        shift_cap = calc_shift_capacity(hour, op_shift)
                        conn.execute("""INSERT INTO product_process(item_code,process_code,process_name,process_seq,
                                        workshop_name,machine_type,hour_capacity,shift_hours,shift_capacity,
                                        prep_ratio,cavity_num,cycle_time,mold_ratio,is_injection,remark)
                                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                     (pcode, sp_code, sp_name, int(op_seq), sp_ws, op_mach,
                                      hour, op_shift, shift_cap, op_prep,
                                      op_cav if sp_inj else None, op_cyc if sp_inj else None,
                                      op_mold if sp_inj else None, 1 if sp_inj else 0, op_rmk))
                        conn.commit()
                        st.success("保存成功")
                        st.rerun()

        if pcode:
            df = pd.read_sql("""SELECT id as 编号,process_seq as 工序序号,process_code as 工序编码,process_name as 工序名称,
                                workshop_name as 车间,machine_type as 推荐机台,
                                hour_capacity as 小时产能,shift_hours as 班次时长,shift_capacity as 班产,
                                COALESCE(cycle_time,0) as 成型周期,COALESCE(cavity_num,0) as 模穴数,COALESCE(mold_ratio,0) as 换模比例,
                                prep_ratio as 备料比例,
                                CASE WHEN is_injection=1 THEN '是' ELSE '否' END as 注塑工序,remark as 备注
                                FROM product_process WHERE item_code=? AND enabled=1 ORDER BY process_seq""",
                               conn, params=(pcode,))
            if df.empty:
                st.info("该产品暂无工序配置")
            else:
                st.dataframe(df, width="stretch", hide_index=True)
            ed = editable_table(df, "op_tbl")
            c1, c2 = st.columns(2)
            if c1.button("保存修改", key="op_save", type="primary"):
                with st.spinner("正在保存..."):
                    n = 0
                    for _, r in ed.iterrows():
                        rid = r.get("编号")
                        if pd.notna(rid) and si(rid) > 0:
                            inj = 1 if str(r.get("注塑工序", "否")) == "是" else 0
                            if inj:
                                cyc = sf(r.get("成型周期", 0))
                                cav = si(r.get("模穴数", 0))
                                if cyc > 0 and cav > 0:
                                    hour = calc_injection_hour_capacity(cyc, cav)
                                else:
                                    hour = sf(r.get("小时产能", 0))
                            else:
                                hour = sf(r.get("小时产能", 0))
                            shift_h = sf(r.get("班次时长", 8))
                            shift_cap = calc_shift_capacity(hour, shift_h)
                            conn.execute("""UPDATE product_process SET process_seq=?,machine_type=?,hour_capacity=?,
                                            shift_hours=?,shift_capacity=?,prep_ratio=?,cavity_num=?,cycle_time=?,
                                            mold_ratio=?,is_injection=?,remark=? WHERE id=?""",
                                         (si(r.get("工序序号",1)), str(r.get("推荐机台","")or""),
                                          hour, shift_h, shift_cap, sf(r.get("备料比例",0)),
                                          si(r.get("模穴数",0)) if inj else None,
                                          sf(r.get("成型周期",0)) if inj else None,
                                          sf(r.get("换模比例",0)) if inj else None,
                                          inj, str(r.get("备注","")or""), si(rid)))
                            n += 1
                    conn.commit()
                st.success(f"保存 {n} 条")
                st.rerun()
            if c2.button("删除选中工序", key="op_del"):
                ids = selected_ids(ed)
                if ids:
                    with st.spinner("正在删除..."):
                        conn.execute(f"DELETE FROM product_process WHERE id IN ({','.join(['?']*len(ids))})", ids)
                        renumber_table(conn, "product_process")
                        conn.commit()
                    st.rerun()
            export_excel(df.drop(columns=["选中"], errors="ignore"), f"工序配置_{pcode}.xlsx")

    # ---- BOM配置 ----
    with t3:
        st.markdown("#### BOM配置（单层BOM，物料绑定消耗工序）")
        if prods:
            bidx = st.selectbox("选择产品", range(len(prods)),
                                format_func=lambda i: f"{prods[i]['code']} {prods[i]['name']}", key="bom_prod")
            bom_code = prods[bidx]["code"]
        else:
            bom_code = ""
            st.warning("暂无产品")

        mats = conn.execute("SELECT code,name,unit FROM materials ORDER BY code").fetchall()
        mat_opts = [f"{r['code']} {r['name']}（{r['unit'] or '个'}）" for r in mats]
        if bom_code:
            pops = conn.execute("SELECT process_code,process_name FROM product_process WHERE item_code=? AND enabled=1 ORDER BY process_seq",
                                (bom_code,)).fetchall()
        else:
            pops = []
        popts = [f"{r['process_code']} {r['process_name']}" for r in pops]

        st.caption("用量口径：单件用量按【每个成品】计算，报工时 = 报工件数 × 单件用量 自动扣料（注塑不按模次、不乘模穴）。"
                   "注塑物料单位一般为公斤（如每件0.05公斤=50克），包装/装配一般为个；单位在物料档案维护。")
        with st.expander("添加BOM物料"):
            with st.form("bom_f", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                bm = c1.selectbox("物料", mat_opts if mat_opts else ["无"], key="bom_mat")
                bq = c2.number_input("单件用量(每个成品)", min_value=0.0, step=0.001, key="bom_qty")
                bp = c3.selectbox("消耗工序", popts if popts else ["无"], key="bom_proc")
                brmk = st.text_input("备注", key="bom_rmk")
                if st.form_submit_button("保存BOM"):
                    if bom_code and bm != "无":
                        mcode = bm.split(" ")[0]
                        mrow = qone(conn, "SELECT name FROM materials WHERE code=?", (mcode,))
                        mname = mrow["name"] if mrow else ""
                        pcode = bp.split(" ")[0] if bp != "无" else ""
                        pname = " ".join(bp.split(" ")[1:]) if bp != "无" else ""
                        conn.execute("""INSERT INTO item_bom(item_code,material_code,material_name,unit_usage,
                                        consume_process_code,consume_process_name,remark)
                                        VALUES(?,?,?,?,?,?,?)""",
                                     (bom_code, mcode, mname, bq, pcode, pname, brmk))
                        conn.commit()
                        st.success("保存成功")
        if bom_code:
            df = pd.read_sql("""SELECT b.id as 编号,b.material_code as 物料编码,b.material_name as 物料名称,
                                IFNULL(m.unit,'') as 单位,b.unit_usage as 单件用量,b.consume_process_code as 消耗工序编码,
                                b.consume_process_name as 消耗工序名称,b.remark as 备注
                                FROM item_bom b LEFT JOIN materials m ON m.code=b.material_code
                                WHERE b.item_code=? ORDER BY b.id""",
                             conn, params=(bom_code,))
            ed = editable_table(df, "bom_tbl")
            c1, c2 = st.columns(2)
            if c1.button("保存修改", key="bom_save", type="primary"):
                with st.spinner("正在保存..."):
                    n = 0
                    for _, r in ed.iterrows():
                        rid = r.get("编号")
                        if pd.notna(rid) and si(rid) > 0:
                            conn.execute("""UPDATE item_bom SET material_code=?,material_name=?,unit_usage=?,
                                            consume_process_code=?,consume_process_name=?,remark=? WHERE id=?""",
                                         (str(r.get("物料编码","")or""), str(r.get("物料名称","")or""),
                                          sf(r.get("单件用量",0)), str(r.get("消耗工序编码","")or""),
                                          str(r.get("消耗工序名称","")or""), str(r.get("备注","")or""), si(rid)))
                            n += 1
                    conn.commit()
                st.success(f"保存 {n} 条")
                st.rerun()
            if c2.button("删除选中", key="bom_del"):
                ids = selected_ids(ed)
                if ids:
                    with st.spinner("正在删除..."):
                        conn.execute(f"DELETE FROM item_bom WHERE id IN ({','.join(['?']*len(ids))})", ids)
                        renumber_table(conn, "item_bom")
                        conn.commit()
                    st.rerun()
            export_excel(df.drop(columns=["选中"], errors="ignore"), f"BOM_{bom_code}.xlsx")

# ============================================================
# 页面：订单管理（无班次字段）
# ============================================================
def page_orders(conn):
    st.header("订单管理")

    def _calc_need():
        q = float(st.session_state.get("nf_qty", 0) or 0)
        s = float(st.session_state.get("nf_stock", 0) or 0)
        st.session_state.nf_need = int(max(0, round(q - s)))  # 产品按件计，需生产数取整数

    if "nf_need" not in st.session_state:
        st.session_state.nf_need = 0

    col_pc, col_btn = st.columns([3, 1])
    inp = col_pc.text_input("输入品号后点击读取", key="ord_pc")
    if col_btn.button("读取产品", key="ord_read"):
        if inp.strip():
            p = qone(conn, "SELECT * FROM products WHERE code=?", (inp.strip(),))
            if p:
                st.session_state.nf_code = inp.strip()
                st.session_state.nf_name = p["name"] or ""
                st.session_state.nf_spec = p["spec"] or ""
                st.session_state.nf_stock = float(get_fg_stock(conn, inp.strip()))
                _calc_need()
                st.success(f"已读取：{p['name']}，库存：{st.session_state.nf_stock}")
            else:
                st.warning("未找到该品号")

    st.markdown("#### 新增订单")
    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        nf_code = c1.text_input("产品品号", key="nf_code")
        nf_name = c2.text_input("品名", key="nf_name")
        nf_spec = c3.text_input("规格", key="nf_spec")
        c4, c5, c6 = st.columns(3)
        nf_qty = c4.number_input("订单数量（输完按回车或点空白处）", min_value=0.0, step=1.0,
                                 key="nf_qty", on_change=_calc_need)
        nf_stock = c5.number_input("库存数量（输完按回车或点空白处）", min_value=0.0, step=1.0,
                                   key="nf_stock", on_change=_calc_need)
        _calc_need()
        nf_need = int(st.session_state.nf_need)
        c6.markdown("**需生产数（订单−库存，自动·整数）**")
        if nf_need > 0:
            c6.markdown(f"<h2 style='color:#1a7f37;margin:0;'>{nf_need}</h2>", unsafe_allow_html=True)
        else:
            c6.markdown("<h2 style='color:#999;margin:0;'>0</h2>", unsafe_allow_html=True)
        c7, c8, c9 = st.columns(3)
        nf_start = c7.date_input("预计开工时间", key="nf_start")
        nf_end = c8.date_input("预计完工时间（客户交期）", value=None, key="nf_end")
        # 评估完工时间：依据工艺路线+需生产数+预计开工时间自动计算
        eval_date, eval_days, eval_details = evaluate_finish(conn, nf_code.strip(), nf_need, nf_start)
        if eval_date:
            c9.date_input("评估完工时间（系统自动计算）",
                          value=datetime.strptime(eval_date, "%Y-%m-%d").date(), disabled=True)
        else:
            c9.text_input("评估完工时间", value="配好工艺并填写数量后自动计算", disabled=True)
        nf_rmk = st.text_input("备注", key="nf_rmk")
        if eval_details:
            lack = [d[0] for d in eval_details if d[4]]
            with st.expander(f"评估过程：合计约 {eval_days} 个班次天，预计 {eval_date} 完工"):
                for nm, sc, hour, cap, lk in eval_details:
                    line = f"- {nm}：小时产能 {hour}，班产 {cap}，占用约 {round(sc, 3)} 天"
                    if lk:
                        line += "　⚠ 未配置产能/班次时长，无法估算"
                    st.write(line)
            if lack:
                st.warning("以下工序未配置产能或班次时长，评估结果不准：" + "、".join(lack))
        if st.button("保存订单", type="primary", key="nf_save"):
            if not nf_code.strip() or nf_qty <= 0:
                st.error("品号、订单数量必填")
            else:
                fname, fspec = nf_name.strip(), nf_spec
                if not fname:
                    p = qone(conn, "SELECT * FROM products WHERE code=?", (nf_code.strip(),))
                    if p:
                        fname, fspec = p["name"] or "", p["spec"] or ""
                if not fname:
                    st.error("品名为空，请先在产品工艺配置中维护")
                else:
                    order_no = f"ORD{datetime.now().strftime('%m%d%H%M%S')}"
                    conn.execute("""INSERT INTO orders(order_no,item_code,item_name,spec,order_qty,stock_qty,need_qty,
                                    expect_start_time,expect_finish_time,eval_finish_time,status,remark,created_at)
                                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (order_no, nf_code.strip(), fname, fspec, nf_qty, nf_stock, nf_need,
                                  nf_start.isoformat(), nf_end.isoformat() if nf_end else None,
                                  eval_date, "新建订单", nf_rmk, now_str()))
                    conn.commit()
                    add_log(conn, "orders", order_no, "新增", f"{fname} {nf_qty}")
                    st.success(f"订单保存成功，评估完工时间：{eval_date or '无法评估（请先配置工艺）'}")
                    for _k in ["nf_code", "nf_name", "nf_spec", "nf_qty", "nf_stock", "nf_rmk", "nf_need"]:
                        st.session_state.pop(_k, None)
                    st.rerun()

    st.markdown("#### 订单清单")
    df = pd.read_sql("""SELECT id as 编号,order_no as 工单号,item_code as 品号,item_name as 品名,spec as 规格,
                        order_qty as 订单数量,stock_qty as 库存数量,need_qty as 需生产数,
                        expect_start_time as 预计开工时间,expect_finish_time as 预计完工时间,
                        eval_finish_time as 评估完工时间,
                        plan_start_date as 计划开始,plan_end_date as 计划结束,
                        warn_level as 预警,status as 状态,remark as 备注
                        FROM orders ORDER BY id DESC""", conn)
    df = fmt_int_cols(df, ["订单数量", "库存数量", "需生产数"])
    ed = editable_table(df, "order_tbl")
    c1, c2, c3 = st.columns(3)
    if c1.button("保存修改", key="order_save", type="primary"):
        with st.spinner("正在保存..."):
            n = 0
            for _, r in ed.iterrows():
                rid = r.get("编号")
                if pd.notna(rid) and si(rid) > 0:
                    oqty = sf(r.get("订单数量", 0))
                    ostock = sf(r.get("库存数量", 0))
                    oneed = max(0, oqty - ostock)
                    conn.execute("""UPDATE orders SET item_code=?,item_name=?,spec=?,order_qty=?,stock_qty=?,need_qty=?,
                                    expect_start_time=?,expect_finish_time=?,eval_finish_time=?,status=?,remark=? WHERE id=?""",
                                 (str(r.get("品号","")or""), str(r.get("品名","")or""), str(r.get("规格","")or""),
                                  oqty, ostock, oneed, str(r.get("预计开工时间","")or None),
                                  str(r.get("预计完工时间","")or None), str(r.get("评估完工时间","")or None),
                                  str(r.get("状态","")or"新建订单"),
                                  str(r.get("备注","")or""), si(rid)))
                    n += 1
            conn.commit()
        st.success(f"保存 {n} 条")
        st.rerun()
    if c2.button("删除选中", key="order_del"):
        ids = selected_ids(ed)
        if ids:
            with st.spinner("正在删除..."):
                conn.execute(f"DELETE FROM orders WHERE id IN ({','.join(['?']*len(ids))})", ids)
                conn.execute(f"DELETE FROM schedules WHERE order_id IN ({','.join(['?']*len(ids))})", ids)
                conn.execute(f"DELETE FROM work_order_process WHERE order_id IN ({','.join(['?']*len(ids))})", ids)
                renumber_table(conn, "orders")
                renumber_table(conn, "schedules")
                renumber_table(conn, "work_order_process")
                conn.commit()
            st.rerun()
    if c3.button("排产选中订单", key="order_sched"):
        ids = selected_ids(ed)
        if ids:
            with st.spinner("正在排产..."):
                n = run_schedule(conn, ids)
            st.success(f"排产完成，处理 {n} 个订单")
            st.rerun()
    export_excel(df.drop(columns=["选中"], errors="ignore"), "订单清单.xlsx")
    with st.expander("📥 导入订单Excel（先用上方“导出Excel”做模板，外部编辑后导回）"):
        st.caption("必填：品号、订单数量；品名/规格留空会自动按品号带出，需生产数=订单数量-库存数量自动计算。")
        up_ord = st.file_uploader("选择订单 Excel", type=["xlsx"], key="imp_ord_up")
        ord_mode = st.radio("导入方式", ["追加（保留原有订单）", "覆盖（先清空订单/排产/工序工单）"],
                            horizontal=True, key="imp_ord_mode")
        if up_ord is not None and st.button("开始导入订单", key="imp_ord_btn", type="primary"):
            try:
                with st.spinner("正在导入订单..."):
                    m = "覆盖" if ord_mode.startswith("覆盖") else "追加"
                    n = import_orders_excel(conn, up_ord.getvalue(), m)
                st.success(f"订单导入完成，共 {n} 张")
                time.sleep(0.6)
                st.rerun()
            except Exception as e:
                st.error(f"导入失败（已回滚，原数据未改）：{e}")

# ============================================================
# 评估完工时间（下单前按工艺路线+数量快速预估，不落库、不生成工单）
# ============================================================
def evaluate_finish(conn, item_code, need_qty, start_date):
    """与排产引擎同一套公式，从预计开工日逐工序累加推算评估完工日。
    返回 (评估完工日str, 总天数float, 明细list[(工序名,周期天,小时产能,班产,是否缺产能)])"""
    if not item_code or sf(need_qty, 0) <= 0:
        return None, 0.0, []
    ops = conn.execute("SELECT * FROM product_process WHERE item_code=? AND enabled=1 ORDER BY process_seq",
                       (item_code,)).fetchall()
    if not ops:
        return None, 0.0, []
    if isinstance(start_date, str):
        try:
            cur = datetime.strptime(start_date[:10], "%Y-%m-%d")
        except Exception:
            cur = datetime.strptime(today_str(), "%Y-%m-%d")
    else:
        cur = datetime(start_date.year, start_date.month, start_date.day)
    total, details = 0.0, []
    for op in ops:
        is_inj = bool(op["is_injection"])
        lack = False
        if is_inj:
            cyc, cav = sf(op["cycle_time"], 0), si(op["cavity_num"], 0)
            hour = calc_injection_hour_capacity(cyc, cav) if cyc > 0 and cav > 0 else sf(op["hour_capacity"], 0)
            mold = sf(op["mold_ratio"], 0)
        else:
            hour, mold = sf(op["hour_capacity"], 0), 0
        shift_h = sf(op["shift_hours"], 0) or 8
        shift_cap = calc_shift_capacity(hour, shift_h)
        if shift_cap <= 0:
            lack = True
            sc = 0.0
        else:
            base = calc_base_cycle(need_qty, shift_cap)
            sc = calc_schedule_cycle(base, is_inj, mold, sf(op["prep_ratio"], 0))
        total += sc
        details.append((op["process_name"], sc, hour, shift_cap, lack))
        cur += timedelta(days=sc)
    return cur.strftime("%Y-%m-%d"), round(total, 4), details

# ============================================================
# 排产引擎（以预计开工时间为起点，逐工序独立排产；班次时长取自工序配置）
# ============================================================
def run_schedule(conn, order_ids=None):
    warn_h = sf(get_setting(conn, "warn_threshold_hours", "24"), 24)

    if order_ids:
        ph = ",".join(["?"] * len(order_ids))
        orders = conn.execute(f"SELECT * FROM orders WHERE id IN ({ph}) AND status IN ('新建订单','已排产') ORDER BY id",
                              order_ids).fetchall()
    else:
        orders = conn.execute("SELECT * FROM orders WHERE status IN ('新建订单','已排产') ORDER BY id").fetchall()

    if not orders:
        return 0

    count = 0
    for o in orders:
        need = float(o["need_qty"] or 0)
        if need <= 0:
            need = max(0, float(o["order_qty"]) - get_fg_stock(conn, o["item_code"]))
        if need <= 0:
            continue

        ops = conn.execute("SELECT * FROM product_process WHERE item_code=? AND enabled=1 ORDER BY process_seq",
                           (o["item_code"],)).fetchall()
        if not ops:
            continue

        # 起点：订单预计开工时间
        start_str = o["expect_start_time"] or today_str()
        try:
            cur = datetime.strptime(start_str[:10], "%Y-%m-%d")
        except Exception:
            cur = datetime.strptime(today_str(), "%Y-%m-%d")

        # 清除旧排产
        conn.execute("DELETE FROM schedules WHERE order_id=?", (o["id"],))
        conn.execute("DELETE FROM work_order_process WHERE order_id=?", (o["id"],))

        total_cycle = 0.0
        first_start = cur
        last_end = cur

        for op in ops:
            is_inj = bool(op["is_injection"])
            # 注塑小时产能：有成型周期和模穴数就强制用公式
            if is_inj:
                cyc = sf(op["cycle_time"], 0)
                cav = si(op["cavity_num"], 0)
                if cyc > 0 and cav > 0:
                    hour = calc_injection_hour_capacity(cyc, cav)
                else:
                    hour = sf(op["hour_capacity"], 0)
            else:
                hour = sf(op["hour_capacity"], 0)
            # 班次时长：直接取工序配置手工填写的值
            shift_h = sf(op["shift_hours"], 0)
            if shift_h <= 0:
                shift_h = 8.0
            shift_cap = calc_shift_capacity(hour, shift_h)

            # 基础周期 = 数量 ÷ 班产
            base = calc_base_cycle(need, shift_cap)
            if base <= 0:
                base = 0.1
            # 排产周期：注塑叠加换模+备料，通用仅备料
            mold = sf(op["mold_ratio"], 0) if is_inj else 0
            prep = sf(op["prep_ratio"], 0)
            sched_cyc = calc_schedule_cycle(base, is_inj, mold, prep)
            total_cycle += sched_cyc

            op_start = cur
            op_end = op_start + timedelta(days=sched_cyc)
            os_str = op_start.strftime("%Y-%m-%d")
            oe_str = op_end.strftime("%Y-%m-%d")
            mach = op["machine_type"] or ""

            conn.execute("""INSERT INTO work_order_process(main_order_no,order_id,item_code,process_seq,process_code,
                            process_name,workshop_name,machine_type,plan_qty,sub_status,
                            plan_start,plan_end,hour_capacity,shift_hours,shift_capacity,
                            schedule_cycle,cavity_num,cycle_time,mold_ratio,prep_ratio,is_injection,remark)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (o["order_no"], o["id"], o["item_code"], si(op["process_seq"]),
                          op["process_code"] or "", op["process_name"], op["workshop_name"] or "",
                          mach, need, "生产中", os_str, oe_str, hour, shift_h, shift_cap, sched_cyc,
                          op["cavity_num"] if is_inj else None, op["cycle_time"] if is_inj else None,
                          mold if is_inj else None, prep, 1 if is_inj else 0, op["remark"] or ""))
            wo_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            conn.execute("""INSERT INTO schedules(order_id,wo_id,item_code,item_name,process_name,machine_code,
                            plan_start,plan_end,schedule_cycle,hour_capacity,shift_hours,shift_capacity,
                            cavity_num,cycle_time,mold_ratio,prep_ratio,operator,remark,scheduled)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                         (o["id"], wo_id, o["item_code"], o["item_name"], op["process_name"], mach,
                          os_str, oe_str, sched_cyc, hour, shift_h, shift_cap,
                          op["cavity_num"] if is_inj else None, op["cycle_time"] if is_inj else None,
                          mold if is_inj else None, prep, "", op["remark"] or ""))

            cur = op_end
            last_end = op_end

        # 交期预警
        warn = ""
        if o["expect_finish_time"]:
            try:
                d_exp = datetime.strptime(o["expect_finish_time"][:10], "%Y-%m-%d")
                diff_h = (d_exp - last_end).total_seconds() / 3600.0
                if diff_h < 0:
                    warn = "红色预警"
                elif diff_h <= warn_h:
                    warn = "黄色预警"
                else:
                    warn = "正常"
            except Exception:
                pass

        conn.execute("""UPDATE orders SET plan_start_date=?,plan_end_date=?,total_cycle=?,
                        status='已排产',warn_level=? WHERE id=?""",
                     (first_start.strftime("%Y-%m-%d"), last_end.strftime("%Y-%m-%d"), total_cycle, warn, o["id"]))
        count += 1

    conn.commit()
    return count

# ============================================================
# 页面：APS排产（无班次列）
# ============================================================
def page_schedule(conn):
    st.header("APS排产")
    procs = get_process_list(conn)
    pnames = ["全部"] + [n for c, n, inj, ws in procs]
    ft = st.selectbox("工序筛选", pnames, key="sched_ft")

    c1, c2 = st.columns(2)
    if c1.button("全量自动排产", type="primary", key="auto_sched"):
        with st.spinner("正在排产..."):
            n = run_schedule(conn)
        st.success(f"排产完成，处理 {n} 个订单")
        st.rerun()
    if c2.button("取消全部排产", key="cancel_sched"):
        with st.spinner("正在取消..."):
            conn.execute("DELETE FROM schedules WHERE scheduled=1")
            conn.execute("DELETE FROM work_order_process")
            renumber_table(conn, "schedules")
            renumber_table(conn, "work_order_process")
            conn.execute("UPDATE orders SET status='新建订单',plan_start_date=NULL,plan_end_date=NULL,total_cycle=0,warn_level='' WHERE status='已排产'")
            conn.commit()
        st.rerun()

    sql = """SELECT s.id as 编号,o.order_no as 工单号,o.item_code as 品号,o.item_name as 品名,
             o.need_qty as 需生产数,
             s.process_name as 工序,s.machine_code as 生产机台,
             o.expect_start_time as 预计开工时间,o.expect_finish_time as 预计完工时间,
             s.schedule_cycle as 排产周期,s.plan_start as 计划开始,s.plan_end as 计划结束,
             s.shift_capacity as 班产,s.shift_hours as 班次时长,s.hour_capacity as 小时产能,
             COALESCE(s.mold_ratio,0) as 换模比例,COALESCE(s.prep_ratio,0) as 备料比例,
             o.warn_level as 预警,s.operator as 操作人员,s.remark as 备注
             FROM schedules s LEFT JOIN orders o ON o.id=s.order_id WHERE s.scheduled=1"""
    params = ()
    if ft != "全部":
        sql += " AND s.process_name=?"
        params = (ft,)
    sql += " ORDER BY s.id"
    df = pd.read_sql(sql, conn, params=params)
    df = fmt_int_cols(df, ["需生产数"])
    if df.empty:
        st.info("暂无排产数据")
    else:
        def wc(row):
            w = str(row.get("预警", "") or "")
            if w == "红色预警":
                return "background-color: #FFB6C1"
            if w == "黄色预警":
                return "background-color: #FFFFE0"
            if w == "正常":
                return "background-color: #90EE90"
            return ""
        styled = df.style.apply(lambda r: pd.Series([wc(r)] * len(r), index=r.index), axis=1)
        st.dataframe(styled, width="stretch", hide_index=True)
        st.caption("🟢 正常  🟡 黄色预警（临近交期）  🔴 红色预警（已延期）")

    ed = editable_table(df, "sched_tbl")
    c1, c2 = st.columns(2)
    if c1.button("保存修改", key="sched_save", type="primary"):
        with st.spinner("正在保存..."):
            n = 0
            for _, r in ed.iterrows():
                rid = r.get("编号")
                if pd.notna(rid) and si(rid) > 0:
                    conn.execute("UPDATE schedules SET machine_code=?,operator=?,remark=? WHERE id=?",
                                 (str(r.get("生产机台","")or""), str(r.get("操作人员","")or""),
                                  str(r.get("备注","")or""), si(rid)))
                    n += 1
            conn.commit()
        st.success(f"保存 {n} 条")
        st.rerun()
    if c2.button("删除选中", key="sched_del"):
        ids = selected_ids(ed)
        if ids:
            with st.spinner("正在删除..."):
                conn.execute(f"DELETE FROM schedules WHERE id IN ({','.join(['?']*len(ids))})", ids)
                renumber_table(conn, "schedules")
                conn.commit()
            st.rerun()
    export_excel(df.drop(columns=["选中"], errors="ignore"), f"排产列表_{ft}.xlsx")

# ============================================================
# 页面：车间报工
# ============================================================
def page_report(conn):
    st.header("车间生产报工")
    st.markdown("#### 工序子工单列表")

    procs = get_process_list(conn)
    pnames = ["全部"] + [n for c, n, inj, ws in procs]
    ft = st.selectbox("工序筛选", pnames, key="rep_ft")

    sql = """SELECT id as 编号,main_order_no as 工单号,item_code as 品号,process_seq as 工序序号,
             process_name as 工序,workshop_name as 车间,machine_type as 机台,
             plan_qty as 计划数量,report_qty as 累计报工,sub_status as 状态,
             plan_start as 计划开始,plan_end as 计划结束,schedule_cycle as 排产周期,remark as 备注
             FROM work_order_process WHERE 1=1"""
    params = ()
    if ft != "全部":
        sql += " AND process_name=?"
        params = (ft,)
    sql += " ORDER BY id"
    df = pd.read_sql(sql, conn, params=params)
    df = fmt_int_cols(df, ["计划数量", "累计报工"])
    if df.empty:
        st.info("暂无工序工单，请先在订单管理中排产")
    else:
        st.dataframe(df, width="stretch", hide_index=True)

    st.markdown("#### 生产报工（支持分批报工）")
    wos = conn.execute("""SELECT id,main_order_no,item_code,process_name,process_seq,plan_qty,report_qty
                          FROM work_order_process WHERE sub_status IN ('生产中','部分完工')
                          ORDER BY id""").fetchall()
    if wos:
        wopts = [f"{r['id']}|{r['main_order_no']}|{r['item_code']}|{r['process_name']}|计划{r['plan_qty']}|已报{r['report_qty']}"
                 for r in wos]
        sel = st.selectbox("选择工序工单", wopts, key="rep_wo")
        wo_id = si(sel.split("|")[0])
        wo = qone(conn, "SELECT * FROM work_order_process WHERE id=?", (wo_id,))
        if wo:
            remain = float(wo["plan_qty"] or 0) - float(wo["report_qty"] or 0)
            st.info(f"工单：{wo['main_order_no']} | 品号：{wo['item_code']} | 工序：{wo['process_name']} | 剩余可报：{remain}")
            emps = conn.execute("SELECT code,name FROM personnel WHERE IFNULL(status,'在职')!='离职' ORDER BY id").fetchall()
            emp_opts = [f"{r['code']} {r['name']}" for r in emps]
            with st.form("rep_f", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                rqty = c1.number_input("本次报工数量", min_value=0.0, max_value=max(0.0, remain), step=1.0)
                if emp_opts:
                    roper_sel = c2.selectbox("操作员", emp_opts)
                    roper = roper_sel.split(" ", 1)[1] if " " in roper_sel else roper_sel
                else:
                    roper = c2.text_input("操作员（员工档案为空，请先在基础资料添加）")
                rrmk = c3.text_input("备注")
                if st.form_submit_button("确认报工", type="primary"):
                    if rqty <= 0:
                        st.error("报工数量必须大于0")
                    else:
                        consume_details = []
                        with st.spinner("正在提交报工..."):
                            new_total = float(wo["report_qty"] or 0) + rqty
                            if new_total >= float(wo["plan_qty"] or 0):
                                new_status = "已完工"
                                actual_end = now_str()
                            else:
                                new_status = "部分完工"
                                actual_end = wo["actual_end"]
                            actual_start = wo["actual_start"] or now_str()
                            conn.execute("""UPDATE work_order_process SET report_qty=?,sub_status=?,
                                            actual_start=?,actual_end=? WHERE id=?""",
                                         (new_total, new_status, actual_start, actual_end, wo_id))

                            nxt = conn.execute("""SELECT COUNT(*) as cnt FROM work_order_process
                                                  WHERE main_order_no=? AND process_seq > ?""",
                                               (wo["main_order_no"], wo["process_seq"])).fetchone()
                            if nxt and nxt["cnt"] == 0:
                                p = qone(conn, "SELECT name,spec FROM products WHERE code=?", (wo["item_code"],))
                                pname = p["name"] if p else ""
                                pspec = p["spec"] if p else ""
                                cur = get_fg_stock(conn, wo["item_code"])
                                new_bal = cur + rqty
                                conn.execute("INSERT INTO fg_inventory(product_code,product_name,spec,stock_qty) VALUES(?,?,?,?) "
                                             "ON CONFLICT(product_code) DO UPDATE SET stock_qty=excluded.stock_qty",
                                             (wo["item_code"], pname, pspec, new_bal))
                                conn.execute("INSERT INTO fg_io(product_code,product_name,spec,out_qty,in_qty,balance,remark,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                             (wo["item_code"], pname, pspec, 0, rqty, new_bal,
                                              f"工单{wo['main_order_no']}{wo['process_name']}完工", now_str()))
                            else:
                                bill = f"SEMI{datetime.now().strftime('%m%d%H%M%S')}"
                                conn.execute("""INSERT INTO semi_product_io(bill_no,bill_type,item_code,process_code,process_name,
                                                source_order_no,qty,operator,operate_time,remark)
                                                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                             (bill, "半成品入库", wo["item_code"], wo["process_code"] or "",
                                              wo["process_name"], wo["main_order_no"], rqty, roper, now_str(), rrmk))
                                stk = qone(conn, "SELECT * FROM semi_product_stock WHERE item_code=? AND process_code=?",
                                           (wo["item_code"], wo["process_code"] or ""))
                                if stk:
                                    new_in = float(stk["total_in"] or 0) + rqty
                                    new_cur = float(stk["current_qty"] or 0) + rqty
                                    conn.execute("UPDATE semi_product_stock SET total_in=?,current_qty=?,update_time=? WHERE id=?",
                                                 (new_in, new_cur, now_str(), stk["id"]))
                                else:
                                    conn.execute("""INSERT INTO semi_product_stock(item_code,process_code,process_name,
                                                    total_in,current_qty,update_time) VALUES(?,?,?,?,?,?)""",
                                                 (wo["item_code"], wo["process_code"] or "", wo["process_name"],
                                                  rqty, rqty, now_str()))
                            consume_details = consume_materials(conn, wo["item_code"], wo["process_code"],
                                                                wo["process_name"], wo["main_order_no"], rqty)
                            conn.commit()
                            add_log(conn, "work_order_process", wo_id, "报工", f"{wo['process_name']} +{rqty}")
                        st.success(f"报工成功，累计报工：{new_total}")
                        if consume_details:
                            st.markdown("**本次自动领料扣减（按BOM）：**")
                            for d in consume_details:
                                st.write(f"- {d['code']} {d['name']}：{rqty}件 × 单件{d['usage']}{d['unit']} "
                                         f"= 扣减 {d['consume']}{d['unit']}，库存 {d['before']} → {d['after']}{d['unit']}")
                                if d["after"] < 0:
                                    st.warning(f"{d['name']} 库存已不足（结余 {d['after']}{d['unit']}），请及时补料")
                            time.sleep(1.2)
                        st.rerun()
    else:
        st.info("暂无待报工的工序工单")

# ============================================================
# 页面：半成品库存
# ============================================================
def page_semi(conn):
    st.header("半成品库存")
    t1, t2 = st.tabs(["库存台账", "出入库记录"])
    with t1:
        st.markdown("#### 半成品库存台账（品号+工序节点）")
        df = pd.read_sql("""SELECT id as 编号,item_code as 品号,process_code as 工序编码,process_name as 工序名称,
                            warehouse as 仓库,beginning_qty as 期初库存,total_in as 累计入库,
                            total_out as 累计出库,current_qty as 当前库存,
                            moving_cost as 移动加权成本,lock_qty as 锁定库存,update_time as 更新时间
                            FROM semi_product_stock ORDER BY id""", conn)
        if df.empty:
            st.info("暂无半成品库存数据")
        else:
            st.dataframe(df, width="stretch", hide_index=True)
        export_excel(df, "半成品库存台账.xlsx")
    with t2:
        st.markdown("#### 半成品出入库记录")
        df = pd.read_sql("""SELECT id as 编号,bill_no as 单据编号,bill_type as 单据类型,item_code as 品号,
                            process_name as 工序,source_order_no as 来源工单,qty as 数量,
                            warehouse as 仓库,operator as 操作人,operate_time as 操作时间,
                            audit_status as 审核状态,remark as 备注
                            FROM semi_product_io ORDER BY id DESC""", conn)
        if df.empty:
            st.info("暂无出入库记录")
        else:
            st.dataframe(df, width="stretch", hide_index=True)
        export_excel(df, "半成品出入库记录.xlsx")

# ============================================================
# 页面：物料出入库
# ============================================================
def page_material_io(conn):
    st.header("物料出入库")

    # 物料当前库存（剩余数），低于安全库存红色高亮
    st.markdown("#### 物料当前库存（剩余数）")
    stock_df = pd.read_sql("""SELECT code as 物料编号,name as 名称,mtype as 类型,IFNULL(unit,'') as 单位,
                              stock_qty as 当前库存,safe_stock as 安全库存
                              FROM materials ORDER BY code""", conn)
    if stock_df.empty:
        st.info("暂无物料档案，请先到基础资料→物料配置添加")
    else:
        stock_df = fmt_stock_by_unit(stock_df)
        def _low_light(row):
            try:
                if float(row["当前库存"] or 0) <= float(row["安全库存"] or 0):
                    return ["background-color:#FFD6D6"] * len(row)
            except Exception:
                pass
            return [""] * len(row)
        st.dataframe(stock_df.style.apply(_low_light, axis=1), width="stretch", hide_index=True)
        low = stock_df[pd.to_numeric(stock_df["当前库存"], errors="coerce").fillna(0)
                       <= pd.to_numeric(stock_df["安全库存"], errors="coerce").fillna(0)]
        if not low.empty:
            st.caption(f"⚠ 共 {len(low)} 种物料已达/低于安全库存，请及时补料："
                       + "、".join(low["名称"].astype(str).tolist()))
    st.markdown("---")

    with st.form("mio_f", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        mt = c1.selectbox("类型", ["入库", "出库"])
        mc = c2.text_input("物料编号")
        mq = c3.number_input("数量", min_value=0.0, step=1.0)
        mr = c4.text_input("备注")
        if st.form_submit_button("确认"):
            if mc and mq > 0:
                mat = qone(conn, "SELECT * FROM materials WHERE code=?", (mc,))
                if mat:
                    new = mat["stock_qty"] + mq if mt == "入库" else mat["stock_qty"] - mq
                    conn.execute("UPDATE materials SET stock_qty=? WHERE code=?", (new, mc))
                    conn.execute("INSERT INTO material_io(material_code,material_name,io_type,qty,remark,created_at) VALUES(?,?,?,?,?,?)",
                                 (mc, mat["name"], mt, mq, mr, now_str()))
                    conn.commit()
                    st.success(f"{mt}成功，当前库存：{new}")
                else:
                    st.error("物料编号不存在")
    df = pd.read_sql("SELECT id as 编号,material_code as 物料编号,material_name as 名称,io_type as 类型,qty as 数量,remark as 备注,created_at as 时间 FROM material_io ORDER BY id DESC", conn)
    ed = editable_table(df, "mio_tbl")
    c1, c2 = st.columns(2)
    if c1.button("保存修改", key="mio_save", type="primary"):
        with st.spinner("正在保存..."):
            n = 0
            for _, r in ed.iterrows():
                rid = r.get("编号")
                if pd.notna(rid) and si(rid) > 0:
                    conn.execute("UPDATE material_io SET remark=? WHERE id=?", (str(r.get("备注","")or""), si(rid)))
                    n += 1
            conn.commit()
        st.success(f"保存 {n} 条")
        st.rerun()
    if c2.button("删除选中", key="mio_del"):
        ids = selected_ids(ed)
        if ids:
            with st.spinner("正在删除..."):
                conn.execute(f"DELETE FROM material_io WHERE id IN ({','.join(['?']*len(ids))})", ids)
                renumber_table(conn, "material_io")
                conn.commit()
            st.rerun()
    export_excel(df.drop(columns=["选中"], errors="ignore"), "物料出入库.xlsx")

# ============================================================
# 页面：成品出入库
# ============================================================
def page_fg_io(conn):
    st.header("成品出入库")
    with st.form("fg_f", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        ft = c1.selectbox("类型", ["入库", "出库"])
        fc = c2.text_input("产品品号")
        fq = c3.number_input("数量", min_value=0.0, step=1.0)
        fr = c4.text_input("备注")
        if st.form_submit_button("确认"):
            if fc and fq > 0:
                p = qone(conn, "SELECT * FROM products WHERE code=?", (fc,))
                pname = p["name"] if p else ""
                pspec = p["spec"] if p else ""
                cur = get_fg_stock(conn, fc)
                new = cur + fq if ft == "入库" else cur - fq
                conn.execute("INSERT INTO fg_inventory(product_code,product_name,spec,stock_qty) VALUES(?,?,?,?) "
                             "ON CONFLICT(product_code) DO UPDATE SET stock_qty=excluded.stock_qty",
                             (fc, pname, pspec, new))
                conn.execute("INSERT INTO fg_io(product_code,product_name,spec,out_qty,in_qty,balance,remark,created_at) VALUES(?,?,?,?,?,?,?,?)",
                             (fc, pname, pspec, fq if ft == "出库" else 0, fq if ft == "入库" else 0, new, fr, now_str()))
                conn.commit()
                st.success(f"{ft}成功，当前库存：{new}")
    df = pd.read_sql("""SELECT id as 编号,product_code as 品号,product_name as 品名,spec as 规格,
                        out_qty as 出库数,in_qty as 入库数,balance as 库存数,remark as 备注,created_at as 时间
                        FROM fg_io ORDER BY id DESC""", conn)
    ed = editable_table(df, "fg_tbl")
    c1, c2 = st.columns(2)
    if c1.button("删除选中", key="fg_del"):
        ids = selected_ids(ed)
        if ids:
            with st.spinner("正在删除..."):
                conn.execute(f"DELETE FROM fg_io WHERE id IN ({','.join(['?']*len(ids))})", ids)
                renumber_table(conn, "fg_io")
                conn.commit()
            st.rerun()
    export_excel(df.drop(columns=["选中"], errors="ignore"), "成品出入库.xlsx")

# ============================================================
# 页面：系统设置（无全局班次设置）
# ============================================================
def page_settings(conn):
    st.header("系统设置")
    t1, t2, t3, t4 = st.tabs(["用户管理", "预警参数", "操作日志", "数据备份恢复"])

    with t1:
        st.markdown("#### 用户管理")
        with st.form("user_f", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns(4)
            un = c1.text_input("用户名")
            up = c2.text_input("密码", type="password")
            ur = c3.selectbox("角色", ["总调度", "车间计划员", "车间操作员", "仓库管理员", "查看员"])
            uw = c4.text_input("所属车间")
            if st.form_submit_button("保存用户"):
                if un and up:
                    conn.execute("INSERT INTO users(username,password,role,workshop,created_at) VALUES(?,?,?,?,?) "
                                 "ON CONFLICT(username) DO UPDATE SET password=excluded.password,role=excluded.role,workshop=excluded.workshop",
                                 (un, up, ur, uw, now_str()))
                    conn.commit()
                    st.success("保存成功")
        df = pd.read_sql("SELECT id as 编号,username as 用户名,role as 角色,workshop as 所属车间,created_at as 创建时间 FROM users ORDER BY id", conn)
        ed = editable_table(df, "user_tbl")
        if st.button("删除选中用户", key="user_del"):
            ids = selected_ids(ed)
            if ids:
                with st.spinner("正在删除..."):
                    conn.execute(f"DELETE FROM users WHERE id IN ({','.join(['?']*len(ids))})", ids)
                    renumber_table(conn, "users")
                    conn.commit()
                st.rerun()

    with t2:
        st.markdown("#### 交期预警参数")
        st.caption("班次时长在每道工序配置中手工填写，此处不再设置全局班次")
        warn = st.number_input("交期预警阈值(小时)", min_value=0.0, max_value=720.0,
                               value=sf(get_setting(conn, "warn_threshold_hours", "24"), 24))
        if st.button("保存"):
            set_setting(conn, "warn_threshold_hours", str(warn))
            st.success("保存成功")

    with t3:
        st.markdown("#### 操作日志")
        df = pd.read_sql("SELECT id as 编号,entity_type as 类型,entity_id as 对象ID,action as 操作,detail as 详情,created_at as 时间 FROM operation_logs ORDER BY id DESC LIMIT 500", conn)
        st.dataframe(df, width="stretch", hide_index=True)
        export_excel(df, "操作日志.xlsx")

    with t4:
        st.markdown("#### 数据备份与恢复")
        st.info("建议：大批量录入后、或程序升级/更换电脑前，先下载备份保存到电脑；需要时再导回，数据不会丢。")

        st.markdown("##### 方式一：整库备份 / 恢复（推荐，完整无损）")
        st.caption("一个文件包含基础资料、订单、排产、库存、报工、日志等全部数据，原样还原。")
        cc1, cc2 = st.columns(2)
        with cc1:
            st.markdown("**① 下载备份**")
            st.download_button("⬇ 下载整库备份(.db)", backup_db_bytes(),
                               file_name=f"AIPA整库备份_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
                               key="dl_db")
        with cc2:
            st.markdown("**② 上传恢复**")
            up_db = st.file_uploader("选择 .db 备份文件", type=["db"], key="restore_db")
            if up_db is not None and st.button("确认整库恢复（将覆盖当前全部数据）", key="do_restore_db", type="primary"):
                try:
                    with st.spinner("正在恢复整库..."):
                        restore_db_file(conn, up_db.getvalue())
                        time.sleep(0.5)
                    st.success("整库恢复成功，正在刷新...")
                    time.sleep(0.8)
                    st.rerun()
                except Exception as e:
                    st.error(f"恢复失败：{e}")

        st.markdown("---")
        st.markdown("##### 方式二：基础资料 Excel 备份 / 导入")
        st.caption("仅含人员、物料、机台、机台类型、工序模板、产品、工序配置、BOM、用户；表头为英文字段，请勿改动表头行，可编辑数据行后导回。")
        cc3, cc4 = st.columns(2)
        with cc3:
            st.markdown("**① 下载基础资料模板/备份**")
            st.download_button("⬇ 下载基础资料(Excel)", backup_master_excel(conn),
                               file_name=f"AIPA基础资料_{datetime.now().strftime('%Y%m%d')}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               key="dl_master")
        with cc4:
            st.markdown("**② 上传导入**")
            up_x = st.file_uploader("选择基础资料 Excel", type=["xlsx"], key="restore_xlsx")
            imode = st.radio("导入方式", ["覆盖（先清空再导入）", "追加（保留原有数据）"],
                             horizontal=True, key="import_mode")
            if up_x is not None and st.button("确认导入", key="do_import_x", type="primary"):
                try:
                    with st.spinner("正在导入..."):
                        mmode = "覆盖" if imode.startswith("覆盖") else "追加"
                        rep = restore_master_excel(conn, up_x.getvalue(), mmode)
                    st.success("导入完成：" + ("；".join(rep) if rep else "文件中没有可导入的表"))
                    time.sleep(0.8)
                    st.rerun()
                except Exception as e:
                    st.error(f"导入失败（数据已回滚，未改动原数据）：{e}")

# ============================================================
# 页面：生产看板
# ============================================================
def page_dashboard(conn):
    st.header("生产看板")
    st.caption(f"数据更新时间：{now_str()}")

    total_orders = qone(conn, "SELECT COUNT(*) as c FROM orders")["c"]
    pending = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='新建订单'")["c"]
    scheduled = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='已排产'")["c"]
    in_prod = qone(conn, "SELECT COUNT(*) as c FROM work_order_process WHERE sub_status IN ('生产中','部分完工')")["c"]
    finished = qone(conn, "SELECT COUNT(*) as c FROM work_order_process WHERE sub_status='已完工'")["c"]
    warn_red = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE warn_level='红色预警'")["c"]
    warn_yellow = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE warn_level='黄色预警'")["c"]
    today_report = qone(conn, "SELECT COALESCE(SUM(qty),0) as s FROM semi_product_io WHERE date(operate_time)=date('now')")["s"]

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("总工单数", total_orders)
    c2.metric("待排产", pending, delta=f"已排产 {scheduled}")
    c3.metric("生产中工序", in_prod)
    c4.metric("已完工工序", finished)
    c5.metric("交期预警", warn_red + warn_yellow, delta=f"红{warn_red}/黄{warn_yellow}", delta_color="inverse")
    c6.metric("今日报工量", today_report)

    st.markdown("---")
    col_left, col_right = st.columns(2)
    with col_left:
        st.markdown("#### 订单状态分布")
        status_df = pd.read_sql("SELECT status as 状态, COUNT(*) as 数量 FROM orders GROUP BY status ORDER BY 数量 DESC", conn)
        if status_df.empty:
            st.info("暂无订单数据")
        else:
            st.bar_chart(status_df.set_index("状态"), width="stretch", color="#4A90D9")
    with col_right:
        st.markdown("#### 各工序工单数量")
        proc_df = pd.read_sql("""SELECT process_name as 工序, COUNT(*) as 工单数
                                 FROM work_order_process GROUP BY process_name ORDER BY 工单数 DESC""", conn)
        if proc_df.empty:
            st.info("暂无工序工单")
        else:
            st.bar_chart(proc_df.set_index("工序")[["工单数"]], width="stretch", color="#52C41A")

    st.markdown("---")
    st.markdown("#### 交期预警订单")
    warn_df = pd.read_sql("""SELECT order_no as 工单号,item_code as 品号,item_name as 品名,
                             need_qty as 需生产数,expect_finish_time as 预计完工,
                             plan_end_date as 计划结束,warn_level as 预警,status as 状态
                             FROM orders WHERE warn_level IN ('红色预警','黄色预警')
                             ORDER BY CASE warn_level WHEN '红色预警' THEN 1 WHEN '黄色预警' THEN 2 END, id""", conn)
    if warn_df.empty:
        st.success("当前无交期预警订单")
    else:
        def warn_color(row):
            if row.get("预警") == "红色预警":
                return ["background-color: #FFB6C1"] * len(row)
            if row.get("预警") == "黄色预警":
                return ["background-color: #FFF3CD"] * len(row)
            return [""] * len(row)
        st.dataframe(warn_df.style.apply(warn_color, axis=1), width="stretch", hide_index=True)

    st.markdown("---")
    st.markdown("#### 近期排产计划")
    sched_df = pd.read_sql("""SELECT o.order_no as 工单号,o.item_code as 品号,o.item_name as 品名,
                              s.process_name as 工序,s.machine_code as 机台,s.plan_start as 计划开始,
                              s.plan_end as 计划结束,s.schedule_cycle as "周期(班)",o.warn_level as 预警
                              FROM schedules s LEFT JOIN orders o ON o.id=s.order_id
                              WHERE s.scheduled=1 ORDER BY s.plan_start LIMIT 20""", conn)
    if sched_df.empty:
        st.info("暂无排产计划，请先在订单管理中排产")
    else:
        st.dataframe(sched_df, width="stretch", hide_index=True)

    st.markdown("---")
    # 物料库存概览（剩余数），低于安全库存红色高亮
    st.markdown("#### 物料库存概览（剩余数）")
    mat_df = pd.read_sql("""SELECT code as 物料编号,name as 名称,mtype as 类型,IFNULL(unit,'') as 单位,
                            stock_qty as 当前库存,safe_stock as 安全库存
                            FROM materials ORDER BY code""", conn)
    if mat_df.empty:
        st.info("暂无物料库存")
    else:
        mk1, mk2 = st.columns(2)
        mk1.metric("物料种类", len(mat_df))
        _cur = pd.to_numeric(mat_df["当前库存"], errors="coerce").fillna(0)
        _safe = pd.to_numeric(mat_df["安全库存"], errors="coerce").fillna(0)
        low_n = int((_cur <= _safe).sum())
        mk2.metric("低于安全库存", low_n, delta="需补料" if low_n else "充足",
                   delta_color="inverse" if low_n else "normal")
        def _mat_light(row):
            try:
                if float(row["当前库存"] or 0) <= float(row["安全库存"] or 0):
                    return ["background-color:#FFD6D6"] * len(row)
            except Exception:
                pass
            return [""] * len(row)
        show_mat = fmt_stock_by_unit(mat_df)
        st.dataframe(show_mat.style.apply(_mat_light, axis=1), width="stretch", hide_index=True)

    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### 半成品库存概览")
        semi_df = pd.read_sql("""SELECT process_name as 工序, SUM(current_qty) as 当前库存
                                 FROM semi_product_stock GROUP BY process_name ORDER BY 当前库存 DESC""", conn)
        if semi_df.empty:
            st.info("暂无半成品库存")
        else:
            st.dataframe(semi_df, width="stretch", hide_index=True)
    with c2:
        st.markdown("#### 成品库存概览")
        fg_df = pd.read_sql("""SELECT product_code as 品号,product_name as 品名,stock_qty as 库存数
                               FROM fg_inventory ORDER BY stock_qty DESC LIMIT 10""", conn)
        if fg_df.empty:
            st.info("暂无成品库存")
        else:
            st.dataframe(fg_df, width="stretch", hide_index=True)

# ============================================================
# 登录界面
# ============================================================
def login_page(conn):
    st.markdown("<h1 style='text-align:center; margin-top:80px;'>宸赋智控系统</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align:center; color:#888; font-size:16px;'>多工序生产排控平台</p>", unsafe_allow_html=True)
    st.markdown("---")
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("用户名", key="login_user")
            password = st.text_input("密码", type="password", key="login_pwd")
            submitted = st.form_submit_button("登 录", width="stretch", type="primary")
            if submitted:
                if not username or not password:
                    st.error("请输入用户名和密码")
                else:
                    user = qone(conn, "SELECT * FROM users WHERE username=? AND password=?",
                                (username.strip(), password))
                    if user:
                        st.session_state.logged_in = True
                        st.session_state.user_id = user["id"]
                        st.session_state.username = user["username"]
                        st.session_state.user_role = user["role"] or "查看员"
                        st.session_state.user_workshop = user["workshop"] or ""
                        st.query_params["user"] = user["username"]
                        st.success("登录成功")
                        st.rerun()
                    else:
                        st.error("用户名或密码错误")
        st.caption("默认管理员：admin / 123456")

# ============================================================
# 主函数
# ============================================================
def main():
    st.set_page_config(page_title="宸赋智控系统", layout="wide")
    conn = get_conn()
    init_db(conn)

    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False

    # 刷新自动恢复登录
    if not st.session_state.logged_in:
        saved_user = st.query_params.get("user", "")
        if saved_user:
            user = qone(conn, "SELECT * FROM users WHERE username=?", (saved_user,))
            if user:
                st.session_state.logged_in = True
                st.session_state.user_id = user["id"]
                st.session_state.username = user["username"]
                st.session_state.user_role = user["role"] or "查看员"
                st.session_state.user_workshop = user["workshop"] or ""

    if not st.session_state.logged_in:
        login_page(conn)
        conn.close()
        return

    st.sidebar.title("宸赋智控系统")
    st.sidebar.caption(f"用户：{st.session_state.username}（{st.session_state.user_role}）")
    if st.session_state.user_workshop:
        st.sidebar.caption(f"车间：{st.session_state.user_workshop}")
    st.sidebar.markdown("---")

    role = st.session_state.user_role
    if role == "总调度":
        menus = ["生产看板", "基础资料", "产品工艺", "订单管理", "APS排产",
                 "车间报工", "半成品库存", "物料出入库", "成品出入库", "系统设置"]
    elif role == "车间计划员":
        menus = ["生产看板", "订单管理", "APS排产", "车间报工", "半成品库存"]
    elif role == "车间操作员":
        menus = ["生产看板", "车间报工", "半成品库存"]
    elif role == "仓库管理员":
        menus = ["生产看板", "半成品库存", "物料出入库", "成品出入库"]
    else:
        menus = ["生产看板", "订单管理", "APS排产", "车间报工", "半成品库存", "物料出入库", "成品出入库"]

    menu = st.sidebar.radio("功能菜单", menus)
    st.sidebar.markdown("---")
    if st.sidebar.button("退出登录", width="stretch"):
        for k in ["logged_in", "user_id", "username", "user_role", "user_workshop"]:
            st.session_state.pop(k, None)
        if "user" in st.query_params:
            del st.query_params["user"]
        st.rerun()

    if menu == "生产看板":
        page_dashboard(conn)
    elif menu == "基础资料":
        page_master(conn)
    elif menu == "产品工艺":
        page_product(conn)
    elif menu == "订单管理":
        page_orders(conn)
    elif menu == "APS排产":
        page_schedule(conn)
    elif menu == "车间报工":
        page_report(conn)
    elif menu == "半成品库存":
        page_semi(conn)
    elif menu == "物料出入库":
        page_material_io(conn)
    elif menu == "成品出入库":
        page_fg_io(conn)
    elif menu == "系统设置":
        page_settings(conn)

    conn.close()

if __name__ == "__main__":
    main()
