#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上市公司重要提醒 → Outlook 日历订阅（ICS）

数据源：东方财富「股市日历」+ 港股公告（免费免鉴权公开接口）。
A 股事件：预约披露日（年报/中报/季报）、业绩预告、业绩快报、业绩报表、股东大会、分红、限售解禁日。
港股事件：业绩公布、董事会会议、股东大会、股息、盈喜盈警（尽量从公告正文提取会议日/除净日等真实日期）。

用法：
    python listed_company_calendar.py              # 只生成 ICS 文件
    python listed_company_calendar.py --serve      # 生成 ICS 并启动本地订阅服务
                                                   # 默认 http://127.0.0.1:8765/reminders.ics
"""

import argparse
import hashlib
import json
import re
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
    "业绩公布": "财报",
    "董事会会议": "会议",
    "股息": "分红",
    "盈喜盈警": "业绩",
}

HK_ANNOUNCE_API = "https://np-anotice-stock.eastmoney.com/api/security/ann"
HK_CONTENT_API = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
HK_DAYS_BACK = 90  # 只翻最近 90 天的港股公告，够覆盖股东会/派息等未来事件

HK_CATEGORY_KEYWORDS = {
    "业绩公布": ["业绩公布", "业绩报告", "中期报告", "年度报告", "全年业绩", "中期业绩", "季度业绩", "業績公佈", "業績報告", "年度業績", "中期業績"],
    "董事会会议": ["董事会会议", "董事會會議"],
    "股东大会": ["股东大会", "股东周年大会", "股东特别大会", "股東大會", "股東週年大會", "股東特別大會"],
    "股息": ["股息", "派息", "末期息", "中期息", "特别股息", "特別股息", "分派"],
    "盈喜盈警": ["盈喜", "盈警", "盈利警告", "盈利预喜", "盈利預喜", "业绩预告"],
}
HK_EXCLUDE_TITLES = ["翌日披露报表", "月报表", "回购", "股份购回", "证券变动"]

CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "萬": 10000}
DATE_RE_ARABIC = re.compile(r"(20\d{2})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*日?")
DATE_RE_CHINESE = re.compile(r"([一二三四五六七八九零〇两兩0-9]{4})\s*年\s*([一二三四五六七八九十两兩]{1,3})\s*月\s*([一二三四五六七八九十两兩]{1,4})\s*日?")

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
    code = code.strip()
    if code.upper().endswith(".HK") or code.upper().startswith("HK"):
        return f"116.{normalize_hk_code(code)}"
    code = code.zfill(6)
    if code.startswith(("6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def normalize_hk_code(code: str) -> str:
    """把港股代码规范成东财用的 5 位数字，如 700 / 0700.HK / HK00700 → 00700。"""
    digits = re.sub(r"[^0-9]", "", code)
    return digits.zfill(5)


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


def parse_cn_int(text: str) -> int | None:
    """把中文数字转成整数：二零二六→2026，十二→12，一百二十三→123。"""
    text = text.strip()
    if not text:
        return None
    if all(ch in CN_DIGITS for ch in text):
        return int("".join(str(CN_DIGITS[ch]) for ch in text))
    total = 0
    section = 0
    num = 0
    for ch in text:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch in ("十", "百", "千"):
            if num == 0:
                num = 1
            section += num * CN_UNITS[ch]
            num = 0
        elif ch in ("万", "萬"):
            section = (section + num) * 10000
            total += section
            section = 0
            num = 0
        else:
            return None
    return total + section + num


def extract_dates(text: str) -> list[date]:
    """从文本中提取所有日期（支持 2026年8月12日 和 二零二六年八月十二日 两种写法）。"""
    found = []
    for m in DATE_RE_ARABIC.finditer(text):
        try:
            found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    for m in DATE_RE_CHINESE.finditer(text):
        year = parse_cn_int(m.group(1))
        month = parse_cn_int(m.group(2))
        day = parse_cn_int(m.group(3))
        if year and month and day:
            try:
                found.append(date(year, month, day))
            except ValueError:
                pass
    return found


def match_hk_category(title: str) -> str | None:
    for category, keywords in HK_CATEGORY_KEYWORDS.items():
        if any(kw in title for kw in keywords):
            return category
    return None


def fetch_hk_announcement_content(art_code: str) -> tuple[str, str]:
    url = f"{HK_CONTENT_API}?art_code={art_code}&client_source=web&page_index=1"
    try:
        payload = http_get_json(url)
    except Exception:
        return "", ""
    data = payload.get("data") or {}
    return str(data.get("notice_content") or ""), str(data.get("attach_url") or "")


def pick_hk_event_date(category: str, title: str, content: str, notice: date, today: date, max_date: date) -> date:
    """尽量从公告标题/正文里找真正的未来事件日期（会议日、除净日等），找不到就用发布日。"""
    if category in ("业绩公布", "盈喜盈警"):
        return notice
    if category == "股息":
        for kw in ("除净日", "除淨日", "除息日", "股权登记日", "股權登記日", "記錄日期", "记录日期"):
            idx = content.find(kw)
            if idx >= 0:
                segment = content[idx : idx + 80]
                future = [d for d in extract_dates(segment) if today <= d <= max_date]
                if future:
                    return min(future)
    for text in (title, content):
        future = [d for d in extract_dates(text) if today <= d <= max_date]
        if future:
            return min(future)
    return notice


def fetch_hk_events(code: str, days_ahead: int) -> tuple[list[dict], int]:
    """抓取港股公告，按标题分类成业绩公布/董事会会议/股东大会/股息/盈喜盈警。"""
    hk_code = normalize_hk_code(code)
    today = date.today()
    max_date = today + timedelta(days=days_ahead)
    cutoff = today - timedelta(days=HK_DAYS_BACK)
    events = []
    failed = 0
    page = 1

    try:
        while True:
            params = {
                "sr": "-1",
                "page_size": "100",
                "page_index": str(page),
                "ann_type": "H",
                "client_source": "web",
                "stock_list": hk_code,
            }
            url = HK_ANNOUNCE_API + "?" + urllib.parse.urlencode(params)
            try:
                payload = http_get_json(url)
            except Exception as exc:
                log(f"  警告：{code} 港股公告获取失败：{exc}")
                failed += 1
                break

            rows = (payload.get("data") or {}).get("list") or []
            if not rows:
                break

            reached_cutoff = False
            for row in rows:
                try:
                    notice = datetime.strptime(str(row.get("notice_date") or "")[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if notice < cutoff:
                    reached_cutoff = True
                    break
                if notice > today:
                    continue

                title = str(row.get("title_ch") or row.get("title") or "").strip()
                if not title or any(kw in title for kw in HK_EXCLUDE_TITLES):
                    continue
                category = match_hk_category(title)
                if not category:
                    continue

                content = ""
                attach_url = ""
                if category in ("董事会会议", "股东大会", "股息"):
                    content, attach_url = fetch_hk_announcement_content(row.get("art_code") or "")
                    time.sleep(0.15)

                event_date = pick_hk_event_date(category, title, content, notice, today, max_date)
                if not (today <= event_date <= max_date):
                    continue

                detail = title
                if event_date != notice:
                    detail += f"\n事件日期：{event_date.isoformat()}（公告发布日：{notice.isoformat()}）"
                else:
                    detail += f"\n事件日期：{event_date.isoformat()}"
                if attach_url:
                    detail += f"\n公告链接：{attach_url}"
                detail += "\n数据来源：东方财富港股公告"

                events.append(
                    {
                        "stock_code": code,
                        "event_type": category,
                        "date": event_date,
                        "content": title,
                        "detail": detail,
                    }
                )

            total = int((payload.get("data") or {}).get("total_hits") or 0)
            if reached_cutoff or len(rows) < 100 or page * 100 >= total:
                break
            page += 1
    except Exception as exc:
        log(f"  警告：{code} 港股抓取异常：{exc}")
        failed += 1

    # 去重：同一只股票同一类事件同一天只保留一条
    seen, unique = set(), []
    for ev in events:
        key = (ev["stock_code"], ev["event_type"], ev["date"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return unique, failed


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
            if code.upper().endswith(".HK") or code.upper().startswith("HK"):
                events, failed_count = fetch_hk_events(code, days_ahead)
            else:
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
            if "detail" in ev:
                ev["description"] = f"{name}（{code}）\n{ev['detail']}"
            else:
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
