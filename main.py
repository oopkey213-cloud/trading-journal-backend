from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pykrx import stock
from pydantic import BaseModel
from typing import List
import datetime
import asyncio
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────
# 종목 검색용 전체 목록 캐시
# ──────────────────────────────────────────
ticker_map: dict = {}
ticker_map_ready = False
ticker_map_source = ""   # 'fdr', 'pykrx', 'empty'


def _load_via_fdr():
    """FinanceDataReader로 전체 KRX 종목 한 번에 가져오기 (가장 안정적)"""
    import FinanceDataReader as fdr
    df = fdr.StockListing('KRX')
    result = {}
    if df is None or len(df) == 0:
        return result
    code_col = None
    name_col = None
    for c in ['Code', 'Symbol', 'ISU_CD']:
        if c in df.columns:
            code_col = c
            break
    for c in ['Name', 'Korean Name', 'ISU_NM']:
        if c in df.columns:
            name_col = c
            break
    if not code_col or not name_col:
        return result
    for _, row in df.iterrows():
        try:
            code = str(row[code_col]).strip().zfill(6)
            name = str(row[name_col]).strip()
            if code.isdigit() and len(code) == 6 and name and name != 'nan':
                result[code] = name
        except Exception:
            continue
    return result


def _load_via_pykrx():
    """pykrx fallback — 최근 14일 안에서 데이터 있는 거래일 찾기"""
    result = {}
    today = datetime.datetime.now()
    for days_ago in range(0, 14):
        try:
            d = today - datetime.timedelta(days=days_ago)
            if d.weekday() >= 5:
                continue
            date_str = d.strftime("%Y%m%d")
            try:
                kospi = stock.get_market_ticker_list(date=date_str, market="KOSPI")
            except Exception:
                kospi = []
            try:
                kosdaq = stock.get_market_ticker_list(date=date_str, market="KOSDAQ")
            except Exception:
                kosdaq = []
            all_codes = list(kospi) + list(kosdaq)
            if not all_codes:
                continue
            for code in all_codes:
                try:
                    name = stock.get_market_ticker_name(code)
                    if name:
                        result[code] = name
                except Exception:
                    pass
            if result:
                return result
        except Exception:
            continue
    return result


async def build_ticker_map():
    global ticker_map, ticker_map_ready, ticker_map_source
    loop = asyncio.get_event_loop()

    try:
        result = await loop.run_in_executor(None, _load_via_fdr)
        if result and len(result) > 100:
            ticker_map = result
            ticker_map_source = "fdr"
            ticker_map_ready = True
            print(f"[ticker_map] FDR 로드 성공: {len(ticker_map)}개")
            return
    except Exception as e:
        print(f"[ticker_map] FDR 실패: {e}")

    try:
        result = await loop.run_in_executor(None, _load_via_pykrx)
        if result and len(result) > 100:
            ticker_map = result
            ticker_map_source = "pykrx"
            ticker_map_ready = True
            print(f"[ticker_map] pykrx 로드 성공: {len(ticker_map)}개")
            return
    except Exception as e:
        print(f"[ticker_map] pykrx 실패: {e}")

    ticker_map = {}
    ticker_map_source = "empty"
    ticker_map_ready = True
    print("[ticker_map] 모든 로드 실패")


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(build_ticker_map())


# ──────────────────────────────────────────
# 종목 검색 API
# ──────────────────────────────────────────
@app.get("/api/search")
def search_ticker(q: str):
    q = q.strip()
    if not q:
        return []
    if not ticker_map:
        if ticker_map_ready:
            return [{"ticker": "", "name": "⚠️ 종목 목록 빌드 실패"}]
        else:
            return [{"ticker": "", "name": "⏳ 종목 목록 로딩 중..."}]

    q_lower = q.lower()

    if q.isdigit():
        results = []
        for code, name in ticker_map.items():
            if code.startswith(q):
                results.append({"ticker": code, "name": name})
                if len(results) >= 10:
                    return results
        return results

    starts_with = []
    contains = []
    for code, name in ticker_map.items():
        name_lower = name.lower()
        if name_lower.startswith(q_lower):
            starts_with.append({"ticker": code, "name": name})
        elif q_lower in name_lower:
            contains.append({"ticker": code, "name": name})

    results = starts_with[:10]
    if len(results) < 10:
        results.extend(contains[:10 - len(results)])
    return results


# ──────────────────────────────────────────
# 종가 조회 API
# ──────────────────────────────────────────
class TickerList(BaseModel):
    tickers: List[str]


def normalize_ticker(ticker: str) -> str:
    t = ticker.strip().upper()
    if t.startswith("A") and len(t) == 7 and t[1:].isdigit():
        t = t[1:]
    return t


def is_valid_ticker(ticker: str) -> bool:
    return bool(re.match(r'^[0-9A-Z]{6}$', ticker))


