#!/usr/bin/env python3
"""
三亚 -> 济南 机票价格监控脚本
查询指定日期直飞航班价格，低于目标价时通过飞书 webhook 通知。
部署在 GitHub Actions 上，电脑关机也能运行。

数据抓取策略：
  1. requests 直接抓取（快速，适用于 SSR 页面）
  2. Playwright 无头浏览器（适用于动态渲染页面）
     - 拦截 API 响应直接解析 JSON（同程 wx.17u.cn API）
     - 从渲染后的页面文本中正则提取
"""

import re
import os
import json
import requests
from datetime import datetime

# ==================== 配置 ====================
FLIGHT_DATE = os.environ.get("FLIGHT_DATE", "2026-08-10")
TARGET_PRICE = int(os.environ.get("TARGET_PRICE", "700"))
FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")

FROM_CITY = "三亚"
TO_CITY = "济南"
FROM_CODE = "SYX"
TO_CODE = "TNA"

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

PRICE_RANGE = (100, 10000)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ==================== 同程 API 专用解析 ====================

def parse_tongcheng_api(data):
    """解析同程 wx.17u.cn book1/flights API 返回的数据

    数据结构:
      data.data.pc[] = 价格日历数组
      pc[i].dd = 日期
      pc[i].lp = 最低价 (可能含中转)
      pc[i].ms = 详细信息字符串 (管道符分隔, 含航班号和价格)

    ms 字段示例:
      2|580|580|830|15|ZH9322,ZH9935|SZX|...|830|1|0|3U3247|0|2|...
      其中逗号分隔的航班号是中转, 单个航班号是直飞
    """
    flights = []

    # 定位 pc 数组
    pc = None
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], dict):
            pc = data["data"].get("pc", [])
        elif "pc" in data:
            pc = data.get("pc", [])

    if not pc:
        return flights

    for item in pc:
        if not isinstance(item, dict):
            continue
        dd = str(item.get("dd", ""))
        if dd != FLIGHT_DATE:
            continue

        ms = str(item.get("ms", ""))
        if ms:
            parts = ms.split("|")
            for i, part in enumerate(parts):
                part = part.strip()
                # 单个航班号 = 直飞; 逗号分隔 = 中转
                if re.match(r'^[A-Z0-9]{2}\d{3,5}$', part) and "," not in part:
                    # 在附近位置查找价格
                    for j in range(max(0, i - 5), min(len(parts), i + 5)):
                        try:
                            price = int(parts[j])
                            if 300 <= price <= 5000:
                                flights.append({
                                    "flight_no": part,
                                    "price": price,
                                    "source": "同程"
                                })
                                break
                        except ValueError:
                            pass

        # 如果 ms 没提取到, 用 lp 作为备选 (标注可能中转)
        if not any(f["flight_no"] != "未知" for f in flights):
            lp = item.get("lp")
            if lp:
                try:
                    price = int(lp)
                    if PRICE_RANGE[0] <= price <= PRICE_RANGE[1]:
                        flights.append({
                            "flight_no": "未知(含中转)",
                            "price": price,
                            "source": "同程"
                        })
                except (ValueError, TypeError):
                    pass

    return flights


# ==================== 通用提取工具 ====================

def _get_price(d):
    for key in ("price", "ticketPrice", "minPrice", "adultPrice", "parPrice",
                "salePrice", "totalPrice"):
        if key in d:
            try:
                p = int(d[key])
                if p > 1000:
                    p = p // 100  # 可能是分
                return p
            except (ValueError, TypeError):
                pass
    return None


def extract_from_json(data, source, flights=None, depth=0):
    """递归从 JSON 中提取航班价格"""
    if flights is None:
        flights = []
    if depth > 15:
        return flights
    if isinstance(data, dict):
        price = _get_price(data)
        if price and PRICE_RANGE[0] <= price <= PRICE_RANGE[1]:
            fn = "未知"
            for fk in ("flightNo", "flight_no", "flightNumber", "fnum", "flight"):
                if fk in data:
                    fn = str(data[fk])
                    break
            if re.match(r'^[A-Z]{1,2}\d{3,5}$', fn):
                flights.append({"flight_no": fn, "price": price, "source": source})
        for v in data.values():
            extract_from_json(v, source, flights, depth + 1)
    elif isinstance(data, list):
        for item in data:
            extract_from_json(item, source, flights, depth + 1)
    return flights


