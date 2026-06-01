from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pykrx import stock
from pydantic import BaseModel
from typing import List
import datetime
import asyncio

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────
# 종목 검색용 전체 목록 캐시
# 서버 시작 시 백그라운드에서 로드
# ──────────────────────────────────────────
ticker_map: dict = {}   # { "005930": "삼성전자" }
ticker_map_ready = False

def get_recent_trading_day() -> str:
    """오늘 또는 가장 최근 거래일 (YYYYMMDD)"""
    d = datetime.datetime.now()
    # 주말이면 금요일로
    if d.weekday() == 5:  # 토
        d -= datetime.timedelta(days=1)
    elif d.weekday() == 6:  # 일
        d -= datetime.timedelta(days=2)
    return d.strftime("%Y%m%d")

async def build_ticker_map():
    """KOSPI + KOSDAQ 전체 종목 코드-이름 매핑 빌드 (백그라운드)"""
    global ticker_map, ticker_map_ready
    try:
        date = get_recent_trading_day()
        result = {}
        for market in ["KOSPI", "KOSDAQ"]:
            try:
                codes = stock.get_market_ticker_list(date=date, market=market)
                for code in codes:
                    if code not in result:
                        try:
                            name = stock.get_market_ticker_name(code)
                            if name:
                                result[code] = name
                        except Exception:
                            pass
                    # 과도한 요청 방지
                    await asyncio.sleep(0.005)
            except Exception as e:
                print(f"[ticker_map] {market} 로드 실패: {e}")
        ticker_map = result
        ticker_map_ready = True
        print(f"[ticker_map] 로드 완료: {len(ticker_map)}개 종목")
    except Exception as e:
        print(f"[ticker_map] 전체 실패: {e}")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(build_ticker_map())

# ──────────────────────────────────────────
# 종목 검색 API
# GET /api/search?q=삼성전자
# → [{"ticker": "005930", "name": "삼성전자"}, ...]
# ──────────────────────────────────────────
@app.get("/api/search")
def search_ticker(q: str):
    q = q.strip()
    if not q or len(q) < 1:
        return []

    q_lower = q.lower()
    results = []

    # 코드 직접 매칭 우선 (숫자 6자리 입력 시)
    if q.isdigit():
        for code, name in ticker_map.items():
            if code.startswith(q):
                results.append({"ticker": code, "name": name})
                if len(results) >= 10:
                    return results
        return results

    # 이름 매칭 (이름에 검색어가 포함되는 경우)
    for code, name in ticker_map.items():
        if q_lower in name.lower():
            results.append({"ticker": code, "name": name})
            if len(results) >= 10:
                break

    # 아직 ticker_map 빌드 중이면 안내
    if not results and not ticker_map_ready:
        return [{"ticker": "", "name": "⏳ 종목 목록 로딩 중... 잠시 후 다시 검색해주세요"}]

    return results


# ──────────────────────────────────────────
# 종가 조회 API (기존 유지)
# ──────────────────────────────────────────
class TickerList(BaseModel):
    tickers: List[str]

def normalize_ticker(ticker: str) -> str:
    """A-prefix 제거 + 6자리 보정"""
    t = ticker.strip().upper()
    if t.startswith("A") and len(t) == 7 and t[1:].isdigit():
        t = t[1:]
    return t

def is_valid_ticker(ticker: str) -> bool:
    import re
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
                # 등락률 계산
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
    return {"status": "ok", "ticker_map_size": len(ticker_map), "ticker_map_ready": ticker_map_ready}