def get_close_price(ticker: str):
    t = normalize_ticker(ticker)
    if not is_valid_ticker(t):
        return None
    today = datetime.datetime.now()
    for i in range(5):
        target = today - datetime.timedelta(days=i)
        if target.weekday() >= 5:
            continue
        date_str = target.strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv(date_str, date_str, t)
            if df is not None and not df.empty:
                row = df.iloc[-1]
                close = int(row["종가"])
                if close == 0:
                    continue
                try:
                    change_pct = float(row.get("등락률", 0))
                except Exception:
                    change_pct = 0.0
                return {"ticker": t, "close": close, "change_pct": round(change_pct, 2), "date": date_str}
        except Exception:
            continue
    return None


@app.get("/api/close/{ticker}")
def get_close(ticker: str):
    result = get_close_price(ticker)
    if result is None:
        raise HTTPException(status_code=404, detail=f"가격 없음: {ticker}")
    return result


@app.post("/api/closes")
def get_closes(body: TickerList):
    results = {}
    for t in body.tickers:
        r = get_close_price(t)
        if r:
            results[normalize_ticker(t)] = r
    return results


@app.get("/api/ohlcv/{ticker}")
def get_ohlcv(ticker: str, from_date: str = None, to_date: str = None):
    t = normalize_ticker(ticker)
    if not is_valid_ticker(t):
        raise HTTPException(status_code=400, detail="유효하지 않은 종목코드")
    if not from_date:
        from_date = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y%m%d")
    if not to_date:
        to_date = datetime.datetime.now().strftime("%Y%m%d")
    try:
        df = stock.get_market_ohlcv(from_date, to_date, t)
        if df is None or df.empty:
            return []
        result = []
        for date_idx, row in df.iterrows():
            result.append({
                "date": str(date_idx)[:10].replace("-", ""),
                "open": int(row.get("시가", 0)),
                "high": int(row.get("고가", 0)),
                "low": int(row.get("저가", 0)),
                "close": int(row.get("종가", 0)),
                "volume": int(row.get("거래량", 0)),
            })
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "ticker_map_size": len(ticker_map),
        "ticker_map_ready": ticker_map_ready,
        "ticker_map_source": ticker_map_source,
    }


@app.post("/api/reload_tickers")
async def reload_tickers():
    global ticker_map_ready
    ticker_map_ready = False
    asyncio.create_task(build_ticker_map())
    return {"status": "reload started"}


# ──────────────────────────────────────────
# Notion 연동 — 결산/공부일지를 Notion 페이지로 전송 (이미지 포함)
# ──────────────────────────────────────────
import base64
import io
import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionTrade(BaseModel):
    side: str = ""          # BUY / SELL
    stock_name: str = ""
    ticker: str = ""
    quantity: float = 0
    price: float = 0


class NotionImage(BaseModel):
    dataUrl: str = ""


class NotionExportBody(BaseModel):
    token: str
    parent_page_id: str
    title: str = "매매 일지"
    entry_date: str = ""
    tags: List[str] = []
    content: str = ""
    trades: List[NotionTrade] = []
    images: List[NotionImage] = []


def _markup_to_richtext(text: str):
    """도구 마크업(**bold**, {r}red{/r}, {b}blue{/b}, {g}blue{/g})을 Notion rich_text로 변환"""
    pattern = re.compile(
        r'\*\*(.+?)\*\*|\{r\}(.+?)\{/r\}|\{b\}(.+?)\{/b\}|\{g\}(.+?)\{/g\}',
        re.DOTALL
    )
    rich = []
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            plain = text[pos:m.start()]
            if plain:
                rich.append({"type": "text", "text": {"content": plain[:2000]}})
        if m.group(1) is not None:        # **bold**
            rich.append({"type": "text", "text": {"content": m.group(1)[:2000]},
                         "annotations": {"bold": True}})
        elif m.group(2) is not None:      # {r}red{/r}
            rich.append({"type": "text", "text": {"content": m.group(2)[:2000]},
                         "annotations": {"color": "red", "bold": True}})
        elif m.group(3) is not None:      # {b}blue{/b}
            rich.append({"type": "text", "text": {"content": m.group(3)[:2000]},
                         "annotations": {"color": "blue", "bold": True}})
        elif m.group(4) is not None:      # {g}blue{/g} (레거시)
            rich.append({"type": "text", "text": {"content": m.group(4)[:2000]},
                         "annotations": {"color": "blue", "bold": True}})
        pos = m.end()
    if pos < len(text):
        plain = text[pos:]
        if plain:
            rich.append({"type": "text", "text": {"content": plain[:2000]}})
    if not rich:
        rich.append({"type": "text", "text": {"content": (text or "")[:2000]}})
    return rich