def extract_from_text(text, source):
    """从纯文本中提取航班号和价格"""
    flights = []
    flight_nos = re.findall(r'\b([A-Z]{2}\d{3,5})\b', text)
    prices = re.findall(r'[¥￥]\s*(\d{2,5})', text)
    if not prices:
        prices = re.findall(r'(?:票务信息|票价|价格|最低)["：:\s]*(\d{3,5})', text)

    seen = set()
    for i, fn in enumerate(flight_nos):
        if fn in seen:
            continue
        seen.add(fn)
        if i < len(prices):
            try:
                price = int(prices[i])
                if PRICE_RANGE[0] <= price <= PRICE_RANGE[1]:
                    flights.append({"flight_no": fn, "price": price, "source": source})
            except ValueError:
                pass
    return flights


def extract_from_html(html, source):
    """从 HTML 中提取航班号和价格"""
    flights = []
    for match in re.findall(r'window\.__\w+__\s*=\s*(\{.+?\});', html, re.DOTALL):
        try:
            flights.extend(extract_from_json(json.loads(match), source))
        except (json.JSONDecodeError, TypeError):
            pass
    if not flights:
        flights = extract_from_text(html, source)
    return flights


# ==================== requests 抓取 ====================

def fetch_ctrip_requests():
    url = "https://m.ctrip.com/html5/flight/pages/first"
    params = {"dcity": FROM_CODE, "acity": TO_CODE, "ddate": FLIGHT_DATE,
              "dcityName": FROM_CITY, "acityName": TO_CITY, "regionType": "DOMESTIC"}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        resp.encoding = "utf-8"
        flights = extract_from_html(resp.text, "携程")
        log(f"[携程-requests] 获取到 {len(flights)} 条")
        return flights
    except Exception as e:
        log(f"[携程-requests] 失败: {e}")
        return []


def fetch_tongcheng_requests():
    url = "https://m.ly.com/ft/touch/book1"
    params = {"fromCode": FROM_CODE, "toCode": TO_CODE,
              "fromCity": FROM_CITY, "toCity": TO_CITY, "date": FLIGHT_DATE}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        resp.encoding = "utf-8"
        flights = extract_from_html(resp.text, "同程")
        log(f"[同程-requests] 获取到 {len(flights)} 条")
        return flights
    except Exception as e:
        log(f"[同程-requests] 失败: {e}")
        return []


# ==================== Playwright 抓取 ====================

