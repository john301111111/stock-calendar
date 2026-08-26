#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上市公司重要提醒 → Outlook 日历订阅（ICS）

数据源：东方财富「股市日历」（datacenter-web.eastmoney.com），免费免鉴权公开接口。
覆盖事件：预约披露日（年报/中报/季报）、业绩预告、业绩快报、业绩报表、股东大会、分红、限售解禁日。

用法：
    python listed_company_calendar.py              # 只生成 ICS 文件
    python listed_company_calendar.py --serve      # 生成 ICS 并启动本地订阅服务
                                                   # 默认 http://127.0.0.1:8765/reminders.ics
"""

import argparse
import hashlib
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
DEFAULT_OUTPUT = BASE_DIR / "listed_company_reminders.ics"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

CATEGORY_MAP = {
    "预约披露日": "财报披露",
    "业绩预告": "业绩",
    "业绩快报": "业绩",
    "业绩报表": "业绩",
    "股东大会": "股东大会",
    "分红": "分红",
    "限售解禁日": "解禁",
}

SERVE_STATE = {
    "bytes": b"",
    "lock": threading.Lock(),
    "ics_path": None,
    "last_refresh": 0.0,
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://data.eastmoney.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_secid(code: str) -> str:
    """把 6 位股票代码转成东财行情接口的 secid（沪市 1.，深市/北交所 0.）。"""
    code = code.strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def fetch_stock_name(code: str) -> str:
    try:
        url = (
            "https://push2.eastmoney.com/api/qt/stock/get"
            f"?secid={get_secid(code)}&fields=f57,f58"
        )
        data = http_get_json(url)
        name = (data.get("data") or {}).get("f58")
        return name if name else code
    except Exception:
        return code


def fetch_stock_events(code: str, event_types: list[str], days_ahead: int) -> tuple[list[dict], int]:
    """抓取单只股票在指定事件类型下的日历条目，返回 (事件列表, 失败的事件类型数)。"""
    today = date.today()
    max_date = today + timedelta(days=days_ahead)
    found = []
    failed_count = 0

    for event_type in event_types:
        page = 1
        while True:
            params = {
                "reportName": "RPT_STOCKCALENDAR",
                "columns": "ALL",
                "filter": f'(SECURITY_CODE="{code}")(EVENT_TYPE="{event_type}")',
                "pageNumber": str(page),
                "pageSize": "100",
                "sortTypes": "-1",
                "sortColumns": "NOTICE_DATE",
                "source": "WEB",
                "client": "WEB",
            }
            url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
            try:
                payload = http_get_json(url)
            except Exception as exc:
                log(f"  警告：{code} 获取「{event_type}」失败：{exc}")
                failed_count += 1
                break

            rows = (payload.get("result") or {}).get("data") or []
            if not rows:
                break

            for row in rows:
                try:
                    event_date = datetime.strptime(str(row.get("NOTICE_DATE") or "")[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if not (today <= event_date <= max_date):
                    continue
                content = str(row.get("LEVEL1_CONTENT") or "").strip()
                if not content:
                    continue
                found.append(
                    {
                        "stock_code": code,
                        "event_type": event_type,
                        "date": event_date,
                        "content": content,
                    }
                )

            total_pages = int((payload.get("result") or {}).get("pages") or 1)
            if page >= total_pages or len(rows) < 100:
                break
            page += 1

    # 去重（同一股票同一事件同一天可能重复返回）
    seen, unique = set(), []
    for ev in found:
        key = (ev["stock_code"], ev["event_type"], ev["date"], ev["content"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return unique, failed_count


def load_all_events(config: dict) -> list[dict]:
    stocks = config.get("stocks") or []
    event_types = config.get("event_types") or ["预约披露日"]
    days_ahead = int(config.get("days_ahead", 365))

    all_events = []
    attempts = 0
    failed = 0
    for item in stocks:
        code = str(item.get("code", "")).strip()
        if not code:
            continue
        name = str(item.get("name") or "").strip() or fetch_stock_name(code)
        log(f"正在获取 {code} {name} …")
        try:
            events, failed_count = fetch_stock_events(code, event_types, days_ahead)
        except Exception as exc:
            log(f"  警告：{code} 获取失败：{exc}")
            continue
        attempts += len(event_types)
        failed += failed_count
        for ev in events:
            ev["stock_name"] = name
            ev["summary"] = f"{name}：{ev['content']}"
            ev["category"] = CATEGORY_MAP.get(ev["event_type"], "公告")
            ev["description"] = (
                f"{name}（{code}）\n"
                f"{ev['content']}\n"
                f"事件日期：{ev['date'].isoformat()}\n"
                f"事件类型：{ev['event_type']}\n"
                "数据来源：东方财富股市日历（https://data.eastmoney.com/）"
            )
        counts = {}
        for ev in events:
            counts[ev["event_type"]] = counts.get(ev["event_type"], 0) + 1
        detail = "、".join(f"{k} {v} 条" for k, v in counts.items()) or "无"
        log(f"  完成：{detail}")
        all_events.extend(events)

    if stocks and attempts and failed == attempts:
        raise RuntimeError("所有数据请求均失败，已保留上一次日历，请稍后重试")
    return all_events


def ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def build_ics(events: list[dict], cal_name: str = "上市公司重要提醒") -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Codex//Listed Company Reminders//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(cal_name)}",
        "X-WR-TIMEZONE:Asia/Shanghai",
        "X-PUBLISHED-TTL:PT6H",
    ]

    for ev in sorted(events, key=lambda e: (e["date"], e["stock_code"])):
        uid_source = f'{ev["stock_code"]}|{ev["event_type"]}|{ev["date"]}|{ev["content"]}'
        uid = hashlib.sha1(uid_source.encode("utf-8")).hexdigest()[:20].upper()
        start = ev["date"].strftime("%Y%m%d")
        end = (ev["date"] + timedelta(days=1)).strftime("%Y%m%d")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}@listed-company-reminders",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{start}",
                f"DTEND;VALUE=DATE:{end}",
                f"SUMMARY:{ics_escape(ev['summary'])}",
                f"DESCRIPTION:{ics_escape(ev['description'])}",
                f"CATEGORIES:{ics_escape(ev['category'])}",
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                "TRIGGER:-P1D",
                f"DESCRIPTION:{ics_escape(ev['summary'])}",
                "END:VALARM",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def generate_ics(config: dict, output: Path) -> list[dict]:
    events = load_all_events(config)
    text = build_ics(events)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(text.encode("utf-8"))
    log(f"已生成 {output}（共 {len(events)} 条提醒）")
    return events


def refresh_now(config: dict, output: Path) -> None:
    try:
        events = load_all_events(config)
        text = build_ics(events)
        output.write_bytes(text.encode("utf-8"))
        payload = text.encode("utf-8")
        with SERVE_STATE["lock"]:
            SERVE_STATE["bytes"] = payload
            SERVE_STATE["last_refresh"] = time.time()
        log(f"已刷新 {output}（共 {len(events)} 条提醒）")
    except Exception as exc:
        log(f"刷新失败：{exc}")
        if not SERVE_STATE["bytes"]:
            try:
                data = output.read_bytes()
                with SERVE_STATE["lock"]:
                    SERVE_STATE["bytes"] = data
            except Exception:
                pass


def background_refresher(config: dict, output: Path, interval_hours: float) -> None:
    while True:
        time.sleep(max(interval_hours, 0.25) * 3600)
        refresh_now(config, output)


class ICSHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/refresh":
            threading.Thread(
                target=refresh_now,
                args=(self.server.config, self.server.output),
                daemon=True,
            ).start()
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b"refreshing")
            return

        if path not in ("/", "/reminders.ics", "/calendar.ics"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return

        with SERVE_STATE["lock"]:
            payload = SERVE_STATE["bytes"]
        if not payload:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"calendar not ready yet")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args) -> None:
        pass


class CalendarServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, config, output):
        super().__init__(address, ICSHandler)
        self.config = config
        self.output = output


def load_config(path: Path) -> dict:
    if not path.exists():
        log(f"未找到配置文件 {path}，将使用内置示例股票。")
        return {
            "stocks": [
                {"code": "600519", "name": "贵州茅台"},
                {"code": "300750", "name": "宁德时代"},
            ],
            "event_types": ["预约披露日", "业绩预告", "业绩快报", "业绩报表", "股东大会", "分红", "限售解禁日"],
            "days_ahead": 365,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args():
    parser = argparse.ArgumentParser(description="上市公司重要提醒 → Outlook 日历（ICS）")
    parser.add_argument("--serve", action="store_true", help="生成 ICS 并启动本地订阅服务")
    parser.add_argument("--config", default=str(CONFIG_FILE), help="配置文件路径")
    parser.add_argument("--output", default=None, help="ICS 输出路径")
    parser.add_argument("--host", default=None, help="服务监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="服务端口（默认 8765）")
    parser.add_argument("--refresh-hours", type=float, default=None, help="自动刷新间隔（小时）")
    parser.add_argument("--days", type=int, default=None, help="只保留未来 N 天的提醒")
    return parser.parse_args()


def main() -> int:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = parse_args()
    config = load_config(Path(args.config))

    if args.days is not None:
        config["days_ahead"] = args.days

    output = Path(args.output) if args.output else DEFAULT_OUTPUT

    if not args.serve:
        generate_ics(config, output)
        return 0

    host = args.host or str(config.get("host", "127.0.0.1"))
    port = int(args.port or config.get("port", 8765))
    refresh_hours = float(args.refresh_hours or config.get("refresh_hours", 6))

    refresh_now(config, output)
    threading.Thread(
        target=background_refresher,
        args=(config, output, refresh_hours),
        daemon=True,
    ).start()

    server = CalendarServer((host, port), config, output)
    url = f"http://{host}:{port}/reminders.ics"
    log(f"日历订阅地址：{url}")
    log("请在 Outlook 日历中「添加日历 → 从 Internet 订阅」粘贴上面的地址。按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("服务已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