def _build_blocks(body: NotionExportBody):
    children = []

    # 1) 날짜 + 태그 콜아웃
    meta_parts = []
    if body.entry_date:
        meta_parts.append(f"📅 {body.entry_date}")
    if body.tags:
        meta_parts.append("🏷 " + ", ".join(body.tags))
    if meta_parts:
        children.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": "    ".join(meta_parts)}}],
                "icon": {"emoji": "📌"},
                "color": "gray_background",
            }
        })

    # 2) 매매 내역 표
    if body.trades:
        rows = [{
            "type": "table_row",
            "table_row": {"cells": [
                [{"type": "text", "text": {"content": "구분"}}],
                [{"type": "text", "text": {"content": "종목"}}],
                [{"type": "text", "text": {"content": "수량"}}],
                [{"type": "text", "text": {"content": "단가"}}],
            ]}
        }]
        for t in body.trades:
            side = "매수" if (t.side or "").upper() == "BUY" else "매도"
            qty = f"{int(t.quantity):,}" if t.quantity else ""
            price = f"{int(t.price):,}" if t.price else ""
            nm = t.stock_name + (f" ({t.ticker})" if t.ticker else "")
            rows.append({
                "type": "table_row",
                "table_row": {"cells": [
                    [{"type": "text", "text": {"content": side}}],
                    [{"type": "text", "text": {"content": nm}}],
                    [{"type": "text", "text": {"content": qty}}],
                    [{"type": "text", "text": {"content": price}}],
                ]}
            })
        children.append({
            "object": "block",
            "type": "table",
            "table": {
                "table_width": 4,
                "has_column_header": True,
                "has_row_header": False,
                "children": rows,
            }
        })

    # 3) 본문 (줄 단위 → paragraph)
    if body.content:
        for line in body.content.split("\n"):
            if line.strip() == "":
                children.append({"object": "block", "type": "paragraph",
                                 "paragraph": {"rich_text": []}})
            else:
                children.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _markup_to_richtext(line)}
                })

    return children[:100]  # Notion 페이지 생성 시 한 번에 100블록 제한


def _upload_image_to_notion(token: str, data_url: str, idx: int):
    """base64 dataURL을 Notion에 업로드하고 file_upload id 반환"""
    if not data_url.startswith("data:"):
        return None, "dataURL 형식 아님"
    try:
        header, b64 = data_url.split(",", 1)
        mime = header.split(";")[0].replace("data:", "") or "image/png"
        ext = mime.split("/")[-1] or "png"
        raw = base64.b64decode(b64)
    except Exception as e:
        return None, f"디코드 실패: {e}"

    json_headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    # 1) 업로드 객체 생성
    try:
        r1 = requests.post(f"{NOTION_API}/file_uploads", headers=json_headers,
                           json={"filename": f"image_{idx}.{ext}", "content_type": mime},
                           timeout=30)
        if r1.status_code != 200:
            return None, f"업로드객체 실패 {r1.status_code}: {r1.text[:120]}"
        fid = r1.json()["id"]
    except Exception as e:
        return None, f"업로드객체 예외: {e}"

    # 2) 바이너리 전송 (multipart)
    send_headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
    }
    try:
        files = {"file": (f"image_{idx}.{ext}", io.BytesIO(raw), mime)}
        r2 = requests.post(f"{NOTION_API}/file_uploads/{fid}/send",
                           headers=send_headers, files=files, timeout=60)
        if r2.status_code != 200:
            return None, f"전송 실패 {r2.status_code}: {r2.text[:120]}"
    except Exception as e:
        return None, f"전송 예외: {e}"

    return fid, None


@app.post("/api/notion/export")
def notion_export(body: NotionExportBody):
    if not body.token or not body.parent_page_id:
        raise HTTPException(status_code=400, detail="token, parent_page_id 필수")

    headers = {
        "Authorization": f"Bearer {body.token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    # 1) 페이지 생성
    children = _build_blocks(body)
    payload = {
        "parent": {"page_id": body.parent_page_id},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": body.title or "매매 일지"}}]}
        },
        "children": children,
    }
    try:
        res = requests.post(f"{NOTION_API}/pages", headers=headers, json=payload, timeout=30)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Notion 연결 실패: {e}")
    if res.status_code != 200:
        raise HTTPException(status_code=res.status_code,
                            detail=f"페이지 생성 실패: {res.text[:300]}")
    page = res.json()
    page_id = page["id"]
    page_url = page.get("url", "")

    # 2) 이미지 업로드 + 첨부
    img_errors = []
    img_ok = 0
    for idx, img in enumerate(body.images):
        fid, err = _upload_image_to_notion(body.token, img.dataUrl, idx)
        if err:
            img_errors.append(f"이미지{idx+1}: {err}")
            continue
        # 이미지 블록 append
        try:
            ap = requests.patch(
                f"{NOTION_API}/blocks/{page_id}/children",
                headers=headers,
                json={"children": [{
                    "object": "block", "type": "image",
                    "image": {"type": "file_upload", "file_upload": {"id": fid}}
                }]},
                timeout=30
            )
            if ap.status_code == 200:
                img_ok += 1
            else:
                img_errors.append(f"이미지{idx+1}: 첨부 실패 {ap.status_code}")
        except Exception as e:
            img_errors.append(f"이미지{idx+1}: 첨부 예외 {e}")

    return {
        "status": "ok",
        "page_url": page_url,
        "image_total": len(body.images),
        "image_ok": img_ok,
        "image_errors": img_errors,
    }
