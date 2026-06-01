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