def fetch_with_playwright():
    """用 Playwright 无头浏览器抓取：拦截 API 响应 + 解析渲染后页面"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("[Playwright] 未安装，跳过")
        return []

    flights = []
    targets = [
        (f"https://m.ly.com/ft/touch/book1?fromCode={FROM_CODE}&toCode={TO_CODE}"
         f"&fromCity={FROM_CITY}&toCity={TO_CITY}&date={FLIGHT_DATE}", "同程"),
        (f"https://m.ctrip.com/html5/flight/pages/first?dcity={FROM_CODE}&acity={TO_CODE}"
         f"&ddate={FLIGHT_DATE}&dcityName={FROM_CITY}&acityName={TO_CITY}&regionType=DOMESTIC", "携程"),
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=MOBILE_UA,
            locale="zh-CN",
            viewport={"width": 375, "height": 812},
            is_mobile=True,
            has_touch=True,
        )

        for url, source in targets:
            try:
                page = ctx.new_page()
                api_data = []

                def on_response(response):
                    try:
                        ct = response.headers.get("content-type", "")
                        if "json" not in ct:
                            return
                        u = response.url.lower()
                        if any(kw in u for kw in ["flight", "search", "list", "product", "cheap", "book", "segment"]):
                            api_data.append({"url": response.url, "body": response.text()})
                    except Exception:
                        pass

                page.on("response", on_response)
                page.goto(url, wait_until="networkidle", timeout=45000)
                page.wait_for_timeout(5000)

                source_flights = []

                # 优先: 专门解析同程 API
                for api in api_data:
                    try:
                        data = json.loads(api["body"])
                        if "17u.cn" in api["url"] or "ly.com" in api["url"]:
                            source_flights.extend(parse_tongcheng_api(data))
                        else:
                            source_flights.extend(extract_from_json(data, source))
                    except (json.JSONDecodeError, TypeError):
                        pass

                # 备用: 从渲染后的页面文本提取
                if not source_flights:
                    try:
                        body_text = page.inner_text("body")
                        source_flights.extend(extract_from_text(body_text, source))
                    except Exception:
                        pass

                # 备用: 从页面 HTML 提取
                if not source_flights:
                    content = page.content()
                    source_flights.extend(extract_from_html(content, source))

                flights.extend(source_flights)
                log(f"[{source}-Playwright] API拦截{len(api_data)}个, 获取到{len(source_flights)}条航班")
                page.close()

            except Exception as e:
                log(f"[{source}-Playwright] 失败: {e}")

        browser.close()

    return flights


# ==================== 飞书通知 ====================

def send_feishu(flight):
    if not FEISHU_WEBHOOK_URL:
        log("[通知] 未设置 FEISHU_WEBHOOK_URL，跳过")
        return False

    msg = (
        f"✈️ 三亚→济南 {FLIGHT_DATE} 直飞降价！\n"
        f"航班：{flight.get('flight_no', '未知')}\n"
        f"票价：¥{flight['price']}（已低于¥{TARGET_PRICE}目标价）\n"
        f"数据来源：{flight.get('source', '未知')}\n"
        f"查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"建议尽快预订！"
    )

    payload = {"msg_type": "text", "content": {"text": msg}}

    try:
        resp = requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=10)
        result = resp.json()
        if result.get("StatusCode") == 0 or result.get("code") == 0 or result.get("msg") == "success":
            log("[通知] 飞书消息发送成功")
            return True
        log(f"[通知] 飞书发送失败: {result}")
        return False
    except Exception as e:
        log(f"[通知] 发送异常: {e}")
        return False


# ==================== 主流程 ====================

def main():
    log("=== 机票价格监控 ===")
    log(f"航线：{FROM_CITY}({FROM_CODE}) -> {TO_CITY}({TO_CODE})")
    log(f"日期：{FLIGHT_DATE}  目标价：¥{TARGET_PRICE}")
    log("")

    all_flights = []

    # 方案 1: requests 快速抓取
    log("尝试 requests 抓取...")
    all_flights.extend(fetch_ctrip_requests())
    all_flights.extend(fetch_tongcheng_requests())

    # 方案 2: Playwright 备用
    if not all_flights:
        log("requests 未获取到数据，尝试 Playwright...")
        all_flights.extend(fetch_with_playwright())

    if not all_flights:
        log("未能获取到任何航班价格信息")
        return

    # 去重 (按航班号)
    seen = set()
    unique = []
    for f in all_flights:
        key = f["flight_no"]
        if key not in seen and PRICE_RANGE[0] <= f["price"] <= PRICE_RANGE[1]:
            seen.add(key)
            unique.append(f)

    all_flights = unique
    log(f"共获取到 {len(all_flights)} 条航班信息")

    if all_flights:
        # 只看有明确航班号的直飞航班
        direct = [f for f in all_flights if "未知" not in f["flight_no"] and "中转" not in f["flight_no"]]
        if direct:
            lowest = min(direct, key=lambda x: x["price"])
            log(f"直飞最低价：¥{lowest['price']} ({lowest['flight_no']}, {lowest['source']})")
        else:
            lowest = min(all_flights, key=lambda x: x["price"])
            log(f"最低价(可能含中转)：¥{lowest['price']} ({lowest['source']})")

    # 检查是否低于目标价 (优先看直飞)
    candidates = [f for f in all_flights if "未知" not in f["flight_no"] and "中转" not in f["flight_no"]]
    if not candidates:
        candidates = all_flights

    cheap = [f for f in candidates if f["price"] < TARGET_PRICE]
    if cheap:
        cheapest = min(cheap, key=lambda x: x["price"])
        log(f"发现低于目标价 ¥{TARGET_PRICE} 的航班！")
        send_feishu(cheapest)
    else:
        log(f"暂无低于 ¥{TARGET_PRICE} 的航班")


if __name__ == "__main__":
    main()
